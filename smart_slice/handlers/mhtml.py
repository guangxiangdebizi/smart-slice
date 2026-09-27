# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("mhtml")


from smart_slice.exceptions import SliceError, ResourceLimitError

from email import policy
from email.errors import MessageError
from email.parser import BytesParser
from markdownify import markdownify

from smart_slice.handlers.base import BaseSplitHandle
from smart_slice._validation import ParserLimits, decode_text, validate_input
from smart_slice.handlers.html import HTMLSplitHandle, default_pattern_list
from smart_slice.handlers._utils import build_split_model


HTML_CONTENT_TYPES = ('text/html', 'application/xhtml+xml')


def _html_root(message):
    content_type = message.get_content_type()
    if content_type in HTML_CONTENT_TYPES and not message.is_multipart():
        return message
    if content_type == 'multipart/related' and message.is_multipart():
        parts = list(message.iter_parts())
        start = message.get_param('start')
        if start:
            matches = [part for part in parts
                       if str(part.get('Content-ID', '')).strip().strip('<>') == start.strip().strip('<>')]
            if len(matches) != 1:
                raise SliceError(400, 'MHTML root Content-ID is missing or ambiguous')
            return _html_root(matches[0])
        return _html_root(parts[0]) if parts else None
    if content_type == 'multipart/alternative' and message.is_multipart():
        for part in reversed(list(message.iter_parts())):
            root = _html_root(part)
            if root is not None:
                return root
    return None


def _mhtml_content(buffer):
    limits = ParserLimits.load()
    validate_input(buffer, limits)
    try:
        message = BytesParser(policy=policy.strict).parsebytes(buffer)
        if message.get_content_type() != 'multipart/related' or not message.is_multipart():
            raise SliceError(400, 'MHTML requires a multipart/related document')
        pending = [(message, 1)]
        count = 0
        while pending:
            part, depth = pending.pop()
            count += 1
            if count > limits.max_mime_parts or depth > limits.max_mime_depth:
                raise ResourceLimitError('MHTML exceeds the MIME part or nesting limit')
            if part.defects:
                raise SliceError(400, 'Invalid or damaged MHTML MIME structure')
            if part.is_multipart():
                pending.extend((child, depth + 1) for child in part.iter_parts())
        root = _html_root(message)
        if root is None:
            raise SliceError(400, 'MHTML has no HTML root document')
        transfer_encoding = str(root.get('Content-Transfer-Encoding', '7bit')).strip().lower()
        if transfer_encoding not in ('7bit', '8bit', 'binary', 'base64', 'quoted-printable'):
            raise SliceError(400, 'Unsupported MHTML content transfer encoding')
        content = decode_text(root.get_payload(decode=True) or b'', encoding=root.get_content_charset())
    except RecursionError as error:
        raise ResourceLimitError('MHTML exceeds the MIME nesting limit') from error
    except (MessageError, ValueError, LookupError) as error:
        raise SliceError(400, 'Invalid or damaged MHTML MIME content') from error
    return markdownify(HTMLSplitHandle()._remove_anchor_links(content), heading_style='ATX')


class MhtmlSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        return file.name.lower().endswith(('.mht', '.mhtml'))

    def handle(self, file, pattern_list, with_filter, limit, get_buffer, save_image):
        content = _mhtml_content(get_buffer(file))
        split_model = build_split_model(pattern_list, with_filter, limit, default_pattern_list)
        return {'name': file.name, 'content': split_model.parse(content)}

    def get_content(self, file, save_image):
        return _mhtml_content(file.read())
