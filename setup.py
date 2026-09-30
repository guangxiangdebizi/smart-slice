# coding=utf-8
"""Optional C accelerator build.

The wheel published on PyPI is pure Python and needs no compiler: at runtime
``smart_slice._accel`` falls back to ``smart_slice._speedup_py`` when
``smart_slice._speedup`` is not importable.  Building the extension is a pure
optimisation for people who want it:

    pip install ".[accel]"          # or
    python setup.py build_ext --inplace

The build needs a C compiler.  ``zig cc`` works on Windows without installing
Visual Studio, which is what ``tools_build_ext.py`` assumes; any regular compiler
(msvc, gcc, clang) works too once it is the active ``CC``.

This setup.py only builds the C extension.  Distribution metadata (name, version,
dependencies, packaging of ``smart_slice``) lives in pyproject.toml, so we declare
``py_modules=[]`` here to stop setuptools' flat-layout auto-discovery from trying
to package ``csrc`` as well.
"""
from setuptools import Extension, setup

setup(
    name="smart-slice-accel",  # build-only stub; the real metadata is in pyproject.toml
    version="0.5.1",
    py_modules=[],  # do not auto-discover packages; the extension lands in smart_slice/
    ext_modules=[
        Extension(
            "smart_slice._speedup",
            sources=["csrc/_speedup.c"],
            optional=True,  # a build failure must never break installation
        ),
    ],
)