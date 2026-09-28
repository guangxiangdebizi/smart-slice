# coding=utf-8
"""Multimodal slicing: paragraphs and the pictures that belong to them.

    python examples/multimodal.py [output-directory]

Builds four small documents offline (a markdown page with an inline data-URI
figure, an HTML page with a remote image, a packaged zip, and a standalone PNG),
then shows every part of the multimodal API: the one-call entry point, the
collector used on its own, the container probe, the paragraph join, and the
batch form.  No network, no Pillow, no optional extras required.
"""
import base64
import logging
import os
import struct
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from smart_slice import (
    ImageCollector,
    attach_images,
    scan_media,
    slice_bytes,
    slice_many,
    slice_multimodal,
)

# The zip handler logs every inner member it cannot parse, and a loose .png inside
# an archive is one of them (the multimodal probe recovers those instead). An
# example does not need that noise on stderr - and since a library must never
# configure logging for its caller, this line belongs here and not in smart_slice.
logging.getLogger("smart_slice").setLevel(logging.CRITICAL)


def make_png(width=48, height=24, rgb=(70, 130, 200)):
    """A real PNG built from the specification, so the example needs no Pillow."""

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(bytearray(rgb)) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def build_documents(directory):
    chart = make_png()
    logo = make_png(16, 16, (200, 60, 60))
    encoded = base64.b64encode(chart).decode()

    markdown = (
        "# Quarterly Review\n\n"
        "Revenue grew across every region, with the strongest movement in EMEA.\n\n"
        "## Revenue by region\n\n"
        "The chart below breaks the quarter down by region and product line.\n\n"
        f"![revenue](data:image/png;base64,{encoded})\n\n"
        "## Risks\n\n"
        "Two supply contracts expire before the next reporting period.\n"
    )
    html = (
        "<html><body><h1>Field Guide</h1>"
        "<p>Installation takes about ten minutes on a clean machine.</p>"
        '<img src="https://cdn.example.com/diagram.png" alt="diagram"/>'
        "<h2>Troubleshooting</h2>"
        "<p>Restart the service before collecting logs.</p>"
        "</body></html>"
    )
    paths = {}
    paths["review.md"] = markdown.encode()
    paths["guide.html"] = html.encode()
    paths["chart.png"] = chart

    package = os.path.join(directory, "handbook.zip")
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("handbook.md",
                         "# Handbook\n\nSetup notes for the field team, with the logo below.\n\n"
                         "![logo](assets/logo.png)\n")
        archive.writestr("assets/logo.png", logo)
        archive.writestr("assets/chart.png", chart)
    paths["handbook.zip"] = None

    for name, payload in list(paths.items()):
        if payload is None:
            continue
        target = os.path.join(directory, name)
        with open(target, "wb") as handle:
            handle.write(payload)
        paths[name] = target
    paths["handbook.zip"] = package
    return paths


def rule(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def show(result, indent="  "):
    print(f"{indent}{result.summary()}   coverage={result.coverage:.2f}")
    for group in result.groups:
        if not group.images:
            continue
        print(f"{indent}paragraph {group.index} [{group.title[:48]}]")
        for image in group.images:
            size = f"{image.width}x{image.height}" if image.dimensions else "?"
            print(f"{indent}  - {image.mime_type:<12} {size:<9} {image.size:>7} B  "
                  f"match={image.meta.get('match'):<9} source={image.meta.get('source')}")
    for image in result.unattached:
        where = image.meta.get("member") or image.meta.get("src") or image.file_name
        print(f"{indent}  ! unattached: {image.mime_type} {where}")


def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    directory = args[0] if args else tempfile.mkdtemp(prefix="smart-slice-mm-")
    os.makedirs(directory, exist_ok=True)
    paths = build_documents(directory)
    print(f"documents in {directory}")

    rule("1. slice_multimodal: one call, paragraphs and pictures joined")
    for name, path in paths.items():
        with open(path, "rb") as handle:
            content = handle.read()
        print(f"\n{name}")
        show(slice_multimodal(content, name, limit=1000))

    rule("2. what a VL embedding model would receive (paired records)")
    with open(paths["review.md"], "rb") as handle:
        result = slice_multimodal(handle.read(), "review.md", limit=1000)
    for row in result.paired():
        print(f"  title   : {row['title']}")
        print(f"  content : {row['content'][:60]}...")
        for image in row["images"]:
            print(f"  image   : {image['mime_type']} {image['width']}x{image['height']} "
                  f"sha256={image['sha256'][:12]} data_uri={image['data_uri'][:38]}...")

    rule("3. ImageCollector on its own (plain slice_bytes)")
    collector = ImageCollector()
    with open(paths["handbook.zip"], "rb") as handle:
        rows = slice_bytes(handle.read(), "handbook.zip", limit=1000, save_image=collector)
    print(f"  paragraphs        : {len(rows)}")
    print(f"  collected images  : {len(collector.assets)}  (duplicates folded: {collector.duplicates})")
    print(f"  collector         : {collector!r}")
    print(f"  remap returned    : {collector.remapped}")

    rule("4. scan_media: read a container without slicing it")
    with open(paths["guide.html"], "rb") as handle:
        report = scan_media(handle.read(), "guide.html", include_external=True)
    print(f"  {report.summary()}")
    for image in report.assets:
        kind = "inline" if image.content else "remote (not fetched)"
        print(f"    - {image.mime_type:<10} {kind:<22} src={image.meta.get('src') or '-'} "
              f"heading={image.meta.get('heading')!r}")

    rule("5. attach_images: join pictures to paragraphs you sliced yourself")
    paragraphs = [
        {"title": "Setup", "content": "Install the agent, then restart the service."},
        {"title": "Diagram", "content": "The wiring diagram below shows both paths."},
    ]
    from smart_slice.types import ImageAsset

    assets = [
        ImageAsset(id="a1", file_name="wiring.png",
                   meta={"content": make_png(30, 20), "heading": "Diagram"}),
        ImageAsset(id="a2", file_name="logo.png",
                   meta={"content": make_png(8, 8), "anchor": "restart the service"}),
    ]
    joined = attach_images(paragraphs, assets)
    show(joined)

    rule("6. slice_many(collect_images=True): a whole corpus, one shared collector")
    report = slice_many(list(paths.values()), backend="thread", concurrency=4,
                        collect_images=True, limit=1000)
    print(f"  {report.summary()}")
    print(f"  paragraphs: {len(report.paragraphs)}   images: {len(report.images)}")
    for image in report.images:
        print(f"    - {image.mime_type:<12} {str(image.dimensions):<10} {image.size:>7} B  "
              f"{image.file_name}")

    rule("7. probe=True: pictures the handlers never referenced")
    with open(paths["handbook.zip"], "rb") as handle:
        zipped = handle.read()
    print("  probe='auto' (default) - a handler already produced an image, so the")
    print("  container is not read a second time:")
    show(slice_multimodal(zipped, "handbook.zip", limit=1000), indent="    ")
    print("\n  probe=True - read the archive anyway; the unreferenced chart appears:")
    show(slice_multimodal(zipped, "handbook.zip", limit=1000, probe=True), indent="    ")

    rule("8. writing the pictures to disk")
    out = os.path.join(directory, "extracted")
    os.makedirs(out, exist_ok=True)
    with open(paths["handbook.zip"], "rb") as handle:
        result = slice_multimodal(handle.read(), "handbook.zip", limit=1000)
    for index, image in enumerate(result.images):
        if not image.content:
            continue
        label = f"{index:02d}-{image.file_name}"
        if not label.lower().endswith(image.suffix):
            label += image.suffix
        target = os.path.join(out, label)
        with open(target, "wb") as handle:
            handle.write(image.content)
        print(f"  wrote {target}")
    print(f"\n  stats: {result.stats()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())