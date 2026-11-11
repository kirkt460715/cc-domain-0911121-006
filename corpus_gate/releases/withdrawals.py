import json
from corpus_gate.errors import identifier, canonical, digest, fail, text
from corpus_gate.releases import manifests

DDL = '''
CREATE TABLE IF NOT EXISTS subscribers(subscriber_id TEXT PRIMARY KEY,purpose TEXT NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subscriptions(subscriber_id TEXT NOT NULL,release_id TEXT NOT NULL,release_digest TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(subscriber_id,release_id));
CREATE TABLE IF NOT EXISTS notices(notice_id TEXT PRIMARY KEY,subscriber_id TEXT NOT NULL,release_id TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,receipt TEXT);
'''


class Notices:
    def __init__(self, db):
        self.db = db
        if db.depth:
            fail('notice_transaction', 'Initialize notices outside transaction')
        db.db.executescript(DDL)

    def register(self, subscriber_id, purpose):
        identifier(subscriber_id)
        identifier(purpose)
        with self.db.transaction():
            previous = self.db.one('SELECT * FROM subscribers WHERE subscriber_id=?', (subscriber_id,))
            if previous:
                if previous['purpose'] != purpose:
                    fail('subscriber_conflict', 'Subscriber purpose cannot silently change')
                return previous
            self.db.execute("INSERT INTO subscribers VALUES(?,?,'active')", (subscriber_id, purpose))
            self.db.log('subscriber.registered', {'subscriber_id': subscriber_id, 'purpose': purpose})
            return self.db.one('SELECT * FROM subscribers WHERE subscriber_id=?', (subscriber_id,))

    def subscribe(self, subscriber_id, release_id, expected_digest):
        with self.db.transaction():
            subscriber = self.db.one('SELECT * FROM subscribers WHERE subscriber_id=?', (subscriber_id,))
            if subscriber is None or subscriber['state'] != 'active':
                fail('subscriber_missing', 'Subscriber is not active')
            release = manifests.get(self.db, release_id)
            if release['state'] != 'published' or release['digest'] != expected_digest:
                fail('subscription_release', 'Subscription must acknowledge a published release digest')
            if subscriber['purpose'] != release['context']['purpose']:
                fail('subscription_purpose', 'Subscriber purpose differs from release context')
            if not manifests.authorization(self.db, release_id)['allowed']:
                fail('subscription_authorization', 'Release is no longer authorized')
            self.db.execute('INSERT OR IGNORE INTO subscriptions VALUES(?,?,?,?)', (subscriber_id, release_id, expected_digest, self.db.clock()))
            self.db.log('release.subscribed', {'subscriber_id': subscriber_id, 'release_id': release_id})
            return {'subscriber_id': subscriber_id, 'release_id': release_id, 'digest': expected_digest}

    def reconcile(self, at):
        created = []
        with self.db.transaction():
            for subscription in self.db.all('SELECT * FROM subscriptions ORDER BY subscriber_id,release_id'):
                decision = manifests.authorization(self.db, subscription['release_id'], at)
                if decision['allowed']:
                    continue
                payload = {'subscriber_id': subscription['subscriber_id'], 'release_id': subscription['release_id'], 'release_digest': subscription['release_digest'], 'failures': decision['failures']}
                sha = digest(payload)
                identity = 'notice-' + sha
                if self.db.one('SELECT notice_id FROM notices WHERE notice_id=?', (identity,)):
                    continue
                self.db.execute("INSERT INTO notices VALUES(?,?,?,?,?,'pending',?,NULL)", (identity, subscription['subscriber_id'], subscription['release_id'], canonical(payload), sha, self.db.clock()))
                created.append(identity)
            if created:
                self.db.log('notices.reconciled', {'notice_ids': created})
        return {'created': created, 'count': len(created)}

    def pending(self, subscriber_id):
        rows = self.db.all("SELECT * FROM notices WHERE subscriber_id=? AND state='pending' ORDER BY created_at,notice_id", (subscriber_id,))
        for row in rows:
            row['payload'] = json.loads(row['payload'])
            if digest(row['payload']) != row['digest']:
                fail('notice_corrupt', 'Notice digest mismatch')
        return rows

    def acknowledge(self, subscriber_id, notice_id, expected_digest, receipt):
        receipt = identifier(receipt)
        with self.db.transaction():
            notice = self.db.one('SELECT * FROM notices WHERE notice_id=?', (notice_id,))
            if notice is None or notice['subscriber_id'] != subscriber_id or notice['digest'] != expected_digest:
                fail('notice_identity', 'Notice recipient or checksum mismatch')
            if notice['state'] == 'acknowledged':
                if notice['receipt'] != receipt:
                    fail('notice_receipt', 'Notice already has a different receipt')
                return {'notice_id': notice_id, 'state': 'acknowledged', 'receipt': receipt}
            self.db.execute("UPDATE notices SET state='acknowledged',receipt=? WHERE notice_id=?", (receipt, notice_id))
            self.db.log('notice.acknowledged', {'notice_id': notice_id, 'subscriber_id': subscriber_id, 'receipt': receipt})
            return {'notice_id': notice_id, 'state': 'acknowledged', 'receipt': receipt}

    def impact(self, release_id):
        release = manifests.get(self.db, release_id)
        subscriptions = self.db.all('SELECT * FROM subscriptions WHERE release_id=? ORDER BY subscriber_id', (release_id,))
        notices = self.db.all('SELECT subscriber_id,state,COUNT(*) AS count FROM notices WHERE release_id=? GROUP BY subscriber_id,state ORDER BY subscriber_id,state', (release_id,))
        return {'release_id': release_id, 'state': release['state'], 'subscribers': subscriptions, 'notices': notices}
