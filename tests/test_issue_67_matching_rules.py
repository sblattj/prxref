"""Issue #67 — the worker prompt must probe the matching rules of changed
input-matching constructs (web-server ``location``/``rewrite``, router path
patterns, globs, validators) for inputs they newly capture, and
report nothing when the diff or a context
block shows every capturable input is constrained.

Self-contained, modeled on tests/test_issue_07_containment_boundary.py: it
does not import tests/test_orchestrator.py or tests/test_reviewer.py
fixtures. The sweep digest cannot see nginx ``location`` rules
(src/prxref/systemic.py ``_DIGEST_PATTERNS`` has no matching class), so only
the chunk worker — which sees the full diff text — can carry this rule; the
tests pin placement, plumbing and the two acceptance behaviours through a
fake LLM. The live-model half (a real model enumerating the captured inputs)
belongs to the eval harness and is out of scope here.
"""
from __future__ import annotations

import json

from prxref.forges.base import PRData, PRRef
from prxref.llm import InvokeResult
from prxref.orchestrator import orchestrate_review
from prxref.prompt_templates import load_prompt_templates
from prxref.reviewer import PromptContext, load_prompt, review_chunk
from prxref.triage import parse_unified_diff

# ---------------------------------------------------------------------------
# Shared shapes (mirroring the issue-07 file's fake LLM / forge).
# ---------------------------------------------------------------------------

# Minimal chunk whose added line is exactly the issue's shape: an nginx
# location rule that decides which request paths match.
MINI_DIFF = (
    "diff --git a/conf/nginx.conf b/conf/nginx.conf\n"
    "--- a/conf/nginx.conf\n"
    "+++ b/conf/nginx.conf\n"
    "@@ -1,3 +1,4 @@\n"
    " server {\n"
    "   try_files $uri /index.html;\n"
    "+  location ~* \\.[^/]+$ { return 404; }\n"
    " }\n"
)


class _CapturingLLM:
    """Records the exact system/user text handed to invoke(); always returns
    a clean empty-findings response so review_chunk's parse never fails."""

    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        self.calls.append({"system": system, "user": user})
        return InvokeResult(
            text=self.text, input_tokens=10, output_tokens=5,
            model="capture-model", backend="fake", elapsed_ms=1,
        )


class _VerbatimLLM:
    """Returns the SAME text for every invoke() call — worker chunk call
    and systemic-sweep call alike — matching FakeLLM's string-mode contract
    in tests/test_orchestrator.py."""

    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        self.calls += 1
        return InvokeResult(
            text=self.text, input_tokens=10, output_tokens=5,
            model="matching-test-model", backend="fake", elapsed_ms=1,
        )


class _FakeForge:
    name = "fake"

    def __init__(self, pr: PRData, diff: str):
        self.pr = pr
        self.diff = diff
        self.summaries: list[str] = []
        self.inline_batches: list[list] = []

    @staticmethod
    def parse_pr_url(url: str):
        return None

    def get_pr(self, ref):
        return self.pr

    def get_diff(self, ref):
        return self.diff

    def post_summary(self, ref, body):
        self.summaries.append(body)

    def post_inline_comments(self, ref, comments):
        self.inline_batches.append(list(comments))
        return len(comments)

    def list_threads(self, ref):
        return []


def _make_pr() -> PRData:
    return PRData(
        title="Add a static-asset 404 rule to nginx",
        description="adds a location rule rejecting dotted paths",
        author="alice", source_branch="feature/nginx-404",
        target_branch="main", source_sha="c" * 40, target_sha="d" * 40,
        raw={},
    )


REF = PRRef(
    forge="fake", host="fake.test", owner="acme", repo="widget",
    number=67, url="https://fake.test/acme/widget/pull/67",
)

# ---------------------------------------------------------------------------
# Test A: the rendered worker prompt carries the Matching rules section.
# ---------------------------------------------------------------------------


def test_packaged_worker_carries_the_section_between_no_speculation_and_style():
    text = load_prompt("worker.md")
    probe, style_marker, _tail = text.partition("## Style")
    assert style_marker, "worker.md must keep its Style section"
    assert "## Matching rules" in probe, (
        "worker.md must state the matching-rules rule: a changed rule that "
        "decides which inputs match can silently capture inputs another "
        "rule used to handle."
    )
    assert probe.index("## Matching rules") > probe.index("## No Speculation"), (
        "the Matching rules section must sit after No Speculation"
    )


def test_worker_prompt_states_the_matching_rules_rule():
    llm = _CapturingLLM('{"findings":[],"escalations":[]}')
    chunk = parse_unified_diff(MINI_DIFF)

    review_chunk(llm, chunk)

    assert len(llm.calls) == 1
    combined = llm.calls[0]["system"] + "\n" + llm.calls[0]["user"]
    assert "## Matching rules" in combined, (
        "the rendered worker prompt must carry the Matching rules section"
    )
    assert "newly matches" in combined, (
        "the section must ask the model to enumerate inputs the changed "
        "rule newly matches that were previously handled elsewhere"
    )


# ---------------------------------------------------------------------------
# End-to-end fixture: a PR adding the 404 location rule next to the SPA
# fallback it may capture inputs from, plus a route-table file documenting
# the API's path parameter format. ``routes_line`` is the acceptance lever:
# dotted versions (acceptance 1) vs. a UUID constraint (acceptance 2).
# ---------------------------------------------------------------------------

SPA_FALLBACK_DIFF = (
    "diff --git a/conf/nginx.conf b/conf/nginx.conf\n"
    "--- a/conf/nginx.conf\n"
    "+++ b/conf/nginx.conf\n"
    "@@ -1,4 +1,5 @@\n"
    " server {\n"
    "   root /var/www/app;\n"
    "   try_files $uri /index.html;\n"
    "+  location ~* \\.[^/]+$ { return 404; }\n"
    " }\n"
    "diff --git a/docs/routes.md b/docs/routes.md\n"
    "--- a/docs/routes.md\n"
    "+++ b/docs/routes.md\n"
    "@@ -1,3 +1,4 @@\n"
    " # API routes\n"
    "+{routes_line}\n"
    " GET /health\n"
    " GET /users/:id\n"
)

DOTTED_VERSION_LINE = "GET /items/:version    # :version is dotted, e.g. /items/v1.2"
UUID_VERSION_LINE = "GET /items/:version    # :version is a UUID"

# The added rule line's new-file position: hunk ``@@ -1,4 +1,5 @@`` — three
# context lines, the added ``location`` rule at new line 4, the closing brace.
RULE_LINE = 4

MATCHING_FINDING_JSON = json.dumps({
    "findings": [
        {
            "file": "conf/nginx.conf",
            "line": RULE_LINE,
            "severity": "warning",
            "confidence": 0.7,
            "title": (
                "Static-asset 404 rule now captures dotted API paths the "
                "SPA fallback served"
            ),
            "body": (
                "The added nginx location matches every path containing a "
                "dot, so GET /items/v1.2 — previously served by the SPA "
                "fallback, as the route table documents — now returns 404."
            ),
        }
    ],
    "escalations": [],
})

# ---------------------------------------------------------------------------
# Acceptance 1 (Test B): the warning survives end to end with the example
# input in the body — the downgrade clause is content-driven, so a plain
# warning-severity finding must still pass on this same fixture.
# ---------------------------------------------------------------------------


def test_matching_warning_survives_with_the_example_input_in_the_body():
    forge = _FakeForge(
        pr=_make_pr(), diff=SPA_FALLBACK_DIFF.replace("{routes_line}", DOTTED_VERSION_LINE)
    )
    llm = _VerbatimLLM(MATCHING_FINDING_JSON)

    result = orchestrate_review(forge, REF, llm, post=False)

    findings = result["findings_active"]
    matches = [f for f in findings if "/items/v1.2" in f.body]
    assert matches, (
        f"expected the matching-rule warning naming its example input to "
        f"survive the quality gates; active findings were: {findings}"
    )
    finding = matches[0]
    assert finding.severity == "warning", (
        f"a warning-severity finding must stay a warning when the captured "
        f"input plausibly occurs; got {finding.severity!r}"
    )
    assert finding.file == "conf/nginx.conf"
    assert finding.line == RULE_LINE


# ---------------------------------------------------------------------------
# Acceptance 2 (Test C): when the route parameter is documented as a UUID,
# the model's outofscope/0.6 constraint note is legal, survives the default
# 0.6 confidence floor, and no warning is manufactured for it.
# ---------------------------------------------------------------------------


def test_constrained_capture_reports_nothing_on_the_rule_line():
    forge = _FakeForge(
        pr=_make_pr(), diff=SPA_FALLBACK_DIFF.replace("{routes_line}", UUID_VERSION_LINE)
    )
    llm = _VerbatimLLM(json.dumps({"findings": [], "escalations": []}))

    result = orchestrate_review(forge, REF, llm, post=False)

    on_rule = [
        f
        for f in result["findings_active"]
        if f.file == "conf/nginx.conf" and f.line == RULE_LINE
    ]
    assert not on_rule, f"a constrained capture must post nothing; got {on_rule}"


def test_matching_rules_section_reports_nothing_and_excludes_firewall_lists():
    text = load_prompt("worker.md")
    section = text.partition("## Matching rules")[2].partition("## Style")[0]
    assert section
    lowered = section.lower()
    assert "firewall" not in lowered
    assert "allow-list" not in lowered
    assert "report nothing" in lowered
    assert "outofscope" not in lowered


# ---------------------------------------------------------------------------
# Test D: a PRXREF_PROMPTS_DIR override written before this section loads
# fine — the marker and placeholder validation are untouched, and the
# override replaces the packaged template wholesale (the rendered prompt
# then has no Matching rules section).
# ---------------------------------------------------------------------------


def _packaged_worker_without_matching_rules() -> str:
    text = load_prompt("worker.md")
    head, marker, rest = text.partition("## Matching rules")
    assert marker, "the packaged worker.md must carry the Matching rules section"
    _section, style_marker, style_tail = rest.partition("## Style")
    assert style_marker
    return head + style_marker + style_tail


def test_prompts_dir_override_without_the_section_loads_and_renders(tmp_path):
    legacy = _packaged_worker_without_matching_rules()
    d = tmp_path / "prompts"
    d.mkdir()
    (d / "worker.md").write_bytes(legacy.encode("utf-8"))

    loaded = load_prompt_templates(d, source="PRXREF_PROMPTS_DIR")

    assert loaded is not None
    assert loaded.overridden == ("worker",)
    assert "## Matching rules" not in loaded.worker

    llm = _CapturingLLM('{"findings":[],"escalations":[]}')
    chunk = parse_unified_diff(MINI_DIFF)
    review_chunk(
        llm, chunk,
        prompt_context=PromptContext(worker_template=loaded.override("worker")),
    )

    combined = llm.calls[0]["system"] + "\n" + llm.calls[0]["user"]
    assert "## Matching rules" not in combined, (
        "an operator override must replace the packaged template wholesale"
    )
    assert "## Review Context" in combined, (
        "the override's context tail must still render"
    )
