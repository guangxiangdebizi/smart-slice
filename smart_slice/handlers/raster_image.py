# coding=utf-8
"""ExtendedImageSplitHandle：补齐 ImageSplitHandle 未覆盖、但 Pillow 可解码的位图格式。

既有 ImageSplitHandle 只认 .jpg/.jpeg/.png/.gif/.bmp/.tiff/.tif/.webp（+ 可选
.heic/.heif）。实际位图容器远不止这些：.ico/.tga/.pcx/.dds/.sgi/.ppm/.pgm/.pbm/
.pnm/.im/.icns/.qoi/.jfif/.jpe/.apng/.xbm 等 Pillow 均可解码。这些扩展名此前落在
TextSplitHandle 的排除表里（.ico/.raw）或无人认领，产出 400 不支持。

本类**继承** ImageSplitHandle 并只覆写 support() 的扩展名判定，因此：
- handle/get_content 完全复用父类实现（段落语义、save_image 回调、OCR 降级一致）；
- service.split_document 的 ``isinstance(handle, ImageSplitHandle)`` 分支天然命中，
  image_text_extractor 照常传入，扩展位图与既有图片走同一条 OCR 链路；
- 不引入第二套图片实现。

刻意不含的扩展名：.svg（矢量，走 SvgSplitHandle 抽可读文本）、.raw（各家相机私有
格式，Pillow 无通用解码器，维持拒绝语义）、.heic/.heif（由父类依 pillow-heif
可用性认领，本类不重复）。

依赖：Pillow（extra `pdf`/`image` 均含）。缺失时 support() 返回 False，由公共层
报 400 不支持，而不是 import 阶段崩溃。
"""
from smart_slice._logging import get_logger

_log = get_logger("image_ext")

from smart_slice.handlers.image import ImageSplitHandle

#: Pillow 可解码、但既有 ImageSplitHandle 未认领的位图扩展名。
EXTRA_IMAGE_EXTENSIONS = (
    ".ico", ".cur",
    ".tga", ".icb", ".vda", ".vst",
    ".pcx",
    ".dds",
    ".sgi", ".rgb", ".rgba", ".bw",
    ".ppm", ".pgm", ".pbm", ".pnm", ".pfm",
    ".im",
    ".icns",
    ".qoi",
    ".jfif", ".jpe",
    ".apng",
    ".xbm",
    ".psd",
)


class ExtendedImageSplitHandle(ImageSplitHandle):
    """认领扩展位图格式；handle/get_content 继承自 ImageSplitHandle。"""

    def support(self, file, get_buffer):
        # 不 gate Pillow：与父类 ImageSplitHandle 一致——基础链路只产出引用源图的
        # 文件名占位段落（图片进入向量化管道），真正解码只发生在 heif/OCR 分支。
        return file.name.lower().endswith(EXTRA_IMAGE_EXTENSIONS)
