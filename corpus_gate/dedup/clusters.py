import json
from corpus_gate.errors import identifier, integer, fail, digest, canonical
from corpus_gate.dedup import similarity, constraints
from corpus_gate import documents
from corpus_gate.ingest import sources


class DisjointSets:
    def __init__(self, identities):
        self.parents = {identity: identity for identity in identities}

    def root(self, identity):
        trail = []
        while self.parents[identity] != identity:
            trail.append(identity)
            identity = self.parents[identity]
        for member in trail:
            self.parents[member] = identity
        return identity

    def union(self, left, right):
        a = self.root(left)
        b = self.root(right)
        if a != b:
            smaller, larger = sorted([a, b])
            self.parents[larger] = smaller

    def groups(self):
        result = {}
        for identity in sorted(self.parents):
            result.setdefault(self.root(identity), []).append(identity)
        return list(result.values())


def eligible(db):
    rows = db.all("SELECT d.document_id FROM documents d JOIN document_heads h ON h.document_id=d.document_id JOIN sources s ON s.source_id=d.source_id WHERE d.state='accepted' AND s.state='active' ORDER BY d.document_id")
    return [documents.get(db, row['document_id']) for row in rows]


def representative(db, members):
    def rank(document):
        source = sources.get(db, document['source_id'])
        return (source['priority'], -document['metrics']['tokens'], document['source_id'], document['external_id'], -document['version'], document['document_id'])
    return min(members, key=rank)['document_id']


def run(db, run_id, numerator=4, denominator=5, shingle_size=3, maximum_documents=5000):
    identifier(run_id)
    integer(numerator, 'numerator', 1, 10000)
    integer(denominator, 'denominator', numerator, 10000)
    integer(shingle_size, 'shingle_size', 1, 20)
    integer(maximum_documents, 'maximum_documents', 1, 10000)
    with db.transaction():
        if db.one('SELECT run_id FROM cluster_runs WHERE run_id=?', (run_id,)):
            fail('cluster_exists', 'Cluster run identifiers are immutable')
        rows = eligible(db)
        if len(rows) > maximum_documents:
            fail('cluster_capacity', 'Eligible corpus exceeds configured in-memory clustering limit')
        records = {row['document_id']: row for row in rows}
        sets = DisjointSets(records)
        rules = constraints.active(db, records)
        forbidden = [(rule['left_id'], rule['right_id']) for rule in rules if rule['kind'] == 'different']
        exact = {}
        edges = []
        suppressed = []
        for rule in rules:
            if rule['kind'] == 'same':
                if not constraints.can_union(sets, rule['left_id'], rule['right_id'], forbidden):
                    fail('constraint_contradiction', 'Manual same-component requirements contradict different constraints')
                sets.union(rule['left_id'], rule['right_id'])
                edges.append({'left_id': rule['left_id'], 'right_id': rule['right_id'], 'intersection_size': 0, 'union_size': 0, 'kind': 'manual'})
        for row in rows:
            identity = row['document_id']
            previous = exact.get(row['exact_hash'])
            if previous and constraints.can_union(sets, previous, identity, forbidden):
                sets.union(previous, identity)
                if not any(edge['left_id'] == previous and edge['right_id'] == identity for edge in edges):
                    edges.append({'left_id': previous, 'right_id': identity, 'intersection_size': 1, 'union_size': 1, 'kind': 'exact'})
            elif not previous:
                exact[row['exact_hash']] = identity
            else:
                suppressed.append({'left_id': previous, 'right_id': identity, 'reason': 'different_constraint'})
        signatures = {identity: similarity.shingles(row['normalized'], shingle_size) for identity, row in records.items()}
        for left, right in similarity.candidates(signatures, numerator, denominator):
            if records[left]['exact_hash'] == records[right]['exact_hash']:
                continue
            if similarity.matches(signatures[left], signatures[right], numerator, denominator):
                if not constraints.can_union(sets, left, right, forbidden):
                    suppressed.append({'left_id': left, 'right_id': right, 'reason': 'different_constraint'})
                    continue
                if any(edge['left_id'] == left and edge['right_id'] == right for edge in edges):
                    continue
                score = similarity.ratio(signatures[left], signatures[right])
                sets.union(left, right)
                edges.append({'left_id': left, 'right_id': right, 'intersection_size': score['intersection'], 'union_size': score['union'], 'kind': 'near'})
        members = []
        for group in sets.groups():
            cluster_id = 'cluster-' + digest(group)
            chosen = representative(db, [records[identity] for identity in group])
            for identity in group:
                members.append({'cluster_id': cluster_id, 'document_id': identity, 'representative': identity == chosen})
        members.sort(key=lambda member: (member['cluster_id'], member['document_id']))
        edges.sort(key=lambda edge: (edge['left_id'], edge['right_id'], edge['kind']))
        params = {'algorithm': 'connected_components_exact_and_jaccard', 'representative': 'priority_tokens_identity', 'maximum_documents': maximum_documents, 'constraints': rules, 'suppressed': suppressed}
        checksum = digest({'members': members, 'edges': edges, 'parameters': params})
        db.execute("INSERT INTO cluster_runs VALUES(?,?,?,?,?,?, 'complete',?,?)", (run_id, db.revision(), numerator, denominator, shingle_size, canonical(params), checksum, db.clock()))
        for member in members:
            db.execute('INSERT INTO cluster_members VALUES(?,?,?,?)', (run_id, member['cluster_id'], member['document_id'], int(member['representative'])))
        for edge in edges:
            db.execute('INSERT INTO cluster_edges VALUES(?,?,?,?,?,?)', (run_id, edge['left_id'], edge['right_id'], edge['intersection_size'], edge['union_size'], edge['kind']))
        db.log('clusters.completed', {'run_id': run_id, 'documents': len(rows), 'clusters': len(sets.groups()), 'edges': len(edges)})
        return get(db, run_id)


def get(db, run_id):
    row = db.one('SELECT * FROM cluster_runs WHERE run_id=?', (run_id,))
    if row is None:
        fail('cluster_missing', 'Cluster run does not exist')
    row['parameters'] = json.loads(row['parameters'])
    row['members'] = db.all('SELECT cluster_id,document_id,representative FROM cluster_members WHERE run_id=? ORDER BY cluster_id,document_id', (run_id,))
    for member in row['members']:
        member['representative'] = bool(member['representative'])
    row['edges'] = db.all('SELECT left_id,right_id,intersection_size,union_size,kind FROM cluster_edges WHERE run_id=? ORDER BY left_id,right_id,kind', (run_id,))
    expected = digest({'members': row['members'], 'edges': row['edges'], 'parameters': row['parameters']})
    if expected != row['digest']:
        fail('cluster_corrupt', 'Stored cluster manifest digest differs')
    row['fresh'] = row['corpus_revision'] == db.revision()
    return row


def groups(db, run_id):
    result = {}
    for member in get(db, run_id)['members']:
        group = result.setdefault(member['cluster_id'], {'cluster_id': member['cluster_id'], 'members': [], 'representative': None})
        group['members'].append(member['document_id'])
        if member['representative']:
            group['representative'] = member['document_id']
    return [result[key] for key in sorted(result)]


def explain_membership(db, run_id, document_id):
    run = get(db, run_id)
    member = next((row for row in run['members'] if row['document_id'] == document_id), None)
    if member is None:
        fail('cluster_document_missing', 'Document is absent from this cluster run')
    group = next(row for row in groups(db, run_id) if row['cluster_id'] == member['cluster_id'])
    connected = [edge for edge in run['edges'] if edge['left_id'] in group['members'] and edge['right_id'] in group['members']]
    return {'run_id': run_id, 'fresh': run['fresh'], 'group': group, 'edges': connected, 'transitive_warning': 'Connected component members need not all meet pairwise threshold'}


def compare(db, before, after):
    left = get(db, before)
    right = get(db, after)
    old = {row['document_id']: row for row in left['members']}
    new = {row['document_id']: row for row in right['members']}
    changed = []
    for identity in sorted(set(old) | set(new)):
        if old.get(identity) != new.get(identity):
            changed.append({'document_id': identity, 'before': old.get(identity), 'after': new.get(identity)})
    return {'before': before, 'after': after, 'changes': changed}
