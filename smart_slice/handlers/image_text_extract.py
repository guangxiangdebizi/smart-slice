"""图片文本抽取（本地 OCR）：图片段落入库时生成文本召回用的段落内容。

抽取用本地 RapidOCR 引擎（rapidocr-onnxruntime，onnxruntime CPU 推理，
检测/识别模型内置离线可用），不依赖平台模型配置、不需要网络。
结果经质量门线过滤：有效字符数 ≥30 且 中文/英文/数字字符占比 ≥50%
（无文字纯图、乱码、检测噪声都会被门线拦下）。
门线不过 / 引擎不可用 / 推理失败 → 返回空串，由调用方降级回文件名占位
（段落文本必须非空，否则会被向量化管道 chunk_data 丢弃）。

引擎实例为进程级懒加载单例（首次调用才加载内置模型，秒级一次性开销），
初始化用 threading.Lock 双重检查保护：导入线程池与 background worker
都会调用本模块，避免并发重复初始化。

冻结开关（见 IMAGE_OCR_ENABLED）：图片 OCR 已按项目决策暂停生效，代码与依赖
全部保留。开关关闭时 get_ocr_engine() 直接返回 None，本模块对外表现与"引擎不可用"
完全一致，调用方各自的降级路径照常生效，上传/导入/切片/向量化全链路不受影响。
"""
# coding=utf-8
# Derived from the source platform document-slicing layer (see docs/PORTING.md).
# Imports and framework-facing symbols were re-routed; the parsing and
# splitting logic is unchanged.
from smart_slice._config import _env_flag
from smart_slice._logging import get_logger

_log = get_logger("ocr")


import re
import threading


# ---------------------------------------------------------------------------
# 图片 OCR 功能冻结开关（项目决策，2026-09-18）
# ---------------------------------------------------------------------------
# 冻结原因：图片 OCR（本地 RapidOCR）能力暂停实际生效——用户上传图片、导入文档时
#           不再触发 OCR 推理，避免线上 CPU 推理开销与识别质量风险；代码与
#           rapidocr-onnxruntime 依赖全部保留，随时可恢复。
# 开关语义：False = 冻结（默认）；True = 恢复 OCR 生效。
# 单点关闭：门控只加在 get_ocr_engine() 入口，两条消费链路（文档内嵌图 OCR 注入
#           the slicing service layer、图片直传 OCR the image handler）与刷数命令
#           the image-paragraph refresh command 共用该函数，因而全部随之失效，不需要在
#           各调用点分别判断；关闭时不 import rapidocr、不加载模型、不占内存。
# 降级保证：引擎返回 None → extract_image_text() 返回空串 →
#           内嵌图链路跳过注入、直传图链路回退文件名占位、刷数命令跳过该知识库，
#           均不抛异常，切片与向量化照常完成。
# 恢复方式：把常量改为 True → 提交 → 走常规发版流程（不使用环境变量，避免配置申请）。
IMAGE_OCR_ENABLED = _env_flag("SMART_SLICE_OCR_ENABLED", False)

# 有效字符：去除空白、标点、下划线等符号后的字符（\W 为非单词字符，含标点与空白）
_EFFECTIVE_CHAR_PATTERN = re.compile(r'[^\W_]')
# 门线放行的字符：中文 [一-鿿] / 英文字母 / 数字
_TEXT_CHAR_PATTERN = re.compile(r'[一-鿿A-Za-z0-9]')

# 质量门线阈值：有效字符数下限 / 文字类字符占有效字符的比例下限
_MIN_EFFECTIVE_CHARS = 30
_MIN_TEXT_RATIO = 0.5

# RapidOCR 引擎单例与初始化锁（见模块 docstring 的并发说明）
_ocr_engine = None
_ocr_engine_lock = threading.Lock()


def get_ocr_engine():
    """返回进程级 RapidOCR 引擎单例（懒加载，线程安全）；依赖缺失或初始化失败返回 None。

    冻结开关 IMAGE_OCR_ENABLED 为 False 时直接返回 None：不 import rapidocr、
    不加载模型、不占内存，对外表现与"引擎不可用"一致（调用方按既有降级路径处理）。

    失败结果不缓存：环境修复（补装依赖等）后无需重启进程即可恢复。
    """
    global _ocr_engine
    if not IMAGE_OCR_ENABLED:
        # 单点关闭：两条链路与刷数命令都经本函数取引擎，此处即全局失效
        return None
    if _ocr_engine is None:
        with _ocr_engine_lock:
            if _ocr_engine is None:
                try:
                    from rapidocr_onnxruntime import RapidOCR

                    _ocr_engine = RapidOCR()
                except Exception as e:  # noqa: BLE001  OCR 是增强能力，初始化失败不阻断调用方
                    _log.warning(f'rapidocr engine init failed: {e}')
                    return None
    return _ocr_engine


def is_valid_ocr_text(text: str) -> bool:
    """质量门线：有效字符（去除空白与标点）数 ≥30 且 中文/英文/数字占比 ≥50%。

    门线只拦乱码、纯符号、过短输出，不判断文本是否真的来自图片。
    """
    if not text:
        return False
    effective_chars = _EFFECTIVE_CHAR_PATTERN.findall(text)
    if len(effective_chars) < _MIN_EFFECTIVE_CHARS:
        return False
    text_chars = _TEXT_CHAR_PATTERN.findall(text)
    return len(text_chars) / len(effective_chars) >= _MIN_TEXT_RATIO


def extract_image_text(image_bytes: bytes, image_name: str) -> str:
    """本地 RapidOCR 抽取图片文字，门线不过或推理失败返回空串（调用方降级文件名占位）。

    @param image_bytes:  图片二进制内容（RapidOCR 原生支持 bytes 输入）
    @param image_name:   图片文件名（仅用于日志定位）
    @return:             OCR 文本（通过质量门线）或空串
    """
    try:
        engine = get_ocr_engine()
        if engine is None:
            return ''
        result, _elapse = engine(image_bytes)
    except Exception as e:  # noqa: BLE001  OCR 是增强能力，任何异常都降级为空串
        _log.warning(f'image text extract failed for {image_name}: {e}')
        return ''
    if not result:
        return ''
    # result 为按阅读顺序排列的 [box, text, score] 列表，各行 text 以换行拼接
    text = '\n'.join(
        str(item[1]) for item in result
        if item and len(item) > 1 and item[1]
    ).strip()
    # 门线不过（过短 / 乱码 / 噪声）不留原文，直接降级文件名占位
    return text if is_valid_ocr_text(text) else ''
