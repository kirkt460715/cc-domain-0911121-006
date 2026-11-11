from fractions import Fraction
from corpus_gate.errors import integer, shape, fail, digest
from corpus_gate.dedup import similarity, clusters
from corpus_gate import documents


def evaluate_pairs(db, pairs, numerator=4, denominator=5, size=3):
    if not isinstance(pairs, list) or not 1 <= len(pairs) <= 10000:
        fail('pair_samples', '相似度检查需要长度有限的记录对列表')
    integer(numerator, 'numerator', 1, 10000)
    integer(denominator, 'denominator', numerator, 10000)
    seen = set()
    counts = {'true_positive': 0, 'false_positive': 0, 'true_negative': 0, 'false_negative': 0}
    results = []
    for pair in pairs:
        shape(pair, {'left', 'right', 'duplicate'})
        if type(pair['duplicate']) is not bool:
            fail('pair_label', 'Pair duplicate label must be boolean')
        key = tuple(sorted([pair['left'], pair['right']]))
        if key[0] == key[1] or key in seen:
            fail('pair_duplicate', '记录对必须不同且不能重复')
        seen.add(key)
        left = documents.get(db, key[0])
        right = documents.get(db, key[1])
        left_signature = similarity.shingles(left['normalized'], size)
        right_signature = similarity.shingles(right['normalized'], size)
        predicted = left['exact_hash'] == right['exact_hash'] or similarity.matches(left_signature, right_signature, numerator, denominator)
        category = ('true_' if predicted == pair['duplicate'] else 'false_') + ('positive' if predicted else 'negative')
        counts[category] += 1
        results.append({'left': key[0], 'right': key[1], 'expected': pair['duplicate'], 'predicted': predicted, 'category': category, 'score': similarity.ratio(left_signature, right_signature)})
    def ratio(top, bottom):
        return None if not bottom else {'numerator': Fraction(top, bottom).numerator, 'denominator': Fraction(top, bottom).denominator}
    tp = counts['true_positive']
    fp = counts['false_positive']
    fn = counts['false_negative']
    return {'threshold': {'numerator': numerator, 'denominator': denominator}, 'size': size, 'counts': counts, 'precision': ratio(tp, tp + fp), 'recall': ratio(tp, tp + fn), 'f1': ratio(2 * tp, 2 * tp + fp + fn), 'results': results}


def sweep(db, pairs, thresholds, size=3):
    if not isinstance(thresholds, list) or not 1 <= len(thresholds) <= 20:
        fail('threshold_samples', 'Threshold sweep requires one to twenty configurations')
    results = []
    for threshold in thresholds:
        shape(threshold, {'numerator', 'denominator'})
        results.append(evaluate_pairs(db, pairs, threshold['numerator'], threshold['denominator'], size))
    return {'results': results, 'labeled_pairs': len(pairs), 'labels_digest': digest(pairs), 'scope': 'local_labeled_pairs_only'}


def bridges(db, run_id):
    run = clusters.get(db, run_id)
    adjacency = {member['document_id']: set() for member in run['members']}
    for edge in run['edges']:
        adjacency[edge['left_id']].add(edge['right_id'])
        adjacency[edge['right_id']].add(edge['left_id'])
    discovered = {}
    low = {}
    timer = [0]
    bridge_edges = []
    def visit(node, parent):
        discovered[node] = timer[0]
        low[node] = timer[0]
        timer[0] += 1
        for child in sorted(adjacency[node]):
            if child == parent:
                continue
            if child in discovered:
                low[node] = min(low[node], discovered[child])
            else:
                visit(child, node)
                low[node] = min(low[node], low[child])
                if low[child] > discovered[node]:
                    bridge_edges.append(tuple(sorted([node, child])))
    for node in sorted(adjacency):
        if node not in discovered:
            visit(node, None)
    return {'run_id': run_id, 'bridges': [{'left': left, 'right': right} for left, right in sorted(bridge_edges)], 'interpretation': 'Removing a bridge disconnects a duplicate component; not automatically an incorrect match'}


def cohesion(db, run_id, maximum_pairs=10000):
    integer(maximum_pairs, 'maximum_pairs', 1, 100000)
    run = clusters.get(db, run_id)
    inspected = 0
    weak = []
    for group in clusters.groups(db, run_id):
        members = group['members']
        for left_index, left_id in enumerate(members):
            left = documents.get(db, left_id)
            for right_id in members[left_index + 1:]:
                if inspected >= maximum_pairs:
                    return {'complete': False, 'inspected_pairs': inspected, 'weak_pairs': weak}
                right = documents.get(db, right_id)
                a = similarity.shingles(left['normalized'], run['shingle_size'])
                b = similarity.shingles(right['normalized'], run['shingle_size'])
                inspected += 1
                if left['exact_hash'] != right['exact_hash'] and not similarity.matches(a, b, run['threshold_numerator'], run['threshold_denominator']):
                    weak.append({'cluster_id': group['cluster_id'], 'left': left_id, 'right': right_id, 'score': similarity.ratio(a, b)})
    return {'complete': True, 'inspected_pairs': inspected, 'weak_pairs': weak}
