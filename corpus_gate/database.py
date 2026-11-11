import contextlib
import sqlite3
from corpus_gate.errors import now, fail, canonical, digest

DDL = '''
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY,value INTEGER NOT NULL);
INSERT OR IGNORE INTO counters VALUES('corpus',0);
CREATE TABLE IF NOT EXISTS sources(source_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,state TEXT NOT NULL,policy TEXT NOT NULL,priority INTEGER NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_history(source_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(source_id,revision));
CREATE TABLE IF NOT EXISTS profiles(profile_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,definition TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profile_history(profile_id TEXT NOT NULL,revision INTEGER NOT NULL,definition TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(profile_id,revision));
CREATE TABLE IF NOT EXISTS batches(batch_id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(source_id),profile_id TEXT NOT NULL,profile_revision INTEGER NOT NULL,profile_hash TEXT NOT NULL,source_revision INTEGER NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,cursor INTEGER NOT NULL DEFAULT 0,total INTEGER NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intake(batch_id TEXT NOT NULL REFERENCES batches(batch_id),ordinal INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,error TEXT,document_id TEXT,PRIMARY KEY(batch_id,ordinal));
CREATE TABLE IF NOT EXISTS documents(document_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,external_id TEXT NOT NULL,version INTEGER NOT NULL,raw_text TEXT NOT NULL,normalized TEXT NOT NULL,exact_hash TEXT NOT NULL,metadata TEXT NOT NULL,metrics TEXT NOT NULL,findings TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,profile_hash TEXT NOT NULL,batch_id TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(source_id,external_id,version));
CREATE INDEX IF NOT EXISTS docs_identity ON documents(source_id,external_id,version);
CREATE INDEX IF NOT EXISTS docs_hash ON documents(exact_hash);
CREATE TABLE IF NOT EXISTS document_heads(source_id TEXT NOT NULL,external_id TEXT NOT NULL,document_id TEXT NOT NULL REFERENCES documents(document_id),PRIMARY KEY(source_id,external_id));
CREATE TABLE IF NOT EXISTS chunks(document_id TEXT NOT NULL REFERENCES documents(document_id),ordinal INTEGER NOT NULL,start_offset INTEGER NOT NULL,end_offset INTEGER NOT NULL,text TEXT NOT NULL,digest TEXT NOT NULL,tokens INTEGER NOT NULL,PRIMARY KEY(document_id,ordinal));
CREATE TABLE IF NOT EXISTS review_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,document_id TEXT NOT NULL,revision INTEGER NOT NULL,actor TEXT NOT NULL,decision TEXT NOT NULL,reason TEXT NOT NULL,payload TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actors(actor_id TEXT PRIMARY KEY,role TEXT NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_claims(document_id TEXT PRIMARY KEY,owner TEXT NOT NULL,generation INTEGER NOT NULL,expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cluster_runs(run_id TEXT PRIMARY KEY,corpus_revision INTEGER NOT NULL,threshold_numerator INTEGER NOT NULL,threshold_denominator INTEGER NOT NULL,shingle_size INTEGER NOT NULL,parameters TEXT NOT NULL,state TEXT NOT NULL,digest TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cluster_members(run_id TEXT NOT NULL REFERENCES cluster_runs(run_id),cluster_id TEXT NOT NULL,document_id TEXT NOT NULL,representative INTEGER NOT NULL,PRIMARY KEY(run_id,document_id));
CREATE TABLE IF NOT EXISTS cluster_edges(run_id TEXT NOT NULL,left_id TEXT NOT NULL,right_id TEXT NOT NULL,intersection_size INTEGER NOT NULL,union_size INTEGER NOT NULL,kind TEXT NOT NULL,PRIMARY KEY(run_id,left_id,right_id));
CREATE TABLE IF NOT EXISTS releases(release_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,state TEXT NOT NULL,run_id TEXT NOT NULL,context TEXT NOT NULL,definition TEXT NOT NULL,digest TEXT NOT NULL,corpus_revision INTEGER NOT NULL,created_at TEXT NOT NULL,published_at TEXT);
CREATE TABLE IF NOT EXISTS release_members(release_id TEXT NOT NULL REFERENCES releases(release_id),document_id TEXT NOT NULL,cluster_id TEXT NOT NULL,split TEXT NOT NULL,ordinal INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(release_id,document_id));
CREATE TABLE IF NOT EXISTS release_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,release_id TEXT NOT NULL,action TEXT NOT NULL,body TEXT NOT NULL,at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(position INTEGER PRIMARY KEY AUTOINCREMENT,action TEXT NOT NULL,payload TEXT NOT NULL,previous_hash TEXT NOT NULL,entry_hash TEXT NOT NULL,at TEXT NOT NULL);
'''


class Database:
    def __init__(self, path=':memory:', clock=now):
        self.clock = clock
        self.db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            fail('database_version', 'Unsupported corpus database version')
        self.db.executescript(DDL)
        self.db.execute('PRAGMA user_version=1')
        self.depth = 0

    def close(self):
        self.db.close()

    def execute(self, query, args=()):
        return self.db.execute(query, args)

    def one(self, query, args=()):
        row = self.execute(query, args).fetchone()
        return dict(row) if row else None

    def all(self, query, args=()):
        return [dict(row) for row in self.execute(query, args)]

    def scalar(self, query, args=()):
        row = self.execute(query, args).fetchone()
        return row[0] if row else None

    @contextlib.contextmanager
    def transaction(self):
        depth = self.depth
        self.execute(f'SAVEPOINT gate_{depth}' if depth else 'BEGIN IMMEDIATE')
        self.depth += 1
        try:
            yield self
        except BaseException:
            if depth:
                self.execute(f'ROLLBACK TO gate_{depth}')
                self.execute(f'RELEASE gate_{depth}')
            else:
                self.db.rollback()
            raise
        else:
            if depth:
                self.execute(f'RELEASE gate_{depth}')
            else:
                self.db.commit()
        finally:
            self.depth -= 1

    def revision(self):
        return self.scalar("SELECT value FROM counters WHERE name='corpus'")

    def changed(self):
        self.execute("UPDATE counters SET value=value+1 WHERE name='corpus'")
        return self.revision()

    def log(self, action, payload):
        previous = self.scalar('SELECT entry_hash FROM audit ORDER BY position DESC LIMIT 1') or '0' * 64
        at = self.clock()
        encoded = canonical(payload)
        sha = digest([previous, action, encoded, at])
        self.execute('INSERT INTO audit(action,payload,previous_hash,entry_hash,at) VALUES(?,?,?,?,?)', (action, encoded, previous, sha, at))
        return sha

    def audit_check(self):
        previous = '0' * 64
        count = 0
        for row in self.all('SELECT * FROM audit ORDER BY position'):
            expected = digest([previous, row['action'], row['payload'], row['at']])
            if row['previous_hash'] != previous or row['entry_hash'] != expected:
                return {'ok': False, 'position': row['position'], 'count': count}
            previous = expected
            count += 1
        return {'ok': True, 'count': count, 'head': previous}

    def integrity(self):
        checks = [row[0] for row in self.execute('PRAGMA integrity_check')]
        foreign = self.all('PRAGMA foreign_key_check')
        return {'ok': checks == ['ok'] and not foreign, 'checks': checks, 'foreign_keys': foreign}
