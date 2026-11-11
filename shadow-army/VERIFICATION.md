# Backend verification

The suites check gate decisions, Git fingerprints, Kanban completion, plugin loading,
tool dispatch, and API authentication against real Hermes in disposable homes. They do
not prove live model inference, provider login, a dispatcher-spawned worker, or a rendered
Desktop session. Live model inference and Desktop acceptance are recorded separately.

## Reproduce the suites

From the repository root, run:

```sh
./scripts/test-public.sh
```

Use macOS or Linux with Bash, Git, curl, tar, Node.js, and the native compiler tools
required by Hermes dependencies. Network access is needed for public source and dependency
downloads. No model, login, secret, existing Hermes installation, or Python environment is
needed. The runner fetches public Hermes at `v0.23.0`, follows its documented PM activation
and `pm.build_env` test-environment workflow, runs all plugin suites and the host probe,
and checks both JavaScript files. Counts, source version, revision, and duration are printed
by the run. The host probe also runs inside the outer suite; its separate run exposes its
otherwise captured count.

To reuse source, set `HERMES_SOURCE=/path/to/hermes-agent` before the same command. Only
committed files are copied; the source checkout and its environment are never changed.
The runner builds a fresh environment even with this override and reports its actual version.
It clears inherited environment variables and sets `HOME`, `HERMES_HOME`, and
`HERMES_KANBAN_HOME`, tool stores, and caches under a disposable directory. It removes that
directory on success or failure. No live app is restarted.

The public repository's push and pull-request workflow runs this same command with read-only
permissions and a pinned checkout action. Its runs are listed at
https://github.com/SilentKnight87/shadow-army/actions; local results are not evidence of a GitHub Actions run.

Model requests, response identities, and child lifecycle hooks are synthetic. Terminal
commands run locally; the remote-scope test feeds a synthetic result after the real pre-tool
hook refuses it and launches no remote executor. The trial uses simulated reviewers and
real unittest outcomes, including an independent FAIL on a green check after the builder
removes an assertion, and permits tampering only before close. The real plugin manager loads
the settings-form schema and exercises both form fields and legacy settings.

## Manual commands with a prepared environment

For diagnosis, use a Hermes checkout with its already prepared dependency environment.
The one-command runner above is the reproducible path for a new clone.

```sh
HERMES_SOURCE=/path/to/hermes-agent
SANDBOX=$(mktemp -d)
mkdir -p "$SANDBOX/os-home" "$SANDBOX/home"
env HOME="$SANDBOX/os-home" HERMES_HOME="$SANDBOX/home" \
  HERMES_KANBAN_HOME="$SANDBOX/home" PYTHONPATH="$HERMES_SOURCE" \
  PYTHONDONTWRITEBYTECODE=1 HERMES_DISABLE_LAZY_INSTALLS=1 \
  "$HERMES_SOURCE/.venv/bin/python" -B -m unittest discover \
  -s . -p 'test_*.py' -v
rm -rf "$SANDBOX"
node --check shadow-army/desktop/plugin.js
node --check shadow-army/dashboard/dist/index.js
```

The host fixture stops its terminal environments before unloading the plugin. Its subprocess
exits before the temporary home is removed. Each run reports its current test counts.

## Regression coverage

| Area | Proving tests |
| --- | --- |
| Git timeout and optional locks | `test_git_timeouts_and_documentation.GitTimeoutTests`: touched tracked files hit a forced fingerprint deadline, no index lock remains, and real `git add` succeeds. A temporary Git wrapper checks both lock controls and releases a synthetic index lock on SIGTERM. |
| Closed cards | Host `test_20`: real Kanban completion removes a clean linked worktree; done and archived cards retain PASS and its reviewed tree without fingerprinting. Disarming preserves closed PASS and permits an unarmed close. Host `test_24` preserves a closed UNGATED card after changing repo settings. |
| Effective terminal scope | Host `test_21`: a scoped local terminal runs the real check despite ambient SSH settings; an env-configured SSH profile cannot turn a synthetic exit 0 into evidence, and setup reports the non-local terminal. |
| Repository opt-in | `test_git_timeouts_and_documentation.ConfigurationDocumentationTests.test_4`: the main YAML has no repo setting, while the guide explains its all-scratch-card effect and the skill gives specific recovery advice. Host `test_034` covers workspace scope and explicit opt-in. |
| Actionable refusals | Host `test_22`: unborn repo requests a first commit; build and repair pass through unbound; a check creating files names up to five changed paths; same-family refusal describes the requesting or building session. |
| Snapshot cost | Host `test_23`: shared repo fingerprints are reused within two seconds and refreshed after expiry; `army_gate` and completion bypass the snapshot cache. Host `test_20` excludes closed cards. |
| Other-profile workers | `test_git_timeouts_and_documentation.ConfigurationDocumentationTests.test_7`: the stated limitation covers workers unobserved in this profile. Host `test_13` retains same-profile worker-family coverage. |
| Pane wording | Host `test_24`: BLOCKED and STALE reasons describe the observed condition; an ungated decision card says it is waiting on a decision. Closed cards have a closed-card hint. Lifecycle event kinds remain the snapshot contract's `arose` and `finished`; the pane renders them as started and finished. |
| Documentation and artifacts | `test_git_timeouts_and_documentation.ConfigurationDocumentationTests.test_9`: no committed validation receipts or links to them, no retired external CLI configuration, and check-output storage is disclosed. |

Earlier suites cover Git-visible staged, unstaged, flagged, untracked, symlink, nested-repo,
ignore, filter and size-budget behavior; ordinary shell environments; child provenance;
retries and refused batches; named boards and pinned worker contexts; corrupt or unreadable
boards; SQLite version selection, read retry and writer contention; trial controls; endpoint
authentication; line-by-line display filtering; and snapshot revision ordering.

## Validate a committed archive

Commit the candidate first. The runner archives that committed plugin and Hermes source,
copies the existing dependency environment into a temporary source tree, and validates only
that copy. Dependency preparation and doctor write inside disposable homes and caches.

```sh
RECEIPTS=$(mktemp -d)
python3 -B verify_archive.py \
  --hermes-source "$HERMES_SOURCE" --receipts "$RECEIPTS"
```

Successful admission prints `{"failures": [], "warnings": []}`. Read the generated transcripts
in `$RECEIPTS` and keep them outside the repository. No receipts are committed. Doctor can
separately name missing host utilities in its minimal runtime; those diagnostics are distinct
from the plugin validator's failures and warnings.

Board selection and effective terminal scope mirror Hermes context-variable names because
no documented plugin accessor covers those policies. Check that coupling on host upgrades.
The SQLite version rule likewise mirrors Hermes without importing a private helper. These
tests cover cooperative use; they do not attest to ignored files, malicious filters, hostile
shells, arbitrary earlier authors, or workers whose sessions ran in another profile.
