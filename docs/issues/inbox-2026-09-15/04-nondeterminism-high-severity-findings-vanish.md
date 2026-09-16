
**Title:** Finding set varies between identical runs, and high-severity findings are among the ones that vanish

**Labels:** bug, consistency, reliability

**Version:** prxref 0.11.0 · `claude-opus-5,claude-sonnet-5` · Bitbucket Server forge

---

## Summary

Repeated runs over the same PR at the same commit, same model list, minutes
apart, return different finding sets. This is not confined to marginal findings:
in one case the only security-relevant finding in a 36-file PR appeared in one
run and was absent from the run that posted.

## Evidence

**Corpus A** — `refactor: address Figma review follow-ups`, 6 files:

| Run | Findings | Verdict |
|---|---|---|
| 1 (`--no-post`) | 2 error | Request-Changes |
| 2 (posting) | 2 error + 1 warning | Request-Changes |

The extra warning was itself a false positive (filed separately as the
self-hedged-finding issue).

**Corpus B** — `feat: add Splunk tools and expand ServiceNow toolset`, 36 files:

| Run | Active findings |
|---|---|
| 1 (`--no-post`) | 7 (6 verified true, 1 false) |
| 2 (posting) | 2 |

The finding that disappeared:

> 🟧 **Credential-store fallback grants ambient creds to any client**
> (`auth.ts:118`, confidence 0.65) — auth falls back to
> `~/.syf-mcp/credentials.json` when `x-servicenow-*` / `x-splunk-*` headers are
> absent, so any caller reaching the server is authenticated as the host user.

Verified **true**, and materially so: the server is started with
`NodeHttpServer.layer(() => createServer(), { port })` — no `host`, so Node
binds every interface. It was absent from the run that actually posted comments.

**Corpus C** — a 4-file PR: dry run produced 1 warning + 1 outofscope; the
posting run produced only the warning, dropping a true test-quality finding.

## Impact

- An operator who runs prxref once and reads the posted comments cannot know
  what was missed, and the missing item may be the most severe one.
- Comparing runs to evaluate a prompt or model change compares dice rolls.
- Dry-run output is not predictive of what the posting run will say, which
  defeats the point of `--no-post` as a preview.

## Suggested fix

1. Pin temperature / sampling for the review pass. If it is already 0, document
   that, because the observed behaviour is not what a reader would predict — and
   investigate the remaining source of variance (chunk ordering, concurrency,
   prompt assembly).
2. Consider N-run consensus for `error`-severity candidates, or at minimum log a
   per-run seed so a result can be reproduced.
3. Make the dry-run and posting paths share one execution rather than running
   the pipeline twice.

---

## Addendum — still present in 0.11.1, and dry run still disagrees with the posting run

Reconfirmed on two PRs in one session, `claude-opus-5`, same commits, minutes apart.

| PR | head | dry run (`--no-post`) | posting run | delta |
|---|---|---|---|---|
| `syf-mcp` #48 | `c989af3d` | 1 error + 1 warning | 2 warnings | same 2 findings, **one silently downgraded from `error` to `warning`** |
| `syf-mcp` #45 | `f05e179c` | 2 warnings | 1 warning | **one finding disappeared entirely** |

Both observations reinforce the original report:

- Severity is not stable across runs for an identical commit. #48's finding 1 was
  emitted as `error` — the severity that drives a NEEDS_WORK verdict — and as
  `warning` moments later. A verdict that flips on a re-run is not a verdict.
- The dry run is still not predictive of the posting run, which is the specific
  problem "make the dry-run and posting paths share one execution" was meant to
  fix. Any workflow that verifies findings from `--no-post` output and then posts
  is verifying a different set than the one that gets published.

For the record, on #45 the finding that vanished was one of two on the same file;
both were independently verified **true**. So the variance is not a quality gate
correctly discarding a weak candidate — a real finding was dropped.

---

## Addendum — 0.12.1, and the two findings that vanished were the two most severe

Reconfirmed on `syf-mcp` #57 (`feat(mcp): add Nexus IQ tools`), head
`db2044b7`, `claude-opus-5`, same commit, minutes apart. This is the sharpest
reproduction in the corpus because the vanishing findings were **the highest-value
ones in the review**, not marginal ones.

| Run | Active findings on Nexus-IQ source |
|---|---|
| 1 (`--no-post`) | included `acquire.ts` (refresh whole-run abort) and `nexus-iq-refresh.ts` (`--force` no-op) — both blocking |
| 2 (posting) | **both absent**; posted set was 1 error + 5 warnings, neither blocker among them |

Both dropped findings were independently verified **true** and are the strongest
reasons the PR should not merge:

- `acquire.ts` — the Nexus IQ refresh is `yield*`-ed directly instead of through
  the module's `attempt()` wrapper, so its `Error` channel short-circuits the
  whole `Acquire.refresh` and discards every already-refreshed credential. A
  contract violation, `error` severity by any reading.
- `nexus-iq-refresh.ts` — `--force` cannot rotate a healthy/unreachable token and
  inverts the expiry so a forced refresh expires *sooner*.

The posting run kept a genuine `error` (`components.ts:86` empty-array crash) and
five warnings, so the review still reads as a competent NEEDS_WORK — which is the
danger: an operator reading only the posted comments has no signal that the two
**worst** defects were silently dropped. This directly contradicts the 0.12.0
retest note that "posted output now stable across runs (0 and 0)": stability held
only when the true count was zero. On a PR with real high-severity findings, the
active, above-floor, posted set still differs between two runs at one commit, and
severity-ranked recall is where it differs.

Reinforces the original suggested fix, with one addition: **N-run consensus should
be mandatory for any candidate that would drive a NEEDS_WORK verdict**, because the
observed failure mode is not "a weak finding flickers" but "a blocking finding is
present in run 1 and gone in run 2." The single-run operator cannot detect this;
only the verify-twice workflow caught it here.


