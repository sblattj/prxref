"""Removal claims about symbols and files the head tree still has (#116).

A docs-heavy PR adds READMEs that mention a hook; the PR description says the
hook was removed earlier, and the model repeats that as "Remove docs for the
deleted useDraftLock hook" on every README. The hook file is untouched by the
PR, so the diff-only :func:`prxref.quality.apply_removal_claim_check` never
sees it. :func:`prxref.quality.apply_head_removal_claim_check` reads the head
tree and drops those claims.

The orchestrator tests run the REAL reviewer and passes over a three-README
diff with a scripted LLM. The head tree is a ``--repo-dir`` checkout, or a
fake forge with the head-sha reads ``repo_reader.forge_reader`` probes
(``get_file_content``/``list_paths``), the way the production gate runs with
repo_context off. No test touches the network.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from prxref import orchestrator
from prxref.forges.base import PathListing
from prxref.forges.repo_dir import RepoDir
from prxref.llm import InvokeResult
from prxref.quality import apply_head_removal_claim_check, head_removal_claims_name_symbol
from prxref.triage import Finding, parse_unified_diff
from tests.test_orchestrator import REF, FakeForge, make_pr

SWEEP_MARK = "running a systemic sweep"
NO_FINDINGS = json.dumps({"findings": []})
HOOK = "src/components/drafts/use-draft-lock.tsx"
HOOK_TEXT = (
    'import { useState } from "react";\n'  # 1
    "\n"  # 2
    "export function useDraftLock({ id }: { id: string }) {\n"  # 3
    "  const [locked] = useState(false);\n"  # 4
    "  return locked;\n"  # 5
    "}\n"  # 6
)
EDITOR = "src/features/widgets/editor.tsx"
EDITOR_TEXT = 'import { useDraftLock } from "../../components/drafts/use-draft-lock";\n'
READMES = ("docs/drafts/README.md", "docs/widgets/README.md", "docs/editor/README.md")
README_LINES = (
    "# Drafts",
    "",
    "The editor holds a draft open with `useDraftLock` while it saves.",
)
TITLE = "Remove docs for the deleted useDraftLock hook"
BODY = (
    "The PR description says this hook was deleted in an earlier PR, so this README documents "
    "code that no longer exists. Drop the reference or point to the hook that replaced it."
)
DESCRIPTION = (
    "The `useDraftLock` hook was removed in an earlier PR; its notes now live in a historical section."
)


def _readme_diff(path: str) -> str:
    body = "\n".join(f"+{line}" for line in README_LINES)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(README_LINES)} @@\n"
        f"{body}\n"
    )


PATCH = "".join(_readme_diff(p) for p in READMES)
DIFF_RE = re.compile(r"^diff --git a/(\S+) ", re.MULTILINE)


class _ScriptLLM:
    """Answers each chunk with the issue's finding on every README it shows; the sweep with nothing."""

    def __init__(self, *, title: str = TITLE, body: str = BODY):
        self.title = title
        self.body = body

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        if SWEEP_MARK in system:
            text = NO_FINDINGS
        else:
            found = [
                {
                    "file": path, "line": 3, "severity": "error", "confidence": 0.9,
                    "title": self.title, "body": self.body,
                }
                for path in dict.fromkeys(DIFF_RE.findall(user))
                if path in READMES
            ]
            text = json.dumps({"findings": found})
        return InvokeResult(
            text=text, input_tokens=10, output_tokens=5, model="fake-model", backend="fake", elapsed_ms=1,
        )


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    monkeypatch.setattr(orchestrator, "_elapsed_ms", lambda t0: 0)


def _head(root: Path, *, hook: bool = True) -> Path:
    for path in READMES:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("\n".join(README_LINES) + "\n", encoding="utf-8")
    (root / EDITOR).parent.mkdir(parents=True, exist_ok=True)
    (root / EDITOR).write_text(EDITOR_TEXT if hook else "export {};\n", encoding="utf-8")
    if hook:
        (root / HOOK).parent.mkdir(parents=True, exist_ok=True)
        (root / HOOK).write_text(HOOK_TEXT, encoding="utf-8")
    return root


class _ListingForge(FakeForge):
    """FakeForge plus head-sha reads, counting ``list_paths`` calls; ``fail`` makes the lister raise."""

    def __init__(self, *args, repo_files: dict[str, str], fail: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.repo_files = repo_files
        self.list_fail = fail
        self.list_calls = 0

    def get_file_content(self, ref, path, sha=None):
        return self.repo_files.get(path)

    def list_paths(self, ref, sha=None):
        self.list_calls += 1
        if self.list_fail:
            raise RuntimeError("listing down")
        return PathListing(paths=tuple(sorted(self.repo_files)), complete=True)


def _pr():
    pr = make_pr()
    pr.description = DESCRIPTION
    return pr


def _review(tmp_path: Path, *, hook: bool = True, repo_dir: bool = True, **kwargs):
    root = _head(tmp_path / "repo", hook=hook)
    forge = FakeForge(pr=_pr(), diff=PATCH)
    kwargs.setdefault("repo_context", "repo")
    kwargs.setdefault("max_workers", 1)
    return orchestrator.orchestrate_review(
        forge, REF, _ScriptLLM(), post=False,
        repo_dir=RepoDir(root) if repo_dir else None, **kwargs,
    )


def _forge_files(*, hook: bool = True) -> dict[str, str]:
    files = {path: "\n".join(README_LINES) + "\n" for path in READMES}
    files[EDITOR] = EDITOR_TEXT if hook else "export {};\n"
    if hook:
        files[HOOK] = HOOK_TEXT
    return files


def _forge_review(*, hook: bool = True, fail: bool = False, llm=None, ci_wiring: str = "off"):
    """repo_context off (the default) and no --repo-dir: the production gate's setup.

    ci_wiring is off by default here because that stage lists the tree on
    its own reader, so ``list_calls`` then counts only the removal pass.
    """
    forge = _ListingForge(pr=_pr(), diff=PATCH, repo_files=_forge_files(hook=hook), fail=fail)
    res = orchestrator.orchestrate_review(
        forge, REF, llm or _ScriptLLM(), post=False, repo_context="off", max_workers=1,
        ci_wiring=ci_wiring,
    )
    return forge, res


def _issue_findings(findings) -> list[Finding]:
    return [f for f in findings if f.title == TITLE]


DROP = f"claims removal of a symbol defined at head: {HOOK}:3 (useDraftLock)"


class TestTheIssueThroughTheOrchestrator:
    def test_the_hook_at_head_drops_every_deleted_hook_claim(self, tmp_path):
        assert HOOK not in PATCH
        res = _review(tmp_path)
        dropped = _issue_findings(res["findings_dropped"])
        assert _issue_findings(res["findings_active"]) == []
        assert sorted(f.file for f in dropped) == sorted(READMES)
        for f in dropped:
            assert f.drop_reason == DROP
            assert "use-draft-lock.tsx:3" in f.drop_reason
        assert res["verdict"] != "Request-Changes"
        assert res["removal_claims"] == {
            "diff_refuted": 0, "head_refuted": 3, "reader": True, "listing": True,
            "listing_source": "repo_context",
        }

    def test_repo_context_off_drops_them_through_the_forge_listing(self):
        forge, res = _forge_review()
        dropped = _issue_findings(res["findings_dropped"])
        assert _issue_findings(res["findings_active"]) == []
        assert sorted(f.file for f in dropped) == sorted(READMES)
        assert {f.drop_reason for f in dropped} == {DROP}
        assert res["verdict"] != "Request-Changes"
        assert res["removal_claims"] == {
            "diff_refuted": 0, "head_refuted": 3, "reader": True, "listing": True,
            "listing_source": "forge",
        }
        assert forge.list_calls == 1

    def test_repo_context_off_with_the_hook_absent_keeps_the_claims(self):
        _, res = _forge_review(hook=False)
        assert sorted(f.file for f in _issue_findings(res["findings_active"])) == sorted(READMES)
        assert res["verdict"] == "Request-Changes"
        assert res["removal_claims"]["head_refuted"] == 0
        assert res["removal_claims"]["listing_source"] == "forge"

    def test_no_symbol_claim_makes_no_listing_call(self):
        for title, body in (
            ("Typo in heading", "Spelling."),
            ("Dead link", "`docs/history.md` was removed, so this link points nowhere."),
        ):
            forge, res = _forge_review(llm=_ScriptLLM(title=title, body=body))
            assert forge.list_calls == 0, title
            assert res["removal_claims"]["listing"] is False
            assert res["removal_claims"]["listing_source"] is None

    def test_with_ci_wiring_on_only_its_own_listing_call_is_made(self):
        forge, _ = _forge_review(llm=_ScriptLLM(title="Typo in heading", body="Spelling."), ci_wiring="on")
        assert forge.list_calls == 1

    def test_a_raising_lister_leaves_symbol_claims(self):
        forge, res = _forge_review(fail=True)
        assert forge.list_calls == 1
        assert sorted(f.file for f in _issue_findings(res["findings_active"])) == sorted(READMES)
        assert res["removal_claims"] == {
            "diff_refuted": 0, "head_refuted": 0, "reader": True, "listing": False,
            "listing_source": None,
        }

    def test_the_hook_absent_at_head_keeps_the_claims(self, tmp_path):
        res = _review(tmp_path, hook=False)
        active = _issue_findings(res["findings_active"])
        assert sorted(f.file for f in active) == sorted(READMES)
        assert res["verdict"] == "Request-Changes"
        assert res["removal_claims"]["head_refuted"] == 0

    def test_no_head_tree_changes_nothing(self, tmp_path):
        res = _review(tmp_path, repo_dir=False, repo_context="off")
        assert sorted(f.file for f in _issue_findings(res["findings_active"])) == sorted(READMES)
        assert res["removal_claims"] == {
            "diff_refuted": 0, "head_refuted": 0, "reader": False, "listing": False,
            "listing_source": None,
        }

    def test_repo_context_off_with_a_repo_dir_lists_the_checkout(self, tmp_path):
        res = _review(tmp_path, repo_context="off")
        assert _issue_findings(res["findings_active"]) == []
        assert res["removal_claims"]["listing_source"] == "repo-dir"
        assert res["removal_claims"]["head_refuted"] == 3


def _finding(title: str, body: str = "", *, severity: str = "warning", file: str = READMES[0]) -> Finding:
    return Finding(file=file, line=3, severity=severity, confidence=0.9, title=title, body=body)


HEAD = {
    HOOK: HOOK_TEXT,
    EDITOR: EDITOR_TEXT,
    "docs/history.md": "# History\n",
    "src/app.py": "def run():\n    return 1\n",
    "src/guard.py": "class Guard:\n    pass\n",
}
LISTING = sorted(HEAD)
FILES = parse_unified_diff(PATCH)


def _check(findings, *, read=HEAD.get, listing=LISTING, files=FILES):
    return apply_head_removal_claim_check(findings, files, read, listing)


class TestThePass:
    def test_the_issue_finding_is_dropped_at_any_severity(self):
        for severity in ("error", "warning", "outofscope"):
            [out] = _check([_finding(TITLE, BODY, severity=severity)])
            assert out.drop_reason == f"claims removal of a symbol defined at head: {HOOK}:3 (useDraftLock)"

    def test_a_backticked_symbol_subject_is_dropped(self):
        [out] = _check([_finding("Stale reference", "`useDraftLock` no longer exists, so this link is dead.")])
        assert out.drop_reason is not None and f"{HOOK}:3" in out.drop_reason

    def test_a_symbol_absent_at_head_stays(self):
        [out] = _check([_finding("Remove docs for the deleted useGhostLock hook", BODY)])
        assert out.drop_reason is None

    def test_a_file_outside_the_diff_present_at_head_is_dropped(self):
        [out] = _check([_finding("Dead link", "`docs/history.md` was removed, so this link points nowhere.")])
        assert out.drop_reason == "claims removal of a file present at head: docs/history.md"

    def test_a_file_outside_the_diff_absent_at_head_stays(self):
        [out] = _check([_finding("Dead link", "`docs/gone.md` was removed, so this link points nowhere.")])
        assert out.drop_reason is None

    def test_a_file_present_by_read_alone_is_dropped(self):
        [out] = _check(
            [_finding("Dead link", "`docs/history.md` was removed, so this link points nowhere.")],
            listing=None,
        )
        assert out.drop_reason == "claims removal of a file present at head: docs/history.md"

    def test_the_stale_old_extension_note_stays(self):
        # The hook moved from .ts to .tsx: the old path really is gone.
        [out] = _check([_finding("Stale note", "`src/components/drafts/use-draft-lock.ts` was removed.")])
        assert out.drop_reason is None

    def test_a_removed_null_check_in_a_file_stays(self):
        [out] = _check([_finding("Null dereference", "The removed null check in `src/app.py` lets None reach run().")])
        assert out.drop_reason is None

    def test_a_removed_plain_word_is_not_a_symbol(self):
        [out] = _check([_finding("Guard gone", "The PR removed the guard in `src/guard.py`.")])
        assert out.drop_reason is None

    def test_a_removed_call_is_not_a_removed_definition(self):
        findings = [
            _finding("Lock gone", "This removed the `useDraftLock` call in the editor."),
            _finding("Lock gone", "The call to `useDraftLock` was removed from the editor."),
        ]
        assert [f.drop_reason for f in _check(findings)] == [None, None]

    def test_every_governed_object_must_be_at_head(self):
        [out] = _check([
            _finding("Stale docs", "`useDraftLock` was removed and `docs/gone.md` was removed too.")
        ])
        assert out.drop_reason is None

    def test_a_claim_about_a_diff_path_is_left_to_the_diff_check(self):
        [out] = _check([_finding("Stale", f"`{READMES[1]}` was removed.")])
        assert out.drop_reason is None

    def test_no_head_tree_changes_nothing(self):
        findings = [_finding(TITLE, BODY)]
        assert _check(findings, read=None, listing=None) == findings

    def test_a_listing_without_a_reader_judges_files_only(self):
        out = _check(
            [_finding(TITLE, BODY), _finding("Dead link", "`docs/history.md` was removed.")],
            read=None,
        )
        assert out[0].drop_reason is None
        assert out[1].drop_reason == "claims removal of a file present at head: docs/history.md"

    def test_a_read_that_raises_counts_as_not_found(self):
        def boom(path):
            raise OSError("down")

        [out] = _check([_finding(TITLE, BODY)], read=boom)
        assert out.drop_reason is None

    def test_deterministic_and_dropped_findings_pass_through(self):
        det = _finding(TITLE, f"{BODY} (deterministic check, no model)")
        gone = Finding(
            file=READMES[0], line=3, severity="error", confidence=0.9, title=TITLE, body=BODY,
            drop_reason="earlier",
        )
        assert _check([det, gone]) == [det, gone]

    def test_order_and_length_are_preserved(self):
        keep = _finding("Typo", "Spelling in the heading.")
        findings = [keep, _finding(TITLE, BODY), keep]
        out = _check(findings)
        assert len(out) == 3
        assert out[0] is keep and out[2] is keep
        assert out[1].drop_reason is not None

    def test_an_index_file_defines_its_directory_name(self):
        head = {"src/hooks/useDraftLock/index.ts": "export const useDraftLock = () => true;\n"}
        [out] = _check([_finding(TITLE, BODY)], read=head.get, listing=sorted(head))
        assert out.drop_reason == (
            "claims removal of a symbol defined at head: src/hooks/useDraftLock/index.ts:1 (useDraftLock)"
        )


class TestNamesASymbol:
    def test_only_an_active_governed_symbol_claim_counts(self):
        assert head_removal_claims_name_symbol([_finding(TITLE, BODY)], FILES)
        assert not head_removal_claims_name_symbol([_finding("Typo", "Spelling.")], FILES)
        assert not head_removal_claims_name_symbol(
            [_finding("Dead link", "`docs/history.md` was removed.")], FILES,
        )
        assert not head_removal_claims_name_symbol(
            [_finding("Lock gone", "This removed the `useDraftLock` call in the editor.")], FILES,
        )
        gone = Finding(
            file=READMES[0], line=3, severity="error", confidence=0.9, title=TITLE, body=BODY,
            drop_reason="earlier",
        )
        assert not head_removal_claims_name_symbol([gone], FILES)
        assert not head_removal_claims_name_symbol([_finding(TITLE, f"{BODY} (deterministic check, no model)")], FILES)
