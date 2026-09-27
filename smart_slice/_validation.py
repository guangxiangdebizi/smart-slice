# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import SliceError, ResourceLimitError
from smart_slice._config import _load_limits_from_env, integer_setting

import codecs
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, fields

from charset_normalizer import detect



@dataclass(frozen=True)
class ParserLimits:
    max_input_bytes: int = 128 * 1024 * 1024
    max_xml_bytes: int = 32 * 1024 * 1024
    max_package_members: int = 10000
    max_package_bytes: int = 128 * 1024 * 1024
    max_xml_elements: int = 100000
    max_xml_depth: int = 128
    max_table_cells: int = 1000000
    max_mime_parts: int = 1000
    max_mime_depth: int = 32
    max_odf_text_bytes: int = 32 * 1024 * 1024

    @classmethod
    def load(cls, config=None):
        return _load_limits_from_env(cls, config=config)


BINARY_SIGNATURES = (
    b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08', b'\xd0\xcf\x11\xe0',
    b'%PDF-', b'\x89PNG', b'\xff\xd8\xff', b'GIF87a', b'GIF89a', b'RIFF',
    b'MZ', b'\x7fELF', b'\x1f\x8b', b'BZh', b'\xfd7zXZ', b'7z\xbc\xaf', b'Rar!',
)
CONTROL_CHARACTERS = re.compile(r'[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f]')


def validate_input(buffer, limits=None):
    limits = limits or ParserLimits.load()
    if len(buffer) > limits.max_input_bytes:
        raise ResourceLimitError('File exceeds the parser input byte limit')


def decode_text(buffer, encoding=None, detector=detect):
    validate_input(buffer)
    if not buffer:
        return ''
    if buffer.startswith(BINARY_SIGNATURES):
        raise SliceError(400, 'Binary content cannot be parsed as text')
    bom_encoding = None
    for marker, candidate in (
        (codecs.BOM_UTF32_LE, 'utf-32'), (codecs.BOM_UTF32_BE, 'utf-32'),
        (codecs.BOM_UTF8, 'utf-8-sig'),
        (codecs.BOM_UTF16_LE, 'utf-16'), (codecs.BOM_UTF16_BE, 'utf-16'),
    ):
        if buffer.startswith(marker):
            bom_encoding = candidate
            break
    if not bom_encoding and b'\x00' in buffer:
        raise SliceError(400, 'Binary content or Unicode text without a byte order mark')
    if bom_encoding or encoding:
        encodings = [bom_encoding or encoding]
    else:
        detected = detector(buffer[:65536]) or {}
        encodings = ['utf-8']
        candidate = detected.get('encoding')
        confidence = detected.get('confidence')
        try:
            confidence = 1.0 if confidence is None else float(confidence)
        except (TypeError, ValueError, OverflowError):
            confidence = 0.0
        if isinstance(candidate, str) and 0.5 < confidence <= 1.0:
            if not candidate.lower().replace('_', '-').startswith(('utf-16', 'utf-32')):
                encodings.append(candidate)
    for candidate in encodings:
        try:
            content = buffer.decode(candidate)
        except (UnicodeError, LookupError):
            continue
        if CONTROL_CHARACTERS.search(content):
            raise SliceError(400, 'Binary control characters are not valid text')
        return content
    raise SliceError(400, 'Text encoding is invalid or unsupported')


class _BoundedTreeBuilder(ET.TreeBuilder):
    def __init__(self, limits):
        super().__init__()
        self.limits = limits
        self.depth = 0
        self.elements = 0

    def doctype(self, name, pubid, system):
        raise SliceError(400, 'DTD and entity declarations are not supported')

    def start(self, tag, attrs):
        self.depth += 1
        self.elements += 1
        if self.depth > self.limits.max_xml_depth or self.elements > self.limits.max_xml_elements:
            raise ResourceLimitError('XML exceeds the depth or element limit')
        return super().start(tag, attrs)

    def end(self, tag):
        self.depth -= 1
        return super().end(tag)


def parse_xml(buffer, limits=None):
    limits = limits or ParserLimits.load()
    if len(buffer) > limits.max_xml_bytes:
        raise ResourceLimitError('XML exceeds the parser byte limit')
    try:
        return ET.fromstring(buffer, parser=ET.XMLParser(target=_BoundedTreeBuilder(limits)))
    except ET.ParseError as error:
        raise SliceError(400, 'Invalid or damaged XML document') from error


@contextmanager
def document_package(buffer, limits=None):
    limits = limits or ParserLimits.load()
    validate_input(buffer, limits)
    if buffer.startswith(b'\xd0\xcf\x11\xe0'):
        raise SliceError(400, 'Encrypted or legacy binary Office files are not supported by this parser')
    try:
        with zipfile.ZipFile(io.BytesIO(buffer)) as archive:
            members = archive.infolist()
            if len(members) > limits.max_package_members:
                raise ResourceLimitError('Document package exceeds the member count limit')
            if any(member.flag_bits & 1 for member in members):
                raise SliceError(400, 'Encrypted document packages are not supported')
            if sum(member.file_size for member in members) > limits.max_package_bytes:
                raise ResourceLimitError('Document package exceeds the expanded byte limit')
            names = [member.filename for member in members]
            if len(set(names)) != len(names):
                raise SliceError(400, 'Document package contains duplicate member names')
            yield archive
    except (zipfile.BadZipFile, KeyError, NotImplementedError, RuntimeError) as error:
        if isinstance(error, ResourceLimitError):
            raise
        raise SliceError(400, 'Invalid, encrypted or damaged document package') from error


def read_package_xml(archive, name, limits=None):
    limits = limits or ParserLimits.load()
    if archive.getinfo(name).file_size > limits.max_xml_bytes:
        raise ResourceLimitError('XML member exceeds the parser byte limit')
    with archive.open(name) as reader:
        content = reader.read(limits.max_xml_bytes + 1)
    return parse_xml(content, limits)


def validate_ooxml(buffer, main_part, content_types):
    limits = ParserLimits.load()
    with document_package(buffer, limits) as archive:
        root = read_package_xml(archive, '[Content_Types].xml', limits)
        namespace = '{http://schemas.openxmlformats.org/package/2006/content-types}'
        if root.tag != namespace + 'Types':
            raise SliceError(400, 'Invalid Office content types manifest')
        matches = [element.get('ContentType') for element in root.findall(namespace + 'Override')
                   if element.get('PartName') == '/' + main_part]
        if len(matches) != 1 or matches[0] not in content_types:
            raise SliceError(400, 'Office document content type does not match the supported format')
        read_package_xml(archive, main_part, limits)
