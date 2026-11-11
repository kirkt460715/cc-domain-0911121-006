import csv
import io
from corpus_gate.errors import identifier, integer, shape, fail, canonical, loads


def document(value):
    shape(value, {'external_id', 'version', 'text'}, {'metadata'})
    identity = identifier(value['external_id'], 'external_id')
    version = integer(value['version'], 'version', 1, 1000000000)
    raw = value['text']
    if not isinstance(raw, str) or len(raw) > 2000000:
        fail('document_text', 'Document text must be a bounded string')
    metadata = value.get('metadata', {})
    if not isinstance(metadata, dict) or len(canonical(metadata)) > 10000:
        fail('document_metadata', 'Metadata must be a bounded JSON object')
    return {'external_id': identity, 'version': version, 'text': raw, 'metadata': metadata}


def jsonlines(raw, maximum=10000):
    integer(maximum, 'maximum', 1, 10000)
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 33554432:
        fail('input_size', 'JSON-lines content exceeds limit')
    result = []
    for line, content in enumerate(raw.splitlines(), 1):
        if not content.strip():
            fail('blank_record', 'Blank lines are not document records', line=line)
        if len(result) >= maximum:
            fail('input_count', 'Too many input records')
        result.append(loads(content))
    return result


def csv_rows(raw, maximum=10000):
    integer(maximum, 'maximum', 1, 10000)
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 33554432:
        fail('input_size', 'CSV content exceeds limit')
    reader = csv.reader(io.StringIO(raw, newline=''), strict=True)
    try:
        header = next(reader)
        if len(set(header)) != len(header) or set(header) not in ({'external_id', 'version', 'text'}, {'external_id', 'version', 'text', 'metadata'}):
            fail('csv_header', 'CSV header must specify external_id, version and text')
        result = []
        for ordinal, cells in enumerate(reader, 1):
            if ordinal > maximum:
                fail('input_count', 'Too many CSV records')
            if len(cells) != len(header):
                fail('csv_width', 'CSV record width differs from header', row=ordinal)
            row = dict(zip(header, cells))
            try:
                version = int(row['version'])
            except ValueError:
                fail('csv_version', 'CSV version must be an integer', row=ordinal)
            if str(version) != row['version']:
                fail('csv_version', 'CSV version must use canonical decimal notation', row=ordinal)
            row['version'] = version
            if 'metadata' in row:
                row['metadata'] = loads(row['metadata'])
            result.append(row)
        return result
    except StopIteration:
        fail('csv_header', 'CSV input has no header')
    except csv.Error as exc:
        from corpus_gate.errors import GateError
        raise GateError('csv_parse', str(exc)) from exc


def encode_jsonlines(records):
    return ''.join(canonical(record) + '\n' for record in records)
