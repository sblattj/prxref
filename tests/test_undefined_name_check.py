"""Tier 0 of error verification (#126): :func:`prxref.quality.apply_undefined_name_check`.

An active, model-made ``error`` in a Python file that claims a name is not
imported or not defined is dropped when the head file binds every name it
cites at module scope. The drop reason names the binding's ``file:line``.
Everything else is returned unchanged, in order.
"""

from __future__ import annotations

from dataclasses import replace

from prxref.quality import apply_undefined_name_check, module_bindings
from prxref.triage import Finding

SETTINGS = "app/settings.py"
CASE1_BODY = (
    "`os` may not be imported in this module. The new `DATA_DIR = os.path.join(BASE, 'data')` "
    "line would raise a NameError at import time."
)
HEAD_IMPORT_OS = '"""Settings."""\nfrom __future__ import annotations\n\nimport os\n\nBASE = "/srv"\n'


def _f(**kw) -> Finding:
    base = dict(
        file=SETTINGS,
        line=6,
        severity="error",
        confidence=0.9,
        title="`os` used without import",
        body=CASE1_BODY,
    )
    base.update(kw)
    return Finding(**base)


def _reader(files: dict[str, str]):
    return lambda path: files.get(path)


class TestCase1:
    def test_a_name_the_head_file_imports_is_dropped_naming_file_and_line(self):
        out = apply_undefined_name_check([_f()], _reader({SETTINGS: HEAD_IMPORT_OS}))
        assert out[0].drop_reason == f'contradicted by head file: {SETTINGS}:4 binds "os"'

    def test_import_dotted_binds_the_top_package(self):
        head = "import sys\nimport os.path\n"
        out = apply_undefined_name_check([_f()], _reader({SETTINGS: head}))
        assert out[0].drop_reason == f'contradicted by head file: {SETTINGS}:2 binds "os"'

    def test_from_import_as_binds_the_alias_not_the_module(self):
        head = "from os import path as p\n"
        claim_p = _f(title="`p` is not defined", body="`p` is not defined here, so this raises NameError.")
        out = apply_undefined_name_check([_f(), claim_p], _reader({SETTINGS: head}))
        assert out[0].drop_reason is None
        assert out[1].drop_reason == f'contradicted by head file: {SETTINGS}:1 binds "p"'

    def test_import_as_binds_the_alias(self):
        head = "import numpy as np\n"
        claim = _f(title="`np` not imported", body="`np` is not imported; NameError.")
        out = apply_undefined_name_check([claim], _reader({SETTINGS: head}))
        assert out[0].drop_reason == f'contradicted by head file: {SETTINGS}:1 binds "np"'

    def test_parenthesized_multiline_from_import(self):
        head = "from typing import (\n    Any,\n    Mapping,\n)\n"
        claim = _f(title="`Mapping` undefined", body="`Mapping` is an undefined name here.")
        out = apply_undefined_name_check([claim], _reader({SETTINGS: head}))
        assert out[0].drop_reason is not None
        assert '"Mapping"' in out[0].drop_reason

    def test_module_level_try_import_counts(self):
        head = "try:\n    import yaml\nexcept ImportError:\n    yaml = None\n"
        claim = _f(title="`yaml` never imported", body="`yaml` is never imported.")
        out = apply_undefined_name_check([claim], _reader({SETTINGS: head}))
        assert out[0].drop_reason == f'contradicted by head file: {SETTINGS}:2 binds "yaml"'

    def test_several_names_all_bound_are_joined(self):
        head = "import os\nimport json\n"
        claim = _f(title="Missing imports", body="`os` and `json` are not imported: NameError.")
        out = apply_undefined_name_check([claim], _reader({SETTINGS: head}))
        assert out[0].drop_reason == (f'contradicted by head file: {SETTINGS}:1 binds "os"; {SETTINGS}:2 binds "json"')


class TestARealErrorStays:
    def test_the_head_file_does_not_bind_the_name(self):
        head = "import sys\n\nDATA = os.path.join('a', 'b')\n"
        out = apply_undefined_name_check([_f()], _reader({SETTINGS: head}))
        assert out[0].drop_reason is None and out[0].severity == "error"

    def test_a_function_local_import_does_not_count(self):
        head = "def load():\n    import os\n    return os.getcwd()\n\nX = os.sep\n"
        out = apply_undefined_name_check([_f()], _reader({SETTINGS: head}))
        assert out[0].drop_reason is None

    def test_one_of_two_names_unbound_keeps_the_finding(self):
        head = "import os\n"
        claim = _f(body="`os` and `json` are not imported: NameError.")
        out = apply_undefined_name_check([claim], _reader({SETTINGS: head}))
        assert out[0].drop_reason is None

    def test_no_head_file(self):
        out = apply_undefined_name_check([_f()], _reader({}))
        assert out[0].drop_reason is None

    def test_a_reader_that_raises_keeps_the_finding(self):
        def boom(path):
            raise OSError("gone")

        out = apply_undefined_name_check([_f()], boom)
        assert out[0].drop_reason is None


class TestOnlyEligibleFindingsAreChecked:
    def test_a_warning_is_not_checked(self):
        out = apply_undefined_name_check([_f(severity="warning")], _reader({SETTINGS: HEAD_IMPORT_OS}))
        assert out[0].drop_reason is None

    def test_a_deterministic_finding_is_not_checked(self):
        det = _f(body=CASE1_BODY + " (deterministic check, no model)")
        out = apply_undefined_name_check([det], _reader({SETTINGS: HEAD_IMPORT_OS}))
        assert out[0].drop_reason is None

    def test_a_sub_floor_finding_is_not_checked(self):
        out = apply_undefined_name_check(
            [_f(confidence=0.3)],
            _reader({SETTINGS: HEAD_IMPORT_OS}),
            confidence_floor=0.5,
        )
        assert out[0].drop_reason is None

    def test_an_already_dropped_finding_keeps_its_reason(self):
        dropped = _f(drop_reason='hedged: "may not"')
        out = apply_undefined_name_check([dropped], _reader({SETTINGS: HEAD_IMPORT_OS}))
        assert out[0].drop_reason == 'hedged: "may not"'

    def test_a_non_python_file_is_not_checked(self):
        js = _f(file="app/settings.js")
        out = apply_undefined_name_check([js], _reader({"app/settings.js": "import os\n"}))
        assert out[0].drop_reason is None

    def test_a_claim_of_another_shape_is_not_checked(self):
        other = _f(title="`os.path.join` gets a None", body="`os` path join receives None and raises TypeError.")
        out = apply_undefined_name_check([other], _reader({SETTINGS: HEAD_IMPORT_OS}))
        assert out[0].drop_reason is None

    def test_no_reader_returns_the_findings_unchanged(self):
        fs = [_f(), _f(line=7)]
        assert apply_undefined_name_check(fs, None) == fs


class TestShape:
    def test_order_and_length_are_kept_and_the_input_is_not_mutated(self):
        keep = _f(file="app/other.py", body="`json` is not imported: NameError.")
        fs = [keep, _f(), replace(keep, line=9)]
        out = apply_undefined_name_check(fs, _reader({SETTINGS: HEAD_IMPORT_OS, "app/other.py": "x = 1\n"}))
        assert len(out) == 3
        assert [f.file for f in out] == [f.file for f in fs]
        assert out[0] is fs[0] and out[2] is fs[2]
        assert out[1].drop_reason is not None and fs[1].drop_reason is None


class TestModuleBindings:
    def test_bindings_and_their_lines(self):
        text = (
            "from __future__ import annotations\n"  # 1
            "import os, json as j\n"  # 2
            "from os import path as p\n"  # 3
            "\n"  # 4
            "class C:\n"  # 5
            "    import inner\n"  # 6
            "    attr = 1\n"  # 7
            "\n"  # 8
            "def f():\n"  # 9
            "    import local\n"  # 10
            "\n"  # 11
            "if True:\n"  # 12
            "    import yaml\n"  # 13
            "X = 3\n"  # 14
        )
        got = module_bindings(text)
        assert got["os"] == 2 and got["j"] == 2 and got["p"] == 3
        assert got["C"] == 5 and got["f"] == 9 and got["yaml"] == 13 and got["X"] == 14
        assert "json" not in got and "path" not in got
        assert "inner" not in got and "local" not in got and "attr" not in got
