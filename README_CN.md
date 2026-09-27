# smart-slice

**面向 RAG 管线的保真优先文档切片库。** 输入约 30 种文件格式，输出检索可用的段落，且原始字符全程可回溯、不丢失。

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-119%20passed-brightgreen.svg)]()

```python
from smart_slice import slice_text

paragraphs = slice_text("# 章节\n\n正文内容", limit=1000)
# [{'title': '章节', 'content': '正文内容'}]
```

> 英文主文档见 [README.md](README.md)。

---

## 设计取向

多数切分器把文档当作字符流，按固定网格切块，由此丢失检索真正依赖的两样东西：**段落在文档结构中的位置**，以及**段落的原始措辞**。smart-slice 围绕三条规则构建。

### 一、保真优先于聪明

任何清洗步骤都不删原文：

- 标题标记 `#` 仅在**行首**且**不在 ` ``` ` 代码围栏内**才剥离；代码块中的 `#` 注释、`#RRGGBB` 色码原样保留。
- markdown 表格因长度预算被切断时，后续分块**增补**表头行（而非改写），原始数据行一字不动。
- 超长表格行执行**二次切分**，不截断。

### 二、结构优先于长度

`limit` 是**预算**而非网格。切分顺序依次回退：

1. markdown 标题树（1–6 级），
2. 空行分段，
3. 句子边界（`。 . ! ！ ? ？`），
4. 最后才按字符硬切。

每个段落的 `title` 字段保存其**标题链**（如 `"第一部分  第三章"`），使下游分块保留章节语境。

### 三、实测高效

551 KB 结构化文档切片约 140 ms（≈3.9 MB/s）。热路径为单遍线性扫描判定块内可能存在的标题层级，跳过必然为空的正则扫描；可选 C 扩展（`pip install smart-slice[accel]`）原生执行该扫描。数据与推理见 [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md)。

### 四、无框架依赖

纯库实现：无 Django、无 ORM、无网络请求、不配置日志（仅通过 `smart_slice` logger 发出记录，路由交由宿主决定）。错误为 `SliceError`，带 HTTP 风格的 `.code`（`400` 输入问题、`500` 解析失败）。从文档抽出的图片经 `save_image` **回调**交付，库本身不做任何持久化。

---

## 安装

核心安装刻意保持极小（仅含文本解码器）。各格式解析器以可选 extras 提供；解析器缺失时对应 handler 报"不支持"，不会崩溃。

```bash
pip install smart-slice          # 核心：文本/markdown/配置/源码类文件
pip install smart-slice[all]     # 下方全部格式
```

按需 extras：

| Extra | 解锁格式 |
|-------|----------|
| `markup` | HTML、MHTML、EPUB、EML、MSG、MOBI |
| `office` | DOCX、PPTX、PPT、XLSX、XLS、WPS、ET、RTF、ODF |
| `pdf` | PDF |
| `mail` | MSG |
| `archive` | 7z（zip/tar 为标准库） |
| `ebook` | MOBI/AZW |
| `image` | HEIC/HEIF 图片 |
| `ocr` | 文档内嵌图片的本地 OCR（RapidOCR，运行时另需开启） |
| `keywords` | jieba 关键词辅助函数 |
| `fastuuid` | 时间有序的图片 id（内置纯 Python 回退） |
| `dev` | all + pytest、ruff、mypy |

运行时调用 `smart_slice.missing_dependencies()` 可列出尚缺哪些 extras。

---

## 快速开始

### 切分字符串

```python
from smart_slice import slice_text

rows = slice_text("# 指南\n\n引言正文。\n\n## 安装\n\n第一步。", limit=1000)
for r in rows:
    print(r["title"], "->", r["content"][:40])
# 指南 -> 引言正文。
# 指南  安装 -> 第一步。
```

### 从磁盘文件切分

```python
from smart_slice import slice_path

rows = slice_path("report.pdf", limit=1000)   # 文件名决定 handler
```

### 从字节切分（并捕获图片）

```python
from smart_slice import slice_bytes

images = []
def save_image(assets):          # assets: list[ImageAsset]
    images.extend(assets)

rows = slice_bytes(raw_bytes, "deck.pptx", limit=1000, save_image=save_image)
# 每个 ImageAsset 含 .id、.file_name、.content（字节）；
# 段落文本已用 ![name](./oss/file/{id}) 引用它
```

### 输出结构

所有入口返回 `[{"title": str, "content": str}]` 列表：`title` 为标题链（无标题时为空串），`content` 为段落原文。

---

## 支持格式

30 个 handler、197 个声明扩展名。除 `charset-normalizer`（文本解码）外，每个解析器都是**可选 extra**：解析器缺失时该格式报 `400 Unsupported file format`，而不是让 `import smart_slice` 崩溃。

| 类别 | Handler | 扩展名 | 解析器 |
|------|---------|--------|--------|
| Web | `HTMLSplitHandle` | `.html` `.htm` `.xhtml` `.shtml` | markdownify + bs4 |
| Web | `MhtmlSplitHandle` | `.mhtml` `.mht` | markdownify |
| Office | `DocSplitHandle` | `.docx` `.docm` `.doc` `.dotx` `.dotm` | python-docx |
| PDF | `PdfSplitHandle` | `.pdf` | pypdf + Pillow |
| 表格 | `XlsxSplitHandle` | `.xlsx` `.xlsm` `.xltx` `.xltm` | openpyxl |
| 表格 | `XlsSplitHandle` | `.xls` | xlrd |
| 表格 | `CsvSplitHandle` | `.csv` `.tsv` `.tab` | 标准库 csv |
| 压缩包 | `ZipSplitHandle` | `.zip` | 标准库 zipfile |
| 思维导图 | `XmindSplitHandle` | `.xmind` | 标准库 zipfile + json |
| Office | `PptxSplitHandle` | `.pptx` `.pptm` `.ppsx` `.ppsm` `.potx` `.potm` | python-pptx |
| Office | `PptSplitHandle` | `.ppt` `.dps` | olefile / python-pptx |
| Office | `WpsSplitHandle` | `.wps` `.et` | python-docx / openpyxl |
| Office | `RtfSplitHandle` | `.rtf` | striprtf |
| Office | `OdfSplitHandle` | `.odt` `.ods` `.odp` `.fodt` `.fods` `.fodp` | 标准库 zipfile + xml |
| 电子书 | `EpubSplitHandle` | `.epub` | 标准库 zipfile + bs4 |
| 邮件 | `EmlSplitHandle` | `.eml` | 标准库 email + bs4 |
| 邮件 | `MsgSplitHandle` | `.msg` | extract-msg + bs4 |
| 电子书 | `MobiSplitHandle` | `.mobi` `.azw` `.azw1` `.azw3` `.azw4` `.prc` | mobi + bs4 |
| 压缩包 | `TarSplitHandle` | `.tar` `.tar.gz` `.tgz` `.tar.bz2` `.tar.xz` `.txz` | 标准库 tarfile |
| 压缩包 | `SevenZipSplitHandle` | `.7z` | py7zr |
| 图片 | `ImageSplitHandle` | `.jpg` `.jpeg` `.png` `.gif` `.bmp` `.tiff` `.tif` `.webp` `.heic` `.heif` | Pillow（+ pillow-heif） |
| 电子书 | `Fb2SplitHandle` | `.fb2` `.fb2.zip` | 标准库 zipfile + xml |
| 矢量/结构化 | `SvgSplitHandle` | `.svg` `.svgz` | 标准库 xml |
| 矢量/结构化 | `IpynbSplitHandle` | `.ipynb` | 标准库 json |
| 矢量/结构化 | `SubtitleSplitHandle` | `.srt` `.vtt` `.ass` `.ssa` `.sub` | 标准库 re |
| 邮件 | `MboxSplitHandle` | `.mbox` | 标准库 mailbox |
| 矢量/结构化 | `VcalendarSplitHandle` | `.ics` `.ifb` | 标准库 |
| 矢量/结构化 | `VcardSplitHandle` | `.vcf` | 标准库 |
| 图片 | `ExtendedImageSplitHandle` | `.ico` `.cur` `.tga` `.pcx` `.dds` `.sgi` `.ppm` `.pgm` `.pbm` `.pnm` `.pfm` `.im` `.icns` `.qoi` `.jfif` `.jpe` `.apng` `.xbm` `.psd`（含别名） | Pillow |
| 文本/源码 | `TextSplitHandle` | `.txt` `.md` `.markdown` `.log` `.json` `.jsonl` `.yaml` `.toml` `.ini` `.rst` `.tex` `.sql` 及 60+ 源码/配置扩展名（`.py` `.js` `.ts` `.java` `.go` `.c` `.cpp` `.rs` `.rb` `.php` `.cs` `.swift` `.kt` `.scala` `.lua` `.r` `.sh` `.ps1` `.css` `.xml` `.proto` `.graphql` `.adoc` `.org` `.diff` `.patch` `.po` `.properties` `.env` …） | charset-normalizer |


### 明确不支持（抛 `400`）

以下类型是刻意拒绝、而非硬解码成乱码：

| 类别 | 扩展名 | 原因 |
|------|--------|------|
| 视频 | `.mp4` `.avi` `.mov` `.mkv` `.flv` `.wmv` `.webm` `.mpeg` `.mpg` `.3gp` `.rmvb` | 无转写链路 |
| 音频 | `.mp3` `.wav` `.flac` `.aac` `.ogg` `.m4a` `.wma` `.opus` `.alac` `.aiff` `.amr` | 无 ASR 能力 |
| 压缩包 | `.rar` | 无纯 Python 解析方案（rarfile 依赖系统 unrar 二进制） |
| Apple iWork | `.key` `.pages` `.numbers` | 私有 IWA 容器 |
| 老二进制 Office | 老 OLE 的 `.wps` / `.et` / `.doc` | 只解析 OOXML/zip 变体；请先转 `.docx` |
| 可执行/镜像 | `.exe` `.dll` `.msi` `.dmg` `.apk` `.iso` | 非文档 |
| 矢量/原始照片 | `.raw` | 无通用解码器（各家相机私有） |

**内容级拒绝**（扩展名对、内容解析不出）：加密 PDF、无文本无图的扫描版 PDF 抛 `500` 并
注明原因；加密或超限压缩包抛 `ResourceLimitError`。本地 OCR **默认关闭**
（`SMART_SLICE_OCR_ENABLED=0`），装 `ocr` extra 并开启后可从内嵌图与扫描图恢复文本。

---

## 命令行

```bash
smart-slice slice report.pdf --limit 1000 --format json -o out.json
smart-slice slice notes.md --format text --title-prefix --stats
smart-slice detect mystery.bin
smart-slice formats                       # 列出 handler 与缺失的可选依赖
python -m smart_slice slice notes.md --format md
```

- `slice`：`--format {json,jsonl,text,md}`、`--limit`、`--no-filter`、`--title-prefix`、`-o/--output`、`--stats`（摘要写 stderr）。
- `detect`：输出将命中该文件的 handler 类名（无命中退出码 2）。
- `formats`：支持的扩展名与未安装的 extras（`--json` 供机器读取）。

---

## 进阶用法

### 块大小与重叠

两级切分均有合理默认值，也完全可自定义：不传参用默认，传数值或配置对象即自定义。

| 阶段 | 参数 | 默认 | 自定义 |
|------|------|------|--------|
| 切片（段落） | `limit` | `1000` 字符 | `limit=800` |
| 切片（段落） | `overlap` | `0`（不重叠） | `overlap=150` 或 `overlap_ratio=0.15` |
| 分块（embedding） | `chunk_size` | `256` 字符 | `chunk_size=512` |
| 分块（embedding） | `chunk_overlap` | `0`（不重叠） | `chunk_overlap=40` |

```python
from smart_slice import slice_bytes, chunk_paragraphs, ChunkingOptions

rows = slice_bytes(data, "doc.md")                              # 默认：1000 字符、不重叠
rows = slice_bytes(data, "doc.md", limit=800, overlap_ratio=0.15)  # 自定义

opts = ChunkingOptions(limit=600, overlap=90, carry_title=True)   # 复用于多个文档
for name, blob in documents.items():
    rows = slice_bytes(blob, name, options=opts)

chunks = chunk_paragraphs(rows, chunk_size=256, chunk_overlap=40, carry_title=True)
```

重叠为**纯增补**：段落数量与标题不变，每个段落只在正文前追加上一段的结尾；`overlap=0`（默认）时各段互不重叠，拼接可逐字还原原文。`ChunkingOptions` 另含 `boundary`（句子边界对齐）、`lookback`（边界回看比例）、`min_chunk`（短块合并）、`carry_title`（标题链前置）、`overlap_within_section`（仅同章节内重叠）、`length_fn`（按 token 计量，传分词器即可）等字段。

### 自定义标题模式

```python
import re
from smart_slice import slice_bytes, BLANK_LINE

patterns = [re.compile(r"(?m)^SECTION \d+:.*"), *BLANK_LINE]
rows = slice_bytes(data, "spec.txt", limit=800, patterns=patterns)
```

### 保留原始嵌套结构

`normalize=False` 返回 handler 原生结构——多 sheet 工作簿与压缩包为嵌套分组，预览类场景适用：

```python
raw = slice_bytes(data, "book.xlsx", limit=1000, normalize=False)
# [{"name": "Sheet1", "content": [...]}, {"name": "Sheet2", "content": [...]}]
```

### 图片 OCR 钩子

```python
from smart_slice import slice_bytes, build_image_text_extractor

ocr = build_image_text_extractor()   # 需 SMART_SLICE_OCR_ENABLED=1 且装 ocr extra，否则为 None
rows = slice_bytes(data, "scan.docx", limit=1000, image_text_extractor=ocr)
```

### 进度心跳

`progress_hook` 为零参回调，在进入每个 handler、每个压缩包内层条目、每次 OCR 前各触发一次，可在解析大文件时接到任务心跳：

```python
slice_bytes(data, "huge.pdf", limit=1000, progress_hook=lambda: job.touch())
```

### 面向 embedding 的二次分块

切片产出语义段落，embedding 模型仍需固定长度输入。`chunk_paragraphs` 将段落切到窗口大小，段落仍作为检索单元：

```python
from smart_slice import slice_path, chunk_paragraphs

rows = slice_path("doc.md", limit=2000)
chunks = chunk_paragraphs(rows, chunk_size=256)
```

### 异常处理

```python
from smart_slice import slice_bytes, SliceError

try:
    slice_bytes(data, "movie.mp4")
except SliceError as e:
    print(e.code, e.message)   # 400 Unsupported file format: .mp4
```

---

## 配置

防御性解析上限（抗解压炸弹）取自环境变量，规范名 `SMART_SLICE_PARSER_<FIELD>`：

| 变量 | 默认值 | 含义 |
|------|--------|------|
| `SMART_SLICE_PARSER_MAX_INPUT_BYTES` | 134217728 | 输入字节上限 |
| `SMART_SLICE_PARSER_MAX_XML_BYTES` | 33554432 | XML 成员字节上限 |
| `SMART_SLICE_PARSER_MAX_PACKAGE_MEMBERS` | 10000 | 压缩包条目数上限 |
| `SMART_SLICE_PARSER_MAX_PACKAGE_BYTES` | 134217728 | 解压后总字节上限 |
| `SMART_SLICE_PARSER_MAX_XML_ELEMENTS` | 100000 | XML 节点数上限 |
| `SMART_SLICE_PARSER_MAX_XML_DEPTH` | 128 | XML 嵌套深度上限 |
| `SMART_SLICE_PARSER_MAX_TABLE_CELLS` | 1000000 | 表格单元格上限 |
| `SMART_SLICE_PARSER_MAX_MIME_PARTS` | 1000 | MIME 分段上限 |
| `SMART_SLICE_PARSER_MAX_MIME_DEPTH` | 32 | MIME 嵌套上限 |
| `SMART_SLICE_PARSER_MAX_ODF_TEXT_BYTES` | 33554432 | ODF 抽取文本上限 |
| `SMART_SLICE_OCR_ENABLED` | `0` | 开启本地 OCR（需 `ocr` extra） |

也可在调用校验器时直接传入 `ParserLimits` 实例覆盖。

---

## API 速查

| 函数 | 用途 |
|------|------|
| `slice_text(text, *, limit, patterns, with_filter, name)` | 切分内存字符串 |
| `slice_bytes(content, name, *, limit, ...)` | 从字节切分（完整选项） |
| `slice_path(path, *, limit, ...)` | 读文件后切分 |
| `extract_text(content, name, *, save_image)` | 仅抽取文本，不切分 |
| `detect_handler(name, content=None)` | 判断命中哪个 handler |
| `supported_extensions()` | handler → 扩展名映射 |
| `missing_dependencies()` | 未安装的可选 extras |
| `chunk_paragraphs(paragraphs, *, chunk_size)` | 面向 embedding 的二次分块 |
| `chunk(text, *, chunk_size)` | 便捷封装 |
| `split_document(...)` | 底层编排入口 |

构件：`SplitModel`、`smart_split_paragraph`、`filter_special_char`、`MarkChunkHandle`、`ParserLimits`、`ImageAsset`、`SPLIT_HANDLERS`、`MARKDOWN_HEADINGS`、`DEFAULT_PATTERNS`、`BLANK_LINE`、`LITERAL_PATTERNS`。

切片内部原理见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)；抽取过程见 [`docs/PORTING.md`](docs/PORTING.md)。

---

## 开发

```bash
git clone https://github.com/guangxiangdebizi/smart-slice.git
cd smart-slice
pip install -e ".[dev]"
pytest
ruff check smart_slice tests
```

---

## 许可

GPL-3.0，见 [LICENSE](LICENSE)。
