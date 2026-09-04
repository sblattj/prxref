"""Regression tests for issue #03: copy/rename diff headers read as deletion.

docs/issues/inbox-2026-09-04/prxref-issues/03-rename-copy-header-read-as-deletion.md
reports that a Bitbucket Server ``copy from``/``copy to`` diff header (a
monorepo package copied to a new location) got the copy's SOURCE path
reported as removed, even though the same diff contains a separate section
that modifies that source file — it is still present in the post-image.

Two tests below reproduce the bug and must FAIL on the current tree:

- ``test_copy_header_parses_to_copied_status_with_source_old_path`` (issue's
  Test A): ``triage.parse_unified_diff`` has no "copied" status today, so a
  ``copy from``/``copy to`` section is mis-parsed as "renamed" instead.
- ``test_removal_claim_on_a_path_present_in_post_image_is_dropped`` (issue's
  Test B): there is no quality pass that checks a "removed"/"deleted" claim
  against the diff's post-image, so the false-positive finding from the
  issue survives every gate and posts.

Two controls must PASS both before and after the fix:

- a claim about a file that really was deleted (``deleted file mode``, gone
  from the post-image) must stay active — the new gate must not blanket-drop
  every "removed" claim, only ones contradicted by the diff.
- a plain ``rename from``/``rename to`` (no copy) must keep yielding
  ``status == "renamed"`` — the fix must not regress ordinary renames.
"""
from __future__ import annotations

import json

from prxref.forges.base import PRRef
from prxref.orchestrator import orchestrate_review
from prxref.triage import parse_unified_diff
from tests.test_orchestrator import FakeForge, FakeLLM, make_pr

REF = PRRef(
    forge="fake", host="fake.test", owner="acme", repo="widget",
    number=11, url="https://fake.test/acme/widget/pull/11",
)

# The exact two sections from the issue: a Bitbucket Server `copy from` /
# `copy to` header (src:// / dst:// operands, similarity index) creating
# splunk/package.json from servicenow/package.json, followed by a second,
# independent section that modifies the servicenow source file the copy
# read from -- it is still present on the branch.
COPY_AND_MODIFY_DIFF = (
    "diff --git src://src/packages/servicenow/package.json"
    " dst://src/packages/splunk/package.json\n"
    "similarity index 53%\n"
    "copy from src/packages/servicenow/package.json\n"
    "copy to src/packages/splunk/package.json\n"
    "@@ -1,3 +1,4 @@\n"
    " {\n"
    '-  "name": "@syf-mcp/servicenow"\n'
    '+  "name": "@syf-mcp/splunk",\n'
    '+  "version": "1.0.0"\n'
    " }\n"
    "diff --git src://src/packages/servicenow/package.json"
    " dst://src/packages/servicenow/package.json\n"
    "@@ -1,2 +1,3 @@\n"
    '   "scripts": {\n'
    '-    "check-types": "npx tsc --noEmit"\n'
    '+    "check-types": "npx tsc --noEmit",\n'
    '+    "test": "vitest run"\n'
)


def test_copy_header_parses_to_copied_status_with_source_old_path():
    """Test A: `copy from`/`copy to` must yield status "copied", not "renamed".

    The source file's own separate modified section must stay independently
    "modified". old_path on the copy target is already correct today (the
    `diff --git src://... dst://...` header alone supplies both operands) --
    only the status label is wrong, because nothing recognizes a copy as
    distinct from a rename.
    """
    files = parse_unified_diff(COPY_AND_MODIFY_DIFF)
    by_path = {f.path: f for f in files}
    assert set(by_path) == {
        "src/packages/splunk/package.json",
        "src/packages/servicenow/package.json",
    }

    splunk = by_path["src/packages/splunk/package.json"]
    servicenow = by_path["src/packages/servicenow/package.json"]

    assert splunk.old_path == "src/packages/servicenow/package.json"
    assert splunk.status == "copied", (
        f"expected status 'copied' for the copy target, got {splunk.status!r} "
        "-- there is no 'copied' status yet, so parse_unified_diff falls "
        "back to 'renamed' because old_path != new_path"
    )
    assert servicenow.status == "modified", (
        f"expected the source file's own section to stay 'modified', "
        f"got {servicenow.status!r}"
    )


FINDING_JSON = json.dumps({
    "findings": [
        {
            "file": "src/packages/splunk/package.json",
            "line": 2,
            "severity": "warning",
            "confidence": 0.8,
            "title": "ServiceNow package.json removed by rename to splunk",
            "body": (
                "The copy from src/packages/servicenow/package.json to "
                "src/packages/splunk/package.json removed the ServiceNow "
                "package.json; it no longer exists after this PR."
            ),
        }
    ],
    "escalations": [],
})


def test_removal_claim_on_a_path_present_in_post_image_is_dropped():
    """Test B: the issue's exact false-positive finding must be dropped.

    A finding claiming src/packages/servicenow/package.json was "removed"
    by the copy, while that path is present in the diff's post-image (its
    own "modified" section, and it is the copy's still-present source),
    must not post. FakeLLM returns this JSON verbatim for every LLM call
    (both chunk workers and the systemic sweep), so no LLM call is made in
    this test -- the string is a fixed fixture.
    """
    forge = FakeForge(pr=make_pr(), diff=COPY_AND_MODIFY_DIFF)
    llm = FakeLLM(findings_by_path=FINDING_JSON)
    res = orchestrate_review(forge, REF, llm, post=False)

    active_titles = {f.title for f in res["findings_active"]}
    assert "ServiceNow package.json removed by rename to splunk" not in active_titles, (
        "a finding claiming removal of a path present in the post-image "
        "must not survive to post"
    )

    matches = [
        f for f in res["findings_dropped"]
        if f.title == "ServiceNow package.json removed by rename to splunk"
    ]
    assert matches, "the finding must survive as a dropped record, not vanish"
    assert any(
        (f.drop_reason or "").startswith(
            "claims removal of a path present in the post-image"
        )
        for f in matches
    ), f"unexpected drop_reason(s): {[f.drop_reason for f in matches]!r}"


# --- Controls: must PASS today and after the fix -----------------------

DELETED_FILE_DIFF = (
    "diff --git a/src/old.ts b/src/old.ts\n"
    "deleted file mode 100644\n"
    "--- a/src/old.ts\n"
    "+++ /dev/null\n"
    "@@ -1,3 +0,0 @@\n"
    "-old content\n"
    "-more old content\n"
    "-final line\n"
)

OTHER_FILE_DIFF = (
    "diff --git a/src/other.py b/src/other.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/other.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+import foo\n"
    '+print("old.ts removed")\n'
    "+bar()\n"
)

GENUINE_DELETE_DIFF = DELETED_FILE_DIFF + OTHER_FILE_DIFF

CONTROL_FINDING_JSON = json.dumps({
    "findings": [
        {
            "file": "src/other.py",
            "line": 2,
            "severity": "warning",
            "confidence": 0.7,
            "title": "Reference to a file removed elsewhere in this diff",
            "body": "src/old.ts removed; src/other.py still references it by name.",
        }
    ],
    "escalations": [],
})


def test_removal_claim_on_a_genuinely_deleted_path_stays_active():
    """Control: a claim about a truly-deleted path must not be gated.

    src/old.ts carries `deleted file mode` and has no other section in this
    diff, so it really is absent from the post-image -- the new gate must
    tell this apart from the issue's false positive and let it post.
    """
    files = parse_unified_diff(GENUINE_DELETE_DIFF)
    by_path = {f.path: f for f in files}
    assert by_path["src/old.ts"].status == "removed"

    forge = FakeForge(pr=make_pr(), diff=GENUINE_DELETE_DIFF)
    llm = FakeLLM(findings_by_path=CONTROL_FINDING_JSON)
    res = orchestrate_review(forge, REF, llm, post=False)

    active_titles = {f.title for f in res["findings_active"]}
    assert "Reference to a file removed elsewhere in this diff" in active_titles, (
        "a claim about a path that really is absent from the post-image "
        "must not be caught by the removal-claim gate"
    )


PLAIN_RENAME_DIFF = (
    "diff --git a/src/foo.ts b/src/bar.ts\n"
    "similarity index 100%\n"
    "rename from src/foo.ts\n"
    "rename to src/bar.ts\n"
)


def test_plain_rename_still_yields_renamed_status():
    """Control: an ordinary rename (no copy) keeps status "renamed"."""
    files = parse_unified_diff(PLAIN_RENAME_DIFF)
    assert len(files) == 1
    assert files[0].status == "renamed"
    assert files[0].old_path == "src/foo.ts"
    assert files[0].new_path == "src/bar.ts"
