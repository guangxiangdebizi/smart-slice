"""Drive the CLI as a subprocess, then do second-stage embedding chunking.

Run: python examples/cli_and_chunk.py
"""
import os
import subprocess
import sys
import tempfile

from smart_slice import chunk_paragraphs, slice_text

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli() -> None:
    """Invoke `python -m smart_slice formats` and `slice` on a temp markdown file."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "smart_slice", "formats"],
        capture_output=True, text=True, encoding="utf-8", cwd=REPO, env=env,
    )
    print("== CLI: formats ==")
    print("\n".join(proc.stdout.splitlines()[:6]))
    print(f"... ({len(proc.stdout.splitlines())} lines total)\n")

    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write("# Title\n\nFirst paragraph body.\n\n## Sub\n\nSecond paragraph body.")
        path = fh.name
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "smart_slice", "slice", path,
             "--format", "json", "--limit", "1000", "--stats"],
            capture_output=True, text=True, encoding="utf-8", cwd=REPO, env=env,
        )
        print("== CLI: slice --format json ==")
        print(proc.stdout.strip())
        print(f"(stderr) {proc.stderr.strip()}")
    finally:
        os.unlink(path)


def run_chunking() -> None:
    """Slice into paragraphs, then chunk them for an embedding window."""
    long_body = "This is a sentence about the system. " * 40
    doc = f"# Chapter\n\n{long_body}"
    paragraphs = slice_text(doc, limit=2000)
    print(f"\n== chunking: {len(paragraphs)} paragraph(s) -> embedding chunks ==")
    for p in paragraphs:
        print(f"  paragraph: title={p['title']!r} chars={len(p['content'])}")
    chunks = chunk_paragraphs(paragraphs, chunk_size=200)
    print(f"  -> {len(chunks)} chunk(s), sizes={[len(c) for c in chunks][:8]}")
    assert all(len(c) <= 200 for c in chunks)


def main() -> None:
    run_cli()
    run_chunking()


if __name__ == "__main__":
    main()