# smart-slice

**面向 RAG 管线的保真优先文档切片库。** 输入 30 个 handler 覆盖的 197 种文件扩展名，输出检索可用的段落，且原始字符全程可回溯、不丢失；同时输出文档内的图片，并已与所属段落完成对齐。

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-415%20passed-brightgreen.svg)]()
[![PyPI version](https://img.shields.io/pypi/v/smart-slice.svg)](https://pypi.org/project/smart-slice/)

```python
from smart_slice import slice_text

paragraphs = slice_text("# 章节\n\n正文内容", limit=1000)
# [{'title': '章节', 'content': '正文内容'}]
```

> 英文主文档见 [README.md](README.md)。

---

## 设计取向

多数切分器把文档当作字符流，按固定网格切块，由此丢失检索真正依赖的两样东西：**段落在文档结构中的位置**，以及**段落的原始措辞**。smart-slice 围绕五条规则构建。

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

### 三、实测高效，且如实计量

551 KB 结构化文档切片约 125 ms（≈4.4 MB/s）。热路径为单遍线性扫描判定块内可能存在的标题层级，跳过必然为空的正则扫描；可选 C 扩展（`pip install smart-slice[accel]`）原生执行该扫描。数据与推理见 [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md)。

该速度并非同类最快：纯字符切分器快约 36 倍。多出的时间换来了什么、何种场景值得付出，已在同一语料上与 `chonkie`、`langchain-text-splitters` 横向实测，见 [`docs/BENCHMARK.md`](docs/BENCHMARK.md)。结论摘要：smart-slice 是榜单中唯一在全部结构指标上取满分的实现——自然段落单元零破坏、标题链随行、代码围栏零切断、表格数据行与表头零分离。

### 四、无框架依赖

纯库实现：无 Django、无 ORM、无网络请求、不配置日志（仅通过 `smart_slice` logger 发出记录，路由交由宿主决定）。错误为 `SliceError`，带 HTTP 风格的 `.code`（`400` 输入问题、`500` 解析失败）。从文档抽出的图片经 `save_image` **回调**交付，库本身不做任何持久化；外链 `<img src="https://...">` 只登记地址，不发起下载。

### 五、图片同属文档内容

面向视觉语言（VL）embedding 模型时，图表、示意图与截图正是可用信息，纯文本索引会把它们整体丢弃。`slice_multimodal` 同时返回段落与图片，且已完成对齐——见[多模态切片](#多模态切片图片--段落)。

---

## 安装

核心安装刻意保持极小（仅含文本解码器）。各格式解析器以可选 extras 提供；解析器缺失时对应 handler 报"不支持"，不会崩溃。

```bash
pip install smart-slice          # 核心：文本/markdown/配置/源码类文件
pip install smart-slice[all]     # 下方全部格式
```

已发布至 PyPI：[`smart-slice`](https://pypi.org/project/smart-slice/)。`0.4.0` 的 wheel 与 sdist 同时挂在
[GitHub Release](https://github.com/guangxiangdebizi/smart-slice/releases/tag/v0.4.0)，由 `.github/workflows/release.yml` 从对应 tag 提交构建。

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
| `test` | `all` 全部内容 + `xlwt`（测试套件需要它来**写** .xls 夹具，`xlrd` 只能读） |
| `dev` | `test` + accel、pytest、pytest-cov、ruff、mypy |

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


**图片**可从上述所有容器中取出：`docx`/`pptx`/`pdf`/`xlsx`/`zip` 经各自 handler 产出，`html`/`epub`/`eml`/`mhtml`/`odf`/`fb2`/独立图片文件经多模态探测补齐。详见[多模态切片](#多模态切片图片--段落)。

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

## 多模态切片（图片 + 段落）

纯文本索引会丢掉文档中的图表、示意图与截图，而这些正是视觉语言（VL）embedding
模型可用的信息。`slice_multimodal` 同时返回段落与图片，且已完成对齐：

```python
from smart_slice import slice_multimodal

result = slice_multimodal(path="deck.pptx", limit=1000)

result.summary()          # '14 paragraphs, 6 images (6 attached, 0 unattached, 0.42 MB)'
result.coverage           # 1.0 —— 每张图片都落到了某个段落上

for row in result.paired():            # 仅返回带图片的段落
    print(row["title"], len(row["content"]), row["images"][0]["mime_type"])
    send_to_vl_model(row["title"], row["content"],
                     [i["data_uri"] for i in row["images"]])
```

`result.records()` / `result.paired()` 的每一项都是可直接序列化的结构：

```python
{
  "index": 3,
  "title": "Q3 Results  Revenue by region",
  "content": "Revenue grew ... ![chart](./oss/file/0198f...)",
  "images": [{
      "id": "0198f...", "file_name": "chart1.png", "mime_type": "image/png",
      "bytes": 41233, "sha256": "9d1e...", "width": 940, "height": 512,
      "source": "zip", "member": "ppt/media/image3.png", "match": "reference",
      "data_uri": "data:image/png;base64,iVBORw0..."     # with_data=True 时提供
  }]
}
```

### 图片的两条来源通道

两条通道互相独立，并按内容哈希互相去重：

1. **handler 通道**——`docx`、`pptx`、`pdf`、`xlsx`、`zip` 本就会经 `save_image`
   产出 `ImageAsset`。`slice_multimodal` 把 `ImageCollector` 装成该回调，图片由此
   被收集而非丢弃。
2. **探测通道**——当某文档的 handler 未产出任何图片时，`scan_media` 直接打开容器
   读取。此前只出文本的格式由此补齐：

| 容器 | 探测读取的内容 |
|------|----------------|
| `zip`（OOXML / ODF / EPUB / 任意压缩包） | `word/media/*`、`ppt/media/*`、`xl/media/*`、`Pictures/*` 及其他图片成员；随后扫描文本成员中的 `<img src>` 与 `![]()` 引用，并按引用所在成员解析相对路径 |
| `pdf` | `page.images`，并标注页码（需 `pdf` extra） |
| `mime`（`.eml` `.mhtml`） | 图片部件，按 `Content-ID` / `Content-Location` 建索引，正文中的 `cid:` 引用可解析 |
| `markup`（`.html` `.md` 等） | 解码 `data:` URI；外链 URL 只登记、**不下载** |
| 独立图片文件 | 文件本身即资产，固定归属其唯一段落 |

`probe="auto"`（默认）表示"仅当 handler 未产出图片时才探测"，因此 `.docx` 不会被
读两遍；`probe=True` 恒探测，`probe=False` 恒不探测。

### 图片与段落的对齐方式

四道匹配，信号由强到弱；每张已落位的图片在 `meta["match"]` 中记录命中方式，未落位
的一律进入 `result.unattached`，不做静默丢弃：

| 匹配道 | 依据 | 可靠性 |
|--------|------|--------|
| `reference` | handler 写入段落文本的 `./oss/file/{id}` | 精确 |
| `heading` | 图片在源标记中所处的标题 | 强 |
| `anchor` | 标签之前约 96 个字符的可见文本 | 启发式 |
| `document` | 独立图片文件本身即该文档 | 定义上精确 |

**切片文本永不被修改**——对齐是一次连接（join），不是改写；测试套件对每种容器都断言
`result.paragraphs == slice_bytes(...)`。

### 单独使用收集器

```python
from smart_slice import slice_bytes, ImageCollector

collector = ImageCollector()                       # 按 sha256 去重
rows = slice_bytes(data, "report.docx", save_image=collector)
collector.assets                                   # [ImageAsset, ...]
collector.duplicates                               # 被折叠掉的重复张数
```

`ImageCollector` 返回 `save_image` 契约要求的 `{新id: 保留id}` 映射，因此重复图片
收敛到同一 id，段落中所有 `./oss/file/{id}` 引用同步重写。

`ImageAsset` 新增计算属性（构造函数签名未变）：`.content`、`.size`、`.sha256`、
`.mime_type`、`.suffix`、`.dimensions`、`.width`、`.height`、`.data_uri()`、
`.to_dict(with_data=...)`。MIME 类型与像素尺寸取自魔术字节与文件头，无需 Pillow。

### 参数调节

```python
slice_multimodal(
    data, "page.html",
    min_side=20,            # 过滤项目符号、图标与追踪像素
    include_vector=True,    # 同时返回 SVG / EMF / WMF（默认关：VL 模型无法直接消费）
    include_external=False, # 不登记外链 <img src>
    max_images=512,         # 单文档图片上限
    max_image_bytes=64 << 20,
    keep_content=False,     # 仅保留元数据，切分后释放字节
)
```

### 整批语料

```python
from smart_slice import slice_many

report = slice_many(paths, backend="thread", concurrency=4, collect_images=True)
report.paragraphs        # 语义不变：按输入顺序汇总各文档段落
report.images            # 整批图片，跨文档去重
```

`collect_images=True` 需要 thread 或 serial 后端——收集器持有锁，锁不可 pickle，
因此与 `backend="process"` 同用会直接抛错，而不是静默返回空列表。

### 命令行

```bash
smart-slice media deck.pptx                  # 有哪些图、各归属哪个段落
smart-slice media page.html --json           # 机器可读
smart-slice media book.epub --out-dir imgs/  # 同时把图片字节写盘
smart-slice media scan.pdf --min-side 20 --probe always
```

---
## 命令行

```bash
smart-slice slice report.pdf --limit 1000 --format json -o out.json
smart-slice slice notes.md --format text --title-prefix --stats
smart-slice detect mystery.bin
smart-slice formats                       # 列出 handler 与缺失的可选依赖
smart-slice batch docs/*.pdf -j 4 --backend process --stats   # 整个语料库
smart-slice media deck.pptx --out-dir imgs/    # 取出图片，并给出所属段落
smart-slice cores                         # 本机核数、推导宽度、亲和性掩码
python -m smart_slice slice notes.md --format md
```

- `slice`：`--format {json,jsonl,text,md}`、`--limit`、`--no-filter`、`--title-prefix`、`-o/--output`、`--stats`（摘要写 stderr）。
- `batch`：在一套调度策略下切分多个文档：`-j/--concurrency {N,auto,cores,serial,2x}`、`--backend {thread,process,serial}`、`--pin-cores`、`--max-concurrency`、`--error-policy {raise_first,collect}`、`--unordered`、`--timeout`，以及 `slice` 的各项选项；`--format {json,jsonl,text,summary}`、`-o/--output`、`--stats`。退出码 1 会逐个点名失败文档，2 为用法错误。
- `media`：切分文档并列出其中每张图片及其所属段落：`--limit`、`--probe {auto,always,never}`、`--include-vector`、`--min-side`、`--no-external`、`--out-dir`（把图片字节写盘）、`--json`。
- `cores`：输出本机调度事实——可用核数、推导宽度、亲和性掩码、是否支持绑核，以及相关环境变量的当前取值（`--json` 供机器读取）。
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

### 批量切片与调度

单个文档用 `slice_path` 即可；整个语料库是调度问题。源平台切片链路底层的并发调度与
核分配已收编进本包，每个开关都是参数，可按调用逐次选择：

```python
from smart_slice import slice_paths

report = slice_paths(["a.pdf", "b.docx", "c.md"], limit=1000, concurrency=4)
report.ok                 # 全部成功时为 True
report.results            # 逐文档段落，顺序与输入严格一致
report.paragraphs         # 全部段落按输入顺序拼接
print(report.summary())   # 3/3 documents sliced in 412 ms (width=4, backend=thread, ...)
```

并发宽度是**变量**而非常量：可传整数（`4`），也可传 `"auto"`（默认）、`"cores"`、
`"serial"`，或核数的倍数（`"2x"`、`"0.5x"`）：

```python
slice_paths(paths, concurrency="cores")     # 用满可用核
slice_paths(paths, concurrency=2)           # 固定两个 worker
slice_paths(paths, backend="serial")        # 完全不建池
```

`"auto"` 沿用被移植过来的分配规则：超过六核的机器取 3，六核及以下取核数的一半，
且不低于 1。`available_cores()` 返回本进程**实际可用**的核数——Linux 读亲和性掩码，
因此 `taskset` 与 cgroup 限额会被如实计入，而不是宿主机的核数：

```python
from smart_slice import available_cores, auto_concurrency
available_cores(), auto_concurrency()       # 12 核笔记本上为 (12, 3)
```

核分配为可选项：`pin_cores=True` 时每个任务按轮转绑定到掩码中的一个核，任务结束
（含失败）后还原掩码（Linux 用 `sched_setaffinity`，Windows 用 `SetThreadAffinityMask`，
macOS 记日志后跳过）。`TaskOutcome.core` 记录每个任务实际拿到的核。

一个批次可混用多种输入形态，策略对象可复用：

```python
from smart_slice import SchedulerPolicy, SliceJob, slice_many

policy = SchedulerPolicy(concurrency=4, pin_cores=True, error_policy="collect")
report = slice_many([
    "report.pdf",                                    # 路径
    ("upload.docx", uploaded_bytes),                  # (文件名, 字节)
    SliceJob.from_path("big.md", limit=4000),         # 单文档覆盖
    {"name": "notes.md", "content": raw},             # 映射
    upload_handle,                                    # 任何带 .name/.read() 的对象
], policy=policy, limit=1000)

for outcome in report.outcomes:
    if not outcome.ok:
        log.warning("%s failed: %s", outcome.name, outcome.error)   # 状态码在 .error.code
```

失败语义沿用移植来源，并且可选：

| `error_policy` | 行为 |
|----------------|------|
| `"raise_first"`（默认） | 所有文档仍会被尝试，随后按**输入序号最小**的失败原样抛出原异常对象（traceback 完整），其余失败逐个记日志 |
| `"collect"` | 不抛异常，改由 `report.failures` / `outcome.error` 自查 |

两种执行后端，选后端比选宽度更影响结果：

| 后端 | 适用场景 | 代价 |
|------|----------|------|
| `"thread"`（默认） | worker 内还有 I/O（写库、调 embedding、下载），或需要传回调（`save_image`、`progress_hook`、`length_fn` 里的分词器） | 无额外开销；但受 GIL 限制，纯 Python 切片本身不会变快 |
| `"process"` | 纯 CPU 密集、输入可序列化的大批量 | 解释器启动 + 字节双向 pickle；输入必须可 pickle |

12 核笔记本实测（每篇 551 KB 结构化 markdown，CPython 3.12，Windows），
复现命令 `python scripts/benchmark.py --batch`：

| 批量 | serial | threads x4 | processes x4 |
|------|--------|------------|--------------|
| 12 篇（6.6 MB） | 1.00x | 1.07x | 0.69x |
| 48 篇（26.5 MB） | 1.00x | 0.95x | **1.61x** |

即：线程只在 GIL 被释放的地方换来重叠；进程后端在数十篇以上的 CPU 密集批次才换来
真实吞吐。数据与推导见 [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md)。

底层 `run_parallel(items, worker, policy)` 就是这套「保序 + 错误隔离」的并行映射，
不含任何切片逻辑，需要给自己的 worker 套调度时可直接使用。

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

批量调度的配置方式相同，且每个变量都会被对应的调用参数覆盖：

| 变量 | 默认值 | 含义 |
|------|--------|------|
| `SMART_SLICE_CONCURRENCY` | `auto` | 批量宽度：整数、`auto`、`cores`、`serial`，或倍数（`2x`） |
| `SMART_SLICE_MAX_CONCURRENCY` | `32` | 推导宽度的上限 |
| `SMART_SLICE_SCHEDULER_BACKEND` | `thread` | `thread`、`process` 或 `serial` |
| `SMART_SLICE_PIN_CORES` | `0` | 为每个任务按轮转分配一个核 |

也可在调用校验器时直接传入 `ParserLimits` 实例，或向 `slice_many` / `run_parallel`
传入 `SchedulerPolicy` 覆盖。

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
| `slice_many(inputs, *, concurrency, backend, pin_cores, collect_images, ...)` | 一次调用切分整个批次，返回 `BatchReport` |
| `slice_paths(paths, ...)` | 面向路径列表的 `slice_many` |
| `slice_multimodal(content=None, name=None, *, path, limit, probe, ...)` | 段落 + 图片，已对齐，返回 `MultimodalResult` |
| `slice_path_multimodal(path, ...)` | 面向磁盘文件的 `slice_multimodal` |
| `extract_media(content, name, ...)` / `scan_media(...)` | 仅从容器取图，不切分 |
| `attach_images(paragraphs, assets)` | 把图片对齐到段落 |
| `ImageCollector` | 带去重的 `save_image` 收集器，遵守 id 重映射契约 |
| `MultimodalResult` / `MultimodalParagraph` / `MediaExtraction` | 多模态结果对象 |
| `container_kind(content, name)` | 探测将启用哪个容器读取器 |
| `run_parallel(items, worker, policy)` | 保序 + 错误隔离的通用并行映射 |
| `SchedulerPolicy` / `SliceJob` / `BatchReport` / `TaskOutcome` | 调度配置与结果对象 |
| `available_cores()` / `auto_concurrency()` / `resolve_concurrency()` | 核发现与宽度推导 |
| `use_policy(policy)` | 在作用域内发布调度策略（contextvar 隔离） |
| `split_document(...)` | 底层编排入口 |

构件：`SplitModel`、`smart_split_paragraph`、`filter_special_char`、`MarkChunkHandle`、`ParserLimits`、`ImageAsset`（含 `.mime_type`、`.sha256`、`.dimensions`、`.data_uri()`、`.to_dict()`）、`SPLIT_HANDLERS`、`MARKDOWN_HEADINGS`、`DEFAULT_PATTERNS`、`BLANK_LINE`、`LITERAL_PATTERNS`。

切片内部原理见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)；同类库横向实测见 [`docs/BENCHMARK.md`](docs/BENCHMARK.md)；抽取过程见 [`docs/PORTING.md`](docs/PORTING.md)。

---

## 开发

```bash
git clone https://github.com/guangxiangdebizi/smart-slice.git
cd smart-slice
pip install -e ".[dev]"
pytest                                   # 415 项测试，全离线
ruff check smart_slice tests

python scripts/benchmark.py              # 复现 docs/PERFORMANCE.md
pip install chonkie langchain-text-splitters tiktoken
python scripts/benchmark_peers.py        # 复现 docs/BENCHMARK.md
```

---

## 许可

GPL-3.0，见 [LICENSE](LICENSE)。
