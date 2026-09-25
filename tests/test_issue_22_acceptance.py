r"""Issue #22 acceptance: shared-state readers and the pinned-off toggle, end to end.

Every review here runs the production local path, ``cli._run_review`` with
``--diff-file``, ``--repo-dir`` and ``--description-file``, over
``tests/fixtures/issue22``. ``PRXREF_REPO_CONTEXT`` is set in the environment,
as a user sets it. The LLM is the real openai-compat HTTP client talking to a
local scripted server that records every request and answers every prompt
with ``{"findings": []}``, so any finding in a result is a deterministic one.
The payload asserted on is the one ``--format json`` prints. Nothing leaves
localhost.

The fixture is the issue's own generator, kept beside it as
``make-fixture.sh``, run with a fixed placeholder identity and date so that it
is byte-reproducible and carries no real identity. The two ``GIT_CONFIG_*``
variables keep the local git configuration (signing, hooks) out of the commits:

    env GIT_AUTHOR_NAME=Example GIT_AUTHOR_EMAIL=dev@example.com \
        GIT_COMMITTER_NAME=Example GIT_COMMITTER_EMAIL=dev@example.com \
        GIT_AUTHOR_DATE='2026-01-01 00:00:00 +0000' \
        GIT_COMMITTER_DATE='2026-01-01 00:00:00 +0000' \
        GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
        bash tests/fixtures/issue22/make-fixture.sh /tmp/shared-sink

Then ``pr.patch`` is ``git -C /tmp/shared-sink format-patch --stdout
main..feature/progress-notes``, and ``repo/`` is ``git -C /tmp/shared-sink
archive feature/progress-notes`` extracted into an emptied ``repo/``. The
patch's last line is the generating git's version signature, which nothing
reads. ``desc.md`` is the issue's PR description. ``repo/`` holds the fixture
project's own tests, so ``tests/fixtures/issue22/conftest.py`` keeps pytest
out of it and ``tests/fixtures/issue22/ruff.toml`` keeps ruff out of it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from prxref import cli, heuristics, repo_readers
from prxref.chunk_context import READER_HEADER
from prxref.triage import parse_unified_diff
from tests.test_integration import MockOpenAIServer, _completion

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issue22"
PATCH = FIXTURE / "pr.patch"
REPO = FIXTURE / "repo"
DESCRIPTION = FIXTURE / "desc.md"
GENERATOR = FIXTURE / "make-fixture.sh"

PROGRESS = "assistant/progress.py"
STATE_STORE = "assistant/state_store.py"
HISTORY = "assistant/history.py"
ENGINE_TEST = "tests/test_engine.py"
READERS = [f"{HISTORY}:9", f"{STATE_STORE}:15", f"{ENGINE_TEST}:17"]
READER_ROWS = [
    (HISTORY, 9, "table", "reader", "shared-state"),
    (STATE_STORE, 15, "data", "reader", "shared-state"),
    (ENGINE_TEST, 17, "data", "reader", "shared-state"),
]
PR_FILES = [
    ".gitignore", "README.md", "assistant/engine.py", PROGRESS, "tests/conftest.py", "tests/test_progress.py",
]
TOGGLE_LINE = 34
TOGGLE_CALL = 'self.active = enabled("progress_notes", default=True)'
TOGGLE_TITLE = 'Toggle "progress_notes" defaults on but the test setup pins it off'
PIN_LINE = '+    monkeypatch.setenv("ASSISTANT_PROGRESS_NOTES", "false")\n'
CONFTEST_HUNK = "@@ -0,0 +1,7 @@\n"
NO_FINDINGS = json.dumps({"findings": []})
OUTPUT_HEADER = "\n\n## Output Format"
ENTRY_HEAD = re.compile(r"^(\S+:\d+): ", re.MULTILINE)
EMAIL = re.compile(r"[\w.+-]*\w@[\w-]+(?:\.[\w-]+)+")
HEREDOC = re.compile(r"cat > '([^']+)' <<'__EOF__'\n(.*?)^__EOF__$", re.MULTILINE | re.DOTALL)


@pytest.fixture(scope="module")
def llm_server():
    """One scripted server for the module, so each review skips a server shutdown."""
    server = MockOpenAIServer(routes={"fast": _completion(NO_FINDINGS, "stop")})
    base_url = server.start()
    yield server, base_url
    server.stop()


@pytest.fixture
def review(monkeypatch, llm_server):
    """Run one local review at a level: the ``--format json`` payload and every user prompt, in arrival order."""
    server, base_url = llm_server

    def _review(level: str, diff_file: Path = PATCH) -> tuple[dict, list[str]]:
        server.requests.clear()
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "openai-compat")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", base_url)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "fast")
        monkeypatch.setenv("PRXREF_REPO_CONTEXT", level)
        result = cli._run_review(
            None, diff_file=str(diff_file), repo_dir=str(REPO), description_file=str(DESCRIPTION),
        )
        payload = json.loads(json.dumps(cli._build_json_result(result)))
        prompts = [
            [message["content"] for message in request["payload"]["messages"] if message["role"] == "user"][-1]
            for request in server.requests
        ]
        return payload, prompts

    return _review


def _progress_prompt(prompts: list[str]) -> str:
    """The one worker prompt whose own diff holds ``assistant/progress.py``; chunk order is not fixed."""
    owners = [prompt for prompt in prompts if f"diff --git a/{PROGRESS} " in prompt]
    assert len(owners) == 1, f"{len(owners)} prompts carry the {PROGRESS} diff"
    return owners[0]


def _block(prompt: str, header: str) -> str:
    """The prompt's ``header`` block, header included, up to the next block or the output section; "" if absent."""
    if header not in prompt:
        return ""
    start = prompt.index(header)
    ends = [
        end for end in (prompt.find("\n\n### ", start + len(header)), prompt.find(OUTPUT_HEADER, start))
        if end >= 0
    ]
    return prompt[start:min(ends)]


def _entries(payload: dict) -> list[tuple]:
    """Every run-record entry as ``(path, line, symbol, kind, reason)``, in chunk order."""
    return [
        (entry["path"], entry["line"], entry["symbol"], entry["kind"], entry["reason"])
        for row in payload["repo_context"]["units"]["chunks"]
        for entry in row["entries"]
    ]


def _reader_rows(payload: dict) -> list[tuple]:
    return [row for row in _entries(payload) if row[3] == "reader"]


class TestReadersAtRepo:
    """At ``repo`` the progress.py chunk sees the unchanged code that reads what the PR writes."""

    def test_the_progress_chunk_carries_the_readers_of_the_state_it_writes(self, review):
        _, prompts = review("repo")
        prompt = _progress_prompt(prompts)
        block = _block(prompt, READER_HEADER)
        assert ENTRY_HEAD.findall(block) == READERS
        assert "for m in table.recent(session_id, HISTORY_WINDOW):" in block
        assert 'frames = [Frame(f["agent_id"], f["data"]) for f in raw["frames"]]' in block
        for needle in (STATE_STORE, HISTORY, "def model_history(", "def load(self, session_id"):
            assert prompt.count(needle) == 1, needle
            assert needle in block, needle
        assert [p for p in prompts if READER_HEADER in p] == [prompt]

    def test_the_run_record_names_the_same_readers(self, review):
        payload, _ = review("repo")
        assert _reader_rows(payload) == READER_ROWS


class TestNoReadersBelowRepo:
    @pytest.mark.parametrize("level", ["diff", "off"])
    def test_no_prompt_carries_the_reader_block(self, review, level):
        payload, prompts = review(level)
        assert len(prompts) == 3
        assert "diff --git a/tests/conftest.py " in _progress_prompt(prompts)
        assert [p for p in prompts if READER_HEADER in p] == []
        if level == "off":
            assert payload["repo_context"] is None
        else:
            assert _reader_rows(payload) == []


class TestTheToggle:
    """The deterministic pinned-off toggle finding; the model returned nothing."""

    @pytest.mark.parametrize("level", ["repo", "diff", "off"])
    def test_exactly_one_toggle_finding_at_the_toggle_line(self, review, level):
        payload, _ = review(level)
        (finding,) = payload["findings"]
        assert (finding["file"], finding["line"]) == (PROGRESS, TOGGLE_LINE)
        assert (finding["severity"], finding["confidence"]) == ("warning", 1.0)
        assert finding["title"] == TOGGLE_TITLE
        assert finding["drop_reason"] is None
        assert finding["body"].endswith(heuristics._BODY_SUFFIX)
        assert "tests/conftest.py" in finding["body"]
        assert "ASSISTANT_PROGRESS_NOTES" in finding["body"]

    def test_the_toggle_line_is_the_default_on_call(self):
        lines = (REPO / PROGRESS).read_text(encoding="utf-8").splitlines()
        assert lines[TOGGLE_LINE - 1].strip() == TOGGLE_CALL

    @pytest.mark.parametrize("variant", ["pin line deleted", "pin set to true"])
    def test_control_without_the_pin_there_is_no_finding(self, review, tmp_path, variant):
        text = PATCH.read_text(encoding="utf-8")
        assert text.count(PIN_LINE) == 1
        if variant == "pin line deleted":
            assert text.count(CONFTEST_HUNK) == 1
            text = text.replace(PIN_LINE, "").replace(CONFTEST_HUNK, "@@ -0,0 +1,6 @@\n")
            kept = "_progress_notes_off(monkeypatch):"
        else:
            text = text.replace(PIN_LINE, PIN_LINE.replace('"false"', '"true"'))
            kept = 'monkeypatch.setenv("ASSISTANT_PROGRESS_NOTES", "true")'
        control = tmp_path / "pr.patch"
        control.write_text(text, encoding="utf-8")
        payload, prompts = review("off", control)
        assert payload["findings"] == []
        assert payload["chunks_failed"] == 0
        prompt = _progress_prompt(prompts)
        assert kept in prompt
        assert '"ASSISTANT_PROGRESS_NOTES", "false"' not in prompt


class TestReadersOffSwitch:
    """``repo_readers.MAX_READER_ENTRIES = 0`` is the live check's readers-off patch point."""

    def test_zero_entries_removes_exactly_the_reader_block(self, review, monkeypatch):
        payload_on, prompts_on = review("repo")
        monkeypatch.setattr(repo_readers, "MAX_READER_ENTRIES", 0)
        payload_off, prompts_off = review("repo")
        on = _progress_prompt(prompts_on)
        off = _progress_prompt(prompts_off)
        assert READER_HEADER in on
        assert [p for p in prompts_off if READER_HEADER in p] == []
        cut = "\n\n" + _block(on, READER_HEADER)
        assert on.count(cut) == 1
        assert off == on.replace(cut, "")
        assert sorted(p for p in prompts_off if p != off) == sorted(p for p in prompts_on if p != on)
        assert _reader_rows(payload_off) == []
        assert _entries(payload_off) == [row for row in _entries(payload_on) if row[3] != "reader"]
        assert payload_off["repo_context"]["reads"] < payload_on["repo_context"]["reads"]


class TestTheFixture:
    def test_the_patch_carries_only_the_placeholder_identity(self):
        text = PATCH.read_text(encoding="utf-8")
        assert set(EMAIL.findall(text)) == {"dev@example.com"}
        assert "From: Example <dev@example.com>\n" in text

    def test_the_repo_tree_is_the_generators_head_tree(self):
        written: dict[str, str] = {}
        for path, text in HEREDOC.findall(GENERATOR.read_text(encoding="utf-8")):
            written[path] = text
        on_disk = {
            p.relative_to(REPO).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(REPO.rglob("*")) if p.is_file()
        }
        assert len(written) == 14
        assert on_disk == written

    def test_the_repo_tree_is_the_patch_head(self):
        files = parse_unified_diff(PATCH.read_text(encoding="utf-8"))
        assert sorted(f.path for f in files) == sorted(PR_FILES)
        for f in files:
            lines = (REPO / f.path).read_text(encoding="utf-8").splitlines()
            head = [ln for hunk in f.hunks for ln in hunk.lines if ln.new_line is not None]
            assert head, f.path
            for ln in head:
                assert lines[ln.new_line - 1] == ln.text, f"{f.path}:{ln.new_line}"
            if f.status == "added":
                assert len(lines) == len(head), f.path
