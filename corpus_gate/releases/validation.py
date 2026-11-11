from corpus_gate.errors import shape, integer, fail
from corpus_gate.releases import manifests, selection
from corpus_gate.ingest import sources
from corpus_gate.text import chunking, quality


def contract(value):
    shape(value, set(), {'minimum_documents', 'minimum_sources', 'minimum_tokens', 'maximum_document_tokens', 'required_splits', 'maximum_source_percent', 'allow_reviewed_sensitive'})
    result = {'minimum_documents': value.get('minimum_documents', 1), 'minimum_sources': value.get('minimum_sources', 1), 'minimum_tokens': value.get('minimum_tokens', 1), 'maximum_document_tokens': value.get('maximum_document_tokens', 1000000), 'required_splits': value.get('required_splits', []), 'maximum_source_percent': value.get('maximum_source_percent', 100), 'allow_reviewed_sensitive': value.get('allow_reviewed_sensitive', False)}
    for key in ('minimum_documents', 'minimum_sources', 'minimum_tokens', 'maximum_document_tokens'):
        integer(result[key], key, 1, 100000000)
    integer(result['maximum_source_percent'], 'maximum_source_percent', 1, 100)
    if type(result['allow_reviewed_sensitive']) is not bool:
        fail('contract_sensitive', 'Sensitive content flag must be boolean')
    if not isinstance(result['required_splits'], list) or any(not isinstance(split, str) for split in result['required_splits']):
        fail('contract_splits', 'Required splits must be a list')
    return result


def validate(db, release_id, requirements):
    requirements = contract(requirements)
    release = manifests.get(db, release_id)
    problems = []
    members = release['members']
    sources_count = {}
    splits = {}
    tokens = 0
    for member in members:
        document = member['payload']
        source_id = document['source_id']
        sources_count[source_id] = sources_count.get(source_id, 0) + 1
        splits[member['split']] = splits.get(member['split'], 0) + 1
        size = document['metrics']['tokens']
        tokens += size
        if size > requirements['maximum_document_tokens']:
            problems.append({'code': 'oversize_document', 'document_id': member['document_id'], 'tokens': size})
        if not document['chunks']:
            problems.append({'code': 'unchunked_document', 'document_id': member['document_id']})
        if not requirements['allow_reviewed_sensitive']:
            findings = quality.inspect(document['text'], {'minimum_tokens': 0})['findings']
            sensitive = [finding['code'] for finding in findings if finding['code'] in {'email_like', 'phone_like'}]
            if sensitive:
                problems.append({'code': 'sensitive_content', 'document_id': member['document_id'], 'findings': sensitive})
    if len(members) < requirements['minimum_documents']:
        problems.append({'code': 'too_few_documents', 'actual': len(members)})
    if len(sources_count) < requirements['minimum_sources']:
        problems.append({'code': 'too_few_sources', 'actual': len(sources_count)})
    if tokens < requirements['minimum_tokens']:
        problems.append({'code': 'too_few_tokens', 'actual': tokens})
    missing = sorted(set(requirements['required_splits']) - set(splits))
    if missing:
        problems.append({'code': 'empty_required_splits', 'splits': missing})
    for source_id, count in sources_count.items():
        if count * 100 > requirements['maximum_source_percent'] * len(members):
            problems.append({'code': 'source_concentration', 'source_id': source_id, 'documents': count})
    leak = selection.leakage(members)
    if not leak['ok']:
        problems.append({'code': 'duplicate_leakage', 'details': leak})
    authorization = manifests.authorization(db, release_id)
    if not authorization['allowed']:
        problems.append({'code': 'authorization', 'details': authorization})
    return {'ok': not problems, 'release_id': release_id, 'requirements': requirements, 'problems': problems, 'counts': {'documents': len(members), 'sources': sources_count, 'splits': splits, 'tokens': tokens}}


def compare_contracts(db, release_id, before, after):
    left = validate(db, release_id, before)
    right = validate(db, release_id, after)
    from corpus_gate.errors import digest
    old = {digest(problem): problem for problem in left['problems']}
    new = {digest(problem): problem for problem in right['problems']}
    return {'before_ok': left['ok'], 'after_ok': right['ok'], 'introduced': [new[key] for key in sorted(set(new) - set(old))], 'resolved': [old[key] for key in sorted(set(old) - set(new))]}
