import re
import unicodedata
from corpus_gate.errors import fail, integer, shape, digest

DEFAULT = {'unicode_form': 'NFKC', 'casefold': False, 'collapse_spaces': True, 'strip_controls': True, 'max_characters': 1000000}


def settings(value):
    if value is None:
        return dict(DEFAULT)
    shape(value, set(), set(DEFAULT))
    result = {**DEFAULT, **value}
    if result['unicode_form'] not in {'NFC', 'NFKC'}:
        fail('normalization_form', 'Only NFC and NFKC are supported')
    for key in ('casefold', 'collapse_spaces', 'strip_controls'):
        if type(result[key]) is not bool:
            fail('normalization_flag', 'Normalization flags must be booleans', field=key)
    integer(result['max_characters'], 'max_characters', 1, 2000000)
    return result


def normalize(raw, options=None):
    options = settings(options)
    if not isinstance(raw, str):
        fail('document_text', 'Document text must be a Unicode string')
    if len(raw) > options['max_characters']:
        fail('document_size', 'Document exceeds configured character limit')
    for character in raw:
        if 0xD800 <= ord(character) <= 0xDFFF:
            fail('unicode_surrogate', 'Document contains unpaired Unicode surrogate')
    changes = []
    value = raw.replace('\r\n', '\n').replace('\r', '\n')
    if value != raw:
        changes.append('newlines')
    transformed = unicodedata.normalize(options['unicode_form'], value)
    if transformed != value:
        changes.append('unicode')
    value = transformed
    if options['strip_controls']:
        transformed = ''.join(char for char in value if char in '\n\t' or unicodedata.category(char) not in {'Cc', 'Cf'})
        if transformed != value:
            changes.append('controls')
        value = transformed
    if options['casefold']:
        transformed = value.casefold()
        if transformed != value:
            changes.append('casefold')
        value = transformed
    if options['collapse_spaces']:
        transformed = '\n'.join(re.sub(r'[^\S\n]+', ' ', line).strip() for line in value.split('\n'))
        transformed = re.sub(r'\n{3,}', '\n\n', transformed)
        if transformed != value:
            changes.append('spaces')
        value = transformed
    value = value.strip()
    return {'text': value, 'raw_characters': len(raw), 'characters': len(value), 'changes': changes, 'normalizer_hash': digest(options)}


def tokens(value):
    output = []
    start = None
    for index, char in enumerate(value):
        category = unicodedata.category(char)
        cjk = '\u3400' <= char <= '\u9fff' or '\uf900' <= char <= '\ufaff'
        if cjk:
            if start is not None:
                output.append({'text': value[start:index], 'start': start, 'end': index})
                start = None
            output.append({'text': char, 'start': index, 'end': index + 1})
        elif category[0] in {'L', 'N', 'M'} or char == '_':
            if start is None:
                start = index
        elif start is not None:
            output.append({'text': value[start:index], 'start': start, 'end': index})
            start = None
    if start is not None:
        output.append({'text': value[start:], 'start': start, 'end': len(value)})
    return output


def language_hint(value):
    letters = [char for char in value if unicodedata.category(char).startswith('L')]
    if not letters:
        return 'und'
    cjk = sum('\u3400' <= char <= '\u9fff' for char in letters)
    latin = sum('LATIN' in unicodedata.name(char, '') for char in letters)
    if cjk * 2 >= len(letters):
        return 'zh'
    if latin * 2 >= len(letters):
        return 'latin'
    return 'mixed'
