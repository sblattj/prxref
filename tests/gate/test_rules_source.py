"""Base-pinned rules (#102): the repo config and standards come from the base SHA."""

from pathlib import Path

import pytest

from prxref import cli, config, llm_backends, orchestrator
from prxref.gate import rules_source
from tests.test_issue_68_standards_context import (
    STANDARD_GLOBS,
    STANDARDS_DOC,
    TWO_CHUNK_DIFF,
    WEB_SECURITY,
    _pinned_clock,  # noqa: F401  (autouse fixture: importing it registers it here)
    _RecordingLLM,
    _RepoForge,
    _worker_prompts,
    _write,
)
from tests.test_orchestrator import REF, FakeForge

HEAD_SHA, BASE_SHA = "a" * 40, "b" * 40


def _reader(files: dict[str, str]):
    return lambda path: files.get(path)


def test_materializes_the_config_and_its_path_inputs(tmp_path):
    files = {
        ".prxref.toml": 'review_rules = "rules/team.md"\nscoped_rules = ["rules/api.md", "rules/gone.md"]\n',
        "rules/team.md": "- Never log secrets.\n",
        "rules/api.md": "---\npaths: ['api/**']\n---\n- Validate input.\n",
    }
    pinned = rules_source.materialize_rules(_reader(files), tmp_path)
    assert pinned.config_file == tmp_path / ".prxref.toml"
    assert (tmp_path / "rules/team.md").read_text() == "- Never log secrets.\n"
    assert pinned.fetched == ("rules/api.md", "rules/team.md")
    assert pinned.missing == ("rules/gone.md",)
    assert not any(p.is_symlink() for p in tmp_path.rglob("*"))


def test_no_config_at_the_sha_gives_no_config_file(tmp_path):
    assert rules_source.materialize_rules(_reader({}), tmp_path).config_file is None


@pytest.mark.parametrize("raw", ["../outside.md", "/etc/passwd", "~/x.md", "https://x/y.md"])
def test_paths_leaving_the_repo_are_never_fetched(tmp_path, raw):
    asked = []

    def read(path):
        asked.append(path)
        return f'review_rules = "{raw}"\n' if path == ".prxref.toml" else "x"

    rules_source.materialize_rules(read, tmp_path)
    assert asked == [".prxref.toml"]


def test_prompts_dir_and_metadata_switches_are_not_fetched(tmp_path):
    asked = []

    def read(path):
        asked.append(path)
        return 'prompts_dir = "prompts"\nmetadata_rules = "off"\n' if path == ".prxref.toml" else "x"

    rules_source.materialize_rules(read, tmp_path)
    assert asked == [".prxref.toml"]


def test_base_config_wins_over_a_head_edit_that_disables_the_rule(tmp_path):
    base = {".prxref.toml": 'review_rules = "rules/team.md"\n', "rules/team.md": "- RULE-SENTINEL\n"}
    head = {".prxref.toml": 'review_rules = ""\n'}
    by_sha = {BASE_SHA: base, HEAD_SHA: head}
    for source, expected in (("base", "RULE-SENTINEL"), ("head", None)):
        sha = rules_source.rules_sha(HEAD_SHA, BASE_SHA, source)
        directory = tmp_path / source
        directory.mkdir()
        pinned = rules_source.materialize_rules(_reader(by_sha[sha]), directory)
        cfg = config.load_config(config_file=pinned.config_file)
        rules_path = cfg["review_rules"]
        text = Path(rules_path).read_text() if rules_path else None
        assert (expected in text) if expected else text is None


class _ShaForge(_RepoForge):
    """_RepoForge (tests/test_issue_68_standards_context.py:388) with one tree per commit."""

    def __init__(self, diff, roots: dict[str, Path]):
        super().__init__(diff, roots[HEAD_SHA])
        self.roots = roots

    def get_file_content(self, ref, path, *, sha):
        target = self.roots[sha] / path
        return target.read_text(encoding="utf-8") if target.is_file() else None

    def list_paths(self, ref, *, sha):
        self.root = self.roots[sha]
        return super().list_paths(ref, sha=sha)


@pytest.mark.parametrize(
    "source, shown, hidden",
    [
        ("base", "max-age=63072000", "max-age=31536000"),
        ("head", "max-age=31536000", "max-age=63072000"),
    ],
)
def test_standards_document_is_read_at_the_rules_sha(tmp_path, source, shown, hidden):
    base_root, head_root = tmp_path / "base", tmp_path / "head"
    _write(base_root, STANDARDS_DOC, WEB_SECURITY)
    _write(head_root, STANDARDS_DOC, WEB_SECURITY.replace("63072000", "31536000"))
    forge = _ShaForge(TWO_CHUNK_DIFF, {HEAD_SHA: head_root, BASE_SHA: base_root})
    llm = _RecordingLLM()
    orchestrator.orchestrate_review(
        forge,
        REF,
        llm,
        post=False,
        repo_context="repo",
        max_files_per_chunk=1,
        context_standards_globs=STANDARD_GLOBS,
        rules_source=source,
    )
    prompt = _worker_prompts(llm, ["app/middleware.py", "tools/helper.py"])["app/middleware.py"][0]
    assert shown in prompt
    assert "Standards every ACME service" in prompt
    assert hidden not in prompt


def test_run_review_uses_the_given_forge_and_passes_rules_source(monkeypatch):
    seen = {}

    def fake_orchestrate(**kwargs):
        seen.update(kwargs)
        return {"verdict": "Approved"}

    monkeypatch.setattr(orchestrator, "orchestrate_review", fake_orchestrate)
    monkeypatch.setattr(llm_backends, "create_llm_client", lambda cfg: object())
    monkeypatch.setattr(cli, "make_forge", lambda ref: pytest.fail("make_forge must not run"))
    monkeypatch.setenv("PRXREF_RULES_SOURCE", "base")
    given = FakeForge(diff="")
    cli._run_review("https://github.com/acme/widget/pull/7", forge=given)
    assert seen["forge"] is given
    assert seen["rules_source"] == "base"


def _base_run(forge, caplog, **extra):
    llm = _RecordingLLM()
    with caplog.at_level("WARNING", logger="prxref.orchestrator"):
        result = orchestrator.orchestrate_review(
            forge,
            REF,
            llm,
            post=False,
            repo_context="repo",
            max_files_per_chunk=1,
            context_standards_globs=STANDARD_GLOBS,
            rules_source="base",
            **extra,
        )
    return result, llm


def test_base_without_a_base_sha_reads_no_standards_from_head(tmp_path, caplog):
    head_root = tmp_path / "head"
    _write(head_root, STANDARDS_DOC, WEB_SECURITY)
    forge = _ShaForge(TWO_CHUNK_DIFF, {HEAD_SHA: head_root})
    forge.pr.target_sha = ""
    result, llm = _base_run(forge, caplog)
    assert result["verdict"] != "Error"
    assert forge.content_calls[STANDARDS_DOC] == 0
    prompt = _worker_prompts(llm, ["app/middleware.py", "tools/helper.py"])["app/middleware.py"][0]
    assert "Standards every ACME service" not in prompt
    assert "rules_source=base but the PR has no base SHA" in caplog.text


def test_base_with_a_repo_dir_reads_no_standards_from_the_checkout(tmp_path, caplog):
    from prxref.forges.repo_dir import RepoDir

    head_root = tmp_path / "head"
    _write(head_root, STANDARDS_DOC, WEB_SECURITY)
    forge = _ShaForge(TWO_CHUNK_DIFF, {HEAD_SHA: head_root, BASE_SHA: head_root})
    result, llm = _base_run(forge, caplog, repo_dir=RepoDir(head_root))
    assert result["verdict"] != "Error"
    assert forge.content_calls[STANDARDS_DOC] == 0
    prompt = _worker_prompts(llm, ["app/middleware.py", "tools/helper.py"])["app/middleware.py"][0]
    assert "Standards every ACME service" not in prompt
    assert "a repository directory was given" in caplog.text
