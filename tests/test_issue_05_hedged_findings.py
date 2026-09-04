"""Issue #05 / #11 — the quality gate admits self-hedged findings.

A hedged finding states an unverified precondition about the code's own
behaviour ("If figmaProxy.prepare still leases a client", "If they are
members of the root workspaces globs, npm ci will fail"). Both shipped as
`warning` under the user's name and both were false.

Frozen contract: such a finding is dropped with
``drop_reason`` starting ``hedged: `` followed by the matched phrase, and
the gate entry point is ``prxref.quality.apply_hedge_gate(findings)`` —
pure, order-preserving, sets ``drop_reason`` instead of discarding.

Tests A and B FAIL today (no hedge gate exists). Test C is the control
corpus and must PASS both today and after the fix; today it runs against a
no-op shim (``getattr`` fallback) so a green result means "these bodies are
legitimate", and after the fix the same assertions bind the real gate.
"""
from __future__ import annotations

import json

import pytest

from prxref import quality
from prxref.orchestrator import orchestrate_review
from prxref.triage import Finding
from tests.test_orchestrator import REF, FakeForge, FakeLLM  # noqa: F401

MCP_PATH = "src/apps/mcp/src/services/mcp-server.ts"
DOCKERFILE = "Dockerfile"


def _diff_with_lines(path: str, lines: list[str]) -> str:
    body = "\n".join("+" + ln for ln in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}\n"
    )


# mcp-server.ts: the createMcpHandler return lands on line 37, the line the
# real finding cited. Dockerfile: the workspace-manifest COPY lands on 47.
_MCP_LINES = [f"const filler{i} = {i};" for i in range(1, 37)]
_MCP_LINES.append("  return createMcpHandler(serverFactory);")
_MCP_LINES += [f"const tail{i} = {i};" for i in range(1, 4)]

_DOCKER_LINES = [f"RUN echo layer{i}" for i in range(1, 47)]
_DOCKER_LINES.append("COPY src/services/jira/package.json ./src/services/jira/")
_DOCKER_LINES += [f"RUN echo tail{i}" for i in range(1, 4)]

TWO_FILE_DIFF = _diff_with_lines(MCP_PATH, _MCP_LINES) + _diff_with_lines(
    DOCKERFILE, _DOCKER_LINES
)

# Verbatim from docs/issues/inbox-2026-09-04/prxref-issues/
# 05-quality-gate-admits-self-hedged-findings.md
FIGMA_BODY = (
    "The diff removes the close()/stream-wrapper lifecycle and returns "
    "createMcpHandler(serverFactory) directly, so nothing calls figma cleanup "
    "at end of request. If figmaProxy.prepare still leases a client, that "
    "lease is only released when the enclosing scope (service layer) closes, "
    "not per request."
)
# Verbatim from docs/issues/inbox-2026-09-04/prxref-issue-2026-09-03.md §1
NPM_CI_BODY = (
    "The deps stage copies only implementor, agentcore, github-copilot, "
    "typescript-config, bitbucket and jira manifests, but this PR adds "
    "workspaces src/services/confluence, src/services/sso and "
    "src/agents/deep-agent. If they are members of the root workspaces globs, "
    "`npm ci` in this stage will fail."
)

# FakeLLM in STRING mode returns this verbatim for every call, the systemic
# sweep included; the sweep copies are dropped as duplicates, which is why the
# assertions below look at every dropped finding sharing a title.
HEDGED_RESPONSE = json.dumps(
    {
        "findings": [
            {"file": MCP_PATH, "line": 37, "severity": "warning", "confidence": 0.7,
             "title": "Per-request Figma resources no longer released after response",
             "body": FIGMA_BODY},
            {"file": DOCKERFILE, "line": 47, "severity": "warning", "confidence": 0.75,
             "title": "deps stage misses new workspace manifests",
             "body": NPM_CI_BODY},
        ],
        "escalations": [],
    }
)


def _f(body: str, *, title: str = "Finding") -> Finding:
    return Finding(
        file=MCP_PATH, line=37, severity="warning", confidence=0.8,
        title=title, body=body,
    )


class TestAEndToEnd:
    """Both real-world hedged findings must be dropped by orchestrate_review."""

    def test_hedged_findings_are_dropped(self):
        forge = FakeForge(diff=TWO_FILE_DIFF)
        llm = FakeLLM(HEDGED_RESPONSE)

        res = orchestrate_review(forge, REF, llm, post=False)

        active_titles = {f.title for f in res["findings_active"]}

        for title in (
            "Per-request Figma resources no longer released after response",
            "deps stage misses new workspace manifests",
        ):
            assert title not in active_titles, f"{title!r} survived the gate"
            reasons = [
                f.drop_reason or ""
                for f in res["findings_dropped"] if f.title == title
            ]
            assert reasons, f"{title!r} not in findings_dropped"
            assert any(r.startswith("hedged:") for r in reasons), (
                f"{title!r} dropped for {reasons!r}, expected a 'hedged:' reason"
            )


HEDGED_BODIES = [
    # 1. the figmaProxy finding's own shape
    ("if-still", FIGMA_BODY),
    # 2. the npm ci finding's own shape
    ("if-membership", NPM_CI_BODY),
    ("if-is-still",
     "The handler is registered twice. If the legacy router is still mounted "
     "in server.ts, requests will be dispatched to both."),
    ("assuming",
     "Assuming the cache is keyed by tenant, this write will clobber another "
     "tenant's entry on the next flush."),
    ("unless-already",
     "The migration adds a NOT NULL column. Unless the backfill job already "
     "ran in production, this deploy will fail."),
    ("may-still",
     "The subscription is removed from the map, but the socket may still be "
     "held open by the outer pool."),
    ("cannot-verify",
     "This drops the retry wrapper. I cannot verify whether the caller "
     "retries, so the request may now fail permanently."),
    ("not-visible-in-diff",
     "The token refresh is not visible in the diff, so this change probably "
     "leaves the credential stale."),
    ("presumably",
     "The flag is read once at import; presumably the config loader runs "
     "before this module, otherwise the value is always false."),
    ("only-caller",
     "The signature changed from (a, b) to (a). If this is the only caller, "
     "the change is safe; otherwise every other call site breaks."),
    ("likely-still",
     "The listener is detached on unmount, but the timer likely still holds a "
     "reference to the component instance."),
]


class TestBGateUnit:
    """Direct unit test of the frozen entry point ``apply_hedge_gate``."""

    @pytest.mark.parametrize("label,body", HEDGED_BODIES, ids=[b[0] for b in HEDGED_BODIES])
    def test_hedged_body_is_dropped(self, label, body):
        out = quality.apply_hedge_gate([_f(body, title=label)])

        assert len(out) == 1
        assert out[0].drop_reason is not None, f"{label}: not dropped"
        assert out[0].drop_reason.startswith("hedged:"), out[0].drop_reason
        # the reason names the phrase that matched, not just the rule
        assert len(out[0].drop_reason) > len("hedged: "), out[0].drop_reason

    def test_order_and_purity(self):
        findings = [_f(FIGMA_BODY, title="a"), _f("Plain bug.", title="b")]
        out = quality.apply_hedge_gate(findings)

        assert [f.title for f in out] == ["a", "b"]
        assert findings[0].drop_reason is None  # input not mutated


# Control corpus. Every body below contains if/unless/may/assuming/might but
# describes what the code DOES under a runtime condition — a legitimate
# finding. These must stay active. Two are lifted from the existing suite:
# tests/test_integration.py:206,752 and tests/test_orchestrator.py:258.
CONTROL_BODIES = [
    ("divisor",
     "size defaults to None and is used as a divisor on line 42; the diff "
     "adds no guard."),
    ("early-return",
     "Returns early if the list is empty, so the counter is never "
     "incremented."),
    ("retry-spin",
     "The retry loop may spin forever because the deadline is never "
     "decremented."),
    ("typeerror",
     "Throws TypeError if `opts` is undefined: line 12 dereferences "
     "opts.start without a guard."),
    ("lodash-unless",
     "unless() from lodash is called with a string, which it does not "
     "accept."),
    # tests/test_integration.py:206 and :752
    ("jwterror", "decode(token) can raise JWTError if malformed."),
    # tests/test_orchestrator.py:258
    ("null-deref",
     "x may be None when config is missing; data loss follows."),
    # tests/test_quality.py-style concurrency body
    ("concurrency", "Concurrent calls may corrupt state."),
    ("assuming-noun",
     "The parser is assuming-safe only for ASCII; line 20 indexes bytes "
     "directly and raises on multibyte input."),
    ("may-return",
     "readFile may return a Buffer here, and the caller concatenates it with "
     "a string, producing '[object Object]'."),
    ("if-branch",
     "If retries is 0 the loop body never runs, so the initial request is "
     "skipped entirely."),
    ("unless-flag",
     "The endpoint is registered unless DEBUG is set, so production serves "
     "the unauthenticated route."),
]


class TestCControls:
    """Legitimate findings must survive. PASSES today via the no-op shim.

    Until ``apply_hedge_gate`` exists the shim below is an identity function,
    so this test is green today by construction; its value is that the SAME
    assertions bind the real gate the moment the fix seat lands it.
    """

    @pytest.mark.parametrize("label,body", CONTROL_BODIES, ids=[b[0] for b in CONTROL_BODIES])
    def test_control_stays_active(self, label, body):
        gate = getattr(quality, "apply_hedge_gate", lambda fs: list(fs))

        out = gate([_f(body, title=label)])

        assert len(out) == 1
        assert out[0].drop_reason is None, (
            f"false positive on {label!r}: {out[0].drop_reason}"
        )
