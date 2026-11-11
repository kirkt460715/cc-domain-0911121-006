import json
from corpus_gate.errors import canonical, digest, shape, identifier, integer, fail
from corpus_gate.releases import exporting

DDL = '''
CREATE TABLE IF NOT EXISTS transfers(transfer_id TEXT PRIMARY KEY,release_id TEXT NOT NULL,envelope_hash TEXT NOT NULL,header TEXT NOT NULL,total INTEGER NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS transfer_parts(transfer_id TEXT NOT NULL,ordinal INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(transfer_id,ordinal));
'''


class Receiver:
    def __init__(self, db):
        self.db = db
        if db.depth:
            fail('transfer_transaction', 'Initialize receiver outside transaction')
        db.db.executescript(DDL)

    def open(self, transfer_id, header):
        identifier(transfer_id)
        shape(header, {'release_id', 'envelope_hash', 'payload_header', 'total'})
        integer(header['total'], 'total', 0, 10000)
        shape(header['payload_header'], {'format', 'version', 'release_id', 'release_digest', 'context', 'selection', 'obligations'})
        if header['payload_header']['release_id'] != header['release_id']:
            fail('transfer_identity', 'Header release identifiers differ')
        with self.db.transaction():
            existing = self.db.one('SELECT * FROM transfers WHERE transfer_id=?', (transfer_id,))
            if existing:
                if json.loads(existing['header']) != header:
                    fail('transfer_conflict', 'Transfer identifier already binds other data')
                return self.status(transfer_id)
            self.db.execute("INSERT INTO transfers VALUES(?,?,?,?,?,'receiving',1)", (transfer_id, header['release_id'], header['envelope_hash'], canonical(header), header['total']))
            self.db.log('transfer.opened', {'transfer_id': transfer_id, 'total': header['total']})
            return self.status(transfer_id)

    def status(self, transfer_id):
        row = self.db.one('SELECT * FROM transfers WHERE transfer_id=?', (transfer_id,))
        if row is None:
            fail('transfer_missing', 'Transfer does not exist')
        row['header'] = json.loads(row['header'])
        received = [part['ordinal'] for part in self.db.all('SELECT ordinal FROM transfer_parts WHERE transfer_id=? ORDER BY ordinal', (transfer_id,))]
        row['received'] = received
        row['missing'] = sorted(set(range(row['total'])) - set(received))
        return row

    def receive(self, transfer_id, ordinal, part, expected_digest):
        integer(ordinal, 'ordinal')
        if digest(part) != expected_digest:
            fail('transfer_part_digest', 'Transfer part digest mismatch')
        with self.db.transaction():
            current = self.status(transfer_id)
            if ordinal >= current['total']:
                fail('transfer_ordinal', 'Transfer part is outside declared range')
            previous = self.db.one('SELECT digest FROM transfer_parts WHERE transfer_id=? AND ordinal=?', (transfer_id, ordinal))
            if previous:
                if previous['digest'] != expected_digest:
                    fail('transfer_part_conflict', 'A different part already occupies this ordinal')
                return current
            if current['state'] != 'receiving':
                fail('transfer_state', 'Transfer does not accept more parts')
            self.db.execute('INSERT INTO transfer_parts VALUES(?,?,?,?)', (transfer_id, ordinal, canonical(part), expected_digest))
            self.db.execute('UPDATE transfers SET revision=revision+1 WHERE transfer_id=?', (transfer_id,))
            return self.status(transfer_id)

    def assemble(self, transfer_id):
        current = self.status(transfer_id)
        if current['missing']:
            fail('transfer_incomplete', 'Transfer still has missing members', missing=current['missing'])
        rows = self.db.all('SELECT * FROM transfer_parts WHERE transfer_id=? ORDER BY ordinal', (transfer_id,))
        members = []
        for row in rows:
            part = json.loads(row['payload'])
            if digest(part) != row['digest']:
                fail('transfer_stored_digest', 'Stored transfer part was corrupted')
            members.append(part)
        envelope = {'payload': {**current['header']['payload_header'], 'members': members}, 'sha256': current['envelope_hash']}
        exporting.verify(envelope)
        return envelope

    def commit(self, transfer_id, expected_revision):
        with self.db.transaction():
            current = self.status(transfer_id)
            if current['revision'] != expected_revision:
                fail('transfer_stale', 'Transfer revision changed')
            if current['state'] != 'receiving':
                fail('transfer_state', 'Transfer is not receiving')
            envelope = self.assemble(transfer_id)
            self.db.execute("UPDATE transfers SET state='verified',revision=revision+1 WHERE transfer_id=?", (transfer_id,))
            self.db.log('transfer.verified', {'transfer_id': transfer_id, 'sha256': envelope['sha256']})
            return self.status(transfer_id)

    def export(self, transfer_id):
        if self.status(transfer_id)['state'] != 'verified':
            fail('transfer_unverified', 'Transfer must be verified before exposing data')
        return self.assemble(transfer_id)


def package(envelope):
    exporting.verify(envelope)
    payload = dict(envelope['payload'])
    members = payload.pop('members')
    header = {'release_id': payload['release_id'], 'envelope_hash': envelope['sha256'], 'payload_header': payload, 'total': len(members)}
    return {'header': header, 'parts': [{'ordinal': index, 'payload': member, 'digest': digest(member)} for index, member in enumerate(members)]}
