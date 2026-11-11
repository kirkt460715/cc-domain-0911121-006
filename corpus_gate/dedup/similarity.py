import hashlib
from corpus_gate.errors import integer, fail
from corpus_gate.text.normalize import tokens


def shingles(value, size=3):
    integer(size, 'shingle_size', 1, 20)
    terms = [token['text'].casefold() for token in tokens(value)]
    if not terms:
        return frozenset()
    if len(terms) < size:
        return frozenset([tuple(terms)])
    return frozenset(tuple(terms[index:index + size]) for index in range(len(terms) - size + 1))


def ratio(left, right):
    intersection = len(left & right)
    union = len(left | right)
    return {'intersection': intersection, 'union': union, 'equal_empty': not union}


def matches(left, right, numerator, denominator):
    integer(numerator, 'threshold_numerator', 1, 10000)
    integer(denominator, 'threshold_denominator', 1, 10000)
    if numerator > denominator:
        fail('threshold', 'Similarity threshold must not exceed one')
    score = ratio(left, right)
    return bool(score['union']) and score['intersection'] * denominator >= score['union'] * numerator


def candidates(signatures, numerator, denominator):
    postings = {}
    result = set()
    for identity in sorted(signatures):
        signature = signatures[identity]
        for shingle in sorted(signature):
            for other in postings.get(shingle, ()):
                left_size = len(signatures[other])
                right_size = len(signature)
                if min(left_size, right_size) * denominator >= max(left_size, right_size) * numerator:
                    result.add((other, identity))
            postings.setdefault(shingle, []).append(identity)
    return sorted(result)


def simhash(value):
    terms = [token['text'].casefold() for token in tokens(value)]
    if not terms:
        return 0
    weights = [0] * 64
    for term in terms:
        number = int.from_bytes(hashlib.sha256(term.encode()).digest()[:8], 'big')
        for bit in range(64):
            weights[bit] += 1 if number & (1 << bit) else -1
    result = 0
    for bit, weight in enumerate(weights):
        if weight > 0:
            result |= 1 << bit
    return result


def distance(left, right):
    integer(left, 'left', 0, 2**64 - 1)
    integer(right, 'right', 0, 2**64 - 1)
    return (left ^ right).bit_count()


def explain(left_text, right_text, size=3):
    left = shingles(left_text, size)
    right = shingles(right_text, size)
    score = ratio(left, right)
    return {**score, 'shared': [list(item) for item in sorted(left & right)], 'left_only': [list(item) for item in sorted(left - right)], 'right_only': [list(item) for item in sorted(right - left)], 'simhash_distance': distance(simhash(left_text), simhash(right_text)), 'semantics': 'token_shingle_jaccard_not_semantic_equivalence'}
