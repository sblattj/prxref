"""Issue #149: a body citation of a sibling site never moves a valid line."""

from __future__ import annotations

from prxref.orchestrator import added_lines_by_file
from prxref.quality import apply_line_align
from prxref.triage import Finding, parse_unified_diff

PATH = "app/reports.py"


def _numbered_file() -> list[str]:
    lines = [f"# filler {i}" for i in range(1, 31)]
    lines[0] = "import db"
    lines[1] = ""
    lines[2] = "def sales_report(region):"
    lines[3] = "    rows = []"
    lines[4] = "    cur = db.cursor()"
    lines[5] = "    # gather"
    lines[6] = "    total = 0"
    lines[7] = "    # build the query"
    lines[8] = '    sql = f"SELECT * FROM sales WHERE region = {region}"'
    lines[9] = "    cur.execute(sql)"
    lines[10] = "    rows = cur.fetchall()"
    lines[11] = "    return rows"
    lines[12] = ""
    lines[13] = "def refund_report(region):"
    lines[14] = '    sql = f"SELECT * FROM refunds WHERE region = {region}"'
    lines[15] = "    cur = db.cursor()"
    lines[16] = "    cur.execute(sql)"
    lines[17] = "    rows = cur.fetchall()"
    lines[18] = "    return rows"
    lines[19] = ""
    lines[20] = '    sql = f"SELECT * FROM audits WHERE region = {region}"'
    lines[21] = "    cur = db.cursor()"
    lines[22] = "    cur.execute(sql)"
    lines[23] = "    return cur.fetchall()"
    return lines[:24]


def _diff() -> str:
    body = _numbered_file()
    out = [
        f"diff --git a/{PATH} b/{PATH}",
        "new file mode 100644",
        "--- /dev/null",
        f"+++ b/{PATH}",
        f"@@ -0,0 +1,{len(body)} @@",
    ]
    out.extend("+" + ln for ln in body)
    return "\n".join(out) + "\n"


def _align(line: int, title: str, body: str) -> int:
    files = parse_unified_diff(_diff())
    finding = Finding(
        file=PATH,
        line=line,
        severity="warning",
        confidence=0.8,
        title=title,
        body=body,
    )
    return apply_line_align(
        [finding], added_lines_by_file(files), files=files
    )[0].line


TITLE = "SQL built with an f-string"


def test_fixture_has_the_fstring_sql_at_9_15_21():
    text = _numbered_file()
    assert [i for i, ln in enumerate(text, 1) if "sql = f" in ln] == [9, 15, 21]


def test_no_citation_keeps_the_line():
    assert _align(9, TITLE, "The f-string builds SQL from the region value.") == 9


def test_citing_its_own_line_keeps_the_line():
    body = "The f-string at line 9 builds SQL from the region value."
    assert _align(9, TITLE, body) == 9


def test_prose_sibling_citations_keep_the_line():
    body = (
        "The f-string builds SQL from the region value; the same pattern "
        "is at line 15 and line 21."
    )
    assert _align(9, TITLE, body) == 9


def test_path_line_sibling_citation_keeps_the_line():
    body = (
        "The f-string builds SQL from the region value; see also "
        "`app/reports.py:21`."
    )
    assert _align(9, TITLE, body) == 9


def test_wrong_line_still_moves_to_the_cited_line():
    body = (
        "The f-string builds SQL from the audits value; the offending "
        "audits query is at line 21."
    )
    assert _align(4, TITLE, body) == 21


def test_valid_but_unrelated_line_still_moves_to_the_cited_line():
    body = (
        "The audits query is built with an f-string from the region "
        "value (line 21)."
    )
    assert _align(4, TITLE, body) == 21


def test_backticked_slash_path_citation_is_read_as_own_file():
    from prxref.quality import _body_cited_lines

    f = Finding(
        file=PATH,
        line=9,
        severity="warning",
        confidence=0.8,
        title=TITLE,
        body="see also `app/reports.py:21` and (app/reports.py:15)",
    )
    assert _body_cited_lines(f) == [21, 15]
