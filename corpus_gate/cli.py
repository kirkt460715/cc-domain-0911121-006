import argparse
import pathlib
import sys
from corpus_gate.errors import GateError, canonical, loads, shape, fail
from corpus_gate.service import CorpusGate
from corpus_gate import batch, documents, inspection
from corpus_gate.ingest import sources, formats, bundles
from corpus_gate.mail import connector as mail_connector
from corpus_gate.text import profiles
from corpus_gate.dedup import clusters
from corpus_gate.releases import manifests, exporting, transfer


def read(path, maximum=33554432):
    if path == '-':
        raw = sys.stdin.read(maximum + 1)
    else:
        source = pathlib.Path(path)
        if source.stat().st_size > maximum:
            fail('file_size', 'Input file exceeds size limit')
        raw = source.read_text(encoding='utf-8')
    if len(raw.encode('utf-8')) > maximum:
        fail('file_size', 'Input content exceeds size limit')
    return raw


def json_file(path):
    return loads(read(path))


def write(value, path=None):
    encoded = canonical(value) + '\n'
    if path is None or path == '-':
        sys.stdout.write(encoded)
    else:
        target = pathlib.Path(path)
        if target.exists():
            fail('file_exists', 'Output already exists; choose another filename')
        with target.open('x', encoding='utf-8') as stream:
            stream.write(encoded)


def parser():
    root = argparse.ArgumentParser(prog='corpus-gate', description='Offline corpus admission, review, deduplication and immutable dataset releases')
    root.add_argument('--db', default='corpus.sqlite3')
    root.add_argument('--output')
    sub = root.add_subparsers(dest='command', required=True)
    call = sub.add_parser('call')
    call.add_argument('operation')
    call.add_argument('--input', required=True)
    transaction = sub.add_parser('transaction')
    transaction.add_argument('--input', required=True)
    ingest = sub.add_parser('ingest')
    ingest.add_argument('--batch', required=True)
    ingest.add_argument('--source', required=True)
    ingest.add_argument('--profile', required=True)
    ingest.add_argument('--format', choices=['json', 'jsonlines', 'csv'], default='jsonlines')
    ingest.add_argument('--input', required=True)
    ingest.add_argument('--stage-only', action='store_true')
    ingest.add_argument('--chunk-size', type=int, default=100)
    resume = sub.add_parser('resume')
    resume.add_argument('batch_id')
    resume.add_argument('--chunk-size', type=int, default=100)
    cluster = sub.add_parser('cluster')
    cluster.add_argument('run_id')
    cluster.add_argument('--numerator', type=int, default=4)
    cluster.add_argument('--denominator', type=int, default=5)
    cluster.add_argument('--shingle-size', type=int, default=3)
    prepare = sub.add_parser('prepare-release')
    prepare.add_argument('release_id')
    prepare.add_argument('--run', required=True)
    prepare.add_argument('--context', required=True)
    prepare.add_argument('--selection', required=True)
    commit = sub.add_parser('commit-release')
    commit.add_argument('release_id')
    commit.add_argument('--revision', required=True, type=int)
    export = sub.add_parser('export')
    export.add_argument('release_id')
    export.add_argument('--format', choices=['manifest', 'jsonlines', 'csv', 'chunks'], default='manifest')
    export.add_argument('--at')
    export.add_argument('--split')
    verify = sub.add_parser('verify-export')
    verify.add_argument('--input', required=True)
    configure = sub.add_parser('import-configuration')
    configure.add_argument('--input', required=True)
    sub.add_parser('export-configuration')
    sub.add_parser('check')
    sub.add_parser('overview')
    sub.add_parser('operations')
    demo = sub.add_parser('demo')
    demo.add_argument('--fixture', required=True)
    sync_mail = sub.add_parser('mail-sync')
    sync_mail.add_argument('--mailbox', required=True)
    sync_mail.add_argument('--base-url', required=True)
    sync_mail.add_argument('--source', required=True)
    sync_mail.add_argument('--profile', required=True)
    sync_mail.add_argument('--context', required=True)
    sync_mail.add_argument('--epoch')
    sync_mail.add_argument('--limit', type=int)
    sync_mail.add_argument('--sync-id')
    retry_mail = sub.add_parser('mail-retry')
    retry_mail.add_argument('delivery_id')
    retry_mail.add_argument('--context', required=True)
    retry_mail.add_argument('--corrections')
    retry_mail.add_argument('--base-url')
    mail_trace = sub.add_parser('mail-trace')
    mail_trace.add_argument('document_id')
    mail_deliveries = sub.add_parser('mail-deliveries')
    mail_deliveries.add_argument('--mailbox')
    mail_deliveries.add_argument('--epoch')
    mail_deliveries.add_argument('--state')
    sub.add_parser('mail-conflicts')
    return root


def demonstrate(app, fixture):
    shape(fixture, {'source_id', 'authorization', 'profile', 'records', 'context', 'selection'})
    sources.create(app.db, fixture['source_id'], fixture['authorization'])
    profiles.put(app.db, 'default', 0, fixture['profile'])
    batch.stage(app.db, 'demo-batch', fixture['source_id'], 'default', fixture['records'])
    first = batch.process(app.db, 'demo-batch', 1, 1)
    from corpus_gate.ingest import recovery
    checkpoint = recovery.checkpoint(app.db, 'demo-batch')
    if not checkpoint['resumable']:
        fail('demo_checkpoint', 'Synthetic batch did not leave a resumable checkpoint')
    completed = batch.run(app.db, 'demo-batch', 1)
    if completed['counts'].get('failed'):
        fail('demo_input', 'Synthetic records failed ingestion', records=batch.records(app.db, 'demo-batch'))
    clustered = clusters.run(app.db, 'demo-clusters')
    release = manifests.prepare(app.db, 'demo-release', 'demo-clusters', fixture['context'], fixture['selection'])
    published = manifests.commit(app.db, 'demo-release', release['revision'])
    envelope = exporting.export(app.db, 'demo-release')
    verified = exporting.verify(envelope)
    package = transfer.package(envelope)
    app.receiver.open('demo-transfer', package['header'])
    for part in reversed(package['parts']):
        app.receiver.receive('demo-transfer', part['ordinal'], part['payload'], part['digest'])
    app.receiver.commit('demo-transfer', app.receiver.status('demo-transfer')['revision'])
    if app.receiver.export('demo-transfer') != envelope:
        fail('demo_transfer', 'Transfer did not reconstruct exact release envelope')
    sources.revoke(app.db, fixture['source_id'], 1, 'Synthetic withdrawal')
    blocked = not manifests.authorization(app.db, 'demo-release')['allowed']
    if not blocked:
        fail('demo_withdrawal', 'Source withdrawal did not block release authorization')
    checked = inspection.check(app.db)
    if not checked['ok']:
        fail('demo_integrity', 'Synthetic corpus integrity failed', problems=checked['problems'])
    return {'synthetic': True, 'checkpoint_cursor': first['cursor'], 'ingested_records': completed['total'], 'accepted_cluster_members': len(clustered['members']), 'clusters': len(clusters.groups(app.db, 'demo-clusters')), 'published_documents': len(published['members']), 'release_digest': published['digest'], 'export_integrity': verified['integrity'], 'withdrawal_blocks_export': blocked, 'integrity': checked['ok']}


def execute(app, args):
    if args.command == 'call':
        return app.dispatch(args.operation, json_file(args.input))
    if args.command == 'transaction':
        return app.transaction(json_file(args.input))
    if args.command == 'ingest':
        raw = read(args.input)
        records = loads(raw) if args.format == 'json' else formats.jsonlines(raw) if args.format == 'jsonlines' else formats.csv_rows(raw)
        staged = batch.stage(app.db, args.batch, args.source, args.profile, records)
        return staged if args.stage_only else batch.run(app.db, args.batch, args.chunk_size)
    if args.command == 'resume':
        return batch.run(app.db, args.batch_id, args.chunk_size)
    if args.command == 'cluster':
        return clusters.run(app.db, args.run_id, args.numerator, args.denominator, args.shingle_size)
    if args.command == 'prepare-release':
        return manifests.prepare(app.db, args.release_id, args.run, json_file(args.context), json_file(args.selection))
    if args.command == 'commit-release':
        return manifests.commit(app.db, args.release_id, args.revision)
    if args.command == 'export':
        if args.format == 'manifest':
            return exporting.export(app.db, args.release_id, args.at, args.split)
        if args.format == 'csv':
            return exporting.csv_export(app.db, args.release_id, args.at, args.split)
        return exporting.jsonlines(app.db, args.release_id, args.at, args.split, args.format == 'chunks')
    if args.command == 'verify-export':
        return exporting.verify(json_file(args.input))
    if args.command == 'import-configuration':
        return bundles.import_configuration(app.db, json_file(args.input))
    if args.command == 'export-configuration':
        return bundles.export_configuration(app.db)
    if args.command == 'check':
        return inspection.check(app.db)
    if args.command == 'overview':
        return inspection.overview(app.db)
    if args.command == 'operations':
        return [{'operation': key, 'required': required.split(), 'optional': optional.split()} for key, (_, required, optional) in sorted(app.operations().items())]
    if args.command == 'demo':
        return demonstrate(app, json_file(args.fixture))
    if args.command == 'mail-sync':
        return mail_connector.sync(app.db, args.mailbox, args.base_url, args.source, args.profile, json_file(args.context), epoch=args.epoch, limit=args.limit, sync_id=args.sync_id)
    if args.command == 'mail-retry':
        corrections = json_file(args.corrections) if args.corrections else None
        return mail_connector.retry_delivery(app.db, args.delivery_id, json_file(args.context), corrections=corrections, base_url=args.base_url)
    if args.command == 'mail-trace':
        return mail_connector.trace_document(app.db, args.document_id)
    if args.command == 'mail-deliveries':
        return mail_connector.list_deliveries(app.db, args.mailbox, args.epoch, args.state)
    if args.command == 'mail-conflicts':
        return mail_connector.conflicts(app.db)
    fail('command', 'Unsupported command')


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        with CorpusGate(args.db) as app:
            result = execute(app, args)
        write(result, args.output)
        if args.command == 'check' and not result['ok']:
            return 2
        if args.command in {'ingest', 'resume'} and result['counts'].get('failed', 0):
            return 2
        if args.command == 'mail-sync' and result['failures']:
            return 2
        return 0
    except GateError as exc:
        sys.stderr.write(canonical({'ok': False, 'error': exc.as_dict()}) + '\n')
        return 2
    except (OSError, UnicodeError) as exc:
        sys.stderr.write(canonical({'ok': False, 'error': {'code': 'io_error', 'message': str(exc)}}) + '\n')
        return 3
