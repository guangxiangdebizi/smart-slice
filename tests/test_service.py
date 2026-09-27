# coding=utf-8
"""公共切片服务单测（全 mock / 离线构造，不依赖 DB/网络/凭据）。

覆盖 2026-09-17 统一切片底层重构：
  1. 双链路等价快照：同一批最小样例分别按上传参数（normalize=False, limit=4096）
     与批量导入参数（normalize=True, limit=1000）调 split_document，断言输出结构与
     迁移前两链路行为等价（期望值取自迁移前基线实测，见迁移记录 2026-09-17 节）；
  2. normalize=True 逐条行为（标题回退链、截断256、嵌套 dict 防 repr、空段剔除、
     \0 清洗、非 dict 组跳过）；
  3. OCR 注入分支：仅 ImageSplitHandle 收到 extractor，zip 内层与 非 handler 不透传；
  4. F6 递归重写公共函数（嵌套 dict/list/str）；
  5. 字节适配器（BytesSplitFile）与延迟构建包装（LazyImageTextExtractor）。

运行：
  python -m pytest tests/test_service.py -v
  （或）python -m unittest tests.test_service -v
"""
import io
import os
import unittest
import zipfile
from unittest import mock


from smart_slice.exceptions import SliceError
from smart_slice.handlers.image import ImageSplitHandle
from smart_slice.service import (
    BytesSplitFile,
    LazyImageTextExtractor,
    SPLIT_HANDLERS,
    build_image_text_extractor,
    normalize_split_rows,
    replace_image_file_ids,
    split_document,
)

SPLIT_SVC = "smart_slice.service"


def minimal_pdf_bytes(text="bulk import test line"):
    """构造最小合法 PDF（单页 + 一行文本，xref 偏移量按实际写入位置计算）。"""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects[3] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_pos = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode()
    )
    return out.getvalue()


def xlsx_two_sheet_bytes():
    import openpyxl

    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "产品清单"
    ws1.append(["名称", "数量"])
    ws1.append(["打印机", "10"])
    ws2 = wb.create_sheet("价格表")
    ws2.append(["型号", "价格"])
    ws2.append(["A1", "999"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def zip_with_md_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("说明.md", "# 标题\nzip 内正文")
    return buf.getvalue()


class DualPathEquivalenceSnapshotTests(unittest.TestCase):
    """双链路等价快照：split_document 两种参数形态与迁移前行为等价。

    期望值来源：迁移前（feat/image-ocr-local-engine 90993f8）用旧链路函数
    （the upload-path file_to_paragraph / the download-split helper、
    smart_split_markdown）实测采集的基线，键值硬编码于断言（uuid 类动态值归一）。"""

    def _snapshot_upload(self, name, content):
        return split_document(name, content, limit=4096, pattern_list=None, with_filter=None, normalize=False)

    def _snapshot_bulk_import(self, name, content):
        return split_document(name, content, limit=1000, pattern_list=None, with_filter=True, normalize=True)

    def test_upload_txt_snapshot(self):
        result = self._snapshot_upload("a.txt", "第一段内容。\n\n第二段内容。".encode("utf-8"))
        self.assertEqual(result, {
            "name": "a.txt",
            "content": [{"title": "", "content": "第一段内容。"}, {"title": "", "content": "第二段内容。"}],
        })

    def test_upload_md_snapshot(self):
        result = self._snapshot_upload("a.md", "# 标题\n内容正文".encode("utf-8"))
        self.assertEqual(result["name"], "a.md")
        self.assertEqual(len(result["content"]), 1)
        self.assertEqual(result["content"][0]["title"], " 标题")  # 迁移前基线：标题保留前导空格
        self.assertEqual(result["content"][0]["content"], "\n内容正文")

    def test_upload_csv_snapshot(self):
        result = self._snapshot_upload("a.csv", "a,b\n1,2".encode("utf-8"))
        self.assertEqual(result["name"], "a.csv")
        self.assertEqual(result["content"][0]["content"], "| a | b |\n| --- | --- |\n| 1 | 2 |\n")

    def test_upload_pdf_snapshot(self):
        result = self._snapshot_upload("a.pdf", minimal_pdf_bytes())
        self.assertEqual(result["name"], "a.pdf")
        self.assertEqual(result["content"][0]["content"], "bulk import test line\n")

    def test_upload_xlsx_keeps_group_structure(self):
        # 上传链路 normalize=False：xlsx per-sheet 分组结构原样保留（前端预览依赖）
        result = self._snapshot_upload("a.xlsx", xlsx_two_sheet_bytes())
        self.assertIsInstance(result, list)
        self.assertEqual([group["name"] for group in result], ["产品清单", "价格表"])
        self.assertIn("打印机", result[0]["content"][0]["content"])
        self.assertIn("A1", result[1]["content"][0]["content"])

    def test_bulk_import_binary_snapshots_match_baseline(self):
        """批量导入二进制附件：normalize=True 展平结果与迁移前 _split_downloaded_file 基线一致。"""
        pdf_rows = self._snapshot_bulk_import("手册.pdf", minimal_pdf_bytes())
        self.assertEqual(pdf_rows, [{"title": "手册.pdf", "content": "bulk import test line"}])

        xlsx_rows = self._snapshot_bulk_import("报表.xlsx", xlsx_two_sheet_bytes())
        self.assertEqual(xlsx_rows, [
            {"title": "产品清单", "content": "| 名称 | 数量 |\n| --- | --- |\n| 打印机 | 10 |"},
            {"title": "价格表", "content": "| 型号 | 价格 |\n| --- | --- |\n| A1 | 999 |"},
        ])

        zip_rows = self._snapshot_bulk_import("打包.zip", zip_with_md_bytes())
        self.assertEqual(zip_rows, [{"title": "标题", "content": "zip 内正文"}])

    def test_bulk_import_unsupported_format_raises(self):
        with self.assertRaises(SliceError):
            self._snapshot_bulk_import("视频.mp4", b"\x00\x00\x00\x18ftypmp42")


class NormalizeSplitRowsTests(unittest.TestCase):
    """normalize=True 逐条行为测试（原 _flatten_split_rows 行为逐条保留）。"""

    def test_single_dict_group_flattened(self):
        result = {"name": "文件.pdf", "content": [{"title": "段落标题", "content": "正文"}]}
        self.assertEqual(normalize_split_rows(result, "回退名"),
                         [{"title": "段落标题", "content": "正文"}])

    def test_title_fallback_chain_group_name_then_fallback(self):
        # 段落 title 为空 → 回退组名 → 再回退 fallback_title
        result = [
            {"name": "Sheet1", "content": [{"title": "", "content": "内容一"}]},
            {"name": "", "content": [{"title": "", "content": "内容二"}]},
            {"name": "", "content": [{"title": "", "content": "内容三"}]},
        ]
        rows = normalize_split_rows(result, "下载文件名")
        self.assertEqual([r["title"] for r in rows], ["Sheet1", "下载文件名", "下载文件名"])

    def test_fallback_title_param_overrides_adapter_filename(self):
        """fallback_title 显式传入时，第三级回退用调用方标题而非适配文件名。

        等价性修复（2026-09-17）：在线文档 markdown 链路的适配文件名
        （{文档名}.md）不得泄漏进段落标题；未传时维持回落到文件名的原语义。"""
        content = "无标题正文内容。".encode("utf-8")
        explicit = split_document("文档名.md", content, limit=1000, normalize=True,
                                  fallback_title="文档名")
        self.assertEqual(explicit, [{"title": "文档名", "content": "无标题正文内容。"}])
        defaulted = split_document("下载文件.md", content, limit=1000, normalize=True)
        self.assertEqual(defaulted, [{"title": "下载文件.md", "content": "无标题正文内容。"}])

    def test_title_truncated_to_256(self):
        result = {"name": "组名" * 200, "content": [{"title": "", "content": "正文"}]}
        rows = normalize_split_rows(result, "回退名")
        self.assertEqual(len(rows[0]["title"]), 256)

    def test_nested_dict_content_guard_no_repr(self):
        # 嵌套 dict 取其 content，不产出 Python repr 串（缺陷4回归）
        result = {"name": "组", "content": [{"title": "t", "content": {"content": "嵌套正文"}}]}
        rows = normalize_split_rows(result, "回退名")
        self.assertEqual(rows, [{"title": "t", "content": "嵌套正文"}])
        self.assertNotIn("{'", rows[0]["content"])

    def test_non_list_non_dict_items_wrapped_and_empty_dropped(self):
        result = {"name": "组", "content": ["", "   ", "正文", None]}
        rows = normalize_split_rows(result, "回退名")
        self.assertEqual(rows, [{"title": "组", "content": "正文"}])

    def test_nul_stripped_and_content_trimmed(self):
        # 原版行为逐条保留：\0 清洗与 strip 仅作用于段落文本；title 只做 strip
        # （迁移前 _flatten_split_rows 对 title 无 \0 清洗，此处不私自加码）
        result = {"name": "组", "content": [{"title": "标\0题", "content": "  含\0空字符正文 \n"}]}
        rows = normalize_split_rows(result, "回退名")
        self.assertEqual(rows, [{"title": "标\0题", "content": "含空字符正文"}])

    def test_non_dict_group_skipped(self):
        rows = normalize_split_rows(["非法组", {"name": "组", "content": [{"title": "t", "content": "正文"}]}], "回退名")
        self.assertEqual(rows, [{"title": "t", "content": "正文"}])


class OcrInjectionBranchTests(unittest.TestCase):
    """OCR 注入分支：仅 ImageSplitHandle 收到 extractor；zip 内层图片不透传。"""

    def test_image_file_receives_extractor_and_uses_text(self):
        extractor = lambda image_bytes, image_name: "订单编号20240601"  # noqa: E731
        result = split_document(
            "产品截图.png", b"fake-png-bytes", limit=1000,
            image_text_extractor=extractor, normalize=True)
        self.assertEqual(result, [{"title": "订单编号20240601", "content": "订单编号20240601"}])

    def test_image_without_extractor_falls_back_to_filename(self):
        result = split_document("产品截图.png", b"fake-png-bytes", limit=1000, normalize=True)
        self.assertEqual(result, [{"title": "产品截图.png", "content": "产品截图.png"}])

    def test_extractor_not_passed_to_non_image_handlers(self):
        """非图片 handler 的 handle() 不接收 image_text_extractor 关键字（照抄上传链路
        按 handler 能力分支传参的缺陷修复语义）。"""
        handle = mock.Mock()
        handle.support = mock.Mock(return_value=True)
        handle.handle = mock.Mock(return_value={"name": "a.txt", "content": []})
        with mock.patch(f"{SPLIT_SVC}.SPLIT_HANDLERS", [handle]):
            split_document("a.txt", b"data", limit=1000, image_text_extractor=lambda b, n: "ocr", normalize=True)
        self.assertNotIn("image_text_extractor", handle.handle.call_args.kwargs)

    def test_image_handler_receives_extractor_kwarg(self):
        handle = mock.Mock(spec=ImageSplitHandle)
        handle.support = mock.Mock(return_value=True)
        handle.handle = mock.Mock(return_value={"name": "a.png", "content": []})
        extractor = lambda b, n: "ocr"  # noqa: E731
        with mock.patch(f"{SPLIT_SVC}.SPLIT_HANDLERS", [handle]):
            split_document("a.png", b"\x89PNG", limit=1000, image_text_extractor=extractor, normalize=True)
        self.assertIs(handle.handle.call_args.kwargs.get("image_text_extractor"), extractor)

    def test_zip_inner_images_do_not_receive_extractor(self):
        """zip 内层图片不透传 extractor：ZipSplitHandle 的内层 handler 清单不含
        ImageSplitHandle（zip 内嵌图片经其自身的 markdown 引用收集 + save_image
        交付字节路径处理），公共层向 zip handler 本身也不传 extractor 关键字。

        注：上述跳过日志的 file.name 遮蔽缺陷（原基线遗留）已于 Phase 2-B
        （2026-09-18，feat/inline-image-ocr）做最小修复——zip 内含图片/不支持文件
        不再因 except 块内 AttributeError 炸掉整包，详见迁移记录。"""
        from smart_slice.handlers import zip_handler as zip_split_handle

        inner_names = [type(h).__name__ for h in zip_split_handle.split_handles]
        self.assertNotIn("ImageSplitHandle", inner_names)

        received_images = []
        extractor = mock.Mock(return_value="不应出现的OCR文本")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("说明.md", "# 标题\nzip 内正文")
        with mock.patch(f"{SPLIT_SVC}.SPLIT_HANDLERS", list(SPLIT_HANDLERS)):
            result = split_document(
                "打包.zip", buf.getvalue(), limit=1000,
                save_image=lambda image_list: received_images.extend(image_list or []),
                image_text_extractor=extractor, normalize=True)
        self.assertEqual(result, [{"title": "标题", "content": "zip 内正文"}])
        extractor.assert_not_called()  # zip 路径 OCR 全程未被触发
        self.assertEqual(received_images, [])  # zip 内无图片 → 回调不产出图片


class ReplaceImageFileIdsTests(unittest.TestCase):
    """F6 递归重写公共函数（dict/list/str 嵌套）。"""

    MAPPING = {"11111111-1111-1111-1111-111111111111": "22222222-2222-2222-2222-222222222222"}

    def test_nested_structures_rewritten(self):
        result = {
            "name": "文档",
            "content": [
                {"title": "t", "content": "看图 ./oss/file/11111111-1111-1111-1111-111111111111"},
                {"title": "t2", "content": ["前缀 ./oss/file/11111111-1111-1111-1111-111111111111 后缀"]},
            ],
            "meta": {"deep": {"link": "./oss/file/11111111-1111-1111-1111-111111111111"}},
        }
        rewritten = replace_image_file_ids(result, self.MAPPING)
        self.assertIn("./oss/file/22222222-2222-2222-2222-222222222222", rewritten["content"][0]["content"])
        self.assertIn("./oss/file/22222222-2222-2222-2222-222222222222", rewritten["content"][1]["content"][0])
        self.assertIn("./oss/file/22222222-2222-2222-2222-222222222222", rewritten["meta"]["deep"]["link"])

    def test_empty_mapping_returns_original(self):
        result = {"name": "文档", "content": []}
        self.assertIs(replace_image_file_ids(result, {}), result)
        self.assertIs(replace_image_file_ids(result, None), result)

    def test_non_matching_ids_untouched(self):
        text = "引用 ./oss/file/99999999-9999-9999-9999-999999999999"
        self.assertEqual(replace_image_file_ids(text, self.MAPPING), text)


class SaveImageMappingCollectionTests(unittest.TestCase):
    """save_image 回调返回的 F6 去重映射由公共层收集并即时应用（上传链路时序）。"""

    OLD_ID, NEW_ID = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"

    def test_callback_mapping_applied_to_result(self):
        def save_image(image_list):
            return {self.OLD_ID: self.NEW_ID}

        handle = mock.Mock(spec=ImageSplitHandle)
        handle.support = mock.Mock(return_value=True)

        def _handle(file, pattern_list, with_filter, limit, get_buffer, save_image_cb, **kwargs):
            # 模拟 handler 解析期间调用 save_image（F6 映射的同步回调时序）
            save_image_cb([])
            return {"name": "a.png", "content": [{"title": "t", "content": f"看图 ./oss/file/{self.OLD_ID}"}]}

        handle.handle = mock.Mock(side_effect=_handle)
        with mock.patch(f"{SPLIT_SVC}.SPLIT_HANDLERS", [handle]):
            result = split_document("a.png", b"\x89PNG", limit=1000, save_image=save_image, normalize=False)
        self.assertIn(f"./oss/file/{self.NEW_ID}", result["content"][0]["content"])

    def test_none_mapping_not_applied(self):
        handle = mock.Mock()
        handle.support = mock.Mock(return_value=True)
        handle.handle = mock.Mock(return_value={"name": "a.txt", "content": [{"title": "", "content": "正文"}]})
        with mock.patch(f"{SPLIT_SVC}.SPLIT_HANDLERS", [handle]):
            result = split_document("a.txt", b"data", limit=1000, save_image=lambda image_list: None, normalize=False)
        self.assertEqual(result["content"][0]["content"], "正文")


class BytesSplitFileTests(unittest.TestCase):
    def test_read_chunks_name_size(self):
        adapter = BytesSplitFile("a.bin", b"0123456789")
        self.assertEqual(adapter.name, "a.bin")
        self.assertEqual(adapter.size, 10)
        self.assertEqual(adapter.read(), b"0123456789")
        self.assertEqual(list(adapter.chunks()), [b"0123456789"])  # PdfSplitHandle 消费面

    def test_split_document_supports_django_style_file(self):
        file_obj = BytesSplitFile("说明.txt", "适配正文。".encode("utf-8"))
        result = split_document(file_obj.name, file_obj.read(), limit=1000, normalize=True)
        self.assertEqual(result[0]["content"], "适配正文。")


class LazyImageTextExtractorTests(unittest.TestCase):
    def test_builder_not_called_until_first_invocation(self):
        builder = mock.Mock(return_value=lambda b, n: "ocr文本" * 10)
        extractor = LazyImageTextExtractor(builder)
        builder.assert_not_called()
        self.assertEqual(extractor(b"img", "a.png"), "ocr文本" * 10)
        builder.assert_called_once()
        extractor(b"img", "a.png")  # 构建结果复用
        builder.assert_called_once()

    def test_builder_none_degrades_to_empty_text(self):
        extractor = LazyImageTextExtractor(lambda: None)  # 本地引擎不可用形态
        self.assertEqual(extractor(b"img", "a.png"), "")


class BuildImageTextExtractorTests(unittest.TestCase):
    """公共 build_image_text_extractor：引擎可用返回闭包，不可用返回 None。"""

    def test_engine_available_returns_callable(self):
        engine = mock.Mock()
        engine.return_value = ([[[[0, 0], [10, 0], [10, 10], [0, 10]],
                                 "订单编号20240601123456，客户名称：杭州云启科技有限公司", 0.98]], 0.1)
        # get_ocr_engine 经 from-import 进入服务模块命名空间，patch 服务模块的引用
        with mock.patch(f"{SPLIT_SVC}.get_ocr_engine", return_value=engine), \
                mock.patch(f"{SPLIT_SVC}.extract_image_text") as extract_mock:
            extract_mock.return_value = "订单编号20240601123456，客户名称：杭州云启科技有限公司"
            extractor = build_image_text_extractor()
            self.assertTrue(callable(extractor))
            self.assertEqual(extractor(b"img", "a.png"),
                             "订单编号20240601123456，客户名称：杭州云启科技有限公司")

    def test_engine_unavailable_returns_none(self):
        with mock.patch(f"{SPLIT_SVC}.get_ocr_engine", return_value=None):
            self.assertIsNone(build_image_text_extractor())


def _minimal_pdf_bytes(text="progress hook page"):
    """最小合法 PDF（单页一行文本，xref 偏移按实际写入位置计算），供逐页回调用例使用。"""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects[3] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref_pos = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF".encode())
    return out.getvalue()


class ProgressHookTests(unittest.TestCase):
    """progress_hook 接线（2026-09-18 批量导入心跳）：切片过程中按进展回调。

    目的：批量导入链路单文件解析（大 PDF 逐页、zip 内层逐条、内嵌图逐张 OCR）可能持续数分钟，
    导入侧需要在解析期间持续刷新心跳，避免"仍在推进"的库被判为零进展。"""

    def test_hook_called_for_handler_and_zip_inner_entries(self):
        ticks = []
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a.md", "# 一\naaa")
            zf.writestr("b.md", "# 二\nbbb")
        with mock.patch(f"{SPLIT_SVC}.SPLIT_HANDLERS", list(SPLIT_HANDLERS)):
            result = split_document(
                "打包.zip", buf.getvalue(), limit=1000, normalize=True,
                progress_hook=lambda: ticks.append("tick"))
        self.assertTrue(result)
        # 外层条目的 buffer 读取 + 每个内层条目的 get_buffer：至少覆盖 2 个内层文件
        self.assertGreaterEqual(ticks.count("tick"), 3)

    def test_hook_called_per_page_for_pdf(self):
        ticks = []
        pdf_bytes = _minimal_pdf_bytes("progress hook page")
        with mock.patch(f"{SPLIT_SVC}.SPLIT_HANDLERS", list(SPLIT_HANDLERS)):
            split_document(
                "hook.pdf", pdf_bytes, limit=1000, normalize=True,
                progress_hook=lambda: ticks.append("tick"))
        self.assertGreaterEqual(ticks.count("tick"), 2)  # 进入 PDF handler + 逐页解析

    def test_hook_none_keeps_previous_behavior(self):
        """不传 progress_hook 时行为与接线前一致（不产生额外回调、结果不变）。"""
        result = split_document("a.md", "# 一\naaa".encode("utf-8"), limit=1000, normalize=True)
        self.assertEqual(result, [{"title": "一", "content": "aaa"}])


class SplitDocumentContractTests(unittest.TestCase):
    """公共入口契约：limit 必传、name \0 清洗、handler 清单单例。"""

    def test_limit_is_required_keyword(self):
        with self.assertRaises(TypeError):
            split_document("a.txt", b"data")

    def test_nul_in_name_sanitized(self):
        result = split_document("a\0.txt", "正文。".encode("utf-8"), limit=1000, normalize=True)
        self.assertEqual(result[0]["title"], "a.txt")
        self.assertEqual(result[0]["content"], "正文。")

    def test_split_handlers_contains_all_handlers_in_order(self):
        # Phase 2-A（2026-09-17）全格式适配：办公文档族 handler 插入 Xmind 之后、
        # Image 之前；TextSplitHandle 仍为末位兜底
        names = [type(h).__name__ for h in SPLIT_HANDLERS]
        self.assertEqual(names, ["HTMLSplitHandle", "MhtmlSplitHandle", "DocSplitHandle", "PdfSplitHandle", "XlsxSplitHandle",
                                 "XlsSplitHandle", "CsvSplitHandle", "ZipSplitHandle", "XmindSplitHandle",
                                 "PptxSplitHandle", "PptSplitHandle", "WpsSplitHandle", "RtfSplitHandle",
                                 "OdfSplitHandle", "EpubSplitHandle", "EmlSplitHandle", "MsgSplitHandle",
                                 "MobiSplitHandle", "TarSplitHandle", "SevenZipSplitHandle",
                                 "ImageSplitHandle", "TextSplitHandle"])


if __name__ == "__main__":
    unittest.main()
