"""MIME parsing for mailbox deliveries.

The parser only depends on the standard library and turns a raw EML message
into an ordered, traceable list of MIME parts.  Every leaf keeps its MIME
path so downstream documents can always point back to the exact section of
the exact message they came from.
"""
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from corpus_gate.errors import digest, fail, GateError

ALLOWED_ATTACHMENT_TYPES = ('text/plain', 'text/html', 'text/csv')
TEXT_MAIN_TYPES = ('text',)
SUMMARY_LIMIT = 160


class _HTMLToText(HTMLParser):
    block_tags = {'p', 'div', 'br', 'article', 'section', 'tr', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'footer', 'table'}
    skip_tags = {'script', 'style', 'head'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.skip_tags:
            self._skipping += 1
        elif tag in self.block_tags:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self.skip_tags and self._skipping:
            self._skipping -= 1
        elif tag in self.block_tags:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self._skipping:
            self.parts.append(data)

    def text(self):
        lines = [' '.join(line.split()) for line in ''.join(self.parts).splitlines()]
        return '\n'.join(line for line in lines if line).strip()


def html_to_text(markup):
    parser = _HTMLToText()
    parser.feed(markup)
    return parser.text()


def _decode_bytes(part):
    """Resolve Content-Transfer-Encoding and the declared charset ourselves.

    The stdlib silently replaces undecodable bytes when a part omits its
    charset, which would corrupt evidence.  We therefore decode strictly and
    only fall back to UTF-8 / Latin-1 when no charset was declared.
    """
    try:
        raw = part.get_payload(decode=True)
    except (ValueError, TypeError) as exc:
        fail('mail_transfer_encoding', 'MIME transfer encoding cannot be decoded', detail=str(exc))
    if raw is None:
        fail('mail_transfer_encoding', 'MIME part has no decodable payload')
    declared = part.get_content_charset()
    if declared:
        try:
            return raw.decode(declared, errors='strict'), declared
        except LookupError as exc:
            fail('mail_charset', 'MIME part declares an unknown charset', charset=declared, detail=str(exc))
        except UnicodeDecodeError as exc:
            fail('mail_decode', 'MIME payload does not match declared charset', charset=declared, detail=str(exc))
    try:
        return raw.decode('utf-8', errors='strict'), 'utf-8'
    except UnicodeDecodeError:
        return raw.decode('iso-8859-1', errors='strict'), 'iso-8859-1'


def _first_leaf(node):
    if not node.is_multipart():
        return node
    for child in node.iter_parts():
        found = _first_leaf(child)
        if found is not None:
            return found
    return None


def _part_error(part, mime_path, role, exc):
    return {'mime_path': mime_path, 'role': role, 'media_type': part.get_content_type(),
            'charset': part.get_content_charset(), 'filename': part.get_filename(),
            'state': 'failed', 'error': exc.as_dict()}


def _ignored(part, mime_path, role, reason):
    return {'mime_path': mime_path, 'role': role, 'media_type': part.get_content_type(),
            'charset': part.get_content_charset(), 'filename': part.get_filename(),
            'state': 'ignored', 'ignored_reason': reason}


def _content_part(part, mime_path, role, text, charset):
    media_type = part.get_content_type()
    if media_type == 'text/html':
        text = html_to_text(text)
    if not text.strip():
        return _ignored(part, mime_path, role, 'empty_text')
    return {'mime_path': mime_path, 'role': role, 'media_type': media_type,
            'charset': charset, 'filename': part.get_filename(), 'state': 'staged',
            'text': text, 'byte_size': len(text.encode('utf-8')),
            'content_digest': digest(text), 'summary': text[:SUMMARY_LIMIT]}


def _leaf(part, mime_path, body_taken):
    media_type = part.get_content_type()
    main_type = part.get_content_maintype()
    disposition = part.get_content_disposition()
    is_attachment = disposition == 'attachment' or bool(part.get_filename())
    role = 'attachment' if is_attachment else 'body'
    if main_type == 'image':
        return [_ignored(part, mime_path, 'ignored', 'signature_image')]
    if main_type not in TEXT_MAIN_TYPES:
        return [_ignored(part, mime_path, 'ignored', 'disallowed_type')]
    if role == 'attachment' and media_type not in ALLOWED_ATTACHMENT_TYPES:
        return [_ignored(part, mime_path, 'ignored', 'disallowed_type')]
    if role == 'body' and body_taken[0]:
        # Extra inline text beyond the selected body is treated as an
        # attachment candidate, so it still passes the allow-list gate.
        role = 'attachment'
        if media_type not in ALLOWED_ATTACHMENT_TYPES:
            return [_ignored(part, mime_path, 'ignored', 'disallowed_type')]
    try:
        text, charset = _decode_bytes(part)
    except GateError as exc:
        return [_part_error(part, mime_path, role, exc)]
    result = _content_part(part, mime_path, role, text, charset)
    if result['role'] != 'ignored':
        body_taken[0] = True
    return [result]


def _walk(part, path, body_taken):
    if not part.is_multipart():
        return _leaf(part, path, body_taken)
    results = []
    if part.get_content_subtype() == 'alternative':
        children = list(part.iter_parts())
        chosen = None
        for candidate in children:
            leaf = _first_leaf(candidate)
            if leaf is not None and leaf.get_content_type() == 'text/plain':
                chosen = leaf
                break
        if chosen is None:
            for candidate in children:
                leaf = _first_leaf(candidate)
                if leaf is not None and leaf.get_content_maintype() in TEXT_MAIN_TYPES:
                    chosen = leaf
                    break
        for index, child in enumerate(children, 1):
            child_path = f'{path}.{index}' if path else str(index)
            leaf = _first_leaf(child)
            if leaf is not None and chosen is not None and leaf is chosen:
                results.extend(_walk(child, child_path, body_taken))
            elif leaf is not None:
                results.append(_ignored(leaf, child_path, 'ignored', 'alternative_not_selected'))
            else:
                results.extend(_walk(child, child_path, body_taken))
        return results
    for index, child in enumerate(part.iter_parts(), 1):
        child_path = f'{path}.{index}' if path else str(index)
        results.extend(_walk(child, child_path, body_taken))
    return results


def parse_message(raw):
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        fail('mail_eml', 'Mailbox delivery must contain a nonempty EML byte string')
    try:
        message = BytesParser(policy=policy.default).parsebytes(bytes(raw))
    except (ValueError, TypeError) as exc:
        fail('mail_eml', 'Raw delivery is not a parseable MIME message', detail=str(exc))
    parts = _walk(message, '', [False])
    if not any(part['state'] == 'staged' for part in parts):
        # Keep the body role visible when the only candidate failed decoding.
        for part in parts:
            if part['role'] == 'body' and part['state'] == 'failed':
                break
        else:
            for part in parts:
                if part['state'] == 'failed':
                    part['role'] = 'body'
                    break
    return {'message_id': message['Message-ID'], 'subject': message['Subject'], 'parts': parts}
