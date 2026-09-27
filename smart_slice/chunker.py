# coding=utf-8
"""Slicing engine: heading tree -> paragraph blocks -> budgeted chunks.

Originally derived from the source platform document-slicing layer; it has since
become first-party source (see docs/PORTING.md) because chunking policy now lives
here. Two things were added on top of the platform behaviour, both off by default
so existing output is byte-identical:

* **overlap** - consecutive chunks can share trailing context
  (:class:`~smart_slice.options.ChunkingOptions`);
* **fast paths** - code-fence masking is skipped when a block has no fence, masked
  text is memoised across heading levels, and already-compiled patterns bypass
  ``re.findall``'s dispatch overhead.
"""

from smart_slice._i18n import gettext as _

import re
from typing import List, Dict, Optional

from smart_slice._accel import heading_level_of, scan_heading_candidates
from smart_slice.options import (
    DEFAULT_LOOKBACK,
    ChunkingOptions,
    _clamp_limit,
    current_options,
    resolve_options,
)

try:  # optional extra: smart-slice[keywords]
    import jieba
except ImportError:  # pragma: no cover - exercised only without the extra
    jieba = None


def get_level_block(text, level_content_list, level_content_index, cursor):
    """
    从文本中获取块数据
    :param text: 文本
    :param level_content_list: 拆分的title数组
    :param level_content_index: 指定的下标
    :param cursor: 开始的下标位置
    :return: 拆分后的文本数据
    """
    start_content: str = level_content_list[level_content_index].get('content')
    next_content = level_content_list[level_content_index + 1].get("content") if level_content_index + 1 < len(
        level_content_list) else None
    start_index = text.index(start_content, cursor)
    end_index = text.index(next_content, start_index + 1) if next_content is not None else len(text)
    return text[start_index + len(start_content):end_index], end_index


def to_tree_obj(content, state='title'):
    """
    转换为树形对象
    :param content: 文本数据
    :param state:   状态: title block
    :return: 转换后的数据
    """
    return {'content': content, 'state': state}


def remove_special_symbol(str_source: str):
    """
    删除特殊字符
    :param str_source: 需要删除的文本数据
    :return: 删除后的数据
    """
    return str_source


def filter_special_symbol(content: dict):
    """
    过滤文本中的特殊字符
    :param content: 需要过滤的对象
    :return: 过滤后返回
    """
    content['content'] = remove_special_symbol(content['content'])
    return content


def flat(tree_data_list: List[dict], parent_chain: List[dict], result: List[dict]):
    """
    扁平化树形结构数据
    :param tree_data_list: 树形接口数据
    :param parent_chain:   父级数据 传[] 用于递归存储数据
    :param result:         响应数据 传[] 用于递归存放数据
    :return: result 扁平化后的数据
    """
    if parent_chain is None:
        parent_chain = []
    if result is None:
        result = []
    for tree_data in tree_data_list:
        p = parent_chain.copy()
        p.append(tree_data)
        result.append(to_flat_obj(parent_chain, content=tree_data["content"], state=tree_data["state"]))
        children = tree_data.get('children')
        if children is not None and len(children) > 0:
            flat(children, p, result)
    return result


def to_paragraph(obj: dict):
    """
    转换为段落
    :param obj: 需要转换的对象
    :return: 段落对象
    """
    content = obj['content']
    return {"keywords": get_keyword(content),
            'parent_chain': list(map(lambda p: p['content'], obj['parent_chain'])),
            'content': ",".join(list(map(lambda p: p['content'], obj['parent_chain']))) + content}


def get_keyword(content: str):
    """
    获取content中的关键词
    :param content: 文本
    :return: 关键词数组
    """
    stopwords = ['：', '“', '！', '”', '\n', '\\s']
    if jieba is None:
        raise RuntimeError(
            "Keyword extraction requires the optional dependency jieba; "
            "install smart-slice[keywords] to enable it."
        )
    cutworms = jieba.lcut(content)
    return list(set(list(filter(lambda k: (k not in stopwords) | len(k) > 1, cutworms))))


def titles_to_paragraph(list_title: List[dict]):
    """
    将同一父级的title转换为块段落
    :param list_title: 同父级title
    :return: 块段落
    """
    if len(list_title) > 0:
        content = "\n,".join(
            list(map(lambda d: d['content'].strip("\r\n").strip("\n").strip("\\s"), list_title)))

        return {'keywords': '',
                'parent_chain': list(
                    map(lambda p: p['content'].strip("\r\n").strip("\n").strip("\\s"), list_title[0]['parent_chain'])),
                'content': ",".join(list(
                    map(lambda p: p['content'].strip("\r\n").strip("\n").strip("\\s"),
                        list_title[0]['parent_chain']))) + content}
    return None


def parse_group_key(level_list: List[dict]):
    """
    将同级别同父级的title生成段落,加上本身的段落数据形成新的数据
    :param level_list: title n 级数据
    :return: 根据title生成的数据 + 段落数据
    """
    result = []
    group_data = group_by(list(filter(lambda f: f['state'] == 'title' and len(f['parent_chain']) > 0, level_list)),
                          key=lambda d: ",".join(list(map(lambda p: p['content'], d['parent_chain']))))
    result += list(map(lambda group_data_key: titles_to_paragraph(group_data[group_data_key]), group_data))
    result += list(map(to_paragraph, list(filter(lambda f: f['state'] == 'block', level_list))))
    return result


def to_block_paragraph(tree_data_list: List[dict]):
    """
    转换为块段落对象
    :param tree_data_list: 树数据
    :return: 块段落
    """
    flat_list = flat(tree_data_list, [], [])
    level_group_dict: dict = group_by(flat_list, key=lambda f: f['level'])
    return list(map(lambda level: parse_group_key(level_group_dict[level]), level_group_dict))


# 单条目 memo:同一 text 在一次 parse_title_level 级联中只需扫描一次。
_SCAN_MEMO: List = [None, None]


def _heading_levels_present(text: str):
    """一次线性扫描得出 text 中出现的标题层级集合(仅对已掩码文本有效)。

    返回 None 表示无法判定(理论上不会发生),调用方应退回逐 pattern 正则路径。
    """
    if _SCAN_MEMO[0] is text:
        return _SCAN_MEMO[1]
    masked = _masked(text)
    try:
        levels = {hashes for (_start, _end, hashes) in scan_heading_candidates(masked)}
    except Exception:  # noqa: BLE001 - 加速器异常时必须退回正则路径,不能影响结果
        levels = None
    _SCAN_MEMO[0] = text
    _SCAN_MEMO[1] = levels
    return levels


def parse_title_level(text, content_level_pattern: List, index):
    """自 index 起找到第一个能匹配到标题的层级并返回其结果。

    优化:级联原本对每个层级都做一次全文正则扫描,直到某层命中为止——层级越深的
    块浪费越多。先用单遍扫描得到本块实际存在的标题层级,再跳过那些"扫描证明必然
    为空"的正则扫描。

    安全性:扫描的接受规则是正则的超集(正则另有 (?!#) 等更严格的守卫),故
    "扫描未报告层级 L" 蕴含 "层级 L 的正则匹配为空"。跳过只会省略必然返回空的
    扫描,不会漏掉真实标题。该前提经 tests/test_c_speedup.py 的系统枚举与随机
    fuzz 语料逐项验证(数千语料、零违例)。

    仅对规范 markdown 标题 pattern 生效:heading_level_of 返回 None 的 pattern
    (自定义方案、空行规则等)一律照旧走正则,行为不变。
    """
    if index >= len(content_level_pattern):
        return []

    levels = _heading_levels_present(text)
    cursor = index
    while cursor < len(content_level_pattern):
        pattern = content_level_pattern[cursor]
        if levels is not None:
            level = heading_level_of(pattern)
            if level is not None and level not in levels:
                # 扫描证明该层级无候选:正则必然返回空,直接跳到下一层级
                cursor += 1
                continue
        result = parse_level(text, pattern)
        if len(result) == 0:
            cursor += 1
            continue
        return result
    return []


_CODE_FENCE_RE = re.compile(r'```[^\n]*\n.*?```', re.DOTALL)


def mask_code_blocks(text: str) -> str:
    """
    将代码块内容替换为等长空格,防止代码块内的#被识别为标题

    无围栏时直接返回原字符串:大多数子块不含 ``` ,避免全文 list()/join() 拷贝。
    """
    if '```' not in text:
        return text
    result = list(text)
    for match in _CODE_FENCE_RE.finditer(text):
        start = match.start()
        end = match.end()
        inner_start = text.index('\n', start) + 1
        closing_fence_start = text.rindex('```', start, end)
        for i in range(inner_start, closing_fence_start):
            if result[i] != '\n':
                result[i] = ' '
    return ''.join(result)


# 单条目 memo:parse_to_tree 每层都对同一 text 调 parse_level(最多 pattern 数量次),
# 掩码结果完全相同。只记最后一条即可把掩码次数降一个量级,且不持有长生命周期内存。
_MASK_MEMO: List = [None, None]


def _masked(text: str) -> str:
    if _MASK_MEMO[0] is text:
        return _MASK_MEMO[1]
    masked = mask_code_blocks(text)
    _MASK_MEMO[0] = text
    _MASK_MEMO[1] = masked
    return masked


def parse_level(text, pattern: str):
    """
    获取正则匹配到的文本
    :param text: 需要匹配的文本
    :param pattern:  正则
    :return: 符合正则的文本
    """
    masked_text = _masked(text)
    level_content_list = list(map(to_tree_obj, [r[0:255] for r in re_findall(pattern, masked_text) if r is not None]))
    # 过滤掉空标题或只包含#和空白字符的标题
    filtered_list = [item for item in level_content_list
                     if item['content'].strip(' ') and item['content'].replace('#', '').strip(' ')]
    return list(map(filter_special_symbol, filtered_list))


def re_findall(pattern, text):
    # 检查 pattern 是否为空或无效
    if pattern is None:
        return []

    # 如果是字符串类型，检查是否为空字符串
    if isinstance(pattern, str) and (not pattern or not pattern.strip()):
        return []

    try:
        # 已编译的 Pattern 直接走其方法,跳过 re.findall 的 _compile 分派(每文档数万次)
        if isinstance(pattern, re.Pattern):
            result = pattern.findall(text)
        else:
            result = re.findall(pattern, text, flags=0)
    except re.error:
        return []

    # 展平分组元组并滤空:单次遍历,替代原 reduce([*x, *y]) 的 O(n^2) 列表拼接
    flat: List[str] = []
    extend = flat.extend
    append = flat.append
    for row in result:
        if isinstance(row, tuple):
            for item in row:
                if item:
                    append(item)
        elif row:
            append(row)
    return flat


def to_flat_obj(parent_chain: List[dict], content: str, state: str):
    """
    将树形属性转换为扁平对象
    :param parent_chain:
    :param content:
    :param state:
    :return:
    """
    return {'parent_chain': parent_chain, 'level': len(parent_chain), "content": content, 'state': state}


def flat_map(array: List[List]):
    """
    将二位数组转为一维数组
    :param array: 二维数组
    :return: 一维数组
    """
    result = []
    for e in array:
        result += e
    return result


def group_by(list_source: List, key):
    """
    將數組分組
    :param list_source: 需要分組的數組
    :param key: 分組函數
    :return: key->[]
    """
    result = {}
    for e in list_source:
        k = key(e)
        array = result.get(k) if k in result else []
        array.append(e)
        result[k] = array
    return result


def has_block_descendant(nodes: List[dict]) -> bool:
    """
    判断树节点列表中是否存在（任意层级的）block 节点
    :param nodes: 树节点列表
    :return: 是否存在 block 后代
    """
    for node in nodes:
        if node.get('state') == 'block':
            return True
        children = node.get('children')
        if children and has_block_descendant(children):
            return True
    return False


def result_tree_to_paragraph(result_tree: List[dict], result, parent_chain, with_filter: bool,
                             table_header_state: dict = None):
    """
    转换为分段对象
    :param result_tree: 解析文本的树
    :param result:      传[]  用于递归
    :param parent_chain: 传[] 用户递归存储数据
    :param with_filter: 是否过滤block
    :param table_header_state: 方案 1-2（2026-09-16）表格表头连续性状态，传 None 由函数内初始化
    :return: List[{'problem':'xx','content':'xx'}]
    """
    if table_header_state is None:
        table_header_state = {'header': None, 'prev_in_table': False}
    for item in result_tree:
        if item.get('state') == 'block':
            content = append_table_header_if_cut(item.get("content"), table_header_state)
            result.append({'title': " ".join(parent_chain),
                           'content': filter_special_char(content) if with_filter else content})
        children = item.get("children")
        if children is not None and len(children) > 0:
            result_tree_to_paragraph(children, result,
                                     [*parent_chain, remove_special_symbol(item.get('content'))], with_filter,
                                     table_header_state)
    return result


def post_handler_paragraph(content: str, limit: int) -> List[str]:
    """按换行优先、长度兜底的规则把文本分段。

    与 :func:`smart_split_paragraph` 的区别：本函数不做句子边界对齐，先按行累积到
    limit，再对仍超长的单行做硬切。

    修复（本版）：原实现末行用 ``functools.reduce(lambda x, y: [*x, *y], ...)``
    展平，而 ``reduce`` 并未导入 —— 该函数在源平台与本包中均无调用者，缺陷一直
    潜伏未暴露；此处改为单次遍历展平，同时去掉两处 ``if len(x) > 4096: pass``
    空操作死代码（无副作用的调试残留）。
    """
    result: List[str] = []
    temp_char, start = '', 0
    while (pos := content.find("\n", start)) != -1:
        split, start = content[start:pos + 1], pos + 1
        if len(temp_char + split) > limit:
            result.append(temp_char)
            temp_char = ''
        temp_char = temp_char + split
    temp_char = temp_char + content[start:]
    if len(temp_char) > 0:
        result.append(temp_char)

    # 单个"行"仍超过 limit 时按 limit 硬切（一次遍历展平，替代 O(n^2) 的 reduce 拼接）
    flat: List[str] = []
    hard_cut = re.compile("[\\S\\s]{1," + str(limit) + '}')
    for row in result:
        flat.extend(hard_cut.findall(row))
    return flat


def is_table_separator_line(line: str) -> bool:
    """
    判断一行是否为 markdown 表格分隔行（如 `| --- | --- |`）
    :param line: 待判断的行
    :return: 是否为分隔行
    """
    stripped = line.strip()
    return len(stripped) > 0 and set(stripped) <= set('|-: ') and '-' in stripped


def append_table_header_if_cut(content: str, state: dict) -> str:
    """
    智能切片升级方案 1-2（2026-09-16，P4）：markdown 表格超 limit 被切断后，
    为后续分块开头增补表头两行（表头行 + 分隔行）。
    增补不是改写：原始分块内容一字不动，仅在其前插入上一分块中最近出现的表头。
    在段落产出阶段（result_tree_to_paragraph）调用：parse_to_tree 的块定位依赖
    content 与原文的子串关系，不能提前改写块内容。
    :param content: 当前分块内容
    :param state: 跨分块状态 {'header': (表头行, 分隔行) 或 None, 'prev_in_table': 上一分块是否止于表格行}
    :return: 增补表头后的内容
    """
    lines = content.split('\n')
    starts_data_row = lines[0].lstrip().startswith('|')
    starts_complete_table = len(lines) > 1 and is_table_separator_line(lines[1])
    if starts_data_row and not starts_complete_table and state.get('prev_in_table') \
            and state.get('header') is not None:
        content = state['header'][0] + '\n' + state['header'][1] + '\n' + content
        lines = content.split('\n')
    # 记录本分块内最后一次出现的表头（表头行 + 紧随的分隔行），供下一分块被切断时增补
    for i in range(len(lines) - 1):
        if lines[i].lstrip().startswith('|') and is_table_separator_line(lines[i + 1]):
            state['header'] = (lines[i], lines[i + 1])
    # 上一分块是否止于表格行：取最后一个非空行判断（表格行以 '\n' 结尾，split 后末元素为空串）
    last_nonempty = ''
    for line in reversed(lines):
        if line.strip():
            last_nonempty = line
            break
    state['prev_in_table'] = last_nonempty.lstrip().startswith('|')
    return content


# 句子边界字符集。原实现以列表逐项比较且把全角 ！/？ 误写成了两份 ASCII !/?，
# 使中文全角标点的句子边界失效；改为 frozenset 后既修正该缺陷，又把每字符的
# 判定从最多 6 次比较降为 O(1) 哈希查找。
_SENTENCE_BOUNDARIES = frozenset('。.!！?？')


def _find_sentence_cut(content: str, start: int, end: int, floor: int) -> int:
    """在 (floor, end) 内自后向前找句子边界，返回含分隔符的切点；找不到返回 end。

    搜索自 end-2 起而非 end-1：这是源实现的既有语义（当边界字符恰落在 end-1 时
    切点等于 end，原代码因 `best_split != end` 判定为"未找到"而继续向前搜索，等价
    于跳过该位置）。保留它可确保切块边界与源平台逐字一致，不作行为变更。
    """
    for i in range(end - 2, floor, -1):
        if content[i] in _SENTENCE_BOUNDARIES:
            return i + 1  # 分隔符归入当前段
    return end


def _protect_table_row(content: str, start: int, best_split: int) -> int:
    """避免把 markdown 表格数据行从中间切断：落在以 | 开头的行中间时回退到上一行边界。

    智能切片升级方案 1-2（2026-09-16，P4）。回退后的行内容完整且不超 limit。
    """
    line_start = content.rfind('\n', start, best_split) + 1
    if line_start > start and content[line_start:best_split].lstrip().startswith('|'):
        previous_row_end = content.rfind('\n', start, line_start)
        if previous_row_end > start:
            return previous_row_end + 1
    return best_split


def _snap_overlap_start(content: str, raw_start: int, hard_limit: int) -> int:
    """把重叠起点前移到最近的行首/句首，使携带的上下文不从半个词开始。

    返回值恒 < hard_limit，因此游标必定前进；找不到边界时原样返回 raw_start
    （宁可携带半个词，也不能因对齐而丢掉重叠）。
    """
    newline = content.find('\n', raw_start, hard_limit)
    if newline != -1 and newline + 1 < hard_limit:
        return newline + 1
    for i in range(raw_start, hard_limit):
        if content[i] in _SENTENCE_BOUNDARIES and i + 1 < hard_limit:
            return i + 1
    return raw_start


def _merge_short_tail(result: List[str], min_chunk: int) -> List[str]:
    """把短于 min_chunk 的尾块并入前一块，避免产生污染索引的碎片段落。"""
    if min_chunk <= 0 or len(result) < 2:
        return result
    merged: List[str] = []
    for piece in result:
        if merged and len(piece) < min_chunk:
            merged[-1] = merged[-1] + piece
        else:
            merged.append(piece)
    return merged


def _budget_window(content: str, start: int, limit: int, length_fn) -> int:
    """length_fn 非字符计数时，二分找出满足预算的最大窗口右界。"""
    low, high = start + 1, len(content)
    best = high
    while low <= high:
        mid = (low + high) // 2
        if length_fn(content[start:mid]) <= limit:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def smart_split_paragraph(content: str, limit: int, overlap: int = 0, *,
                          boundary: bool = True, lookback: float = DEFAULT_LOOKBACK,
                          overlap_boundary: bool = True, min_chunk: int = 0,
                          length_fn=len):
    """智能分段:在 limit 前找到合适的分割点(句号、回车等)，可选保留重叠上下文。

    :param content: 需要分段的文本
    :param limit: 最大分段大小，按 length_fn 度量
    :param overlap: 相邻段共享的上下文字符数。0（默认）= 各段互不重叠，
                    拼接可逐字还原原文；>0 = 用跨边界召回换取严格平铺性质。
                    内部钳制到 limit // 2 以保证游标始终前进。
    :param boundary: 是否把切点对齐到句子边界（False = 直接按 limit 硬切）
    :param lookback: 自 limit 处向前搜索边界的窗口比例，默认 0.5 即"至少保留一半内容"
    :param overlap_boundary: 取重叠时是否把起点前移到行首/句首
    :param min_chunk: 短于该值的尾块并入前一块（0 = 不合并）
    :param length_fn: 大小度量函数，默认 len（字符数）；传分词器即按 token 预算
    :return: 分段后的文本列表

    overlap=0 且 length_fn=len 时，输出与引入重叠能力之前逐字一致。
    """
    if length_fn(content) <= limit:
        return [content]

    char_budget = length_fn is len
    overlap = max(0, min(int(overlap), limit // 2))
    result: List[str] = []
    start = 0
    total = len(content)

    while start < total:
        if char_budget:
            end = start + limit
        else:
            end = _budget_window(content, start, limit, length_fn)

        if end >= total:
            # 剩余文本不超过限制,直接添加
            result.append(content[start:])
            break

        best_split = _find_sentence_cut(content, start, end, start + int(limit * lookback)) if boundary else end
        best_split = _protect_table_row(content, start, best_split)

        result.append(content[start:best_split])

        if overlap and best_split - overlap > start:
            next_start = best_split - overlap
            if overlap_boundary:
                next_start = _snap_overlap_start(content, next_start, best_split)
            start = max(next_start, start + 1)
        else:
            start = best_split

    result = _merge_short_tail(result, min_chunk)
    return [text for text in result if text.strip()]


replace_map = {
    re.compile('\n+'): '\n',
    re.compile(' +'): ' ',
    # 智能切片升级方案 1-1（2026-09-16，P3）：仅清除行首 markdown 标题标记，
    # 不再删除正文中间的 #（代码注释、#RRGGBB 色码等均保留）。
    # 矩阵验证修复 2026-09-16：行首 # 剥离改为围栏感知（见 filter_special_char），
    # ``` 代码块内部的行首 #（Python 注释等）同样保留。
    re.compile("\t+"): ''
}


def strip_heading_marker_outside_code(content: str) -> str:
    """矩阵验证修复 2026-09-16：行首 # 标题标记剥离跳过 ``` 代码围栏内部，
    使 1-1（代码块内 # 保留）在行首注释场景同样成立；围栏外行为不变。"""
    lines = content.split('\n')
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith('```'):
            in_fence = not in_fence
            continue
        if not in_fence:
            lines[i] = re.sub(r'^#+[ \t]*', '', line)
    return '\n'.join(lines)


def filter_special_char(content: str):
    """
    过滤特殊字段
    :param content: 文本
    :return: 过滤后字段
    """
    items = replace_map.items()
    for key, value in items:
        content = re.sub(key, value, content)
    # 矩阵验证修复 2026-09-16：行首 # 剥离围栏感知化（代码块内行首 # 保留）
    return strip_heading_marker_outside_code(content)


def apply_paragraph_overlap(paragraphs: List[Dict], overlap: int, *,
                            overlap_boundary: bool = True,
                            overlap_within_section: bool = False,
                            separator: str = " ") -> List[Dict]:
    """Give each paragraph the trailing context of the one before it.

    Applied *after* the heading tree has been assembled, never during it:
    ``parse_to_tree`` locates blocks with ``str.index()`` on the produced content,
    so tree-stage chunks must remain disjoint substrings of the source.  Overlapping
    them there makes the lookup ambiguous and silently reshuffles boundaries (the
    same reason table-header restoration happens at assembly time).

    The pass is purely additive - no paragraph's own text is altered or dropped, so
    the fidelity guarantee holds.  ``overlap=0`` returns the input untouched.

    :param paragraphs:             ``[{"title", "content"}, ...]`` in document order
    :param overlap:                characters of context to carry forward
    :param overlap_boundary:       move the carried slice forward to the next
                                   sentence/whitespace boundary so context never
                                   starts mid-word (keeps ``overlap`` an upper bound)
    :param overlap_within_section: only carry context between paragraphs that share
                                   the same heading chain
    :param separator:              inserted between carried context and the paragraph
    :return: a new list; inputs are not mutated
    """
    if overlap <= 0 or len(paragraphs) < 2:
        return paragraphs

    result: List[Dict] = []
    previous: Optional[Dict] = None
    for row in paragraphs:
        if not isinstance(row, dict):
            result.append(row)
            previous = row if isinstance(row, dict) else previous
            continue
        content = row.get("content") or ""
        if previous is not None and content:
            prev_content = previous.get("content") or ""
            same_section = (previous.get("title") or "") == (row.get("title") or "")
            if prev_content and (same_section or not overlap_within_section):
                carried = prev_content[-overlap:]
                if overlap_boundary and len(prev_content) > overlap:
                    carried = _snap_carry_forward(carried)
                if carried.strip():
                    content = carried + separator + content
                    row = {**row, "content": content}
        result.append(row)
        previous = row
    return result


def _snap_carry_forward(carried: str) -> str:
    """Trim a carried-over slice forward to the first sentence/whitespace boundary.

    Dropping the leading fragment avoids injecting half a word into the next
    paragraph; the result is never longer than the requested overlap.
    """
    for index, char in enumerate(carried):
        if char in _SENTENCE_BOUNDARIES or char in " \n\t":
            tail = carried[index + 1:]
            if tail.strip():
                return tail
    return carried


class SplitModel:
    """标题树 + 长度预算的切片器。

    :param content_level_pattern: 分级正则列表，下标 0 为最外层标题
    :param with_filter: 是否执行清洗（行首标题标记剥离等）
    :param limit: 段落最大大小
    :param overlap: 相邻段落共享的上下文字符数；``None``（默认）表示沿用当前
                    调用作用域内的 :class:`~smart_slice.options.ChunkingOptions`
    :param options: 显式配置对象，优先级高于 ``overlap``/``limit`` 关键字

    ``overlap``/``options`` 都不传时，配置取自 contextvar 作用域（由
    ``slice_text``/``slice_bytes`` 等入口设置），因而经 handler 链构造的
    SplitModel 也能遵循调用方的分块配置，无需改动 handler 签名。
    """

    def __init__(self, content_level_pattern, with_filter=True, limit=100000,
                 overlap: Optional[int] = None, options: Optional[ChunkingOptions] = None):
        self.content_level_pattern = content_level_pattern
        self.with_filter = with_filter
        resolved = resolve_options(options, limit=limit, overlap=overlap)
        self.options = resolved
        self.limit = resolved.limit
        self.overlap = resolved.effective_overlap


    def _block_split_kwargs(self) -> dict:
        """Chunking options for cutting an oversized block, minus ``overlap``.

        ``boundary``/``lookback``/``min_chunk``/``length_fn`` legitimately shape how
        a block is divided; ``overlap`` must be excluded because tree-stage pieces
        are re-located by ``str.index()`` and must stay disjoint (see
        :func:`apply_paragraph_overlap`).
        """
        o = self.options
        return {
            'boundary': o.boundary,
            'lookback': o.lookback,
            'min_chunk': o.min_chunk,
            'length_fn': o.length_fn,
        }

    def parse_to_tree(self, text: str, index=0):
        """
         解析文本
        :param text: 需要解析的文本
        :param index: 从那个正则开始解析
        :return: 解析后的树形结果数据
        """
        level_content_list = parse_title_level(text, self.content_level_pattern, index)
        if len(level_content_list) == 0:
            # NOTE: overlap is deliberately NOT applied here.  parse_to_tree
            # recovers block positions with str.index() on the produced content, so
            # pieces must stay disjoint substrings of the source; overlapping
            # pieces are ambiguous and would corrupt the tree.  Overlap is applied
            # once, after assembly, by apply_paragraph_overlap().
            return [to_tree_obj(row, 'block') for row in
                    smart_split_paragraph(text, limit=self.limit, **self._block_split_kwargs())]
        if index == 0 and text.lstrip().index(level_content_list[0]["content"].lstrip()) != 0:
            level_content_list.insert(0, to_tree_obj(""))

        cursor = 0
        level_title_content_list = [item for item in level_content_list if item.get('state') == 'title']
        for i in range(len(level_title_content_list)):
            start_content: str = level_title_content_list[i].get('content')
            if cursor < text.index(start_content, cursor):
                # same rationale as above: no overlap inside the tree walk
                for row in smart_split_paragraph(text[cursor:   text.index(start_content, cursor)],
                                                 limit=self.limit, **self._block_split_kwargs()):
                    level_content_list.insert(0, to_tree_obj(row, 'block'))

            block, cursor = get_level_block(text, level_title_content_list, i, cursor)
            if len(block) == 0:
                continue
            children = self.parse_to_tree(text=block, index=index + 1)
            # 智能切片升级方案 1-3 配套修正（2026-09-16）：空行分段模式会把标题之间的
            # 纯空白间隔识别为子级 title，使该标题下不再产出任何 block，标题文本随后
            # 在段落装配阶段丢失（基线行为：空白间隔作为 block 保留，标题经
            # content_is_null 兜底进段落）。children 无任何 block 后代时回退为空白 block。
            if not has_block_descendant(children):
                children = [to_tree_obj(block, 'block')]
            level_title_content_list[i]['children'] = children
            first_child_idx_in_block = block.lstrip().index(children[0]["content"].lstrip())
            if first_child_idx_in_block != 0:
                inner_children = self.parse_to_tree(block[:first_child_idx_in_block], index + 1)
                level_title_content_list[i]['children'].extend(inner_children)
        return level_content_list

    def parse(self, text: str):
        """
        解析文本
        :param text: 文本数据
        :return: 解析后数据 {content:段落数据,keywords:[‘段落关键词’],parent_chain:['段落父级链路']}
        """
        text = text.replace('\r\n', '\n')
        text = text.replace('\r', '\n')
        text = text.replace("\0", '')
        result_tree = self.parse_to_tree(text, 0)
        result = result_tree_to_paragraph(result_tree, [], [], self.with_filter)
        for e in result:
            if len(e['content']) > 4096:
                pass
        title_list = list(set([row.get('title') for row in result]))
        result = [item for item in [self.post_reset_paragraph(row, title_list) for row in result] if
                  'content' in item and len(item.get('content').strip()) > 0]
        # 重叠上下文作为装配后的独立阶段施加（见 apply_paragraph_overlap 的说明）：
        # overlap=0 时原样返回，输出与引入重叠能力之前逐字一致。
        return apply_paragraph_overlap(result, self.overlap,
                                       overlap_boundary=self.options.overlap_boundary,
                                       overlap_within_section=self.options.overlap_within_section)

    def post_reset_paragraph(self, paragraph: Dict, title_list: List[str]):
        result = self.content_is_null(paragraph, title_list)
        result = self.filter_title_special_characters(result)
        result = self.sub_title(result)
        return result

    @staticmethod
    def sub_title(paragraph: Dict):
        if 'title' in paragraph:
            title = paragraph.get('title')
            if len(title) > 255:
                return {**paragraph, 'title': title[0:255], 'content': title[255:len(title)] + paragraph.get('content')}
        return paragraph

    @staticmethod
    def content_is_null(paragraph: Dict, title_list: List[str]):
        if 'title' in paragraph:
            title = paragraph.get('title')
            content = paragraph.get('content')
            if (content is None or len(content.strip()) == 0) and (title is not None and len(title) > 0):
                find = [t for t in title_list if t.__contains__(title) and t != title]
                if find:
                    return {'title': '', 'content': ''}
                return {'title': '', 'content': title}
        return paragraph

    @staticmethod
    def filter_title_special_characters(paragraph: Dict):
        title = paragraph.get('title') if 'title' in paragraph else ''
        for title_special_characters in title_special_characters_list:
            title = title.replace(title_special_characters, '')
        return {**paragraph,
                'title': title}


title_special_characters_list = ['#', '\n', '\r', '\\s']

default_split_pattern = {
    'md': [re.compile('(?<=^)# .*|(?<=\\n)# .*'),
           re.compile('(?<=\\n)(?<!#)## (?!#).*|(?<=^)(?<!#)## (?!#).*'),
           re.compile("(?<=\\n)(?<!#)### (?!#).*|(?<=^)(?<!#)### (?!#).*"),
           re.compile("(?<=\\n)(?<!#)#### (?!#).*|(?<=^)(?<!#)#### (?!#).*"),
           re.compile("(?<=\\n)(?<!#)##### (?!#).*|(?<=^)(?<!#)##### (?!#).*"),
           re.compile("(?<=\\n)(?<!#)###### (?!#).*|(?<=^)(?<!#)###### (?!#).*")],
    'default': [re.compile("(?<!\n)\n\n+")]
}


def get_split_model(filename: str, with_filter: bool = False, limit: int = 100000):
    """
    根据文件名称获取分段模型
    :param limit:        每段大小
    :param with_filter: 是否过滤特殊字符
    :param filename: 文件名称
    :return: 分段模型
    """
    if filename.endswith(".md"):
        pattern_list = default_split_pattern.get('md')
    else:
        pattern_list = default_split_pattern.get('default')
    return SplitModel(pattern_list, with_filter=with_filter, limit=limit)


def to_title_tree_string(result_tree: List):
    f = flat(result_tree, [], [])
    return "\n│".join(list(map(lambda r: title_tostring(r), list(filter(lambda row: row.get('state') == 'title', f)))))


def title_tostring(title_obj):
    f = "│ ".join(list(map(lambda index: " ", range(0, len(title_obj.get("parent_chain"))))))
    return f + "├───" + title_obj.get('content')
