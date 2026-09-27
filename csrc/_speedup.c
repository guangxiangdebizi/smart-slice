/*
 * smart-slice optional C accelerator.
 *
 * Scope is deliberately narrow: one O(n) scan that classifies every candidate
 * markdown ATX heading line in a single pass.  The Python side then filters the
 * candidates by heading level instead of running six separate regular expressions
 * over the whole document at every recursion level.
 *
 * Why this is safe: the scan reproduces the *exact* acceptance rule of the
 * canonical heading patterns (see scan_heading_candidates below), and it is only
 * used when the caller's pattern list is recognised as that canonical set - any
 * custom pattern falls back to the regular-expression path.  A pure-Python
 * implementation of the same scan (smart_slice._speedup_py) is used when the
 * extension is unavailable, and tests/test_c_speedup.py asserts all three paths
 * agree on generated corpora.
 *
 * Build: optional.  `pip install smart-slice[accel]` or `python setup.py build_ext`.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

/*
 * scan_heading_candidates(text) -> list[(line_start, line_end, hashes)]
 *
 * Reports each line that could be an ATX heading, in document order.
 *
 * Acceptance rule (matches the canonical patterns exactly):
 *   - the line's first character is '#' with NO leading whitespace, because the
 *     patterns anchor with (?<=^) / (?<=\n) immediately before the hashes;
 *   - it is followed by a run of '#' (1..n) and then a single ASCII space
 *     (U+0020).  A tab does not qualify: the patterns all require a literal
 *     space, which is also why "## " never matches a "###" line.
 *
 * line_start is the index of the first '#'; line_end is one past the last
 * character of the line (the newline itself excluded).  The caller slices
 * text[line_start:line_end] and truncates to 255 characters, which reproduces
 * the `r[0:255]` behaviour of the regex path.
 *
 * Callers pass the code-fence-masked text, so a '#' inside a fence has already
 * been turned into a space and cannot be reported.
 */
static PyObject *
scan_heading_candidates(PyObject *module, PyObject *arg)
{
    PyObject *result;
    Py_ssize_t length;
    Py_ssize_t pos = 0;
    Py_ssize_t line_start = 0;
    int kind;
    const void *data;

    if (!PyUnicode_Check(arg)) {
        PyErr_SetString(PyExc_TypeError, "scan_heading_candidates expects a str");
        return NULL;
    }
    if (PyUnicode_READY(arg) < 0) {
        return NULL;
    }

    length = PyUnicode_GET_LENGTH(arg);
    kind = PyUnicode_KIND(arg);
    data = PyUnicode_DATA(arg);

    result = PyList_New(0);
    if (result == NULL) {
        return NULL;
    }

    while (line_start < length) {
        Py_ssize_t line_end = line_start;
        Py_ssize_t cursor = line_start;
        Py_ssize_t hashes = 0;
        Py_UCS4 ch;

        /* locate the end of this line */
        while (line_end < length) {
            ch = PyUnicode_READ(kind, data, line_end);
            if (ch == '\n') {
                break;
            }
            line_end++;
        }

        /* count the leading '#' run; it must start at the very first character */
        while (cursor < line_end && PyUnicode_READ(kind, data, cursor) == '#') {
            cursor++;
            hashes++;
        }

        if (hashes > 0 && cursor < line_end
            && PyUnicode_READ(kind, data, cursor) == ' ') {
            PyObject *item = Py_BuildValue("(nnn)", line_start, line_end, hashes);
            if (item == NULL) {
                goto error;
            }
            if (PyList_Append(result, item) < 0) {
                Py_DECREF(item);
                goto error;
            }
            Py_DECREF(item);
        }

        line_start = line_end + 1;
    }

    return result;

error:
    Py_DECREF(result);
    return NULL;
}


static PyMethodDef speedup_methods[] = {
    {"scan_heading_candidates", scan_heading_candidates, METH_O,
     "scan_heading_candidates(text) -> list[(line_start, line_end, hashes)]\n\n"
     "Single-pass scan for candidate markdown ATX heading lines.\n"
     "Reproduces the acceptance rule of smart_slice.patterns.MARKDOWN_HEADINGS."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef speedup_module = {
    PyModuleDef_HEAD_INIT,
    "smart_slice._speedup",
    "Optional C accelerator for smart-slice (a pure-Python fallback exists).",
    -1,
    speedup_methods,
    NULL, NULL, NULL, NULL
};

PyMODINIT_FUNC
PyInit__speedup(void)
{
    return PyModule_Create(&speedup_module);
}