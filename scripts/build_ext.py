# coding=utf-8
"""Build the optional C accelerator in place.

    python scripts/build_ext.py

Why not just ``setup.py build_ext``?  On Windows, setuptools ignores the ``CC``
environment variable and shells out to ``cl.exe`` unconditionally, so a machine
without Visual Studio cannot build even when a perfectly good compiler is
available.  This script drives ``zig cc`` (installable from PyPI via the ``accel``
extra, no Visual Studio needed) directly, and falls back to the normal setuptools
path on platforms where a system compiler is already configured.

Either way the extension is optional: ``smart_slice._accel`` uses the identical
pure-Python scan when it is absent, and results do not change.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "csrc" / "_speedup.c"
MODULE = "smart_slice._speedup"


def find_zig() -> str | None:
    """Locate the zig executable shipped by the ``ziglang`` PyPI package."""
    found = shutil.which("zig")
    if found:
        return found
    try:
        import ziglang
    except ImportError:
        return None
    candidate = Path(ziglang.__file__).parent / ("zig.exe" if sys.platform == "win32" else "zig")
    return str(candidate) if candidate.exists() else None


def output_name() -> str:
    """Platform-specific extension file name, e.g. _speedup.cp312-win_amd64.pyd."""
    suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    return MODULE.rsplit(".", 1)[1] + suffix


def build_with_zig(zig: str) -> int:
    include = sysconfig.get_path("include")
    out = ROOT / "smart_slice" / output_name()
    command = [zig, "cc", "-shared", "-O3", "-I", include, str(SOURCE)]
    if sys.platform == "win32":
        # Windows extension modules link against the interpreter's import library.
        libdir = Path(sysconfig.get_config_var("LIBDIR") or Path(sys.base_prefix) / "libs")
        if not libdir.exists():
            libdir = Path(sys.base_prefix) / "libs"
        version = f"python{sys.version_info.major}{sys.version_info.minor}"
        command += ["-L", str(libdir), f"-l{version}"]
    command += ["-o", str(out)]
    print("running:", " ".join(command))
    status = subprocess.call(command)
    if status == 0:
        print(f"built {out.relative_to(ROOT)}")
    return status


def build_with_setuptools() -> int:
    print("running: setup.py build_ext --inplace")
    return subprocess.call([sys.executable, "setup.py", "build_ext", "--inplace"], cwd=ROOT)


def main() -> int:
    if not SOURCE.exists():
        print(f"error: {SOURCE} not found", file=sys.stderr)
        return 2

    zig = find_zig()
    if zig:
        print(f"using zig cc: {zig}")
        return build_with_zig(zig)

    print("zig not found; falling back to setuptools with the system compiler")
    return build_with_setuptools()


if __name__ == "__main__":
    raise SystemExit(main())