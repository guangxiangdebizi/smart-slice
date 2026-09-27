"""统一文档切片服务：单一的数据处理底层。

背景（2026-09-17 统一切片底层重构）：上传「智能切片」（the upload path the upload-path entry point）
与批量导入（the bulk-import module the download-split helper / the markdown-split helper）各自持有一份
切片编排胶水，行为已漂移（导入侧缺 OCR 注入等）。本模块把 handler 清单、bytes→file 适配、
handler 分发（含 ImageSplitHandle 的 image_text_extractor 分支传参）、F6 图片引用递归重写、
段落归一化（原 the row-flattening helper）收编为唯一实现，两条链路只保留各自的调用差异：

- 上传链路：normalize=False 保留 handler 原始返回结构（前端分段预览消费），
  段落级 source_file_id 由 the upload path 在返回后附加；
- 导入链路：normalize=True 展平为 [{title, content}]（原 the row-flattening helper 行为逐条保留）。

依赖边界：本模块不得反向 import the upper-layer serializers（the bulk-import module 已 import
the upload path 的 the upper-layer serializer，公共层若引用 serializers 会形成循环导入）；
对 handler 的消费面与 the handler implementations 各实现保持一致（file.name / file.read() /
file.chunks()）。
"""
# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("service")


from smart_slice._i18n import gettext as _
from smart_slice.exceptions import SliceError

import hashlib
import io
import re
from typing import Any, Dict, List, Tuple


from smart_slice.handlers.archive import SevenZipSplitHandle, TarSplitHandle
from smart_slice.handlers.csv_handler import CsvSplitHandle
from smart_slice.handlers.doc import DocSplitHandle
from smart_slice.handlers.eml import EmlSplitHandle
from smart_slice.handlers.epub import EpubSplitHandle
from smart_slice.handlers.html import HTMLSplitHandle
from smart_slice.handlers.image import ImageSplitHandle
from smart_slice.handlers.image_text_extract import extract_image_text, get_ocr_engine
from smart_slice.handlers.mobi import MobiSplitHandle
from smart_slice.handlers.mhtml import MhtmlSplitHandle
from smart_slice.handlers.msg import MsgSplitHandle
from smart_slice.handlers.odf import OdfSplitHandle
from smart_slice.handlers.pdf import PdfSplitHandle
from smart_slice.handlers.ppt import PptSplitHandle
from smart_slice.handlers.pptx import PptxSplitHandle
from smart_slice.handlers.rtf import RtfSplitHandle
from smart_slice.handlers.text import TextSplitHandle
from smart_slice.handlers.wps import WpsSplitHandle
from smart_slice.handlers.xls import XlsSplitHandle
from smart_slice.handlers.xlsx import XlsxSplitHandle
from smart_slice.handlers.xmind import XmindSplitHandle
from smart_slice.handlers.zip_handler import FileBufferHandle, ZipSplitHandle
from smart_slice.files import ImportSplitFile
from smart_slice.handlers import SPLIT_HANDLERS as REGISTERED_SPLIT_HANDLERS

default_split_handle = TextSplitHandle()

# ---------------------------------------------------------------- Phase 2-B（2026-09-18）：文档内嵌图片默认 OCR
# 单文档内嵌图片 OCR 数量上限（按去重后的不同图片内容计数，超出部分跳过并记一次
# warning）：单文档图片量通常在个位到几十之间，50 足够覆盖正常文档；上限用于封顶
# 极端场景（如内嵌上千图的导出文档）的同步 OCR 耗时（单图亚秒到秒级）。
MAX_INLINE_IMAGE_OCR_COUNT = 50
# 极小图过滤：宽或高低于该像素数的图片视为装饰性图标，无文字价值，直接跳过 OCR。
MIN_INLINE_IMAGE_SIDE_PX = 20
# 注入文字的前缀标记：与正文形成清晰分隔，且为普通文本（非 markdown 链接语法），
# 不破坏段落其余部分的 markdown 渲染。
INLINE_IMAGE_OCR_TEXT_PREFIX = "[图片文字] "

# handler 单例清单：顺序即分发优先级（HTML→Doc→Pdf→Xlsx→Xls→Csv→Zip→Xmind→
# 办公文档族→电子书/邮件/压缩包族→Image→Text 兜底），与原 the upload path split_handles 逐项一致；
# Phase 2-A（2026-09-17）在 Xmind 之后、Image 之前插入办公文档族新 handler。
# the document-extract node 等既有导入方经 the upper-layer split_handles alias
# 别名继续消费同一份单例。
SPLIT_HANDLERS = list(REGISTERED_SPLIT_HANDLERS)


class BytesSplitFile:
    """bytes → split handler file 入参的统一适配对象。

    收编导入侧原 _DownloadedFile / _MarkdownFileHandle：handler 的最小消费面为
    .name / .read()，PdfSplitHandle.handle 经 file.chunks() 分块读取（Django UploadedFile
    形态），缺 chunks 会 AttributeError 且不被逐文件异常分支捕获（缺陷2修复语义），
    故三个属性/方法齐备。"""

    def __init__(self, name: str, content: bytes):
        self.name = name
        self.size = len(content)
        self._content = content

    def read(self) -> bytes:
        return self._content

    def chunks(self, chunk_size=None):
        yield self._content


class LazyImageTextExtractor:
    """延迟构建的 OCR 抽取器包装：首次被 ImageSplitHandle 调用时才执行 builder。

    保持上传链路原有构建时机（仅图片文件触发 extractor 构建，非图片文件零开销、
    引擎不可用时不产生逐文件告警噪音）；builder 返回 None（本地引擎不可用）时
    返回空串——与 ImageSplitHandle 对 extractor 空返回的文件名占位降级语义一致。"""

    def __init__(self, builder):
        self._builder = builder
        self._extractor = False  # False=未构建；None=构建失败（引擎不可用）；callable=可用

    def __call__(self, image_bytes, image_name):
        if self._extractor is False:
            self._extractor = self._builder()
        extractor = self._extractor
        if extractor is None:
            return ''
        return extractor(image_bytes, image_name)


def build_image_text_extractor():
    """组装图片 OCR 抽取闭包：本地 RapidOCR 引擎，不依赖工作区模型配置（两侧共用）。

    返回 None 表示本地 OCR 引擎不可用（调用方降级文件名占位）。OCR 是增强能力，
    任何异常都只记日志返回 None，不阻断文件导入主链路。失败结果不缓存：环境修复
    （补装依赖等）后无需重启进程即可恢复。

    同步调用说明：文件解析在 Web 请求线程 / 导入线程池内同步执行，单图本地 OCR
    推理为亚秒到秒级（引擎单例首次初始化额外约 1 秒），图片导入的耗时增量被接受。

    消费面（Phase 2-B，2026-09-18 起）：本闭包同时驱动两类 OCR——
    - 独立图片文件：经 ImageSplitHandle 的 image_text_extractor 分支传参直接抽取；
    - 文档内嵌图片（含 zip 内层图片）：split_document 公共层对 save_image 回调收集的
      File 实例（meta.content 携带字节）统一执行 OCR 并注入宿主段落，见
      extract_inline_image_texts / inject_image_ocr_text。
    the document-extract node 等不经 split_document、直连 handler 的路径不接入本机制。
    """
    try:
        if get_ocr_engine() is None:
            return None
    except Exception as e:  # noqa: BLE001  OCR 是增强能力，任何异常都降级
        _log.warning(f'build image text extractor failed: {e}')
        return None

    def extractor(image_bytes, image_name):
        return extract_image_text(image_bytes, image_name)

    return extractor


def replace_image_file_ids(result, id_mapping):
    """F6（2026-09-16）：把解析产物段落内容里的 ./oss/file/{新图片id} 重写为
    去重命中后复用的既有 File 行 id（the image-persistence step 返回的映射），
    避免重复图片产生新 File 行后段落引用指向未落库的临时 id。
    递归处理 dict / list / str 嵌套结构。"""
    if not id_mapping:
        return result

    def _walk(value):
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        if isinstance(value, str):
            for old_id, new_id in id_mapping.items():
                value = value.replace(f"./oss/file/{old_id}", f"./oss/file/{new_id}")
        return value

    return _walk(result)


def _is_tiny_image(image_bytes: bytes) -> bool:
    """极小图判定：宽或高低于 MIN_INLINE_IMAGE_SIDE_PX 视为装饰性图标。

    图片解码失败（非法字节 / PIL 不识别的容器如 heif 未注册 opener 时）不判小，
    返回 False 交由 OCR 抽取链路自行降级——过滤判定必须保守，宁可多 OCR 不可漏图。
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as img:
            width, height = img.size
        return width < MIN_INLINE_IMAGE_SIDE_PX or height < MIN_INLINE_IMAGE_SIDE_PX
    except Exception:  # noqa: BLE001  解码失败不拦截，交由 OCR 链路降级
        return False


def extract_inline_image_texts(image_bytes_items: List[Tuple[str, bytes]],
                               image_text_extractor, progress_hook=None) -> Dict[str, str]:
    """对 save_image 回调收集的内嵌图片执行 OCR，产出 {图片引用id: OCR文本} 映射。

    @param image_bytes_items:  [(图片File实例id字符串, 图片字节), ...]；字节快照须在
                               save_image 回调消费 meta.content 之前拷贝（上传链路
                               the image-persistence step 会 pop 掉 meta.content）
    @param image_text_extractor: OCR 抽取闭包（与 ImageSplitHandle 共用）
    @param progress_hook:      可选无参回调，每张图片进入 OCR 前调用一次（2026-09-18
                               导入心跳接线：单图 OCR 亚秒至秒级，逐图回调让导入侧
                               心跳在大文档解析期间持续刷新）；传 None 时行为不变
    @return:                   {str(File.id): OCR文本}；OCR 空串（无文字/门线不过/
                               失败）的图片不进入映射，注入层静默跳过

    防护与去重语义：
    - 按 sha256 对图片内容去重，同一内容只调用一次 extractor（命中缓存的同内容
      不同 id 共享结果，各自注入各自的引用位）；
    - 数量上限 MAX_INLINE_IMAGE_OCR_COUNT 按实际 OCR 调用次数（去重后）计数，
      超限部分跳过并只记一次 warning；
    - 极小图（_is_tiny_image）跳过 OCR，不占用上限额度。
    """
    if image_text_extractor is None or not image_bytes_items:
        return {}
    texts: Dict[str, str] = {}
    ocr_text_by_sha: Dict[str, str] = {}
    ocr_used = 0
    limit_warned = False
    for file_id, image_bytes in image_bytes_items:
        if not file_id or not image_bytes:
            continue
        if progress_hook is not None:
            progress_hook()
        sha = hashlib.sha256(image_bytes).hexdigest()
        if sha in ocr_text_by_sha:
            text = ocr_text_by_sha[sha]
        else:
            if ocr_used >= MAX_INLINE_IMAGE_OCR_COUNT:
                if not limit_warned:
                    _log.warning(
                        f"inline image ocr count reached limit {MAX_INLINE_IMAGE_OCR_COUNT}, "
                        f"remaining inline images in this document are skipped")
                    limit_warned = True
                continue
            if _is_tiny_image(image_bytes):
                text = ""
            else:
                ocr_used += 1
                try:
                    text = (image_text_extractor(image_bytes, f"inline-{file_id}") or "").strip()
                except Exception as e:  # noqa: BLE001  OCR 失败降级为不注入，不阻断主链路
                    _log.warning(f'inline image ocr failed for {file_id}: {e}')
                    text = ""
            ocr_text_by_sha[sha] = text
        if text:
            texts[file_id] = text
    return texts


def inject_image_ocr_text(result: Any, ocr_text_map: Dict[str, str]) -> Any:
    """把内嵌图片 OCR 文本注入宿主段落：在 ./oss/file/{id} 引用之后追加 [图片文字] 块。

    注入规则：
    - 仅注入 dict 中 key 为 "content" 的字符串（段落正文）；title / name 等字段不注入；
    - 优先识别完整 markdown 图片语法 ![alt](./oss/file/{id})，注入在整个引用之后
      （不得插进 [alt](...) 括号内部破坏渲染）；引用以 <img src="..."> 形态出现时
      （zip 内 HTML 页内嵌图）注入在标签闭合之后；两者都未命中时该图静默跳过；
    - 同一 id 在多个段落被引用时逐处注入；OCR 文本为空串的图片不产生注入规则
      （extract_inline_image_texts 已过滤）。

    时序：必须在 replace_image_file_ids 之后调用（F6 重写后内容里是最终 File 行 id，
    ocr_text_map 的键也须为最终 id），在 normalize_split_rows 之前调用（展平不修改
    content 字符串内容，注入随之进入展平结果与后续向量化管道）。
    """
    if not ocr_text_map:
        return result
    rules = []
    for file_id, ocr_text in ocr_text_map.items():
        escaped_id = re.escape(str(file_id))
        # 尾部换行：引用后紧跟正文时把 OCR 块与后续正文隔开；引用在段尾时多余
        # 换行被 normalize_split_rows 的 strip 收敛，上传预览链路无副作用
        suffix = f"\n\n{INLINE_IMAGE_OCR_TEXT_PREFIX}{ocr_text}\n"
        md_pattern = re.compile(r"(!\[[^\]]*\]\([^)]*\./oss/file/" + escaped_id + r"[^)]*\))")
        tag_pattern = re.compile(r"(<img[^>]*\./oss/file/" + escaped_id + r"[^>]*>)")
        rules.append((md_pattern, tag_pattern, suffix))

    def _inject(text: str) -> str:
        for md_pattern, tag_pattern, suffix in rules:
            if md_pattern.search(text):
                text = md_pattern.sub(lambda m: m.group(1) + suffix, text)
            elif tag_pattern.search(text):
                text = tag_pattern.sub(lambda m: m.group(1) + suffix, text)
        return text

    def _walk(value, key=None):
        if isinstance(value, dict):
            return {k: _walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v, key) for v in value]
        if isinstance(value, str) and key == "content":
            return _inject(value)
        return value

    return _walk(result)


def normalize_split_rows(result: Any, fallback_title: str) -> List[Dict[str, str]]:
    """把切分 handler 的返回结构统一展平为 [{title, content}]（原 the bulk-import logic
    the row-flattening helper 行为逐条保留，缺陷4修复语义）。

    handler 返回形态分两类：
    - 单文件结构 {"name": 文件名, "content": [{title, content}, ...]}：
      pdf/docx/html/csv/md/txt/xmind/图片；
    - 列表结构（每项 {"name"/"title": 组名, "content": [段落 dict, ...]}）：
      xlsx/xls 为 per-sheet、zip 为 per-内层文件。
    此前对列表项直接 str(row["content"]) 把段落 dict 列表拍成 Python repr 串，
    导入计数正常但向量化内容无效（静默数据损坏）。展平规则：段落自身 title
    非空用之；为空回退组名（sheet 名/内层文件名）；再回退 fallback_title。"""
    groups = result if isinstance(result, list) else [result]
    paragraphs: List[Dict[str, str]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_title = str(group.get("title") or group.get("name") or "").strip()
        content = group.get("content")
        items = content if isinstance(content, list) else [content]
        for item in items:
            if isinstance(item, dict):
                title = str(item.get("title") or "").strip()
                text = item.get("content")
            else:
                title = ""
                text = item
            if isinstance(text, dict):
                # 防御：嵌套 dict 取其 content，避免 repr 串
                text = str(text.get("content") or "")
            text = text if isinstance(text, str) else str(text or "")
            text = text.replace("\0", "").strip()
            if not text:
                continue
            paragraphs.append({"title": (title or group_title or fallback_title)[:256], "content": text})
    return paragraphs


def split_document(name, content, *, limit, pattern_list=None, with_filter=True,
                   save_image=None, image_text_extractor=None, normalize=False,
                   fallback_title=None, progress_hook=None):
    """统一切片入口：按 handler 清单分发解析，返回段落结构。

    @param name:                  文件名（决定 handler 匹配与产物 name/标题回退；\0 清洗）
    @param content:               文件完整字节
    @param limit:                 段落最大字符数（必传，防隐式漂移；上传链路 4096、导入链路 1000）
    @param pattern_list:          自定义分段正则列表；None 即智能切片缺省语义（六级标题 + 空行）
    @param with_filter:           是否过滤特殊字符（透传 handler）
    @param save_image:            图片落库回调（接收 File 实例列表）。回调可返回
                                  {新图片id: 既有行id} 映射（the image-persistence step 的 F6 返回值），
                                  公共层收集后在解析结果上即时应用 ./oss/file/ 引用递归重写；
                                  落库时机由调用方回调自行决定（上传侧=即时落库，
                                  导入侧=image_sink 收集后统一落库、映射延后由调用方重写）。
                                  传 None 时公共层代以 no-op（ZipSplitHandle 会无条件调用该
                                  回调，与原导入服务 the download-split helper 的防御行为一致）。
    @param image_text_extractor:  图片 OCR 抽取闭包（image_bytes, image_name → text）。
                                  仅传入 ImageSplitHandle 分支（照抄上传链路按 handler 能力
                                  分支传参的缺陷修复语义）。传入时同时驱动 Phase 2-B
                                  内嵌图片 OCR：save_image 回调收集的 File 实例（含 zip
                                  内层图片，meta.content 携带字节）统一 OCR 并注入宿主
                                  段落 content。可传 LazyImageTextExtractor 延迟构建。
    @param normalize:             True 时展平为 [{title, content}]（导入链路）；False 时保留
                                  handler 原始返回结构（上传预览链路依赖嵌套结构）。
    @param progress_hook:         可选无参回调（2026-09-18 导入心跳接线）：进入每个
                                  命中的格式处理器前、每个 zip 内层条目解析前、每张内嵌
                                  图片 OCR 前各调用一次；handler 不直接持有该回调，由本层
                                  包装 get_buffer 实现（zip 内层条目）并在 OCR 循环内传递。
                                  传 None 时行为与接线前完全一致。
    @param fallback_title:        normalize=True 时空标题段落的回退标题。显式传入时适配
                                  文件名组名不参与回退，空标题段落直接回退到该值
                                  （回退链退化为 段落 title → fallback_title，供调用方
                                  声明原始标题如在线文档文档名、避免适配文件名
                                  泄漏进段落标题）；None（缺省）时回退链为
                                  段落 title → 组名 → name，即原 the download-split helper
                                  的文件名回退语义。
    @return:                      normalize=False → handler 原始返回（dict 或 list）；
                                  normalize=True → List[Dict[str, str]]
    @raise SliceError:       无 handler 支持该文件格式时抛 400
    """
    name = str(name or "").replace("\0", "")
    if limit is None:
        raise SliceError(500, _("split limit is required"))

    file = content if isinstance(content, ImportSplitFile) else BytesSplitFile(name, content)
    buf_handle = FileBufferHandle()
    if not isinstance(content, ImportSplitFile):
        buf_handle.buffer = content
    if progress_hook is not None:
        # zip/7z/tar 内层条目经 get_buffer 逐个解析：每次取流前回调一次，
        # 使"压缩包内含大文档"的解析过程也能持续上报进展（2026-09-18 心跳接线）。
        # 注意：file.read() 不经本包装（惰性 bytes 适配），故外层文件读取不受影响。
        base_get_buffer = buf_handle.get_buffer

        def get_buffer(*args, **kwargs):
            progress_hook()
            return base_get_buffer(*args, **kwargs)
    else:
        get_buffer = buf_handle.get_buffer

    # F6：save_image 回调可能返回去重映射（同步回调，handle 返回时映射已完整）
    image_id_mapping: Dict[str, str] = {}
    # Phase 2-B：内嵌图片字节快照（(str(File.id), bytes) 列表）。必须在 save_image
    # 回调消费 meta.content 之前拷贝字节——上传链路 the image-persistence step 会
    # pop("content")，回调返回后 File 实例上不再有图片字节。
    inline_image_bytes: List[Tuple[str, bytes]] = []

    def _collecting_save_image(image_list):
        # Phase 2-B：回调消费前先快照携带字节的 File 实例（image_text_extractor
        # 未传入时不快照，保持零额外开销）；上传落库回调与导入收集回调统一走本包装。
        if image_text_extractor is not None and image_list:
            for image in image_list:
                meta = getattr(image, "meta", None)
                image_id = getattr(image, "id", None)
                if not isinstance(meta, dict) or image_id is None:
                    continue
                image_content = meta.get("content")
                if isinstance(image_content, (bytes, bytearray)) and image_content:
                    inline_image_bytes.append((str(image_id), bytes(image_content)))
        returned = save_image(image_list) if save_image is not None else None
        if isinstance(returned, dict) and returned:
            image_id_mapping.update(returned)

    effective_save_image = _collecting_save_image

    for split_handle in SPLIT_HANDLERS:
        if not split_handle.support(file, get_buffer):
            continue
        if progress_hook is not None:
            progress_hook()  # 进入该格式处理器（大 PDF/docx 解析）前上报一次进展
        # 图片段落接入本地 OCR：抽取成功写入段落文本，失败降级文件名占位。
        # 仅 ImageSplitHandle 接受 image_text_extractor 参数，其余 handler
        # 的 handle() 签名不含该关键字，无条件透传会 TypeError 阻断全部
        # 非图片类型的上传（缺陷修复：按 handler 能力分支传参）。
        if isinstance(split_handle, ImageSplitHandle):
            result = split_handle.handle(
                file, pattern_list, with_filter, limit, get_buffer, effective_save_image,
                image_text_extractor=image_text_extractor,
            )
        elif isinstance(split_handle, PdfSplitHandle):
            # PDF 逐页解析是单文件最慢原子操作：按 handler 能力分支透传 progress_hook
            # （其余 handler 签名不含该关键字，无条件透传会 TypeError）。
            result = split_handle.handle(
                file, pattern_list, with_filter, limit, get_buffer, effective_save_image,
                progress_hook=progress_hook,
            )
        else:
            result = split_handle.handle(
                file, pattern_list, with_filter, limit, get_buffer, effective_save_image
            )
        # Phase 2-B：内嵌图片 OCR（含 zip 内层图片）。在 F6 重写之前执行 OCR，
        # 注入在重写之后进行（键随 F6 映射换算为最终 File 行 id）。
        inline_ocr_texts: Dict[str, str] = {}
        if image_text_extractor is not None and inline_image_bytes:
            inline_ocr_texts = extract_inline_image_texts(
                inline_image_bytes, image_text_extractor, progress_hook=progress_hook
            )
        # F6：解析期间 save_image 回调可能产生去重映射，落库后在结果上重写引用
        result = replace_image_file_ids(result, image_id_mapping)
        if inline_ocr_texts:
            final_ocr_texts = {
                (str(image_id_mapping[file_id]) if file_id in image_id_mapping else file_id): text
                for file_id, text in inline_ocr_texts.items()
            }
            result = inject_image_ocr_text(result, final_ocr_texts)
        if normalize:
            if fallback_title:
                # 调用方显式声明回退标题时，组名（bytes 适配文件名，内部实现细节）
                # 不参与标题回退，避免适配文件名（如 "{文档名}.md"）泄漏进段落标题；
                # 回退链退化为 段落 title → fallback_title（与迁移前
                # the markdown-split helper 的 `title or fallback_title` 一致）。
                # 未传 fallback_title 时维持 段落 title → 组名 → 文件名 的原语义。
                if isinstance(result, dict):
                    result = {k: v for k, v in result.items() if k != "name"}
                elif isinstance(result, list):
                    result = [
                        {k: v for k, v in group.items() if k != "name"} if isinstance(group, dict) else group
                        for group in result
                    ]
            return normalize_split_rows(result, fallback_title if fallback_title else name)
        return result
    # 兜底拦截（Phase 2-A，2026-09-17）：所有 handler support 均 False 时两条链路
    # 统一抛 400（带扩展名），不再走"按文本硬解码"兜底——消灭媒体/未知二进制被
    # charset 探测误判后乱码入库的问题。注意：TextSplitHandle 的编码探测兜底对
    # .log/.json/.xml 等纯文本仍在其 support() 内正常放行，不受本拦截影响
    # （拦截仅作用于"无任何 handler 命中"的文件）。
    # - 上传链路（normalize=False）：SliceError 400 经 handle_exception 序列化为
    #   {code, message} JSON 返回，前端按接口错误提示呈现；
    # - 导入链路（normalize=True）：与既有 400 Unsupported file format 语义一致，
    #   由导入编排按不支持文件跳过（消息新增扩展名便于排查）。
    extension = ""
    if "." in name:
        extension = "." + name.rsplit(".", 1)[1].lower() if name.rsplit(".", 1)[1] else ""
    raise SliceError(400, _("Unsupported file format") + (f": {extension}" if extension else ""))
