# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("xmind")


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import SliceError

import io
import json
import re
import traceback
import xml.etree.ElementTree as ET
import zipfile
from typing import List


from smart_slice.handlers.base import BaseSplitHandle
from smart_slice.chunker import SplitModel

default_pattern_list = [
    re.compile('(?<=^)# .*|(?<=\\n)# .*'),
    re.compile('(?<=\\n)(?<!#)## (?!#).*|(?<=^)(?<!#)## (?!#).*'),
    re.compile("(?<=\\n)(?<!#)### (?!#).*|(?<=^)(?<!#)### (?!#).*"),
    re.compile("(?<=\\n)(?<!#)#### (?!#).*|(?<=^)(?<!#)#### (?!#).*"),
    re.compile("(?<=\\n)(?<!#)##### (?!#).*|(?<=^)(?<!#)##### (?!#).*"),
    re.compile("(?<=\\n)(?<!#)###### (?!#).*|(?<=^)(?<!#)###### (?!#).*")
]


class XmindSplitHandle(BaseSplitHandle):
    def support(self, file, get_buffer):
        file_name: str = file.name.lower()
        return file_name.endswith('.xmind')

    @staticmethod
    def _json_node_to_markdown(node, depth):
        """Convert a JSON topic node (new Xmind format) to Markdown text."""
        lines = []
        title = node.get('title')
        if title:
            if depth == 0:
                lines.append(f'# {title}')
            elif depth <= 5:
                lines.append(f'{"#" * (depth + 1)} {title}')
            else:
                indent = '  ' * (depth - 5)
                lines.append(f'{indent}- {title}')
        # Notes
        notes = node.get('notes')
        if notes and isinstance(notes, dict):
            plain = notes.get('plain')
            if plain and isinstance(plain, dict):
                content = plain.get('content')
                if content:
                    lines.append(content)
        # Children (attached and detached)
        children = node.get('children')
        if children and isinstance(children, dict):
            for key in ('attached', 'detached'):
                child_list = children.get(key)
                if child_list and isinstance(child_list, list):
                    for child in child_list:
                        lines.append(XmindSplitHandle._json_node_to_markdown(child, depth + 1))
        return '\n'.join(lines)

    @staticmethod
    def _xml_node_to_markdown(node, ns, depth):
        """Convert an XML topic element (old Xmind format) to Markdown text."""
        lines = []
        title_el = node.find(f'{ns}title')
        title = title_el.text if title_el is not None and title_el.text else None
        if title:
            if depth == 0:
                lines.append(f'# {title}')
            elif depth <= 5:
                lines.append(f'#{"#" * depth} {title}')
            else:
                indent = '  ' * (depth - 5)
                lines.append(f'{indent}- {title}')
        # Notes
        notes = node.find(f'{ns}notes')
        if notes is not None:
            plain = notes.find(f'{ns}plain')
            if plain is not None:
                text = plain.find(f'{ns}text')
                if text is not None and text.text:
                    lines.append(text.text)
        # Children
        children = node.find(f'{ns}children')
        if children is not None:
            topics = children.find(f'{ns}topics')
            if topics is not None:
                for topic in topics.findall(f'{ns}topic'):
                    lines.append(XmindSplitHandle._xml_node_to_markdown(topic, ns, depth + 1))
        return '\n'.join(lines)

    @staticmethod
    def _parse_json_format(zip_file):
        """Parse new Xmind format (content.json, Xmind Zen/ZEN+)."""
        data = json.loads(zip_file.read('content.json'))
        sheet_texts = []
        for sheet in data:
            root_topic = sheet.get('rootTopic')
            if root_topic:
                sheet_text = XmindSplitHandle._json_node_to_markdown(root_topic, 0)
                if sheet_text:
                    sheet_texts.append(sheet_text)
        return '\n\n'.join(sheet_texts)

    @staticmethod
    def _parse_xml_format(zip_file):
        """Parse old Xmind format (content.xml, Xmind 8)."""
        tree = ET.parse(zip_file.open('content.xml'))
        root = tree.getroot()
        # Detect namespace from root tag, e.g. {urn:xmind:xmap:content}
        ns = root.tag[root.tag.index('{'):root.tag.index('}') + 1] if '}' in root.tag else ''
        sheet_texts = []
        for sheet in root.findall(f'{ns}sheet'):
            topic = sheet.find(f'{ns}topic')
            if topic is not None:
                sheet_text = XmindSplitHandle._xml_node_to_markdown(topic, ns, 0)
                if sheet_text:
                    sheet_texts.append(sheet_text)
        return '\n\n'.join(sheet_texts)

    @staticmethod
    def _xmind_to_markdown(buffer):
        """Convert Xmind file bytes to Markdown text."""
        with zipfile.ZipFile(io.BytesIO(buffer)) as zf:
            names = zf.namelist()
            if 'content.json' in names:
                return XmindSplitHandle._parse_json_format(zf)
            elif 'content.xml' in names:
                return XmindSplitHandle._parse_xml_format(zf)
            else:
                return ''

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image):
        buffer = get_buffer(file)
        if type(limit) is str:
            limit = int(limit)
        if type(with_filter) is str:
            with_filter = with_filter.lower() == 'true'
        if pattern_list is not None and len(pattern_list) > 0:
            split_model = SplitModel(pattern_list, with_filter, limit)
        else:
            split_model = SplitModel(default_pattern_list, with_filter=with_filter, limit=limit)
        try:
            markdown_text = self._xmind_to_markdown(buffer)
        except BaseException as e:
            _log.error(f"Error processing Xmind file {file.name}: {e}, {traceback.format_exc()}")
            # 解析失败显式报错，避免静默返回空段落
            raise SliceError(
                500,
                _("Failed to parse Xmind file {name}: {reason}").format(name=file.name, reason=e),
            )
        return {'name': file.name, 'content': split_model.parse(markdown_text)}

    def get_content(self, file, save_image):
        buffer = file.read()
        try:
            return self._xmind_to_markdown(buffer)
        except BaseException as e:
            _log.error(f'Exception: {e}', exc_info=True)
            return f'{e}'
