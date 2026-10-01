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
fake LLM. The fake LLM is prompt-sensitive: it answers with the canned finding only
when the rendered prompt carries the Matching rules section. The live-model
half (a real model enumerating the captured inputs) belongs to the eval
harness.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from prxref import chunk_context, config
from prxref.cli import main
from prxref.forges.base import PRData, PRRef
from prxref.llm import ConfigError, InvokeResult
from prxref.orchestrator import orchestrate_review
from prxref.prompt_templates import load_prompt_templates
from prxref.reviewer import PromptContext, load_prompt, review_chunk
from prxref.triage import parse_unified_diff
from tests.test_cli import _install_fake_module

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


class _SectionAwareLLM:
    """Returns the canned text only for a call whose system or user text carries
    the ``## Matching rules`` section, and an empty findings response for every
    other call, so the test fails when the section stops reaching the model."""

    EMPTY = '{"findings":[],"escalations":[]}'

    def __init__(self, text: str):
        self.text = text
        self.prompted_with_section = 0
        self.calls = 0

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        self.calls += 1
        seen = "## Matching rules" in (system + "\n" + user)
        if seen:
            self.prompted_with_section += 1
        return InvokeResult(
            text=self.text if seen else self.EMPTY,
            input_tokens=10, output_tokens=5,
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

FIXTURES = Path(__file__).parent / "fixtures" / "issue_67"
SPA_DOTTED_DIFF = (FIXTURES / "spa_dotted.diff").read_text(encoding="utf-8")
SPA_UUID_DIFF = (FIXTURES / "spa_uuid.diff").read_text(encoding="utf-8")


def test_fixture_diffs_parse_into_the_rule_and_the_route_table():
    for diff in (SPA_DOTTED_DIFF, SPA_UUID_DIFF):
        files = parse_unified_diff(diff)
        assert {f.path for f in files} == {"conf/nginx.conf", "docs/routes.md"}


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
        pr=_make_pr(), diff=SPA_DOTTED_DIFF
    )
    llm = _SectionAwareLLM(MATCHING_FINDING_JSON)

    result = orchestrate_review(forge, REF, llm, post=False)

    assert llm.prompted_with_section >= 1
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


def test_matching_warning_is_absent_when_the_prompt_lacks_the_section(tmp_path):
    d = tmp_path / "prompts"
    d.mkdir()
    (d / "worker.md").write_bytes(
        _packaged_worker_without_matching_rules().encode("utf-8")
    )
    loaded = load_prompt_templates(d, source="PRXREF_PROMPTS_DIR")
    forge = _FakeForge(pr=_make_pr(), diff=SPA_DOTTED_DIFF)
    llm = _SectionAwareLLM(MATCHING_FINDING_JSON)

    result = orchestrate_review(forge, REF, llm, post=False, prompts=loaded)

    assert llm.prompted_with_section == 0
    assert not [f for f in result["findings_active"] if "/items/v1.2" in f.body]


# ---------------------------------------------------------------------------
# Acceptance 2 (Test C): when the route parameter is documented as a UUID,
# the model's outofscope/0.6 constraint note is legal, survives the default
# 0.6 confidence floor, and no warning is manufactured for it.
# ---------------------------------------------------------------------------


def test_constrained_capture_reports_nothing_on_the_rule_line():
    forge = _FakeForge(
        pr=_make_pr(), diff=SPA_UUID_DIFF
    )
    llm = _SectionAwareLLM(json.dumps({"findings": [], "escalations": []}))

    result = orchestrate_review(forge, REF, llm, post=False)

    assert llm.prompted_with_section >= 1
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


# ---------------------------------------------------------------------------
# Test E: PRXREF_ROUTING_PROBE (OD2, default on) gates the section. Off
# renders the worker system prompt exactly as the template without the
# section; on (the default) renders it unchanged.
# ---------------------------------------------------------------------------

_REF_CLI = PRRef(
    forge="github", host="github.com", owner="acme", repo="widget", number=67,
    url="https://github.com/acme/widget/pull/67",
)


def _system_prompt(prompt_context: PromptContext) -> str:
    llm = _CapturingLLM('{"findings":[],"escalations":[]}')
    review_chunk(llm, parse_unified_diff(MINI_DIFF), prompt_context=prompt_context)
    (call,) = llm.calls
    return call["system"]


def test_routing_probe_off_drops_the_section_from_the_run():
    forge = _FakeForge(pr=_make_pr(), diff=SPA_DOTTED_DIFF)
    llm = _SectionAwareLLM(MATCHING_FINDING_JSON)

    result = orchestrate_review(forge, REF, llm, post=False, routing_probe="off")

    assert llm.calls >= 1
    assert llm.prompted_with_section == 0
    assert not [f for f in result["findings_active"] if "/items/v1.2" in f.body]


def test_routing_probe_on_is_the_default_and_keeps_the_section():
    default = inspect.signature(orchestrate_review).parameters["routing_probe"].default
    assert default == "on"
    forge = _FakeForge(pr=_make_pr(), diff=SPA_DOTTED_DIFF)
    llm = _SectionAwareLLM(MATCHING_FINDING_JSON)

    orchestrate_review(forge, REF, llm, post=False, routing_probe="on")

    assert llm.prompted_with_section >= 1


def test_routing_probe_off_system_prompt_is_the_template_without_the_section():
    legacy = _packaged_worker_without_matching_rules()
    off = _system_prompt(PromptContext(routing_probe=False))
    without = _system_prompt(PromptContext(worker_template=legacy))
    assert off == without
    assert "## Matching rules" not in off
    assert "\n\n\n" not in off


def test_routing_probe_on_system_prompt_is_byte_identical_to_the_template():
    head = load_prompt("worker.md").partition("## Review Context")[0].strip()
    assert _system_prompt(PromptContext()) == head
    assert _system_prompt(PromptContext(routing_probe=True)) == head


def test_routing_probe_off_drops_the_section_from_an_override_that_copied_it(tmp_path):
    d = tmp_path / "prompts"
    d.mkdir()
    (d / "worker.md").write_bytes(load_prompt("worker.md").encode("utf-8"))
    loaded = load_prompt_templates(d, source="PRXREF_PROMPTS_DIR")
    system = _system_prompt(
        PromptContext(worker_template=loaded.override("worker"), routing_probe=False)
    )
    assert "## Matching rules" not in system
    assert "## Style" in system


def test_routing_probe_unknown_mode_is_a_value_error():
    forge = _FakeForge(pr=_make_pr(), diff=SPA_DOTTED_DIFF)
    with pytest.raises(ValueError, match="routing_probe"):
        orchestrate_review(
            forge, REF, _CapturingLLM("{}"), post=False, routing_probe="maybe",
        )


def test_routing_probe_config_default_vocabulary_and_partition(monkeypatch):
    monkeypatch.delenv("PRXREF_ROUTING_PROBE", raising=False)
    assert config._DEFAULTS["routing_probe"] == "on"
    assert config.load_config()["routing_probe"] == "on"
    assert config._CHOICE_KEYS["routing_probe"] == frozenset({"off", "on"})
    assert "routing_probe" in config.FILE_KEYS
    monkeypatch.setenv("PRXREF_ROUTING_PROBE", "off")
    assert config.load_config()["routing_probe"] == "off"
    monkeypatch.setenv("PRXREF_ROUTING_PROBE", "maybe")
    with pytest.raises(ConfigError, match="PRXREF_ROUTING_PROBE"):
        config.load_config()


def test_routing_probe_config_file_value_loads(tmp_path, monkeypatch):
    monkeypatch.delenv("PRXREF_ROUTING_PROBE", raising=False)
    path = tmp_path / ".prxref.toml"
    path.write_text('routing_probe = "off"\n', encoding="utf-8")
    assert config.load_config(config_file=path)["routing_probe"] == "off"


@pytest.mark.parametrize("env, want", [(None, "on"), ("off", "off"), ("on", "on")])
def test_routing_probe_env_reaches_the_orchestrator(monkeypatch, env, want):
    calls: list[dict] = []

    def fake_orchestrate_review(**kwargs):
        calls.append(kwargs)
        return {"verdict": "commented", "findings_active": [], "findings_dropped": []}

    _install_fake_module(monkeypatch, "prxref.llm_backends", create_llm_client=lambda cfg: object())
    _install_fake_module(monkeypatch, "prxref.orchestrator", orchestrate_review=fake_orchestrate_review)
    monkeypatch.setattr("prxref.cli.detect_forge", lambda url: _REF_CLI)
    if env is None:
        monkeypatch.delenv("PRXREF_ROUTING_PROBE", raising=False)
    else:
        monkeypatch.setenv("PRXREF_ROUTING_PROBE", env)
    assert main(["review", "--pr-url", _REF_CLI.url, "--no-post"]) == 0
    (call,) = calls
    assert call["routing_probe"] == want


def test_routing_probe_bogus_env_exits_2_naming_the_variable(monkeypatch, capsys):
    monkeypatch.setattr("prxref.cli.detect_forge", lambda url: _REF_CLI)
    monkeypatch.setenv("PRXREF_ROUTING_PROBE", "maybe")
    rc = main(["review", "--pr-url", _REF_CLI.url, "--no-post"])
    assert rc == 2
    _, err = capsys.readouterr()
    assert "PRXREF_ROUTING_PROBE" in err


# ---------------------------------------------------------------------------
# Test F: the route table the probe checks against usually sits OUTSIDE the
# diff. With the probe on and a forge file reader, a chunk adding a web-server
# or static-host matching rule fetches the conventional route-table files at
# the PR head and renders their route lines as a context block, so the model
# can check the newly captured inputs against what the repo defines.
# ---------------------------------------------------------------------------

NGINX_ONLY_DIFF = (FIXTURES / "nginx_only.diff").read_text(encoding="utf-8")
ROUTES_DOTTED = (FIXTURES / "routes_dotted.tsx").read_text(encoding="utf-8")
ROUTES_UUID = (FIXTURES / "routes_uuid.tsx").read_text(encoding="utf-8")
ROUTE_TABLE_PATH = "src/routes.tsx"


class _RouteForge(_FakeForge):
    """A fake forge whose file reader serves one route table at the PR head
    and records every path the run asked for."""

    def __init__(self, pr: PRData, diff: str, files: dict[str, str]):
        super().__init__(pr, diff)
        self.files = files
        self.reads: list[str] = []

    def get_file_content(self, ref, path, *, sha):
        self.reads.append(path)
        return self.files.get(path)


class _RaisingRouteForge(_FakeForge):
    def get_file_content(self, ref, path, *, sha):
        raise RuntimeError("forge read exploded")


class _RouteAwareLLM:
    """Answers with the canned text only when the system prompt carries the
    Matching rules section AND the user prompt carries the route-table line;
    records every user prompt."""

    EMPTY = '{"findings":[],"escalations":[]}'

    def __init__(self, text: str, needle: str):
        self.text = text
        self.needle = needle
        self.users: list[str] = []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        self.users.append(user)
        hit = "## Matching rules" in system and self.needle in user
        return InvokeResult(
            text=self.text if hit else self.EMPTY,
            input_tokens=10, output_tokens=5,
            model="route-test-model", backend="fake", elapsed_ms=1,
        )

    def worker_prompts(self) -> list[str]:
        return [u for u in self.users if "### Diff" in u]


def test_route_table_fixtures_sit_outside_the_diff():
    files = parse_unified_diff(NGINX_ONLY_DIFF)
    assert {f.path for f in files} == {"conf/nginx.conf"}
    assert ROUTE_TABLE_PATH in chunk_context.ROUTE_TABLE_CANDIDATES


def test_route_table_outside_the_diff_reaches_the_prompt_and_the_warning_survives():
    forge = _RouteForge(_make_pr(), NGINX_ONLY_DIFF, {ROUTE_TABLE_PATH: ROUTES_DOTTED})
    llm = _RouteAwareLLM(MATCHING_FINDING_JSON, '"/items/:version"')

    result = orchestrate_review(forge, REF, llm, post=False)

    assert ROUTE_TABLE_PATH in forge.reads
    (user,) = llm.worker_prompts()
    assert chunk_context.ROUTES_HEADER in user
    assert f'{ROUTE_TABLE_PATH}:7:   {{ path: "/items/:version"' in user
    assert "e.g. /items/v1.2" in user
    matches = [f for f in result["findings_active"] if "/items/v1.2" in f.body]
    assert matches, result["findings_active"]
    assert matches[0].severity == "warning"
    assert (matches[0].file, matches[0].line) == ("conf/nginx.conf", RULE_LINE)


def test_route_table_outside_the_diff_uuid_variant_posts_nothing():
    forge = _RouteForge(_make_pr(), NGINX_ONLY_DIFF, {ROUTE_TABLE_PATH: ROUTES_UUID})
    llm = _RouteAwareLLM('{"findings":[],"escalations":[]}', '"/items/:id"')

    result = orchestrate_review(forge, REF, llm, post=False)

    (user,) = llm.worker_prompts()
    assert chunk_context.ROUTES_HEADER in user
    assert ":id is a UUID" in user
    assert not [
        f for f in result["findings_active"]
        if f.file == "conf/nginx.conf" and f.severity != "outofscope"
    ]


def test_routing_probe_off_reads_no_route_table():
    forge = _RouteForge(_make_pr(), NGINX_ONLY_DIFF, {ROUTE_TABLE_PATH: ROUTES_DOTTED})
    llm = _RouteAwareLLM(MATCHING_FINDING_JSON, '"/items/:version"')

    orchestrate_review(forge, REF, llm, post=False, routing_probe="off")

    assert not set(forge.reads) & set(chunk_context.ROUTE_TABLE_CANDIDATES)
    (user,) = llm.worker_prompts()
    assert chunk_context.ROUTES_HEADER not in user


def test_non_routing_chunk_reads_no_route_table():
    diff = (
        "diff --git a/app/main.py b/app/main.py\n"
        "--- a/app/main.py\n"
        "+++ b/app/main.py\n"
        "@@ -1,2 +1,3 @@\n"
        " import os\n"
        "+print(os.getcwd())\n"
        " x = 1\n"
    )
    forge = _RouteForge(_make_pr(), diff, {ROUTE_TABLE_PATH: ROUTES_DOTTED})
    llm = _RouteAwareLLM(MATCHING_FINDING_JSON, '"/items/:version"')

    orchestrate_review(forge, REF, llm, post=False)

    assert not set(forge.reads) & set(chunk_context.ROUTE_TABLE_CANDIDATES)
    assert chunk_context.ROUTES_HEADER not in llm.worker_prompts()[0]


def test_route_table_in_the_diff_is_not_fetched_again():
    diff = SPA_DOTTED_DIFF.replace("docs/routes.md", ROUTE_TABLE_PATH)
    forge = _RouteForge(_make_pr(), diff, {ROUTE_TABLE_PATH: ROUTES_DOTTED})
    llm = _RouteAwareLLM(MATCHING_FINDING_JSON, '"/items/:version"')

    orchestrate_review(forge, REF, llm, post=False)

    assert "src/routes.ts" in forge.reads
    for user in llm.worker_prompts():
        assert chunk_context.ROUTES_HEADER not in user


def test_route_table_read_failure_leaves_the_prompt_without_the_block():
    forge = _RaisingRouteForge(_make_pr(), NGINX_ONLY_DIFF)
    llm = _RouteAwareLLM(MATCHING_FINDING_JSON, '"/items/:version"')

    result = orchestrate_review(forge, REF, llm, post=False)

    (user,) = llm.worker_prompts()
    assert chunk_context.ROUTES_HEADER not in user
    assert result["findings_active"] == []


@pytest.mark.parametrize("path, line, want", [
    ("conf/nginx.conf", "  location ~* \\.[^/]+$ { return 404; }", True),
    ("deploy/nginx/site.conf", "    rewrite ^/old/(.*)$ /new/$1 last;", True),
    ("public/.htaccess", "RewriteRule ^ index.html [L]", True),
    ("public/_redirects", "/*    /index.html   200", True),
    ("vercel.json", '  "rewrites": [{ "source": "/(.*)", "destination": "/" }]', True),
    ("Caddyfile", "  try_files {path} /index.html", True),
    ("conf/nginx.conf", "  # a comment about location", False),
    ("conf/nginx.conf", "  gzip on;", False),
    ("app/main.py", "location = '/x'", False),
    ("public/_redirects", "# comment", False),
])
def test_routing_rule_trigger(path, line, want):
    files = [chunk_context.ChunkFile(path=path, added=(line,))]
    assert chunk_context.has_routing_rule(files) is want


def test_route_table_lines_are_capped_and_exclude_non_route_lines():
    big = "\n".join(f'  {{ path: "/r{i}/:id", element: <P{i} /> }},' for i in range(500))
    text = 'import x from "y";\n' + big

    def read(path):
        return text if path == ROUTE_TABLE_PATH else None

    lines = chunk_context.route_table_lines(read)

    assert sum(len(entry) for entry in lines) <= chunk_context.MAX_ROUTE_CHARS + 80
    assert lines[-1].startswith("…")
    assert not any("import x" in entry for entry in lines)
    assert lines[0] == f'{ROUTE_TABLE_PATH}:2:   {{ path: "/r0/:id", element: <P0 /> }},'


def test_route_table_lines_skip_excluded_and_survive_a_raising_reader():
    def read(path):
        return ROUTES_DOTTED if path == ROUTE_TABLE_PATH else None

    assert chunk_context.route_table_lines(read, exclude={ROUTE_TABLE_PATH}) == []

    def boom(path):
        raise RuntimeError("nope")

    assert chunk_context.route_table_lines(boom) == []


def test_render_context_blocks_route_lines_render_last_and_are_identity_when_empty():
    base = chunk_context.render_context_blocks(["dep"], ["def"])
    assert chunk_context.render_context_blocks(["dep"], ["def"], route_lines=()) == base
    out = chunk_context.render_context_blocks(["dep"], ["def"], route_lines=["a:1: x"])
    assert out.startswith(base + "\n\n" + chunk_context.ROUTES_HEADER)
    assert out.endswith("a:1: x")
