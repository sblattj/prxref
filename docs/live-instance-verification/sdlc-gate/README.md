# sdlc review gate: live proof on `sblattj/sdlc-sandbox` (#102)

Repo `sblattj/sdlc-sandbox`, ruleset `sdlc-review` id **24960663** (required check `sdlc-review` pinned to
integration 5278363, strict, 1 approval, last-push approval, dismiss stale), app id **5278363**
(`prxref-sdlc[bot]`), gate version **0.34.0** (branch `sdlc-review-gate`, run from the worktree venv, not
launchd). Model: `nvidia/nemotron-3-ultra-550b-a55b:free` via OpenRouter. Every capture is `gh api` JSON (or
harness output for phase 4) listed in `index.json` with its SHAs and ids. Base for PR 7 and 8:
`c85ee0b56e3227644b35c2fe5198d94f9f6d36c7`.

## Phase 1: a same-named status from a non-app source does not clear the gate

PR #7, head `3ef4a7cc4a568c42d961c267d1e29b40e9b39378`, gate paused, no app check run, no review.
`sblattj` posted a `success` status with context `sdlc-review`
([1-foreign-status.json](1-foreign-status.json)); the PR stayed `mergeable_state: blocked`
([1-pr-blocked.json](1-pr-blocked.json)). Ruleset: [1-ruleset.json](1-ruleset.json).

## Phase 2: the app's check and approval clear the gate on the same head

Same head `3ef4a7c`, gate resumed. Check run **115067619961**: `sdlc-review`, `success`, `app.id` 5278363
([2-check-run.json](2-check-run.json)). `prxref-sdlc[bot]` (Bot) `APPROVED`
([2-app-review.json](2-app-review.json)). PR `mergeable_state: clean` ([2-pr-clean.json](2-pr-clean.json)).

## Phase 3: a new push dismisses the approval and re-gates

Push to head `260b952648dea5fd185a5efb18601f7282558416`: the earlier approval became `DISMISSED`
([3-approval-dismissed.json](3-approval-dismissed.json)) and the PR went `blocked`
([3-pr-blocked-new-head.json](3-pr-blocked-new-head.json)); the gate then published check run
**115067926133**, `success` ([3-check-run-new-head.json](3-check-run-new-head.json)), and the app approved the
new head again.

## Phase 4: failure cases

| Case | Result | Capture |
|---|---|---|
| request-changes-review, pr-blocked-failing | PR #8 head `dfcf9a28309bcea83c08c1d39ea809e913ac298c` (eval/exec/hardcoded password): app review `CHANGES_REQUESTED`, check run 115070136545 `failure`, `blocked`; `gh pr merge 8` refused ("base branch policy prohibits the merge") | [review](4-request-changes-review.json), [pr](4-pr-blocked-failing.json) |
| owner-dismissal | Owner dismissed the app's approve (review 5497355706) on PR #7 head `260b952`: `DISMISSED`, `reviewDecision` `REVIEW_REQUIRED`, `mergeStateStatus` `BLOCKED`, check run 115067926133 still `success` | [4-owner-dismissal.json](4-owner-dismissal.json) |
| bad-signature | 401 `signature rejected`; no new generation | [4-bad-signature.json](4-bad-signature.json) |
| duplicate | same delivery twice: first `accepted`, second `duplicate`; one generation (#4) | [4-duplicate.json](4-duplicate.json) |
| rerequest | `check_run.rerequested` signed delivery: 202, gen 5 on the same head `260b952`, new check run 115070771949 `success` | [4-rerequest.json](4-rerequest.json) |
| incomplete | `PRXREF_LLM_TIMEOUT=5`, head `de067fe`: check run 115071555265 `failure`, Verdict Incomplete (chunk ok, systemic sweep timed out) | [4-incomplete.json](4-incomplete.json) |
| deadline | `PRXREF_GATE_REVIEW_DEADLINE=20`, head `2ad09ab`: check run 115071705158 `failure`, `deadline: review exceeded 20s` | [4-deadline.json](4-deadline.json) |
| stale-head | pushed `7a4e5bf`, then `56e002a` mid-review: gen 6 `stale` ("head moved"), its check run 115071013187 stayed `in_progress` (never `success`); second head got a verdict (see Findings) | [4-stale-head.json](4-stale-head.json) |
| close-reopen | paused, pushed `bfdecc8`, closed: queued gen 8 `cancelled`; reopened: gen 9 `reopened` completed `success`, check run 115071361079 | [4-close-reopen.json](4-close-reopen.json) |
| publish-5xx | in-process gate with injected 502: stderr `injected 502 after GitHub answered 201`; head `26da6bd` has exactly one `sdlc-review` check run, 115071894735, `success` | [4-publish-5xx.json](4-publish-5xx.json) |
| worker-restart | SIGTERM+start while check run 115072319004 was `in_progress` (head `973f0e1`): log `re-queued in-progress generations of a previous worker`; the same check run concluded `success` | [4-worker-restart.json](4-worker-restart.json) |
| secret-rotation | grace 2 min, restart 17:41:09; old-secret delivery at 17:41 accepted (202), at 17:44:43 rejected (401); new secret accepted | [4-rotation-grace.json](4-secret-rotation.json) |

## Findings (behaved differently from the plan or spec)

- **No launchd, branch code.** The gate ran from the worktree venv (`prxref gate serve`); "kickstart" was
  SIGTERM plus start. The app's webhook secret in GitHub settings was not changed in the rotation test; only the
  gate's env changed, then the original env was restored (`cmp` identical).
- **Duplicate returns 202, not 200.** The repeat delivery got HTTP 202 with body `{"status": "duplicate"}`.
- **`faults.py` rerequest payload lacked the PR base.** The harness's `pull_requests` entry had only `number`;
  real GitHub payloads carry `base.sha/ref`. The first attempt created gen 4 with an empty base, which the
  worker marked `stale` ("retargeted") without reviewing. `faults.py` now includes `base`; the capture holds both
  attempts. Reviewing the receiver: a rerequest whose payload lacks a base is stale, not repaired.
- **A bad signature is not recorded in `last_auth_failure`.** `/health` and `gate status` showed `null` after
  the 401 (the 401 appears in the access log).
- **Free-model flakes fail safe.** On the second stale-head head `56e002a`, the sweep got a `malformed response
  (KeyError)` and the check concluded `failure` / Incomplete instead of `success`. A reply with no `findings`
  list also triggered one parse retry on another review. No 429 was seen.
- **Check run before a restart.** The first worker-restart attempt (head `2715d39`) restarted before the
  generation had created its check run, so there was no id to compare; the second attempt (above) shows the
  same id concluding.
- **Check run listing.** `commits/<sha>/check-runs` lists only the latest run per name; generations 3 and 4 on
  `260b952` therefore appear as one run.
- **File name.** The secret-rotation capture is `4-rotation-grace.json` because the commit guard rejects `*secret*` names; `index.json` maps it.
- **Owner can dismiss the app's approval** (documented limitation): the check still pins the ruleset.
