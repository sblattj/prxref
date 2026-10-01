"""Configuration loading and forge-factory wiring.

Canonical environment-variable table (every name prefixed PRXREF_):

LLM / pipeline:
  PRXREF_LLM_BACKEND            LLM backend: openai-compat | ferry | http
                                (aliases) | litellm | claude-cli | kiro-cli,
                                read case-insensitively; any other value is
                                a configuration error
  PRXREF_LLM_BASE_URL           Base URL of the OpenAI-compatible endpoint;
                                required for openai-compat/ferry/http, not
                                used by litellm, claude-cli or kiro-cli (a
                                set value is ignored there with one INFO
                                line)
  PRXREF_LLM_API_KEY            API key for the openai-compat endpoint
                                (optional; empty for a local no-auth server)
  PRXREF_LLM_MODELS             Comma- or whitespace-separated model fallback
                                chain, first that answers wins; required by
                                every backend
  PRXREF_LLM_REASONING_EFFORT   Reasoning effort for models that cannot
                                disable reasoning; provider-specific string,
                                passed through unvalidated; empty = omit
  PRXREF_LLM_MAX_TOKENS         Completion-token budget per worker review
                                call; positive int (default 4096; 8192 when
                                PRXREF_SUGGESTIONS=on and this is left unset)
  PRXREF_LLM_TIMEOUT            Wall-clock deadline for one model's review
                                call, in seconds; the chain then tries the
                                next model, so a run can exceed it. Must be
                                greater than 0 (default 120.0)
  PRXREF_LLM_TIMEOUT_PER_1K     Deadline scaling (issue #72), openai-compat
                                only: seconds of per-request deadline per 1k
                                estimated input tokens, applied only while
                                PRXREF_LLM_TIMEOUT is at its default (120.0)
                                — scaling extends the deadline above that
                                floor, never below it. An explicit timeout —
                                flag, variable or config file — disables
                                scaling entirely.
                                Must be greater than 0 (default 1.6)
  PRXREF_LLM_TEMPERATURE        Sampling temperature, e.g. "0.2"; finite and
                                >= 0, no upper bound (provider-specific).
                                Unset or empty sends the built-in default
                                0.0 rather than omitting the parameter, so
                                an identical diff reviews identically by
                                default; an operator-set value wins
  PRXREF_LLM_SEED               Optional integer sampling seed handed to
                                OpenAI-compatible backends as top-level
                                "seed" in the request; >= 0 (0 is a valid
                                seed); empty or unset falls back to the
                                factory's once-per-process seed; "off"
                                (lowercase) sends no seed at all
  PRXREF_LLM_CLI_PATH           claude-cli / kiro-cli only: path to the CLI
                                binary, ``~`` expanded; empty = "claude" or
                                "kiro-cli" on PATH. Not found = configuration
                                error
  PRXREF_LLM_CLI_CONCURRENCY    claude-cli / kiro-cli only: max CLI
                                processes one client runs at once; positive
                                int (default 2)
  PRXREF_LLM_PARSE_RETRIES      Times a reply that is empty, does not parse, or
                                lacks a ``findings`` list is re-sent up to N
                                times; >= 0, where 0 keeps only the single
                                empty-reply retry (default 1). Each discarded
                                attempt is written as
                                ``<label>.attempt<K>.response.json`` when
                                ``--trace-dir`` is set.
  PRXREF_CONFIDENCE_FLOOR       Findings below this confidence are dropped;
                                a probability in [0.0, 1.0] (default 0.6)
  PRXREF_MAX_ERROR_FINDINGS     Max error-severity findings reported per
                                review; >= 0, where 0 caps every error
                                (legacy alias: PRXREF_MAX_ERRORS)
  PRXREF_MAX_WARNING_FINDINGS   Per-severity caps (0.15.0): max
                                warning-severity findings reported per
                                review, the excess dropped
                                lowest-confidence-first; >= 0, where 0 caps
                                every warning. Unset (default) = unlimited
  PRXREF_MAX_OUTOFSCOPE_FINDINGS
                                Per-severity caps (0.15.0): the same cap for
                                the minor tier, ``outofscope``-severity
                                findings. ``outofscope`` is a severity, not
                                ticket scope ``out`` (see
                                PRXREF_TICKET_CONTEXT_FILE); ``spec`` is
                                never capped. >= 0; unset (default) =
                                unlimited
  PRXREF_MAX_FINDINGS_PER_RULE  Per-rule cap (0.15.0): the most findings
                                one team rule may produce in a review,
                                across files. Applies only when a review
                                rules file (PRXREF_REVIEW_RULES or
                                PRXREF_SCOPED_RULES) is loaded; the excess
                                fold into the best one's ``Also at:``
                                list. >= 0, where 0 = off (default 2)
  PRXREF_GROUP_FINDINGS         Finding grouping (0.15.0): literal "1" folds
                                chunk findings that break the same rule in
                                the same file (by normalized title when the
                                model names no rule) into one comment that
                                lists the other locations; runs before the
                                caps, so they count groups. Default off:
                                prompts and output unchanged
  PRXREF_DEDUP_SIMILARITY       Reworded-duplicate dedup (0.15.0): title
                                similarity (Jaccard over title tokens, at
                                least 3 shared) at or above which two
                                findings on the same file and line are one;
                                a chunk copy beats a sweep copy of equal or
                                lower severity. Greater than 0 and at most
                                1.0; unset (default) = off, only the
                                exact-title dedup runs
  PRXREF_MAX_CHUNKS             Max diff chunks reviewed per PR; positive int
                                (default 8)
  PRXREF_CHUNK_TOKEN_BUDGET     Approximate token budget per diff chunk;
                                lowering it splits a PR into more, smaller
                                chunks (positive int, default 25000)
  PRXREF_CHUNK_MAX_FILES        Cap on files placed in one review chunk;
                                chunks stay under it while any chunk has
                                room, and the max_chunks overflow branch
                                may exceed it rather than drop a file
                                (positive int, default 5)
  PRXREF_CHUNK_CONTEXT_LINES    Context lines kept around each change when a
                                chunk's diff is rendered for the worker
                                prompt; 0 emits the changed lines only.
                                Trims the forge's diff, never adds
                                (int >= 0, default 3)
  PRXREF_MAX_WORKERS            Parallel chunk-review workers; positive int
                                (default 4)
  PRXREF_MAX_INLINE_COMMENTS    Max inline comments posted per review, after
                                the quality gate; positive int (default 15)
  PRXREF_TRACE_FILE             path to append a JSONL run trace to; unset
                                (the default) disables tracing entirely.
                                One event per line, flushed as it happens, so
                                a run still in flight is readable. Render it
                                with ``prxref trace render``.
  PRXREF_TRACE_DIR              directory for per-unit prompt/response
                                traces: each review unit (``chunk0``,
                                ``chunk1``, … and the whole-PR ``sweep``)
                                writes ``<unit>.system.md``,
                                ``<unit>.user.md``, ``<unit>.response.json``
                                (the raw model text) and
                                ``<unit>.meta.json`` (model, token counts,
                                elapsed, error) there. Unset (the default)
                                writes nothing at no cost.
                                ``prxref review --trace-dir DIR`` is the
                                per-run equivalent and wins when both are
                                set.
  PRXREF_DRY_RUN                literal "1" reviews without writing anything
                                to the forge — no summary, no inline comments
                                (default off). Applies to the webhook daemon
                                as well as the CLI; ``--no-post`` is the
                                per-invocation equivalent and still wins.
  PRXREF_FAIL_ON                Exit-code policy for ``prxref review``:
                                "never" (default) keeps the advisory
                                contract — the exit code never reflects
                                findings; "error" exits 1 when the
                                completed review carries an active
                                error-severity finding; "any" exits 1 on
                                any active finding. Under "error" and
                                "any", a review that does not complete
                                also exits 1: it crashes, or it ends with
                                verdict "Error" (the forge could not be
                                read, the diff could not be parsed or
                                chunked, or every chunk review failed) or
                                verdict "Incomplete" (some chunk reviews
                                failed, so the review only partially
                                happened). An empty PR diff is not a
                                failure (verdict "Approved", exit 0). The
                                webhook daemon has no exit code and is
                                unaffected.
  PRXREF_POST_MODE              What gets posted to the forge:
                                "summary+inline" (default) | "summary" |
                                "inline". Any other value is a
                                configuration error. Superseded entirely by
                                PRXREF_DRY_RUN / ``--no-post``, which post
                                nothing in any mode.
  PRXREF_POST_VERDICT           literal "1" keeps the verdict stamp in the
                                 posted summary; any other value renders the
                                 summary without it (default on). The
                                 total-failure notice always names its status.
  PRXREF_PRICE_TABLE            Fallback price table for runs whose backend
                                reports no dollar cost: inline JSON (first
                                non-space character "{") or a path to a JSON
                                file, mapping model name -> {"input": USD,
                                "output": USD} per million tokens, keyed on
                                the model name the run reports. A reported
                                cost always wins; a figure from this table
                                marks the run cost_estimated. A malformed
                                table is a configuration error. Empty (the
                                default) estimates nothing. After loading,
                                the key holds the parsed table (a dict).
  PRXREF_POST_COST              literal "1" appends the run's dollar cost to
                                the posted summary's attribution line
                                (default off). The cost is always in the run
                                record, --format json and the traces.
  PRXREF_SEVERITY_MARKERS       Finding glyphs (#59): comma-separated
                                name=glyph pairs overriding any of error,
                                warning, spec, outofscope and
                                out_of_ticket, e.g.
                                "error=🔴,warning=🟡"; whitespace around
                                pairs, names and glyphs is stripped. A
                                name not given keeps its default glyph;
                                empty (default) overrides nothing. An
                                unknown name, a pair without "=", an empty
                                glyph, a repeated name, a glyph holding
                                whitespace or a comma, or two of the five
                                effective glyphs being equal is a
                                configuration error.
  PRXREF_SUMMARY_BULLET_SEPARATOR
                                Text between a summary bullet's location
                                and its title (#59); default " — " (space,
                                em dash, space). Never stripped: leading
                                and trailing spaces are part of the value,
                                though a whitespace-only environment value
                                reads as unset like any other. Empty = the
                                default. A newline or more than 16
                                characters is a configuration error.
  PRXREF_SIZE_WARN_LINES        Advisory-only threshold on lines changed
                                (added + removed, from the parsed diff,
                                excluding lock and generated files); one
                                non-blocking line tops the summary when the
                                count is above it. Unset (default) disables
                                it; >= 0, where 0 is a legal threshold
                                distinct from unset. Never affects the
                                verdict or the exit code.
  PRXREF_SIZE_WARN_FILES        Same contract as PRXREF_SIZE_WARN_LINES,
                                thresholding files changed instead.
  PRXREF_SIZE_IGNORE_GLOBS      Extra fnmatch globs (case-sensitive, matched
                                against the full diff path, ``*`` crosses
                                ``/``) excluded from both size counts, ADDED
                                to the built-in lock-file and generated-file
                                detection, never replacing it. Empty
                                (default) adds nothing.
  PRXREF_METADATA_RULES         Opt-in deterministic PR-metadata checks
                                (#70): "off" (default) or empty runs none,
                                keeps every prompt, finding and run-record
                                key byte-identical, and stamps nothing. A
                                path names a separate TOML rules file
                                holding the four check settings below
                                (branch_patterns and area_globs also as
                                tables), loaded and validated before any
                                network call: an unreadable, oversized
                                (64 KiB) or invalid file, or a flat key
                                below set beside it, exits 2. From the
                                config file the path stays inside the
                                repository. "on" is the back-compat alias
                                that runs the checks from the flat keys
                                below. Each check skips itself
                                (recorded in the run record's
                                ``metadata_rules`` stamp, never a finding)
                                when unconfigured or when the PR offers
                                nothing to check. Violations are summary
                                notes in a "PR metadata" section, never
                                findings: never posted inline, never
                                capped, and never changing the verdict or
                                the exit code (PRXREF_FAIL_ON included).
  PRXREF_BRANCH_PATTERNS        Branch-name check: "type=regex" entries
                                mapping a PR type to the fullmatch pattern
                                its source branch must satisfy (matched
                                with re.fullmatch, so ^/$ anchors in a team
                                pattern are harmless). The PR's type comes
                                from its labels first, else the
                                conventional-commit prefix of its title
                                ("fix: ..." -> fix); a PR whose type is
                                unknown, or whose type has no entry here,
                                is skipped, not a violation. Empty
                                (default) skips the check.
  PRXREF_COMMIT_REFERENCE       Commit-subject check: a regex every
                                non-merge commit subject (first line of the
                                message) of the PR must CONTAIN (re.search),
                                e.g. "PROJ-[0-9]+". One summary note per
                                offending commit. Needs the forge's
                                commit listing (GitHub, GitLab, Bitbucket
                                Cloud, Bitbucket Server / Data Center,
                                Gitea, Azure DevOps); without one,
                                or on a --diff-file run, the check skips
                                with "skipped: no commit source". Empty
                                (default) skips the check.
  PRXREF_AREA_GLOBS             Area check: "name=glob" entries classifying
                                diff paths into named areas (a path belongs
                                to every area whose glob matches it; globs
                                match like scoped-rules applies_to, ``**/``
                                matches zero directories). A path matching
                                no area is ignored. More distinct areas
                                than PRXREF_MAX_AREAS_PER_PR makes one
                                summary note listing the areas.
                                Empty (default) skips the check.
  PRXREF_MAX_AREAS_PER_PR       Most distinct areas a PR may touch before
                                the area check flags it; >= 0 (default 2).
  PRXREF_SPEC_SOURCES           Spec/ticket sources to review against, as
                                 comma- or whitespace-separated web URLs and
                                 local file/dir paths; the repeatable
                                 ``--spec`` flag replaces (never merges) this
                                 list. Jira ticket URLs are routed to the
                                 Jira REST fetcher below.
  PRXREF_SPEC_MAX_CHARS         Raw fetched characters kept per spec source
                                 before pruning; positive int (default
                                 120000)
  PRXREF_SPEC_DIGEST_TOKENS     Token budget for the spec digest injected
                                 into worker prompts; positive int (default
                                 3000)
  PRXREF_REVIEW_RULES           Path to a team review-rules file (Markdown,
                                optional front matter with a ``severity:``
                                map) added to every review prompt, by
                                ``prxref review`` and the webhook daemon
                                alike. A missing, unreadable or malformed
                                file is a configuration error. Read it from a checkout
                                the PR cannot change. ``--rules-file PATH``
                                wins; ``--rules-file ""`` turns it off for
                                one run. Empty (the default) = no rules.
  PRXREF_REVIEW_RULES_MAX_CHARS Characters of the rules body (after the
                                front matter) kept in the prompt; longer is
                                truncated with a warning; positive int
                                (default 24000)
  PRXREF_SCOPED_RULES           Path-scoped review rules (0.15.0): rules
                                files and directories (``*.md`` one level
                                deep) whose ``applies_to:`` front-matter
                                globs pick the chunks each file reaches; the
                                sweep gets the union. Added to
                                PRXREF_REVIEW_RULES, never replacing it. A
                                file without ``applies_to`` reaches every
                                unit; a URL, ``applies_to: []`` or a
                                malformed file is a configuration error.
                                The repeatable ``--scoped-rules PATH`` flag
                                replaces the list. Empty (the default) = off
  PRXREF_SCOPED_RULES_MAX_CHARS Path-scoped review rules (0.15.0):
                                characters of scoped-rules text one review
                                unit receives. Whole files go in while they
                                fit; the first that does not is cut to the
                                room left, or left out when no room is left;
                                every later file is left out, and one
                                warning per run names this variable;
                                positive int (default 24000)
  PRXREF_PROMPTS_DIR            Prompt template overrides (0.15.0):
                                directory of replacement ``worker.md``,
                                ``systemic.md`` and ``summary.md``; an
                                absent file keeps the packaged one. Each
                                template is validated before any network
                                call (marker, placeholders, 256 KiB), and a
                                failure is a configuration error.
                                ``--prompts-dir DIR`` wins. Unset (the
                                default) = the packaged templates
  PRXREF_TICKET_CONTEXT_FILE    Path to a text file holding the ticket this
                                PR implements; each finding is then marked
                                in, out of, or of unknown ticket scope. An
                                empty (or whitespace-only) file means "this
                                PR has no ticket". A missing, unreadable or
                                non-UTF-8 file is a configuration error.
                                Ignored by ``prxref serve``.
                                ``--context-file PATH`` wins;
                                ``--context-file ""`` turns it off for one
                                run. Empty (the default) = no ticket.
  PRXREF_TICKET_CONTEXT_MAX_CHARS
                                Characters of ticket text kept in the
                                prompt; longer is truncated with a visible
                                marker; positive int (default 6000)
  PRXREF_EVIDENCE_FILES         Execution evidence (#69): paths to files of
                                commands the caller ran (a CI step, an
                                agent, a script) — nginx -t, helm lint, the
                                test suite, HTTP probes. Each file is JSON
                                (a top-level array, or an object holding an
                                "evidence" array of {command, exit_code,
                                output, files}) or plain text (blank-line-
                                separated blocks, the first line the
                                command, an exit: N line the exit code).
                                Items whose paths match a chunk ride that
                                chunk's prompt; the rest ride every prompt,
                                the whole-PR sweep's included, and a worker
                                must not report a finding the evidence
                                contradicts. A finding claiming a header
                                is missing is dropped when an exit-0 item
                                shows that header as a Name: value line
                                for the resource it names. A missing,
                                unreadable or non-UTF-8 file, or JSON of
                                the wrong shape,
                                is a configuration error. Comma- or
                                whitespace-separated; the repeatable
                                ``--evidence-file PATH`` flag replaces this
                                list for one run. Empty (the default) = no
                                evidence
  PRXREF_EVIDENCE_MAX_CHARS
                                Execution evidence (#69): characters of
                                evidence text one review unit's prompt may
                                carry; a unit's matched items go in ahead of
                                the global ones and items that no longer fit
                                are left out whole behind one truncation
                                line; positive int (default 8000; the
                                old name PRXREF_EVIDENCE_MAX_CHUNK_CHARS is
                                still read)
  PRXREF_REPO_CONTEXT           Repository context (0.16.0): "off" (default) |
                                "diff" | "repo". "off" adds no repository
                                context entry, read, trace event or log line;
                                the same-file definitions and dependency
                                versions, Java and Kotlin ones included since
                                0.17.0, do not depend on it. "diff" adds
                                cross-chunk definitions from other files
                                already in the diff, plus diff-file entries,
                                all read from the diff itself; no repository
                                reader is needed. "repo" also reads files
                                outside the diff — import, path-convention and
                                name-search definitions, plus contract excerpts
                                and, with a file listing, readers (since
                                0.17.0: excerpts of unchanged code that reads
                                state the chunk's added lines write, in a last
                                "Code elsewhere that reads state this chunk
                                writes" block) — through the forge's
                                repository reader when one is available, or
                                --repo-dir. Matching is
                                exact and case-sensitive, like PRXREF_FAIL_ON;
                                any other value is a configuration error
  PRXREF_REPO_CONTEXT_MAX_CHARS Repository context (0.16.0): per-chunk
                                character budget shared by the cross-chunk,
                                contract and other repository-context entries;
                                positive int (default 12000)
  PRXREF_REPO_CONTEXT_MAX_READS Repository context (#61): uncached reads
                                every chunk together may make in one run;
                                positive int (default 200)
  PRXREF_REPO_CONTEXT_MAX_CHUNK_READS
                                Repository context (#61): uncached reads one
                                chunk may make; positive int (default 16). A
                                PR diff file never spends either cap
  PRXREF_CONTEXT_FOLLOWUP       Context follow-up (0.18.0): "off" (the
                                default) makes no extra call, read, log line,
                                trace event or trace file, byte-identical to
                                0.17.0. "on" re-sends a chunk once, at
                                PRXREF_REPO_CONTEXT=repo only, when its first
                                reply asks about a symbol it was not shown,
                                with that symbol's definition appended; at
                                another level, or at "repo" with no
                                repository reader, the run logs one WARNING
                                and the follow-up stays off for that run. Any
                                other value is a configuration error
  PRXREF_SUGGESTIONS            Code suggestions (#30): "off" (the default)
                                leaves every prompt, call and finding as
                                before, and the run record's "suggestions"
                                is null. "on" asks each chunk worker (never
                                the sweep) for an optional replacement text
                                per finding, keeps only the ones that pass a
                                deterministic check against the diff, and
                                counts them in the run record. Matched
                                exactly, like PRXREF_FAIL_ON; any other value
                                is a configuration error. Suggestions lengthen
                                the reply, so "on" raises the completion-token
                                budget (see PRXREF_LLM_MAX_TOKENS) when that is
                                left unset; an explicit budget is respected as
                                given
  PRXREF_ROUTING_PROBE          Matching-rules probe (#67): "on" (the
                                default) keeps the worker prompt's
                                "## Matching rules" section, which asks the
                                model, for each added or widened rule that
                                decides which inputs match (a web-server
                                location or rewrite, a router pattern, a
                                glob, a regex validator), which inputs it
                                newly captures. With a file reader, "on"
                                also reads the conventional route-table
                                files for a chunk that adds a web-server or
                                static-host rule and appends their route
                                lines as a context block. "off" cuts that
                                section out, so the worker prompt is the
                                template without it byte for byte, and
                                reads nothing. No extra LLM call either
                                way; the sweep prompt never
                                carries it. Matched exactly; any other
                                value is a configuration error
  PRXREF_CI_WIRING             CI wiring (#66): "off" reads nothing,
                                changes no byte of the review and stamps
                                ci_wiring=null on the run record.
                                "on" (the default) flags a check-shaped
                                file the PR adds
                                (a script whose name or a --flag it gains
                                says verify/smoke/check, a file that gains
                                a shebang, a new test file outside the
                                runner's default include) that no CI
                                configuration file invokes: one finding per
                                unwired check, "spec" when the ticket
                                mentions regression checks, CI, pipelines
                                or automated tests ("warning" otherwise),
                                listing the CI files searched. Needs the
                                forge's head-sha file reads or --repo-dir;
                                without a reader the run logs one notice
                                naming PRXREF_CI_WIRING (a WARNING when
                                "on" was set, INFO on the default) and
                                records why.
                                Matched exactly; any other value is a
                                configuration error. Never changes the
                                verdict or the exit code
  PRXREF_CI_WIRING_GLOBS        CI wiring (#66): globs (matched like
                                PRXREF_SIZE_IGNORE_GLOBS) selecting the CI
                                configuration files the check reads. A set
                                value REPLACES the built-in set below
                                rather than adding to it, and an empty
                                value reads as unset (the built-in set
                                stays); at most 12 CI files are read per
                                run. Built-in set:
                                .github/workflows/*.y*ml, .gitlab-ci.yml,
                                azure-pipelines.yml, .circleci/config.yml,
                                Jenkinsfile, bitbucket-pipelines.yml,
                                .drone.yml, cloudbuild.yaml, .travis.yml
  PRXREF_INCREMENTAL            Incremental re-review on push (#34): "off"
                                (the default) reviews every file on every run
                                and writes no marker. "on" stamps each summary
                                with the PR head it reviewed and, when the PR
                                already carries such a summary, chunks and
                                reviews only the files changed since that
                                head; the systemic sweep still sees the whole
                                PR. The chunk findings, and so the verdict,
                                then cover only the re-reviewed files (plus
                                what the sweep and the deterministic checks
                                find anywhere in the PR), while earlier inline
                                comments on the other files stay standing. It
                                applies only with PRXREF_POST_MODE summary or
                                summary+inline; the run reviews every file on
                                a first review, when the forge cannot read its
                                summary, when that read fails, when the
                                summary has no reviewed-head marker, when the
                                PR head is unknown, when the forge cannot
                                compare commits, when the compare diff fails
                                (a force-push), with
                                --full-review, with PRXREF_FAIL_ON other than
                                "never", and on every replay. --full-review
                                and a PRXREF_FAIL_ON gate read no previous
                                summary but still record the head, so the
                                following push is incremental again. Costs one
                                extra forge read per run for the previous
                                summary, plus a compare diff read when the
                                head has moved. Matched exactly; any
                                other value is a configuration error
  PRXREF_FALLBACK               Where the review goes when a post fails
                                (#48): "auto" (the default) acts only when a
                                summary or inline post fails (a read-only
                                token, a fork PR): prxref review then emits
                                the review through the CI it runs under --
                                GitHub Actions annotations and the job
                                summary, Azure Pipelines logging commands, a
                                GitLab Code Quality report
                                (gl-code-quality-report.json in the working
                                directory) -- and logs it at WARNING
                                everywhere, Bitbucket Pipelines and local runs
                                included. Under --format json, annotations and
                                logging commands are skipped so stdout stays
                                one JSON document. "off" emits nothing. Either
                                way the run record's "degraded" says which
                                posts or chunks failed and why; the exit
                                code never changes. Matched exactly; any
                                other value is a configuration error
  PRXREF_STABLE_IDS             Deprecated and ignored (#71): stable
                                finding ids are always on. Any value is
                                still accepted (never a configuration
                                error) so an existing environment keeps
                                working; a set value other than "1"
                                (which reads as off) logs one WARNING
                                saying the knob is ignored. Every finding
                                carries a content-derived id
                                (<file>#<rule or norule>#<12-hex claim
                                hash>) that survives reworded titles and
                                anchor drift, plus the anchor block (the
                                enclosing function, YAML key or manifest
                                key — metadata the id excludes) and an
                                id_reused_from label saying where a
                                reused id came from ("run", "verdict" or
                                "thread")
  PRXREF_VERDICT_STORE          Stable finding ids (#71): path to the JSON
                                verdict store earlier runs' verdicts are
                                read from, keyed by stable id. Read
                                whenever set; a finding
                                whose id the store holds as "refuted" is
                                dropped with drop_reason "refuted in
                                earlier run (<id>)"; so is a reworded
                                duplicate of the same file and rule
                                whose title restates the entry's
                                recorded title, and a finding whose
                                0.30.0 id (the claim hash before it
                                stemmed words) an entry without a title
                                is keyed by. Unset (the default)
                                = no persistence; ids are still stamped
                                but nothing from an earlier run can
                                match. The review never writes the
                                store; recording a verdict is a caller's
                                decision. A missing file reads as empty;
                                an unreadable or malformed one is a
                                configuration error (exit 2)
  PRXREF_RULE_SCOPING           Rule scope check (#75): "on" (the default)
                                leaves a scoped rules section out of every
                                chunk whose files it does not cover, and
                                clears the rule label of a finding whose
                                cited section declares a scope that does
                                not cover the file, or whose cited rule
                                names another kind of defect than the
                                finding's title, keeping the finding;
                                "off" sends every chunk the whole rules
                                text, leaves every label as the model
                                wrote it and the run record's
                                "rule_scope_cleared" null. Matched exactly; any other value is a
                                configuration error
  PRXREF_CONTEXT_CONTRACT_GLOBS Repository context (0.16.0): globs (matched
                                like PRXREF_SIZE_IGNORE_GLOBS) selecting the
                                contract files — OpenAPI, JSON Schema,
                                Liquibase/SQL migrations — excerpted under
                                "repo". A set value REPLACES the built-in set
                                below rather than adding to it, and an empty
                                value reads as unset (the built-in set stays);
                                there is no way to turn contract excerpts off
                                on their own in 0.16.0 short of setting
                                PRXREF_REPO_CONTEXT to "off" or "diff".
                                Built-in set: **/openapi*.y*ml,
                                **/openapi*.json, **/openapi/**, **/swagger*,
                                **/*.schema.json, **/db/changelog/**,
                                **/db/migration/**, **/migrations/**
  PRXREF_CONTEXT_EXCLUDE_GLOBS  Repository context (0.16.0): globs (matched
                                like PRXREF_SIZE_IGNORE_GLOBS) whose paths are
                                never read for repository context, not even a
                                diff file. ADDED to a floor that is always on:
                                **/expected.json, **/cases.json, **/case.json,
                                **/prxref-eval/**, **/.env*, **/*.pem,
                                **/*.key. Empty (the default) adds nothing
  PRXREF_CONTEXT_STANDARDS_GLOBS
                                 In-repo standards (#68): globs (matched like
                                 PRXREF_SIZE_IGNORE_GLOBS) selecting the
                                 repository's own standards documents - the
                                 security standard, the ADRs, CONTRIBUTING -
                                 whose matching sections are excerpted under
                                 PRXREF_REPO_CONTEXT="repo" only, ranked by
                                 what the chunk's own changes name and capped
                                 by PRXREF_CONTEXT_STANDARDS_MAX_CHARS. A set
                                 value REPLACES the built-in set below rather
                                 than adding to it; a bare empty value in the
                                 environment reads as unset (the house rule),
                                 so the built-in set stays - the exact value
                                 "off" (lowercase, the PRXREF_LLM_SEED
                                 precedent) turns standards excerpts off on
                                 their own, as do --context-standards-globs
                                 "" (or off) for one run and, in
                                 .prxref.toml, [] or "off".
                                 Built-in set: docs/standards/**, docs/adr/**,
                                 STANDARDS*.md, SECURITY.md, CONTRIBUTING.md,
                                 .github/SECURITY.md, .github/CONTRIBUTING.md
  PRXREF_CONTEXT_STANDARDS_MAX_CHARS
                                 In-repo standards (#68): per-chunk character
                                 budget for the standards sections admitted
                                 into one worker prompt. 0 disables the
                                 excerpts: no standards document is read
                                 and no block renders (default 6000)

Spec sources / Jira:
  PRXREF_JIRA_BASE_URL          Jira base URL (scheme://host plus any
                                 context path) that ticket fetches are
                                 looked up on, overriding a ticket URL's own
                                 base (a self-hosted board often sits behind
                                 a different REST host than its browse URL).
                                 Jira credentials are only ever sent here;
                                 empty = the ticket URL's own base, fetched
                                 anonymously
  PRXREF_JIRA_EMAIL             Jira account email for HTTP basic auth,
                                 used only together with
                                 PRXREF_JIRA_BASE_URL; without it the fetch
                                 is anonymous and a warning is logged.
                                 Missing credentials are a fetch failure
                                 (the review proceeds un-grounded), never a
                                 configuration error.
  PRXREF_JIRA_API_TOKEN         Jira API token paired with
                                 PRXREF_JIRA_EMAIL for HTTP basic auth, sent
                                 only to PRXREF_JIRA_BASE_URL

Per-forge auth:
  PRXREF_BITBUCKET_TOKEN        Bitbucket Cloud bearer token
  PRXREF_BITBUCKET_USER         Bitbucket Cloud username (app-password pair)
  PRXREF_BITBUCKET_APP_PASSWORD Bitbucket Cloud app password
  PRXREF_BITBUCKET_SERVER_TOKEN Bitbucket Server/Data Center HTTP access token
                                (falls back to PRXREF_BITBUCKET_TOKEN)
  PRXREF_BITBUCKET_SERVER_USER  Bitbucket Server username (basic-auth pair)
  PRXREF_BITBUCKET_SERVER_PASSWORD Bitbucket Server password (basic-auth pair)
  PRXREF_GITHUB_TOKEN           GitHub token (github.com)
  PRXREF_GITHUB_ENTERPRISE_TOKEN GitHub Enterprise token (GHES hosts)
  PRXREF_GITLAB_TOKEN           GitLab token
  PRXREF_GITEA_TOKEN            Gitea/Forgejo access token (any host;
                                empty reads public repositories anonymously)
  PRXREF_AZURE_DEVOPS_TOKEN     Azure DevOps personal access token (Code
                                Read to review, Read & write to post); empty
                                falls back to SYSTEM_ACCESSTOKEN, then to
                                anonymous access (public projects only)

Webhooks:
  PRXREF_BITBUCKET_WEBHOOK_SECRET HMAC secret for Bitbucket webhook payloads
  PRXREF_GITHUB_WEBHOOK_SECRET    HMAC secret for GitHub webhook payloads
  PRXREF_GITLAB_WEBHOOK_SECRET    HMAC secret for GitLab webhook payloads
  PRXREF_GITEA_WEBHOOK_SECRET     HMAC secret for Gitea/Forgejo webhook
                                  payloads
  PRXREF_AZURE_DEVOPS_WEBHOOK_SECRET
                                  Basic-auth password of the Azure DevOps
                                  service hook (the user name is ignored);
                                  empty rejects Azure DevOps webhooks with
                                  401 unless PRXREF_ALLOW_UNSIGNED is "1"
  PRXREF_ALLOW_UNSIGNED           literal "1" accepts unsigned
                                  webhooks (default off; insecure)

List-valued keys (PRXREF_LLM_MODELS, PRXREF_SPEC_SOURCES,
PRXREF_SIZE_IGNORE_GLOBS, PRXREF_SCOPED_RULES, PRXREF_CONTEXT_CONTRACT_GLOBS,
PRXREF_CONTEXT_EXCLUDE_GLOBS, PRXREF_BRANCH_PATTERNS, PRXREF_AREA_GLOBS,
PRXREF_CI_WIRING_GLOBS and PRXREF_CONTEXT_STANDARDS_GLOBS) split on any run
of commas and/or
whitespace, so no item can contain either; a glob that must match a
literal space writes it as ``?``.

Precedence: built-in defaults < environment < ``overrides`` kwargs.
An error names the source that actually supplied the offending value — the
environment variable that was read (including a legacy alias), or the caller's
own name for an override (``--max-chunks`` rather than ``PRXREF_MAX_CHUNKS``
when the flag is what the operator typed).
An empty or whitespace-only environment value reads as unset, so a stray
``PRXREF_LLM_TIMEOUT= `` in a .env file keeps the default instead of aborting.
``None``-valued overrides are ignored (callers may pass optional values).
Unknown override keys raise ``ValueError`` so typos surface immediately.
A malformed value, one out of its numeric range, or one outside its key's
allowed vocabulary (``PRXREF_FAIL_ON`` accepts only never | error | any)
raises :class:`~prxref.llm.ConfigError`, which the CLI reports as a
configuration error and exits 2 for — never as a review failure.

Config file (#38):
  A repository config file, ``.prxref.toml``, is a flat TOML document whose
  keys are the lowercase names above without the ``PRXREF_`` prefix
  (``max_chunks = 4``). With a file, precedence is built-in defaults < file <
  environment < ``overrides`` kwargs, and an error about a file value names
  ``<file>: <key>``. :func:`find_config_file` locates it: ``--config PATH``,
  else PRXREF_CONFIG_FILE, else ``.prxref.toml`` in the working directory
  only (no walk up the tree). The value ``off`` (any case) in the flag or the
  variable disables the file. PRXREF_CONFIG_FILE is read only there; it is
  not a config key. Credentials, endpoints, executables, local writes, local
  reads and the gate stay environment-only (:data:`ENV_ONLY_KEYS`), paths
  set by the file must stay inside its directory, ``spec_sources`` in the
  file takes local paths only, and an unknown key is an error.
  ``llm_temperature`` takes a TOML number or a string. See
  docs/config-file.md.
"""
from __future__ import annotations

import difflib
import math
import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

from prxref.forges.base import Forge, PRRef

from . import costs, markers
from .llm import ConfigError
from .quality import DEFAULT_CONFIDENCE_FLOOR, DEFAULT_MAX_ERRORS
from .triage import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_MAX_FILES_PER_CHUNK,
    DEFAULT_TOKEN_BUDGET,
)

_ENV_PREFIX = "PRXREF_"

#: Completion-token budget applied when suggestions are on and the operator
#: left ``llm_max_tokens`` unset (issue #30, part D): the reference-model
#: measurement showed the 4096 default truncating every suggestions-on run.
SUGGESTIONS_MAX_TOKENS = 8192

#: The text between a summary bullet's location and its title (issue #59).
_DEFAULT_BULLET_SEPARATOR = " — "

#: The longest ``summary_bullet_separator`` accepted, in characters.
_MAX_BULLET_SEPARATOR_CHARS = 16

_DEFAULTS: dict[str, object] = {
    "llm_backend": "openai-compat",
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_models": [],
    "llm_reasoning_effort": "",
    "llm_max_tokens": 4096,
    "llm_timeout": 120.0,
    # Deadline-scaling coefficient (issue #72): seconds of per-request
    # deadline per 1k estimated input tokens, applied by the openai-compat
    # client ONLY while llm_timeout is at its default. An explicit timeout
    # (flag, variable or file) disables scaling entirely. Must be > 0; tune
    # upward for slow endpoints, downward for fast ones.
    "llm_timeout_per_1k": 1.6,
    "llm_temperature": "",
    # ``None`` is the declared unset: no seed is configured, so the factory
    # falls back to its once-per-process seed. Unlike ``llm_temperature``
    # (whose "" marker survives to the backend that owns the wire decision),
    # the seed is a first-class int key — coerced and range-checked here —
    # because "no seed" is representable in its own type. The one word value,
    # ``_SEED_OFF``, passes through uncoerced and unranged (issue #26).
    "llm_seed": None,
    "llm_cli_path": "",
    "llm_cli_concurrency": 2,
    "llm_parse_retries": 1,
    "confidence_floor": DEFAULT_CONFIDENCE_FLOOR,
    "max_error_findings": DEFAULT_MAX_ERRORS,
    "max_warning_findings": None,
    "max_outofscope_findings": None,
    "max_findings_per_rule": 2,
    "group_findings": False,
    "dedup_similarity": None,
    "max_chunks": 8,
    "chunk_token_budget": DEFAULT_TOKEN_BUDGET,
    "chunk_max_files": DEFAULT_MAX_FILES_PER_CHUNK,
    "chunk_context_lines": DEFAULT_CONTEXT_LINES,
    # Mirrors orchestrator.MAX_WORKERS / MAX_INLINE_COMMENTS. Restated rather
    # than imported: config is a leaf module and importing the orchestrator
    # here would pull the whole review pipeline into every config read.
    # TestChunkingAndFanoutKnobs pins the three literals together.
    "max_workers": 4,
    "max_inline_comments": 15,
    "fail_on": "never",
    "dry_run": False,
    "trace_file": "",
    "trace_dir": "",
    "post_mode": "summary+inline",
    "post_verdict": True,
    # A str on the way in (inline JSON or a file path); _check_price_table
    # replaces it with the parsed dict, so a loaded config never holds the raw
    # text and every consumer sees one type.
    "price_table": "",
    "post_cost": False,
    # ``name=glyph`` pairs; _check_severity_markers validates the value and
    # leaves it as given, and cli._run_review hands it to markers.configure.
    "severity_markers": "",
    # Never stripped: the spaces around the dash are part of the value.
    "summary_bullet_separator": _DEFAULT_BULLET_SEPARATOR,
    # ``None`` = the advisory is off, the second "None means off" class next
    # to ``llm_seed``: 0 is a legal threshold, so it cannot spell "unset".
    "size_warn_lines": None,
    "size_warn_files": None,
    "size_ignore_globs": [],
    "spec_sources": [],
    "spec_max_chars": 120000,
    "spec_digest_tokens": 3000,
    "review_rules": "",
    "review_rules_max_chars": 24000,
    "scoped_rules": [],
    "scoped_rules_max_chars": 24000,
    "prompts_dir": None,
    "ticket_context_file": "",
    "ticket_context_max_chars": 6000,
    # Execution evidence (#69): caller-run command results as review
    # context. The list mirrors spec_sources/scoped_rules (paths from the
    # environment or the repeatable --evidence-file flag, which replaces
    # it); the int is the per-unit prompt budget the orchestrator trims
    # blocks to.
    "evidence_files": [],
    "evidence_max_chars": 8000,
    "repo_context": "off",
    "repo_context_max_chars": 12000,
    "repo_context_max_reads": 200,
    "repo_context_max_chunk_reads": 16,
    "context_followup": "off",
    "suggestions": "off",
    "routing_probe": "on",
    "incremental": "off",
    "fallback": "auto",
    # The built-in contract-glob set. Unlike the
    # other _LIST_KEYS defaults, this one is non-empty: an env value REPLACES
    # it rather than adding to it, and an empty value reads as unset (the
    # normal "empty or whitespace-only reads as unset" rule), so this set
    # stays in place. A ``set`` literal would work too -- the _LIST_KEYS
    # coercion branch always returns a ``list`` -- but the default is typed
    # as a ``list`` from the start so both paths give callers the same type.
    "context_contract_globs": [
        "**/openapi*.y*ml",
        "**/openapi*.json",
        "**/openapi/**",
        "**/swagger*",
        "**/*.schema.json",
        "**/db/changelog/**",
        "**/db/migration/**",
        "**/migrations/**",
    ],
    "context_exclude_globs": [],
    # In-repo standards documents (#68): sections of the repository's own
    # rules offered to each chunk worker at the ``repo`` level, like the
    # contract globs above. The default is non-empty and
    # replace-not-append, with the same house rule that a bare empty value
    # reads as unset; the exact value ``off``
    # (``PRXREF_CONTEXT_STANDARDS_GLOBS=off``) is the one way to turn the
    # feature off on its own, the ``llm_seed`` precedent.
    "context_standards_globs": [
        "docs/standards/**",
        "docs/adr/**",
        "STANDARDS*.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        ".github/SECURITY.md",
        ".github/CONTRIBUTING.md",
    ],
    "context_standards_max_chars": 6000,
    # CI wiring (#66): the switch, on by default (OD2; the eval harness pins
    # it off), plus the CI-file globs. The globs
    # default is non-empty and replace-not-append like
    # ``context_contract_globs`` above; the list restates
    # ``ci_wiring.DEFAULT_CI_GLOBS`` (config stays a leaf module), pinned
    # together by tests/test_issue_66_ci_wiring.py.
    "ci_wiring": "on",
    "ci_wiring_globs": [
        ".github/workflows/*.y*ml",
        ".gitlab-ci.yml",
        "azure-pipelines.yml",
        ".circleci/config.yml",
        "Jenkinsfile",
        "bitbucket-pipelines.yml",
        ".drone.yml",
        "cloudbuild.yaml",
        ".travis.yml",
    ],
    # PR-metadata rules (#70): "off", "on" (the back-compat alias reading
    # the four flat keys below) or the path to a separate TOML rules file
    # holding the same four settings. The "off" default keeps every check
    # off and the run record free of the metadata_rules key.
    "metadata_rules": "off",
    # Stable finding ids (#71): on by default; "0" opts out. The store
    # is read only when a path is set (None = no persistence, in-run
    # reuse only), and the pipeline never writes it.
    "stable_ids": True,
    "rule_scoping": "on",
    "verdict_store": None,
    "branch_patterns": [],
    "commit_reference": "",
    "area_globs": [],
    "max_areas_per_pr": 2,
    "jira_base_url": "",
    "jira_email": "",
    "jira_api_token": "",
    "bitbucket_token": "",
    "bitbucket_user": "",
    "bitbucket_app_password": "",
    "bitbucket_server_token": "",
    "bitbucket_server_user": "",
    "bitbucket_server_password": "",
    "github_token": "",
    "github_enterprise_token": "",
    "gitlab_token": "",
    "gitea_token": "",
    "azure_devops_token": "",
    "bitbucket_webhook_secret": "",
    "github_webhook_secret": "",
    "gitlab_webhook_secret": "",
    "gitea_webhook_secret": "",
    "azure_devops_webhook_secret": "",
    "allow_unsigned": False,
}

_INT_KEYS = frozenset({
    "max_error_findings", "max_chunks", "llm_max_tokens", "llm_seed",
    "chunk_token_budget", "chunk_max_files", "chunk_context_lines",
    "max_workers", "max_inline_comments",
    "spec_max_chars", "spec_digest_tokens",
    "llm_cli_concurrency", "review_rules_max_chars",
    "ticket_context_max_chars", "size_warn_lines", "size_warn_files",
    "max_warning_findings", "max_outofscope_findings", "scoped_rules_max_chars",
    "max_findings_per_rule", "repo_context_max_chars", "llm_parse_retries",
    "repo_context_max_reads", "repo_context_max_chunk_reads",
    "max_areas_per_pr", "evidence_max_chars",
    "context_standards_max_chars",
})
_FLOAT_KEYS = frozenset({
    "confidence_floor", "llm_timeout", "llm_timeout_per_1k", "dedup_similarity",
})
_BOOL_KEYS = frozenset({
    "allow_unsigned", "dry_run", "post_verdict", "post_cost", "group_findings",
    "stable_ids",
})
_LIST_KEYS = frozenset({
    "llm_models", "spec_sources", "size_ignore_globs", "scoped_rules",
    "context_contract_globs", "context_exclude_globs",
    "branch_patterns", "area_globs", "ci_wiring_globs", "evidence_files",
    "context_standards_globs",
})

# An enum-valued key has no numeric interval to check, so its legal vocabulary
# is declared here instead and enforced on the same pass as the ranges. A
# value outside the set is a ConfigError (exit 2) naming the legal values —
# never a silent fall-back to the default, which would turn a typo'd
# PRXREF_FAIL_ON=eror into an undetected "never".
_CHOICE_KEYS: dict[str, frozenset[str]] = {
    "fail_on": frozenset({"never", "error", "any"}),
    "repo_context": frozenset({"off", "diff", "repo"}),
    "context_followup": frozenset({"off", "on"}),
    "rule_scoping": frozenset({"off", "on"}),
    "suggestions": frozenset({"off", "on"}),
    "routing_probe": frozenset({"off", "on"}),
    "incremental": frozenset({"off", "on"}),
    "fallback": frozenset({"auto", "off"}),
    "ci_wiring": frozenset({"off", "on"}),
}

# ``llm_seed``'s one non-integer value: send no seed at all. Matched exactly
# (lowercase), like every _CHOICE_KEYS value; restated from
# prxref.llm_backends.SEED_OFF, because config stays a leaf module.
_SEED_OFF = "off"

# ``context_standards_globs``'s one non-glob value (#68): read no standards
# document at all. Matched exactly (lowercase) like ``llm_seed``'s sentinel,
# because the normal list coercion would read ``off`` as a one-glob list. In
# the environment a bare empty value keeps the house rule (it reads as unset,
# so the built-in set stays); ``--context-standards-globs ""``, a TOML ``[]``
# and a TOML ``"off"`` all turn it off.
_STANDARDS_GLOBS_OFF = "off"

# ``metadata_rules``'s values that are not a rules-file path (#70): "off"
# and "" run no checks, "on" is the back-compat alias reading the flat keys.
# Any other value is a path to a TOML rules file, loaded by
# prxref.metadata_rules.load_metadata_rules before any network call.
METADATA_RULES_SWITCHES = frozenset({"", "off", "on"})

# The flat keys a metadata rules file replaces; setting one beside a rules
# file is refused rather than silently ignored.
_METADATA_FLAT_KEYS = ("branch_patterns", "commit_reference", "area_globs", "max_areas_per_pr")

# The posting-behaviour vocabulary, validated rather than trusted. Restated in
# prxref.orchestrator (config stays a leaf module); pinned together by
# TestPostMode::test_the_vocabulary_matches_the_orchestrator.
_POST_MODES = ("summary+inline", "summary", "inline")


class _Range(NamedTuple):
    """The legal interval for one numeric config key.

    ``high`` defaults to ``math.inf`` — unbounded above, deliberately. The
    ceiling for a token budget, a timeout or a worker count is provider- and
    machine-specific, and an invented limit would be worse than none. Only a
    semantically bounded quantity gets a real ``high``: today that is the
    confidence floor, which is a 0-1 probability everywhere in
    ``triage.Finding`` and in the prompts, and the dedup similarity, a 0-1
    Jaccard score whose low end is open because 0 would merge every pair of
    findings on a line.

    ``low_inclusive`` distinguishes "must be positive" from "must not be
    negative". Zero is meaningless for a token budget (it asks the model for an
    empty completion), for a timeout (every request fails instantly), for a
    worker count (``ThreadPoolExecutor`` rejects it) and for a chunk count
    (``build_chunks`` raises on the overflow branch). Zero IS meaningful for the
    error, warning and outofscope caps, where it means "report none of that
    severity", for the per-rule cap, where it turns the cap off, for the
    context-line count, where it means "emit the changed
    lines only", for the sampling seed,
    where 0 is a perfectly valid seed, and for the PR-size thresholds, where
    0 flags any change at all.
    """

    low: float
    high: float = math.inf
    low_inclusive: bool = False

    def accepts(self, value: float) -> bool:
        """True if ``value`` lies inside the interval; assumes it is finite."""
        low_ok = value >= self.low if self.low_inclusive else value > self.low
        return low_ok and value <= self.high

    def describe(self) -> str:
        """The bound in words, for the error an operator has to act on."""
        low = (
            f"greater than or equal to {self.low}"
            if self.low_inclusive
            else f"greater than {self.low}"
        )
        if math.isinf(self.high):
            return f"must be a finite number {low}"
        return f"must be a finite number {low} and at most {self.high}"


# The whole numeric surface, in one place. Checked after environment AND
# overrides, so no path into the config can smuggle a degenerate value through.
# Every key in _INT_KEYS | _FLOAT_KEYS must appear here;
# TestPreExistingNumericRanges::test_every_numeric_key_declares_a_range fails
# if a future key is added without a bound.
_RANGES: dict[str, _Range] = {
    "llm_max_tokens": _Range(0),
    "llm_timeout": _Range(0),
    "llm_timeout_per_1k": _Range(0),
    "chunk_token_budget": _Range(0),
    "max_workers": _Range(0),
    "max_inline_comments": _Range(0),
    "max_chunks": _Range(0),
    "chunk_max_files": _Range(0),
    "chunk_context_lines": _Range(0, low_inclusive=True),
    "max_error_findings": _Range(0, low_inclusive=True),
    "llm_seed": _Range(0, low_inclusive=True),
    "spec_max_chars": _Range(0),
    "spec_digest_tokens": _Range(0),
    "llm_cli_concurrency": _Range(0),
    "llm_parse_retries": _Range(0, low_inclusive=True),
    "review_rules_max_chars": _Range(0),
    "ticket_context_max_chars": _Range(0),
    "size_warn_lines": _Range(0, low_inclusive=True),
    "size_warn_files": _Range(0, low_inclusive=True),
    "max_warning_findings": _Range(0, low_inclusive=True),
    "max_outofscope_findings": _Range(0, low_inclusive=True),
    "max_findings_per_rule": _Range(0, low_inclusive=True),
    "scoped_rules_max_chars": _Range(0),
    "repo_context_max_chars": _Range(0),
    "repo_context_max_reads": _Range(0),
    "repo_context_max_chunk_reads": _Range(0),
    "max_areas_per_pr": _Range(0, low_inclusive=True),
    "evidence_max_chars": _Range(0),
    "context_standards_max_chars": _Range(0, low_inclusive=True),
    "confidence_floor": _Range(0.0, 1.0, low_inclusive=True),
    "dedup_similarity": _Range(0.0, 1.0),
}

_LEGACY_ENV_ALIASES: dict[str, str] = {
    "max_error_findings": _ENV_PREFIX + "MAX_ERRORS",
    "evidence_max_chars": _ENV_PREFIX + "EVIDENCE_MAX_CHUNK_CHARS",
}

#: The repository config file auto-discovered in the working directory (#38).
CONFIG_FILE_NAME = ".prxref.toml"

#: The environment variable naming a config file, or ``off`` to disable it.
#: Not a config key: only :func:`find_config_file` reads it.
CONFIG_FILE_ENV = _ENV_PREFIX + "CONFIG_FILE"

#: Where every config-file error points the operator.
CONFIG_DOCS_URL = "https://github.com/sblattj/prxref/blob/main/docs/config-file.md"

#: Keys a repository config file may set (#38). Listed by hand, like
#: :data:`ENV_ONLY_KEYS`, so a new key must be classified before it loads.
FILE_KEYS = frozenset({
    "llm_models", "llm_reasoning_effort", "llm_max_tokens", "llm_timeout",
    "llm_timeout_per_1k",
    "llm_temperature", "llm_seed", "llm_cli_concurrency", "llm_parse_retries",
    "confidence_floor", "max_error_findings", "max_warning_findings",
    "max_outofscope_findings", "max_findings_per_rule", "group_findings",
    "dedup_similarity", "max_chunks", "chunk_token_budget", "chunk_max_files",
    "chunk_context_lines", "max_workers", "max_inline_comments",
    "post_mode", "post_verdict", "post_cost",
    "severity_markers", "summary_bullet_separator",
    "size_warn_lines", "size_warn_files", "size_ignore_globs",
    "spec_sources", "spec_max_chars", "spec_digest_tokens",
    "review_rules", "review_rules_max_chars",
    "scoped_rules", "scoped_rules_max_chars", "prompts_dir",
    "ticket_context_file", "ticket_context_max_chars",
    "evidence_files", "evidence_max_chars",
    "repo_context", "repo_context_max_chars", "context_followup",
    "repo_context_max_reads", "repo_context_max_chunk_reads",
    "suggestions", "routing_probe", "incremental",
    "context_contract_globs", "context_exclude_globs",
    "context_standards_globs", "context_standards_max_chars",
    "metadata_rules", "branch_patterns", "commit_reference",
    "area_globs", "max_areas_per_pr",
    "ci_wiring", "ci_wiring_globs",
    "stable_ids", "verdict_store", "rule_scoping",
})

_ENV_ONLY_REASONS: dict[str, str] = {
    "llm_backend": "executable",
    "llm_base_url": "endpoint",
    "llm_api_key": "credential",
    "llm_cli_path": "executable",
    "fail_on": "gate",
    "dry_run": "gate",
    "allow_unsigned": "gate",
    "trace_file": "local write",
    "trace_dir": "local write",
    "fallback": "local write",
    "price_table": "local read",
    "jira_base_url": "endpoint",
    "jira_email": "credential",
    "jira_api_token": "credential",
    "bitbucket_token": "credential",
    "bitbucket_user": "credential",
    "bitbucket_app_password": "credential",
    "bitbucket_server_token": "credential",
    "bitbucket_server_user": "credential",
    "bitbucket_server_password": "credential",
    "github_token": "credential",
    "github_enterprise_token": "credential",
    "gitlab_token": "credential",
    "gitea_token": "credential",
    "azure_devops_token": "credential",
    "bitbucket_webhook_secret": "credential",
    "github_webhook_secret": "credential",
    "gitlab_webhook_secret": "credential",
    "gitea_webhook_secret": "credential",
    "azure_devops_webhook_secret": "credential",
}

#: Keys only the pipeline may set (#38). A repository file is controlled by
#: whoever can change the repository, a PR author included when CI reads the
#: PR's checkout, so credentials, endpoints, executables, local reads and
#: writes, and the gate never come from it.
ENV_ONLY_KEYS = frozenset(_ENV_ONLY_REASONS)

_FILE_PATH_KEYS = frozenset({
    "review_rules", "scoped_rules", "prompts_dir", "ticket_context_file",
    "spec_sources", "evidence_files", "verdict_store", "metadata_rules",
})


def _toml_type_name(value: object) -> str:
    """The TOML name of a parsed value's type, for a type error."""
    if isinstance(value, bool):
        return "a boolean"
    if isinstance(value, int):
        return "an integer"
    if isinstance(value, float):
        return "a float"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "a table"
    return f"a {type(value).__name__}"


def _file_value(key: str, value: object, display: str) -> object:
    """Type-check one file value and return it in the env layer's type."""
    def wrong(expected: str) -> ConfigError:
        return ConfigError(
            f"{display}: {key!r} must be {expected}, got "
            f"{_toml_type_name(value)}; see {CONFIG_DOCS_URL}"
        )

    if key in _INT_KEYS:
        if key == "llm_seed" and value == _SEED_OFF:
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise wrong('an integer or "off"' if key == "llm_seed" else "an integer")
    if key in _FLOAT_KEYS:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise wrong("a number")
    if key in _BOOL_KEYS:
        if isinstance(value, bool):
            return value
        raise wrong("a boolean")
    if key in _LIST_KEYS:
        if (
            key == "context_standards_globs"
            and isinstance(value, str)
            and value.strip() == _STANDARDS_GLOBS_OFF
        ):
            return []
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return [v.strip() for v in value if v.strip()]
        raise wrong(
            'an array of strings, or "off" to read no standards'
            if key == "context_standards_globs" else "an array of strings"
        )
    if isinstance(value, str):
        return value
    if key == "llm_temperature":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        raise wrong("a number or a string")
    raise wrong("a string")


def _contained_path(key: str, raw: str, base: str, display: str) -> str:
    """Resolve a file-supplied path against the file's directory, contained.

    ``base`` is the real path of the file's directory. An absolute path, a
    ``~`` path, or one whose real path (symlinks followed) leaves ``base`` is
    a :class:`~prxref.llm.ConfigError`.
    """
    def outside(why: str) -> ConfigError:
        return ConfigError(
            f"{display}: {key!r} path {raw!r} must stay inside the repository "
            f"({why}); use a path relative to the config file's directory; "
            f"see {CONFIG_DOCS_URL}"
        )

    if raw.startswith("~"):
        raise outside("a home-directory path")
    if os.path.isabs(raw):
        raise outside("an absolute path")
    resolved = os.path.realpath(os.path.join(base, raw))
    if os.path.commonpath([base, resolved]) != base:
        raise outside("it resolves outside the config file's directory")
    return resolved


def find_config_file(
    *,
    explicit: str | None,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Locate the repository config file for one run, or ``None`` for none.

    ``explicit`` is the ``--config`` value; when it is ``None`` or empty,
    :data:`CONFIG_FILE_ENV` in ``environ`` (default ``os.environ``) is used,
    an empty value reading as unset. ``off`` (any case) in either disables
    the file. A named path is resolved against ``cwd`` (default the working
    directory) and must be an existing file, else
    :class:`~prxref.llm.ConfigError` naming ``--config`` or
    ``PRXREF_CONFIG_FILE``. With neither, ``cwd/.prxref.toml`` is returned
    when it is a file; parent directories are never searched.
    """
    env = os.environ if environ is None else environ
    base = Path.cwd() if cwd is None else cwd
    named, source = explicit, "--config"
    if named is None or not named.strip():
        named, source = env.get(CONFIG_FILE_ENV), CONFIG_FILE_ENV
    if named is not None and named.strip():
        if named.strip().lower() == "off":
            return None
        path = base / Path(named)
        if not path.is_file():
            raise ConfigError(f"{source}: config file not found: {named}")
        return path
    candidate = base / CONFIG_FILE_NAME
    return candidate if candidate.is_file() else None


def _display_path(path: Path) -> str:
    """``path`` relative to the working directory when inside it, else as given.

    Inside is decided lexically first, then with the file's directory and
    the working directory both resolved, so a path spelled through a
    symlinked directory (``/tmp`` for ``/private/tmp`` on macOS) displays
    as its resolved spelling does. The file name itself is never followed.
    """
    absolute = Path(os.path.abspath(path))
    try:
        return str(absolute.relative_to(Path.cwd()))
    except ValueError:
        pass
    try:
        resolved = absolute.parent.resolve() / absolute.name
        return str(resolved.relative_to(Path.cwd().resolve()))
    except (OSError, RuntimeError, ValueError):
        return str(path)


def read_config_file(path: Path, *, display: str | None = None) -> dict[str, object]:
    """Parse and validate a repository config file into ``{key: value}``.

    ``display`` names the file in errors (default ``str(path)``). Values come
    back in the types the environment layer produces; an empty string or an
    empty array reads as unset and is left out, like an empty environment
    variable. ``llm_temperature`` takes a TOML number or a string, and a
    number comes back as the string the environment would hold (``0.2``
    reads as ``"0.2"``). Path keys come back resolved against the file's
    directory. A syntax error (with its line), a non-UTF-8 file, a table, an unknown key,
    an :data:`ENV_ONLY_KEYS` key, a wrong type, a ``spec_sources`` URL, or a
    path outside the file's directory raises
    :class:`~prxref.llm.ConfigError`. The environment is never read.
    """
    name = str(path) if display is None else display
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"{name}: not valid UTF-8 (byte {exc.start}); save the file as "
            f"UTF-8; see {CONFIG_DOCS_URL}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"{name}: cannot read config file: {exc.strerror}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{name}: invalid TOML: {exc}; see {CONFIG_DOCS_URL}") from exc
    base = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    result: dict[str, object] = {}
    for key, raw in data.items():
        if isinstance(raw, dict):
            raise ConfigError(
                f"{name}: {key!r} is a table, but the config file is flat; "
                f"write each key at the top level; see {CONFIG_DOCS_URL}"
            )
        if key not in _DEFAULTS:
            probe = key.lower().removeprefix(_ENV_PREFIX.lower())
            close = difflib.get_close_matches(probe, list(_DEFAULTS), n=1)
            hint = f" did you mean {close[0]!r}?" if close else ""
            raise ConfigError(
                f"{name}: unknown key {key!r};{hint} see {CONFIG_DOCS_URL}"
            )
        if key in ENV_ONLY_KEYS:
            raise ConfigError(
                f"{name}: {key!r} cannot be set in a repository config file "
                f"({_ENV_ONLY_REASONS[key]}); set {_ENV_PREFIX}{key.upper()} "
                f"in the pipeline instead; see {CONFIG_DOCS_URL}"
            )
        value = _file_value(key, raw, name)
        if key == "context_standards_globs" and not value:
            result[key] = []
            continue
        if value == "" or value == []:
            continue
        if key == "spec_sources":
            for entry in value:
                if "://" in entry:
                    raise ConfigError(
                        f"{name}: 'spec_sources' entry {entry!r} is a URL; a "
                        f"repository config file lists local paths only, so "
                        f"set PRXREF_SPEC_SOURCES in the pipeline for web and "
                        f"Jira sources; see {CONFIG_DOCS_URL}"
                    )
        if key in _FILE_PATH_KEYS and not (
            key == "metadata_rules" and value.strip() in METADATA_RULES_SWITCHES
        ):
            if isinstance(value, list):
                value = [_contained_path(key, v, base, name) for v in value]
            else:
                value = _contained_path(key, value, base, name)
        result[key] = value
    return result


def _truthy(raw: str) -> bool:
    """Parse a security-gating boolean; only the literal "1" enables it.

    Deliberately rejects "true"/"yes"/"on" so a typo or a shell quirk fails safe
    with verification left ON. Must stay identical to
    prxref.webhooks._allow_unsigned, which is the gate that actually runs;
    TestAllowUnsignedAgreesWithGate pins the two together.
    """
    return raw.strip() == "1"


def _coerce_env(key: str, raw: str, source: str) -> object:
    """Type-coerce one env value; a malformed one is a usage error, not a crash.

    ``source`` is the variable the value was actually read from, so a value set
    through a legacy alias is reported under the name the operator typed.
    """
    try:
        if key == "llm_seed" and raw.strip() == _SEED_OFF:
            return _SEED_OFF
        if key == "context_standards_globs" and raw.strip() == _STANDARDS_GLOBS_OFF:
            return []
        if key in _INT_KEYS:
            return int(raw.strip())
        if key in _FLOAT_KEYS:
            return float(raw.strip())
        if key in _BOOL_KEYS:
            return _truthy(raw)
        if key in _LIST_KEYS:
            return [
                part.strip() for part in re.split(r"[,\s]+", raw) if part.strip()
            ]
        return raw
    except ValueError as exc:
        raise ConfigError(f"{source}: {exc}") from exc


def _check_ranges(cfg: dict[str, object], sources: dict[str, str]) -> None:
    """Reject out-of-range numbers before they reach the wire.

    Runs after environment AND overrides, so every path into the config is
    covered. Failures are ``ConfigError`` (exit 2) rather than a mid-review
    error the operator has to decode from a provider's rejection — or, worse, a
    review that looks like it succeeded.

    The message names ``sources[key]``: whichever input actually supplied the
    offending value. Naming the environment variable unconditionally sent an
    operator who typed ``--max-chunks 0`` to hunt for a ``PRXREF_MAX_CHUNKS``
    they had never set.

    A ``None`` value is the declared unset for keys whose resolved default
    lives in the factory (``llm_seed``: unset falls back to the
    once-per-process seed resolved in ``llm_backends.create_llm_client``):
    there is no number to range-check, and
    "not configured" is not a violation. The PR-size thresholds
    (``size_warn_lines``, ``size_warn_files``) are the second such class:
    ``None`` means the advisory is off, because 0 is a legal threshold and
    cannot double as "unset". The 0.15.0 keys add two more: the warning and
    outofscope caps (``max_warning_findings``, ``max_outofscope_findings``),
    where ``None`` means unlimited because 0 caps every finding of that
    severity, and ``dedup_similarity``, where ``None`` means the
    reworded-duplicate pass is off. ``llm_seed`` also takes the exact
    string ``"off"`` (send no seed, issue #26), which is skipped the same
    way; any other string, ``"OFF"`` included, still fails the check. Every
    other value that is not ``None`` — including one smuggled in through an
    override — is still checked.
    """
    for key, rng in sorted(_RANGES.items()):
        value = cfg[key]
        if value is None or (key == "llm_seed" and value == _SEED_OFF):
            continue
        finite = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
        if not finite or not rng.accepts(value):
            raise ConfigError(f"{sources[key]}: {rng.describe()}, got {value!r}")


def _check_choices(cfg: dict[str, object], sources: dict[str, str]) -> None:
    """Reject a value outside its key's allowed vocabulary.

    Same pass, same failure mode as :func:`_check_ranges`: it runs after
    environment AND overrides, and the message names ``sources[key]`` —
    whichever input actually supplied the offending value.
    """
    for key, choices in sorted(_CHOICE_KEYS.items()):
        value = cfg[key]
        if isinstance(value, str) and value in choices:
            continue
        allowed = ", ".join(repr(c) for c in sorted(choices))
        raise ConfigError(f"{sources[key]}: must be one of {allowed}, got {value!r}")


def _check_post_mode(cfg: dict[str, object], sources: dict[str, str]) -> None:
    """Reject a ``post_mode`` outside the documented vocabulary.

    Same doctrine as :func:`_check_ranges`: it runs after environment AND
    overrides, and a failure is a ``ConfigError`` naming whichever input
    supplied the value — exit 2 before anything is reviewed, never a silent
    fall-back to the default mode that would post findings nobody asked for.
    """
    value = cfg["post_mode"]
    if value not in _POST_MODES:
        raise ConfigError(
            f"{sources['post_mode']}: must be one of "
            f"{' | '.join(_POST_MODES)}, got {value!r}"
        )


def _check_price_table(cfg: dict[str, object], sources: dict[str, str]) -> None:
    """Parse ``price_table`` in place, rejecting a malformed table.

    Same doctrine as :func:`_check_post_mode`: it runs after environment AND
    overrides, and a failure is a ``ConfigError`` naming whichever input
    supplied the value, so a typo'd price is exit 2 before anything is
    reviewed, never a silently wrong estimate. The value may be inline JSON,
    a path to a JSON file, or a mapping from a library caller; all three are
    validated by :func:`prxref.costs.parse_price_table`. Afterwards the key
    holds a ``dict[str, costs.ModelPrice]``, ``{}`` when no table is set.
    """
    cfg["price_table"] = costs.parse_price_table(
        cfg["price_table"], source=sources["price_table"]
    )


def _check_severity_markers(cfg: dict[str, object], sources: dict[str, str]) -> None:
    """Reject a malformed ``severity_markers`` table (#59).

    Same doctrine as :func:`_check_price_table`: it runs after environment
    AND overrides, and a failure is a ``ConfigError`` naming whichever input
    supplied the value. The value is the ``name=glyph`` pair string, or a
    mapping from a library caller; both are validated by
    :func:`prxref.markers.parse_overrides` and left as given, for
    :func:`prxref.markers.configure` to install.
    """
    try:
        markers.parse_overrides(cfg["severity_markers"])
    except ValueError as exc:
        raise ConfigError(f"{sources['severity_markers']}: {exc}") from exc


def _check_bullet_separator(cfg: dict[str, object], sources: dict[str, str]) -> None:
    """Validate ``summary_bullet_separator`` (#59); ``""`` reads as the default.

    The value is never stripped, because its leading and trailing spaces are
    the point. A non-string, a newline or carriage return, or more than
    :data:`_MAX_BULLET_SEPARATOR_CHARS` characters is a ``ConfigError``
    naming whichever input supplied it.
    """
    value = cfg["summary_bullet_separator"]
    source = sources["summary_bullet_separator"]
    if not isinstance(value, str):
        raise ConfigError(f"{source}: must be a string, got {value!r}")
    if value == "":
        cfg["summary_bullet_separator"] = _DEFAULT_BULLET_SEPARATOR
        return
    if "\n" in value or "\r" in value:
        raise ConfigError(f"{source}: must not contain a newline, got {value!r}")
    if len(value) > _MAX_BULLET_SEPARATOR_CHARS:
        raise ConfigError(
            f"{source}: must be at most {_MAX_BULLET_SEPARATOR_CHARS} characters, "
            f"got {len(value)} ({value!r})"
        )


def _check_metadata_switch(
    cfg: dict[str, object], sources: dict[str, str], supplied: set[str],
) -> None:
    """Validate ``metadata_rules`` (#70): a switch value or a rules-file path.

    The value must be a string. ``off``, ``""`` and ``on`` are switches;
    anything else names a rules file, which is opened later, before any
    network call, by :func:`prxref.metadata_rules.load_metadata_rules`. A
    rules file replaces the four flat keys, so an operator who also set one
    of them (env, file or override; a default does not count) gets a
    ``ConfigError`` naming both inputs instead of a value silently ignored.
    """
    value = cfg["metadata_rules"]
    if not isinstance(value, str):
        raise ConfigError(
            f"{sources['metadata_rules']}: must be 'off', 'on' or a rules file "
            f"path, got {value!r}"
        )
    if value.strip() in METADATA_RULES_SWITCHES:
        return
    for key in _METADATA_FLAT_KEYS:
        if key in supplied:
            raise ConfigError(
                f"{sources[key]}: cannot be set together with the metadata rules "
                f"file {value!r} ({sources['metadata_rules']}); put the setting in "
                f"the rules file, or set {sources['metadata_rules']} to 'on' to "
                f"use the flat keys"
            )


def _check_metadata_rules(cfg: dict[str, object], sources: dict[str, str]) -> None:
    """Validate the PR-metadata rule keys (#70); every entry must parse.

    Same doctrine as :func:`_check_choices`: it runs after environment AND
    overrides, and a failure is a ``ConfigError`` (exit 2) naming whichever
    input supplied the value — a bad pattern is a usage error before any
    review, never a mid-review crash. ``branch_patterns`` entries must be
    ``TYPE=REGEX`` with both sides non-empty and a regex that compiles;
    ``area_globs`` entries must be ``NAME=GLOB`` with both sides non-empty
    (the glob itself is checked where it is matched); a non-empty
    ``commit_reference`` must compile. The values are left exactly as
    given, so the config dict keeps the operator's strings and the checks
    compile them once more at use time — a third copy of the pattern never
    exists.
    """
    pairs: list[tuple[str, str]] = [
        ("branch_patterns", "TYPE=REGEX"), ("area_globs", "NAME=GLOB"),
    ]
    for key, shape in pairs:
        value = cfg[key]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{sources[key]}: must be a list of strings, got {value!r}")
        for entry in value:
            name, _, pattern = entry.partition("=")
            if not name.strip() or not pattern.strip():
                raise ConfigError(
                    f"{sources[key]}: entry {entry!r} must be {shape} "
                    f"with both sides non-empty"
                )
            if key != "branch_patterns":
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(
                    f"{sources[key]}: entry {entry!r} regex does not "
                    f"compile: {exc}"
                ) from exc
    reference = cfg["commit_reference"]
    if reference:
        if not isinstance(reference, str):
            raise ConfigError(
                f"{sources['commit_reference']}: must be a string, got {reference!r}"
            )
        try:
            re.compile(reference)
        except re.error as exc:
            raise ConfigError(
                f"{sources['commit_reference']}: regex does not compile: {exc}"
            ) from exc


def load_config(
    *,
    config_file: Path | None = None,
    source_labels: dict[str, str] | None = None,
    **overrides: object,
) -> dict:
    """Build the runtime config dict from defaults, environment, then overrides.

    ``config_file`` (#38) adds a repository config file layer between the
    defaults and the environment, read by :func:`read_config_file`; its
    values are attributed to ``<file>: <key>`` in errors, where ``<file>`` is
    the path relative to the working directory when inside it, and count as
    operator-supplied. ``None`` (the default) reads no file; this function
    never discovers one itself (see :func:`find_config_file`).

    Keys mirror the env table above (lowercase, no prefix). Env values are
    type-coerced per key (int / float / bool / comma-or-whitespace list /
    str); an empty or whitespace-only value reads as unset. A string value
    is never stripped, so ``summary_bullet_separator`` keeps its spaces.
    ``price_table`` comes back parsed, as a dict. A malformed value, one out of its numeric
    range, or one outside its key's allowed vocabulary raises
    :class:`~prxref.llm.ConfigError` naming the input that supplied it, which
    the CLI turns into exit 2.

    ``source_labels`` lets a caller say what its user calls an override — the
    CLI passes ``{"max_chunks": "--max-chunks"}`` so a bad flag is reported as
    the flag. It is used for error messages only, and only for keys the caller
    actually overrode; an unlabelled override is reported under its config key.
    Config itself knows no flag names: the caller that owns the surface names
    it.
    """
    return load_config_with_sources(
        config_file=config_file, source_labels=source_labels, **overrides,
    )[0]


def load_config_with_sources(
    *,
    config_file: Path | None = None,
    source_labels: dict[str, str] | None = None,
    **overrides: object,
) -> tuple[dict, dict[str, str]]:
    """:func:`load_config`, plus which layer supplied each key (#38).

    Takes the same arguments, validates the same way and returns the same
    config dict, paired with ``{key: layer}`` for every key, where ``layer``
    is ``"default"``, ``"file"`` (the ``config_file``), ``"env <NAME>"``
    (``NAME`` being the variable actually read, a legacy alias included) or
    ``"override"`` (a keyword argument). A key the ``suggestions``
    token-budget bump raised keeps the layer that supplied its value before
    the bump, ``"default"``.
    """
    cfg: dict[str, object] = {
        key: list(value) if isinstance(value, list) else value
        for key, value in _DEFAULTS.items()
    }
    # What supplied each value, for error messages. Defaults start out attributed
    # to their environment variable: that is the name an operator would set to
    # change one, and a built-in default is never out of range anyway.
    sources: dict[str, str] = {key: _ENV_PREFIX + key.upper() for key in _DEFAULTS}
    labels = source_labels or {}
    # Keys an operator actually supplied (env, its legacy alias, or an
    # override), as opposed to a key merely present in ``sources`` because
    # every default is pre-attributed to its env var name. Used below to
    # decide whether the suggestions token-budget bump may apply.
    supplied: set[str] = set()
    layers: dict[str, str] = dict.fromkeys(_DEFAULTS, "default")
    if config_file is not None:
        display = _display_path(config_file)
        for key, value in read_config_file(config_file, display=display).items():
            cfg[key] = value
            sources[key] = f"{display}: {key}"
            layers[key] = "file"
            supplied.add(key)
    for key in _DEFAULTS:
        name = _ENV_PREFIX + key.upper()
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            legacy = _LEGACY_ENV_ALIASES.get(key)
            if legacy:
                raw = os.environ.get(legacy)
                name = legacy
        if raw is None or not raw.strip():
            continue
        cfg[key] = _coerce_env(key, raw, name)
        sources[key] = name
        layers[key] = f"env {name}"
        supplied.add(key)
    for key, value in overrides.items():
        if key not in _DEFAULTS:
            raise ValueError(f"unknown config key: {key!r}")
        if value is None:
            continue
        cfg[key] = value
        sources[key] = labels.get(key, key)
        layers[key] = "override"
        supplied.add(key)
    if cfg["suggestions"] == "on" and "llm_max_tokens" not in supplied:
        # Issue #30 part D: at the default budget, GLM 5.3 Flash truncated
        # every suggestions-on reference run. An explicit value (env, its
        # legacy alias, or an override) always wins, even when lower than
        # SUGGESTIONS_MAX_TOKENS.
        cfg["llm_max_tokens"] = max(cfg["llm_max_tokens"], SUGGESTIONS_MAX_TOKENS)
    _check_ranges(cfg, sources)
    _check_choices(cfg, sources)
    _check_post_mode(cfg, sources)
    _check_price_table(cfg, sources)
    _check_severity_markers(cfg, sources)
    _check_bullet_separator(cfg, sources)
    _check_metadata_switch(cfg, sources, supplied)
    _check_metadata_rules(cfg, sources)
    return cfg, layers


def make_forge(ref: PRRef, session=None) -> Forge:
    """Instantiate the ForgeImpl matching ``ref.forge``.

    ``session`` optionally injects a custom ``requests.Session`` (tests,
    shared connection pools). Unknown forge names raise ``ValueError``.
    """
    from prxref.forges import azure_devops, bitbucket, bitbucket_server, gitea, github, gitlab

    impls = {
        "bitbucket": bitbucket.ForgeImpl,
        "bitbucket-server": bitbucket_server.ForgeImpl,
        "github": github.ForgeImpl,
        "gitlab": gitlab.ForgeImpl,
        "gitea": gitea.ForgeImpl,
        "azure-devops": azure_devops.ForgeImpl,
    }
    impl = impls.get(ref.forge)
    if impl is None:
        raise ValueError(f"unknown forge: {ref.forge!r}")
    return impl(session=session)
