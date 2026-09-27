"""Issue #29: ``--diff-file`` with ``--repo-dir`` builds chunk context.

A review whose forge has no file reader (``--diff-file`` without
``--pr-url``) reads its chunk context (dependency versions, same-file
definitions) from ``--repo-dir``. A forge that can read files at the PR head
still serves chunk context even when ``--repo-dir`` is also given, and a
failing ``--repo-dir`` read never costs the review.
"""
from __future__ import annotations

import difflib
import json
import threading
from pathlib import Path

import pytest

from prxref import cli, orchestrator
from prxref.chunk_context import DEFINITIONS_HEADER, DEPENDENCY_HEADER
from prxref.forges.base import PRData, PRRef
from prxref.forges.replay import LocalDiffForge
from prxref.forges.repo_dir import RepoDir
from prxref.llm import InvokeResult
from tests.test_integration import MockOpenAIServer, _completion

JAVA = "src/main/java/com/example/app/Mapper.java"
DATABIND = "com.fasterxml.jackson.core:jackson-databind"
VERSION = "2.17.1"
OTHER_VERSION = "9.9.9-local"
NO_FINDINGS = '{"findings": []}'
OUTPUT_HEADER = "\n\n## Output Format"

OLD_JAVA = """package com.example.app;

public class Mapper {
    public String render(String name) {
        return name;
    }

    public int size() {
        return 0;
    }

    public boolean empty() {
        return true;
    }

    private static String formatLabel(String value) {
        return "[" + value + "]";
    }
}
"""

NEW_JAVA = OLD_JAVA.replace(
    "package com.example.app;\n\npublic class Mapper {\n",
    "package com.example.app;\n\nimport com.fasterxml.jackson.databind.ObjectMapper;\n\n"
    "public class Mapper {\n    private final ObjectMapper mapper = new ObjectMapper();\n\n",
).replace("        return name;\n", "        return formatLabel(name);\n")


def _pom(version: str) -> str:
    return (
        "<project>\n"
        "  <modelVersion>4.0.0</modelVersion>\n"
        "  <groupId>com.example</groupId>\n"
        "  <artifactId>app</artifactId>\n"
        "  <version>1.0</version>\n"
        "  <dependencies>\n"
        "    <dependency>\n"
        "      <groupId>com.fasterxml.jackson.core</groupId>\n"
        "      <artifactId>jackson-databind</artifactId>\n"
        f"      <version>{version}</version>\n"
        "    </dependency>\n"
        "  </dependencies>\n"
        "</project>\n"
    )


def _diff() -> str:
    body = difflib.unified_diff(
        OLD_JAVA.splitlines(keepends=True), NEW_JAVA.splitlines(keepends=True),
        fromfile=f"a/{JAVA}", tofile=f"b/{JAVA}", n=3,
    )
    return f"diff --git a/{JAVA} b/{JAVA}\n" + "".join(body)


def _repo(root: Path, version: str = VERSION) -> Path:
    root.mkdir(parents=True)
    (root / "pom.xml").write_text(_pom(version), encoding="utf-8")
    java = root / JAVA
    java.parent.mkdir(parents=True)
    java.write_text(NEW_JAVA, encoding="utf-8")
    return root


def _block(prompt: str, header: str) -> str:
    """The prompt's ``header`` block up to the next block or the output section; "" if absent."""
    if header not in prompt:
        return ""
    start = prompt.index(header)
    ends = [
        end for end in (prompt.find("\n\n### ", start + len(header)), prompt.find(OUTPUT_HEADER, start))
        if end >= 0
    ]
    return prompt[start:min(ends)]


class CapturingLLM:
    """Records every ``(system, user)`` prompt and answers with no findings."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=None):
        with self._lock:
            self.calls.append((system, user))
        return InvokeResult(
            text=NO_FINDINGS, input_tokens=10, output_tokens=5,
            model="fake-model", backend="fake", elapsed_ms=1,
        )

    def worker_prompt(self) -> str:
        (prompt,) = [user for _system, user in self.calls if f"diff --git a/{JAVA} " in user]
        return prompt


class ReadingForge:
    """A forge that reads files at a head sha from ``files`` and records every read."""

    name = "fake"

    def __init__(self, diff: str, files: dict[str, str]) -> None:
        self.diff = diff
        self.files = files
        self.reads: list[str] = []

    @staticmethod
    def parse_pr_url(url: str) -> PRRef | None:
        return None

    def get_pr(self, ref: PRRef) -> PRData:
        return PRData(
            title="Map with Jackson", description="", author="acme-dev",
            source_branch="feature", target_branch="main",
            source_sha="a" * 40, target_sha="b" * 40, raw={},
        )

    def get_diff(self, ref: PRRef) -> str:
        return self.diff

    def get_file_content(self, ref: PRRef, path: str, *, sha: str) -> str | None:
        self.reads.append(path)
        return self.files.get(path)

    def list_threads(self, ref: PRRef) -> list:
        return []


REF = PRRef(forge="fake", host="example.com", owner="acme", repo="app", number=1, url="https://example.com/acme/app/1")


def _local_review(tmp_path: Path, repo_dir: RepoDir | None) -> tuple[dict, CapturingLLM]:
    diff_file = tmp_path / "p.patch"
    diff_file.write_text(_diff(), encoding="utf-8")
    forge = LocalDiffForge(_diff(), path=str(diff_file))
    llm = CapturingLLM()
    result = orchestrator.orchestrate_review(
        forge, LocalDiffForge.ref_for(str(diff_file)), llm, post=False, repo_dir=repo_dir, max_workers=1,
    )
    return result, llm


@pytest.fixture
def llm_server():
    server = MockOpenAIServer(routes={"*": lambda payload: (200, _completion(NO_FINDINGS, "stop"))})
    base_url = server.start()
    yield server, base_url
    server.stop()


class TestDiffFileWithRepoDirThroughTheCli:
    """The issue's shape: ``prxref review --diff-file p.patch --repo-dir <checkout>``."""

    def _worker_prompt(self, monkeypatch, llm_server, tmp_path, *, with_repo_dir: bool) -> str:
        server, base_url = llm_server
        repo = _repo(tmp_path / "checkout")
        patch = tmp_path / "p.patch"
        patch.write_text(_diff(), encoding="utf-8")
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "openai-compat")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", base_url)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "fast")
        monkeypatch.delenv("PRXREF_REPO_CONTEXT", raising=False)
        cli._run_review(None, diff_file=str(patch), repo_dir=str(repo) if with_repo_dir else None)
        users = [
            m["content"] for r in server.requests for m in r["payload"]["messages"]
            if m.get("role") == "user" and f"diff --git a/{JAVA} " in m["content"]
        ]
        assert len(users) == 1
        return users[0]

    def test_repo_dir_gives_the_dependency_versions_block(self, monkeypatch, llm_server, tmp_path):
        prompt = self._worker_prompt(monkeypatch, llm_server, tmp_path, with_repo_dir=True)
        block = _block(prompt, DEPENDENCY_HEADER)
        assert DATABIND in block
        assert VERSION in block

    def test_without_repo_dir_there_is_no_such_block(self, monkeypatch, llm_server, tmp_path):
        prompt = self._worker_prompt(monkeypatch, llm_server, tmp_path, with_repo_dir=False)
        assert DEPENDENCY_HEADER not in prompt
        assert DEFINITIONS_HEADER not in prompt


class TestDefinitions:
    """A symbol the added lines call, defined outside the hunk in the same file, is shown."""

    def test_repo_dir_gives_the_same_file_definition(self, tmp_path):
        _result, llm = _local_review(tmp_path, RepoDir(_repo(tmp_path / "checkout")))
        block = _block(llm.worker_prompt(), DEFINITIONS_HEADER)
        assert f"{JAVA}:" in block
        assert "private static String formatLabel(String value)" in block

    def test_without_repo_dir_there_is_no_definitions_block(self, tmp_path):
        _result, llm = _local_review(tmp_path, None)
        assert DEFINITIONS_HEADER not in llm.worker_prompt()


class TestForgeReaderWins:
    """A forge that can read files at the head sha serves chunk context even beside ``repo_dir``."""

    def test_forge_is_read_and_repo_dir_content_is_not_shown(self, tmp_path):
        forge = ReadingForge(_diff(), {"pom.xml": _pom(VERSION), JAVA: NEW_JAVA})
        repo = RepoDir(_repo(tmp_path / "checkout", version=OTHER_VERSION))
        llm = CapturingLLM()
        orchestrator.orchestrate_review(forge, REF, llm, post=False, repo_dir=repo, max_workers=1)
        assert "pom.xml" in forge.reads
        assert JAVA in forge.reads
        prompt = llm.worker_prompt()
        assert VERSION in _block(prompt, DEPENDENCY_HEADER)
        assert OTHER_VERSION not in prompt


class TestNeverRaises:
    """A ``repo_dir`` that serves nothing, or whose ``read`` raises, still gives a review."""

    def test_an_empty_repo_dir_gives_a_review_with_no_context(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        result, llm = _local_review(tmp_path, RepoDir(empty))
        prompt = llm.worker_prompt()
        assert DEPENDENCY_HEADER not in prompt
        assert DEFINITIONS_HEADER not in prompt
        assert result["verdict"] != "Error"

    def test_a_raising_read_gives_a_review_with_no_context(self, tmp_path):
        class RaisingRepoDir(RepoDir):
            def read(self, path: str) -> str | None:
                raise RuntimeError("disk on fire")

        result, llm = _local_review(tmp_path, RaisingRepoDir(_repo(tmp_path / "checkout")))
        prompt = llm.worker_prompt()
        assert DEPENDENCY_HEADER not in prompt
        assert DEFINITIONS_HEADER not in prompt
        assert result["verdict"] != "Error"


class TestMakeFileReader:
    """The one construction path: forge first, ``repo_dir`` as the fallback, cached per run."""

    def test_neither_source_gives_none(self, tmp_path):
        forge = LocalDiffForge(_diff(), path=str(tmp_path / "p.patch"))
        pr = forge.get_pr(LocalDiffForge.ref_for(str(tmp_path / "p.patch")))
        assert orchestrator._make_file_reader(forge, REF, pr, repo_dir=None) is None

    def test_repo_dir_reads_are_cached(self, tmp_path):
        calls: list[str] = []

        class CountingRepoDir(RepoDir):
            def read(self, path: str) -> str | None:
                calls.append(path)
                return super().read(path)

        forge = LocalDiffForge(_diff(), path=str(tmp_path / "p.patch"))
        pr = forge.get_pr(LocalDiffForge.ref_for(str(tmp_path / "p.patch")))
        read = orchestrator._make_file_reader(
            forge, REF, pr, repo_dir=CountingRepoDir(_repo(tmp_path / "checkout")),
        )
        assert read("pom.xml") == _pom(VERSION)
        assert read("pom.xml") == _pom(VERSION)
        assert read("missing.txt") is None
        assert read("missing.txt") is None
        assert calls == ["pom.xml", "missing.txt"]


def test_payload_is_json_serializable_with_repo_dir(tmp_path):
    result, _llm = _local_review(tmp_path, RepoDir(_repo(tmp_path / "checkout")))
    json.dumps(cli._build_json_result(result))
