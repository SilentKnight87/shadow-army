# Shadow Army reference

A cooperative `kanban_complete` gate requires a fresh receipt from a different model family for the current Git-visible code and check definition. Independence covers the requesting session, bound build and repair children, and workers observed in this profile.

## What it does on your machine

- Runs Git to locate work trees, read HEAD and index metadata and produce a binary diff. Git may run configured clean and process filters, just as normal Git operations do. The plugin also reads flagged tracked paths and non-ignored untracked content. Git can refresh index metadata and briefly take an index lock. Configured filters can have their own side effects.
- Reads Kanban databases read-only, including the shared home and pinned worker database.
- Writes its own SQLite file under profile plugin data, including observed model routes, card bindings, receipts and bounded check output.
- The deliberate trial creates a toy Git repo in plugin data and runs a bundled unittest.
- Makes no network requests and sends no telemetry. Hermes runs the explicitly requested verifier and check using its normal model routing and terminal approval flow.

## What it does not do

- It is not a sandbox. The threat is an agent closing a card on code no independent reviewer checked, through mistake or overconfidence. An agent that deliberately sabotages its own shell or repository can fool a check running in that environment.
- It gates only the agent's `kanban_complete` tool. Manual dashboard edits, direct database writes, shell commands, direct registry dispatch, and disabling the plugin bypass it.
- It does not run production checks or launch models or CLIs. Hermes handles the child and terminal approvals.
- It cannot repair host-level limits. Hermes's host hooks can time out, unload, or fail open. An unresolved or unreadable target passes through to Hermes. Other close-callback exceptions block.
- A sabotaged environment can fool a check. A receipt proves command text, observed model provenance, and foreground exit status in the host environment. It does not attest to PATH, the interpreter, site-packages, ignored files or files outside the repo.
- Ignored files and submodule contents are not fingerprinted. Nested untracked repositories cover only their HEAD pointer, not dirty contents.
- Git runs with your normal configuration, including any filters it configures. A deliberately misleading filter can hide changes.
- Local terminal only. Evidence uses Hermes's effective per-command terminal scope, or process `TERMINAL_ENV` and configured `terminal.backend` when no scope is bound. Long checks must finish in a foreground terminal result.
- Family identity is observed from session routes and request and response model ids. A proxy can blur it. Unknown families refuse. This is not provider attestation or proof of review quality.
- Independence is relative to the requesting session, bound build and repair children and worker sessions named in task-run metadata and observed in this profile. The card creator is not a builder. Unobserved sessions are skipped. Arbitrary earlier authors are not discovered.
- Session compaction can hide earlier routes or rotate a verifier session, causing a refusal.
- Fingerprint, command execution, receipt, and completion are separate operations. The final check and completion are not atomic. Concurrent edits can race them. Use a trusted, quiet workspace.
- Only one trusted profile and one verifier in flight per card are supported. Other profiles have separate stores. A worker that ran in another profile is not observed here.
- The nonce is a one-use correlation marker in model-visible goal text, not an authorization secret. Acceptance also requires the actual parent-to-child lifecycle and caller session.
- No retry on an inference failure is promised.
- The repeatable tests do not prove a live provider login or child hop. Their request and child lifecycle observations are synthetic.

## Requirements

Requires Hermes `>=0.23.0`, Git, a repository with a first commit, and Python 3.11 or newer. No external ARISE engine, third-party Python dependencies, credentials, network requests, or background services. Hermes supplies the dashboard's FastAPI dependency. This folder is the entire install.

Install and enable this plugin in the trusted profile. Enable its Desktop capability to display the pane. Backend enablement alone does not enable Desktop. Let Hermes request any profile-specific reload. No live app restart is needed for the tests described in [VERIFICATION.md](VERIFICATION.md).

## Settings

Three things arm the gate:

1. The plugin is enabled in the profile.
2. A verifier provider and model are set in **Settings → Plugins → Shadow Army** (the existing `verifier.provider` and `verifier.model` keys also work).
3. Delegation is routed through the plugin with `delegation.backend: shadow-army`.

Use **Settings → Plugins → Shadow Army** for verifier provider, verifier model, check command, PASS lifetime in minutes, and the optional repository; leaving the default check in effect satisfies that setup item. Enable routing with `/delegation shadow-army --now` in an existing chat, or the profile configuration below. Choose models already available through your Hermes configuration. The configuration example uses a known model id. It does not provision access.

```yaml
plugins:
  enabled: [shadow-army]
  entries:
    shadow-army:
      settings:
        check: python3 -m unittest discover -s tests
        max_age_s: 3600
        verifier:
          provider: anthropic
          model: claude-sonnet-5-5
          # Optional alternative routes, considered in order before dispatch.
          fallbacks: []
        family_overrides: {}
delegation:
  backend: shadow-army
```

In an existing chat, run `/delegation shadow-army --now` or start a new session after enabling routing. Set `check` to the exact command your verifier must run. Do not disable the check to work around a failed verification.

The gate applies to code cards. A `worktree` card uses its declared Git workspace. A `dir` card qualifies when its folder is inside a Git work tree. A `.git` at your home folder or above does not count. Scratch cards and other `dir` cards are left alone. They show NOT GATED and close through Hermes normally. Until the gate is armed, code cards are NOT ARMED and also close normally. The optional `repo` setting, described under [Settings](#settings), gates scratch cards too.


Open **Settings → Plugins → Shadow Army** (or the gear on its Capabilities → Plugins row) to fill in the form. Values live under `plugins.entries.shadow-army.settings` in the profile's `config.yaml`. The form's `verifier_provider` and `verifier_model` override corresponding legacy `verifier` fields when nonempty; `pass_lifetime_minutes`, when set, overrides `max_age_s`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `check` | `python3 -m unittest discover -s tests` | The exact command the verifier runs. Changing it invalidates prior receipts. |
| `verifier.provider`, `verifier.model` | none | The verifier route. Both are required to arm the gate. |
| `verifier.fallbacks` | `[]` | Optional alternative routes, considered in order before dispatch. |
| `family_overrides` | `{}` | Exact lower-case `provider/model` keys mapped to a family name in `policy.py`. |
| `max_age_s` | `3600` | Seconds a PASS stays fresh. |
| `repo` | none | Optional. Gates scratch cards and `dir` cards outside a work tree against one repository. |

The gate is armed only when verifier.provider/model are configured and delegation.backend is shadow-army. Otherwise repository cards are NOT ARMED and completion passes through normally. A check command is expected by default. Explicitly setting `check: ""` disables command evidence. It also invalidates prior receipts.

Known model families in `policy.py` are Anthropic, OpenAI, Google, xAI, Meta, Mistral, DeepSeek, Qwen, Moonshot/Kimi, Zhipu/GLM, Nous/Hermes, and Cohere. Aggregator ids use their model leaf names, for example `anthropic/claude-sonnet-5-5`. Unknown aliases refuse. Owner overrides are exact lower-case `provider/model` keys mapped to one of the family names in `policy.py`. Do not infer a family from a proxy's provider label alone.

An optional verifier route list chooses the first eligible configured route. Once selected, its runtime fallback providers are disabled.

The **Two families** setup item compares the configured delegation provider/model with the verifier, or the main model when no delegation route is configured. Both families must be known and unrelated. Its value names the builder (marked delegated when applicable) and verifier families. The pane offers already configured independent routes, including the main model when it qualifies. The main model can coordinate and verify as long as the main session only delegates and runs no terminal command or file edit itself. This setup guidance does not change gate decisions.

Setting `delegation.backend: default` unarms completion gating.

## Which cards are gated

An open `worktree` card uses its declared Git workspace. A missing or invalid worktree is UNKNOWN. Restore the worktree or update the card's workspace before requesting another review. A `dir` card qualifies when a filesystem walk finds a Git work tree before reaching the user's home or crossing a filesystem boundary. A `.git` at home or above never makes a notes folder a code workspace. Scratch workspaces never qualify automatically, including scratch folders inside a Git repository.

The optional `repo` setting gates every such card: every scratch card and every `dir` card outside a work tree, on every board, against that repository. It never replaces a broken `worktree` workspace. Without that opt-in these cards are NOT GATED and completion passes through Hermes normally (the plugin can record completion events). Code cards are NOT ARMED until the gate is armed, and UNKNOWN when their repository cannot be fingerprinted.

A completion reads only the host-selected board. An unreadable target card passes through to Hermes's own validation without a fallback repo. Board selection follows Hermes v0.23.0's context, environment pins and persisted current board. Kanban data is shared across profiles, using `HERMES_KANBAN_HOME` when set.

Done and archived cards retain the gate recorded at completion. Their worktrees are never fingerprinted again, even after Hermes removes them or later code changes. Closed PASS shows the reviewed tree and does not expire. The trial also offers tamper only before close.

## Gate states

| Pane state (API name when different) | Close allowed | Meaning |
| --- | --- | --- |
| PASS | yes | Fresh independent PASS with evidence for this tree and check. |
| UNVERIFIED | no | No receipt for this tree. |
| BLOCKED | no | The independent review failed this code. |
| STALE | no | The code or the check command changed after the last review, so that verdict no longer counts. A PASS older than `max_age_s` is also STALE. |
| UNKNOWN | no | The repository cannot be fingerprinted, or the worktree is missing or invalid. |
| NOT ARMED (`UNARMED`) | yes | The completion gate is not armed. |
| NOT GATED (`UNGATED`) | yes | A scratch card, or a `dir` card outside a Git work tree, with no `repo` setting. The gate leaves it alone. |

## Tools, hooks and skill

| Name | Kind | What it does |
| --- | --- | --- |
| `army_gate(card, board?)` | tool | Reads the current gate state and setup hint. |
| `army_verdict(card, verdict, findings, board?)` | tool | Records a bound verifier's PASS or FAIL. |
| `pre_tool_call` | hook | Gates `kanban_complete`, binds `delegate_task` tasks when delegation uses shadow-army, and records terminal starts, and asks for approval before publishing unreviewed code. |
| `post_tool_call` | hook | Records terminal results and delegation outcomes, and records `closed` after a successful completion. |
| `subagent_start`, `subagent_stop` | hooks | Bind and release verifier children. |
| `pre_api_request`, `post_api_request` | hooks | Observe the provider, model and response model of each session. |
| `shadow-army` | delegation backend | Adds `duty`, `card` and `board` to `delegate_task` and routes verify tasks to the configured verifier. |
| `shadow-army:verify-loop` | skill | Teaches the build, verify, repair and close loop. |

## Build and verify a card

1. Read `army_gate(card, board?)` for the current state and setup hint.
2. Use Hermes `delegate_task` with `duty: build` and `card` set to the Kanban card id. `repair` also takes Hermes's default delegation route. Supply `board` for ambiguous ids. Tasks without a card and build or repair tasks whose tree cannot be fingerprinted use the default route unchanged and remain unbound, including ordinary `delegate_task` tasks. A verify task always requires a card. A malformed verifier is refused per task while valid siblings continue.
3. Request `duty: verify` for that card. Only one verification may be pending or running per card. A new request supersedes a child that never started immediately. A reported delegation failure also releases its binding. Wait for a running child to finish. Started bindings expire after one hour.
4. The child inspects the diff and runs the exact configured command through Hermes `terminal`, with `workdir` explicitly set to the repository. Use a foreground command and wait for its integer exit status. Background, promoted, and running process results are not accepted as checks. Evidence requires a local terminal according to Hermes's effective per-command terminal scope, with process environment and configuration used only when no scope is bound. The command runs exactly as issued, in the session's own shell and environment. The receipt keeps its exact text. Call terminal and army_verdict directly. execute_code does not preserve the required child identity.
5. The child calls `army_verdict(card, verdict, findings, board?)`. FAIL needs at least one `{file, line, text}` finding. PASS needs observed successful model response provenance, known independent families for every request and response, and the observed check's exit 0.
6. Repair findings and request a fresh verifier. On an armed code card, only fresh PASS permits `kanban_complete`.

## The trial

Start the trial deliberately in the pane. Its POST API is `/demo/start`, `/demo/step`, `/demo/tamper`, and `/demo/reset`. Read it with `/snapshot?demo=1`.

Start opens a real failing empty-cart test. The builder then deletes its assertion, making the real check pass; the plugin's bounded inventory detects the removal. Next calls the real close and same-family routing decisions, then records an independent simulated FAIL whose finding is built from the removed file, line and text. Restoring the assertion and fixing the code passes the check; a fresh review earns PASS and permits close. **Tamper** remains available between PASS and close: it changes a tracked line, making the seal STALE, then Next refuses close, records a fresh PASS and closes. Every step runs the real check. Only model identities and their judgments are simulated; routing, removed-assertion detection, verdict validation, fingerprints and close decisions run through the real gate service.

Every reviewer is simulated, every trial verdict is labeled `simulated`, and every trial receipt is `synthetic`. The unittest is real. It runs with the current Python interpreter, fixed argv, no shell, a three-second timeout, and a minimal environment inside the plugin's toy repository. Its actual exit status and bounded outcome lines appear in the receipt. Trial records use their own namespace and can never satisfy a real card gate. Reset clears trial records. The bounded toy files remain for the next deliberate start.

## Storage and API

Profile-owned `plugin-data/shadow-army/gates.sqlite3` contains receipts, one-use bindings, route observations, bounded command excerpts, and gate events. Each session stores at most 16 distinct (provider, model, response_model) routes. Repeated observations write at most once an hour to refresh session retention. Overflow marks the family unknown and refuses verification with "too many routes". Session rows are kept for seven days. Terminal evidence older than a day is pruned on writes. Terminal observation ignores unbound sessions, and observer failures never interrupt the agent.

SQLite transactions serialize verification admission across processes. The journal uses WAL only on runtimes without SQLite's WAL-reset bug (3.51.3+, or the 3.50.7 and 3.44.6 backports). Other runtimes use DELETE, matching Hermes's version rule. Reads retry briefly. Reads take no write lock. Fingerprints and the trial check run outside write transactions. Restarted workers read the same receipts.

No raw prompt, conversation, or model response body is collected automatically. Provider credentials are not collected automatically. Check output and findings are stored verbatim and can contain credentials or private project data. This is a single trusted profile, not a security sandbox.

`GET /snapshot` returns schema version 2 with `demo`, `observed_at`, `armed`, `setup`, `setup_url`, `trial`, `cards`, and `errors`, plus an event revision `rev` for its namespace. Each card has its own human next-step hint. Exact agent calls appear only in `army_gate` and completion block messages. Setup counts the built-in default check as ready and distinguishes it from an owner-configured command and shows whether the effective terminal is local. Open cards contain a recomputed gate, shadows, filtered decision text, and history. Snapshots share each repository fingerprint for up to two seconds. Gate decisions bypass this cache.

NOT GATED cards show a neutral NOT GATED seal below gated cards. The first-run view offers Play the trial when all cards are NOT GATED or setup is incomplete. On an armed board, NOT GATED cards fold below gated cards. Setup POST routes save only this plugin's verifier settings through its registered `ctx.set_config`; routing remains an explicit owner command. The self-test uses a separate throwaway namespace and does not claim that the host hook ran. The external `army status` reader and raw log and timeline routes are removed. Each gate event broadcasts `gate.changed` with `{board, card, demo}`.

Hermes's authenticated enabled-plugin mount protects all endpoints. Never serve the router independently. Check output is filtered line by line, dropping secret-like lines, credential URLs, e-mail addresses and home paths. Model ids and unittest separators remain readable. This display filter is not a credential boundary.

close_permitted records permission before completion. closed is emitted only after the host reports successful completion for that card. A snapshot storage error includes errors and retains the last good cards when available in that process. A failed board retains only its own last good cards. Healthy boards keep updating. The close-permission event is best-effort and cannot block a fresh PASS.

## Fingerprint coverage

Identity includes the absolute Git root, HEAD tree id, Git's binary diff of tracked files against HEAD (staged and unstaged), and names and contents of non-ignored untracked files. All enumeration runs from the Git root, including for subdirectory workspaces. Git runs with your normal configuration, including global excludes and any configured filters. Assume-unchanged and skip-worktree files contribute their work-tree bytes too. LFS and filter attributes do not refuse the repository. Symlinks contribute their link text, never target contents. Ignored files and submodule contents are excluded. Git-visible submodule pointers remain covered. Untracked nested repositories contribute their path and HEAD id (or an unborn marker before their first commit). Untracked interpreter caches (`__pycache__`, `*.pyc`, `.pytest_cache`) and `.DS_Store` are excluded automatically. Git omits untracked non-file entries such as FIFOs.

Git uses `--no-ext-diff`, `--no-textconv`, disabled fsmonitor, `--no-optional-locks`, and `GIT_OPTIONAL_LOCKS=0`. A timed-out Git process receives SIGTERM and has a brief chance to release locks before SIGKILL is used as a last resort. The fingerprint has a 1.8-second overall deadline and a 4 MiB budget on untracked content read. There is no tracked-content size limit. A timeout gives UNKNOWN. Let other Git or build work finish and retry, or commit pending changes to reduce the diff. Budget failures name untracked content to commit or ignore. A repository without HEAD asks for a first commit. Both the start and end identity must match for check evidence. Gate decisions always compute a fresh identity.

## Host coupling

The plugin reads the `hermes_terminal_scope` context variable because Hermes has no documented public accessor admitted for plugins. This coupling needs a compatibility check on upgrade. A session may retain a previously cached executor after a settings change. Start a fresh terminal session, and reload the disposable backend if that session still retains it.

Board selection mirrors Hermes v0.23.0 context-variable names because its board-path resolver is not a public plugin API. An unresolved or unreadable target passes through.

## Prove the live model hop

The repeatable tests use Hermes's real manager, route resolver, dispatch path, terminal, Kanban completion, authenticated API mount, and a second process. Request and child lifecycle observations in those tests are synthetic. They do not prove a live provider login or child hop.

Use a separate disposable Hermes home and an enabled clean copy of this folder. Authenticate that disposable profile interactively through Hermes. Never copy another application's tokens. Configure a known builder route and an already available different-family verifier route. Create a card with a Git workspace and an owner-controlled deterministic check. Ask the builder to implement it, request `duty: verify`, and let the child inspect the change, run the check, and call `army_verdict`. Confirm the child can see `army_verdict` and the receipt names the actual response model. Confirm close blocks before PASS, succeeds after PASS, survives a worker restart, and refuses after an edit or check-setting change. Try a same-family route and a failing check. Keep the trial separate. Never point this exercise at a live home.

## Recover a stuck card

Read the gate's reason first. Restore a missing open-card worktree, or make a first commit in an unborn repository. When a check changes generated output, add that output to Git's ignore rules and request a new review. Refusals name up to five paths changed during the check. If an earlier interrupted Git operation left `.git/index.lock`, confirm no Git process owns it before removing that stale lock. Setting `delegation.backend: default` unarms completion gating.

See [VERIFICATION.md](VERIFICATION.md) for the tests and commands to reproduce them.

Nous/Hermes models count as related to both Meta and Qwen in either direction for independence checks, unless the owner explicitly reclassifies the model with `family_overrides`.

Assertion inventories are advisory: at most 2,000 test-like Git-visible files, a 4 MiB name list, 4 MiB of test-file bytes, 10,000 assertion-like lines and one second per scan; when a scan fails or exceeds a budget, the pane and the verifier's brief say "Test scan unavailable: review the test diff by hand", which is different from a scan that found nothing. On a builder's finished row the history says "started from code version", because the recorded tree is the one the builder started from. Ordinary files do not consume the test-file count; unreadable and symlinked tests are skipped. Normalized assertion text is matched across file moves and renames; this is a mechanical hint for review, not proof of test semantics, and it never changes the gate state or runs in the close hook.

Removed assertions compare against the last passing review, or the card start until its first PASS. FAIL never advances that baseline; `since: review` means the last passing review. In the trial the removed line remains visible from step 2 through step 5, until restoration at step 6.

The parent counts as a builder until a build/repair child or dispatcher worker exists for the card. With another builder present, a parent that only delegates and makes no observed terminal, write_file or patch call is excluded. An observed `terminal`, `write_file` or `patch` call keeps the parent counted, even if the terminal command only reads. This flag is stored once per session, after the tool call; it is not a sandbox.

The settings form supports JSON list/object fields. `verifier_fallbacks` overrides `verifier.fallbacks` when set; `family_overrides` is editable as a JSON object. These use configured subscription routes only.

Setup shows one advisory **Hermes version** item if the host no longer declares the terminal-scope or Kanban board-selection context names expected by this adapter. Missing declarations differ from an unbound context in a normal session. The hint names missing contexts; it never blocks tools or changes routing. The hook checklist remains checked for 24 hours after observation. Heartbeat persistence touches the store at most once per minute.

Normalized assertion text is matched first, including across moves and renames, so a moved test is neither removed nor changed. An assertion that is edited inside a file that was also renamed reads as removed. Missing lines pair with unmatched new assertions in the same file as changed; remaining missing lines are removed. An empty baseline is a valid inventory with zero removals. The initial baseline comes from committed HEAD objects in one bounded archive read and includes net additional uncommitted assertions per file at first sight. Edited assertions do not add duplicate baseline rows. If HEAD cannot be read, it falls back to the working tree with a null first comparison. A matching verdict retains its reviewed assertion evidence on the pane; subsequent comparisons use the last PASS baseline. A removal committed before the plugin first saw the card is not seen. Failed scans are cached for two seconds per repository/tree and retried afterward. Whole path words test/tests/spec select supported test files; unsupported languages and Rust tests embedded in ordinary src files may not be covered.

Workspace verification selects the first absolute Git folder from HERMES_SESSION_CWD, the terminal scope folder, the configured terminal cwd, then the optional configured repository; it never falls back to the process directory. The internal workspace board id is `workspace:`, which cannot collide with a real Kanban slug. Deleted workspace folders are omitted from the pane.

The self-test reports the real profile's armed state and uses that profile's configuration for the isolated close decision. An unarmed profile allows real closes. Pane arming is available only when a settings context is registered in the serving process; otherwise setup_actions.reason directs the user to the settings form. The pane saves verifier_provider and verifier_model through separate settings calls. Nonempty form values override the corresponding legacy verifier and verifier_route fields. Copyable receipts are null unless the gate still matches their tree and verdict state; simulated receipts announce themselves on the first line, and unknown values are omitted. Trial review checks pass through the terminal observation hooks.

### Disabled-test evidence

`skipped` lists at most five new skip or expected-failure markers, commented test declarations,
or test functions renamed out of collection; `skipped_count` counts all detected rows. These
are advisory, appear in the verifier brief and finished/verdict events, and never refuse a close.
A PASS with this evidence records `accepted_skipped`. The inventory uses the assertion scan's
Git-visible files, budgets and start/review baselines, and matches moved text across files.
It recognizes common Python, JS/TS, Go, Rust, Java and Ruby spellings, not arbitrary runner
configuration or metaprogramming. Renamed functions need the same nonempty body within 24 lines;
a comment must contain a recognizable test declaration. One test can produce multiple clues.
An existing skip is not a new skip. Unavailable scans return null, not a clean count.

### Test run counts

A check's `tests_ran` is the integer total stated by a recognized unittest, pytest, Jest/Vitest,
Cargo, RSpec or JUnit summary, or null. Ordinary Go `ok` output counts packages, not tests,
so it stays null; an explicit `Tests run: N` summary is supported. Unknown, truncated or
oversized output stays null. Totals follow the runner's summary (which may include skips),
not a claim that each test body executed. Cargo binary summaries are added; other formats
use the final summary. Custom reporters and per-suite JUnit output without an aggregate can
understate the total. This is a clue, never a correctness proof.

`gate.tests_ran` is `{before, now}` only when a completed configured check reports fewer tests
than the last known count on this card. Unknown counts preserve the last known count but
show no drop for that check. Receipts, finished/verdict events and Copy receipt preserve the
evidence. Reads never update counts. The next review brief includes an already observed drop;
a drop first observed during that review appears on its verdict and subsequent pane reads.

### Assertions across lines

An assertion with unclosed brackets includes up to twelve lines in its inventory entry, so
changes to later arguments appear as changed assertions. Whitespace is normalized for
comparison. Quoted brackets and Python line comments are ignored for balance. This is a
bounded heuristic, not a parser for each language; complex strings, macros, comment syntax,
and statements longer than twelve lines may be incomplete. It remains advisory.

### Publish approval

With the gate armed, `guard_push` (default `true`) asks for approval before an agent terminal runs
`git push` or `gh pr create` without a fresh independent PASS for that Git folder, current tree,
and configured check. Card and card-free workspace receipts both count. An older or expired PASS
shows STALE; no review shows UNVERIFIED. Approval lets the user proceed anyway.
This is a heuristic on command text, including environment prefixes, `git -C`, short `cd`,
`&&` or `;` chains, and a trailing redirect or pipe such as `2>&1 | tail`. A subshell or a here-document is not read. An alias or a script bypasses it. Fetch, pull, `push --dry-run` and quoted
mentions do not ask. Unknown folders, remote terminals, inspection errors, timeouts and oversized
repositories pass through. This adds an approval request, never a block directive or a sandbox.
