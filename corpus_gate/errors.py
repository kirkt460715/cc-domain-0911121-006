import datetime
import hashlib
import json
import re


class GateError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details

    def as_dict(self):
        return {'code': self.code, 'message': str(self), 'details': self.details}


def fail(code, message, **details):
    raise GateError(code, message, **details)


def identifier(value, field='identifier'):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9._:-]{0,119}', value):
        fail('identifier', 'Invalid identifier', field=field)
    return value


def integer(value, field, minimum=0, maximum=1000000):
    if type(value) is not int or not minimum <= value <= maximum:
        fail('integer', 'Expected bounded integer', field=field)
    return value


def text(value, field, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        fail('text', 'Expected nonempty bounded text', field=field)
    return value.strip()


def shape(value, required, optional=()):
    if not isinstance(value, dict):
        fail('object', 'Expected JSON object')
    missing = set(required) - set(value)
    extra = set(value) - set(required) - set(optional)
    if missing or extra:
        fail('fields', 'Unexpected object fields', missing=sorted(missing), extra=sorted(extra))
    return value


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise GateError('json_value', 'Value cannot be represented as canonical JSON') from exc


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail('duplicate_key', 'JSON key repeats', key=key)
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda value: fail('nonfinite', 'Nonfinite JSON number'))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise GateError('json_decode', 'Malformed JSON input') from exc


def instant(value):
    if not isinstance(value, str):
        fail('timestamp', 'Timestamp must be a string')
    try:
        parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise GateError('timestamp', 'Malformed timestamp') from exc
    if parsed.tzinfo is None:
        fail('timestamp', 'Timestamp requires timezone')
    return parsed.astimezone(datetime.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def symbols(values, field, allow_empty=False):
    if not isinstance(values, list) or len(values) > 1000 or (not values and not allow_empty):
        fail('symbols', 'Expected bounded symbol list', field=field)
    result = [identifier(value, field) for value in values]
    if len(result) != len(set(result)):
        fail('symbols_duplicate', 'Symbol repeats', field=field)
    return sorted(result)
