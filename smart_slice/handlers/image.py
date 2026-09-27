# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._logging import get_logger

_log = get_logger("image")


from typing import List

from smart_slice.handlers.base import BaseSplitHandle

image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp')
# Phase 2-A（2026-09-17）：.heic/.heif 仅在 pillow-heif 可用时纳入支持面（纯 wheel 依赖，
# Docker 镜像随 pyproject.toml 安装）；依赖缺失时这些扩展名不被 ImageSplitHandle 命中，
# 由 TextSplitHandle 排除表兜底 → 公共层统一抛 400 不支持
heif_extensions = ('.heic', '.heif')

try:
    from pillow_heif import register_heif_opener
    from PIL import Image
    register_heif_opener()
    HEIF_SUPPORTED = True
except Exception:  # noqa: BLE001  pillow-heif 不可用时 heic/heif 降级为不支持
    HEIF_SUPPORTED = False


def _decode_to_png_bytes(image_bytes: bytes) -> bytes:
    """heic/heif 字节经 Pillow（已注册 heif opener）转 PNG，供 OCR 引擎解码。"""
    import io
    with Image.open(io.BytesIO(image_bytes)) as img:
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()


class ImageSplitHandle(BaseSplitHandle):
    """图片文件拆分处理器：为图片文件产出一个引用源图片的段落，让图片进入向量化管道。

    背景：此前图片文件被拆分管道所有 handler 拒收（TextSplitHandle 的 end 列表明确
    拒绝图片扩展名），产出 0 段落——文档显示"已向量化"但 pgvector 无记录，图片内容
    无法通过任何路径进入知识库。

    段落与图片的关联方式：上传的源图片本身已由 file_to_paragraph 作为 File 保存，
    并写入 the document source-file id；向量化管道（the vectorisation query +
    the vector batch-write step）据此识别图片段落——embedding 模型支持图片输入时
    （如 qwen3-vl-embedding，走 DashScope MultiModalEmbedding），直接对图片本体
    向量化；纯文本模型则退化为对文件名文本向量化。

    段落文本：传入 image_text_extractor 时先用 Vision 模型抽取图片 OCR 文本写入
    title/content（文本检索由此可命中图片段落）；OCR 失败 / 未配置模型 / 门线不过
    时降级为文件名占位——既保证纯文本模型场景可按文件名检索，也保证向量化管道的
    分块逻辑不会丢弃该段落（空文本会被 chunk_data 丢弃）。

    注意：zip 内嵌图片与文档抽取节点路径不传入 extractor（本次不接入），保持文件名占位。
    """

    def support(self, file, get_buffer):
        file_name = file.name.lower()
        if file_name.endswith(image_extensions):
            return True
        return HEIF_SUPPORTED and file_name.endswith(heif_extensions)

    def handle(self, file, pattern_list: List, with_filter: bool, limit: int, get_buffer, save_image,
               image_text_extractor=None):
        text = self._extract_text(file, get_buffer, image_text_extractor)
        return {'name': file.name, 'content': [{'title': text[:256], 'content': text}]}

    def get_content(self, file, save_image, image_text_extractor=None, get_buffer=None):
        return self._extract_text(file, get_buffer, image_text_extractor)

    @staticmethod
    def _extract_text(file, get_buffer, image_text_extractor):
        """优先用 extractor 抽 OCR 文本；任何失败或结果为空都降级文件名占位（保证段落非空）。

        heic/heif 先转 PNG 再抽取（本地 OCR 引擎不识别 heif 容器）；转换失败降级
        文件名占位，与 OCR 失败同语义。
        """
        if image_text_extractor is None or get_buffer is None:
            return file.name
        try:
            content_bytes = get_buffer(file)
            if file.name.lower().endswith(heif_extensions):
                content_bytes = _decode_to_png_bytes(content_bytes)
            content = (image_text_extractor(content_bytes, file.name) or '').strip()
        except Exception as e:  # noqa: BLE001  OCR 失败降级文件名占位
            _log.warning(f'image text extract failed for {file.name}: {e}')
            content = ''
        return content if content else file.name
