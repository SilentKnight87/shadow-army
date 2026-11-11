#!/usr/bin/env bash
set -eo pipefail

if [[ ${SHADOW_TEST_SANDBOX:-} != 1 ]]; then
    for tool in git node curl tar; do
        command -v "$tool" >/dev/null || { echo "Required tool not found: $tool" >&2; exit 1; }
    done
    runner=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")
    sandbox=$(mktemp -d)
    trap 'rm -rf "$sandbox"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    mkdir -p "$sandbox/os-home" "$sandbox/home" "$sandbox/tmp"
    env -i PATH="$PATH" HOME="$sandbox/os-home" HERMES_HOME="$sandbox/home" \
        HERMES_KANBAN_HOME="$sandbox/home" HERMES_RUNTIME_DIR="$sandbox/home/tools" \
        XDG_CACHE_HOME="$sandbox/cache" XDG_CONFIG_HOME="$sandbox/config" \
        XDG_DATA_HOME="$sandbox/data" UV_CACHE_DIR="$sandbox/uv-cache" \
        TMPDIR="$sandbox/tmp" SHADOW_TEST_SANDBOX=1 SHADOW_TEST_ROOT="$sandbox" \
        HERMES_SOURCE="${HERMES_SOURCE:-}" PYTHONDONTWRITEBYTECODE=1 PYTHONWARNINGS=ignore::ResourceWarning \
        HERMES_DISABLE_LAZY_INSTALLS=1 GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 \
        NO_COLOR=1 CI=1 bash "$runner"
    exit $?
fi

started=$SECONDS
root=$(cd "$(dirname "$0")/.." && pwd)
suites=$root
if [[ ! -d "$suites/shadow-army" ]]; then
    suites="$root/."
fi
source_dir="$SHADOW_TEST_ROOT/hermes-source"
if [[ -n "$HERMES_SOURCE" ]]; then
    revision=$(git -C "$HERMES_SOURCE" rev-parse HEAD)
    version=$(git -C "$HERMES_SOURCE" describe --tags --always)
    mkdir "$source_dir"
    git -C "$HERMES_SOURCE" archive HEAD | tar -x -C "$source_dir"
else
    git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 clone --depth 1 --branch v0.23.0 https://github.com/NousResearch/hermes-agent.git "$source_dir"
    revision=$(git -C "$source_dir" rev-parse HEAD)
    version=$(git -C "$source_dir" describe --tags --always)
fi
printf 'Hermes %s (%s)\n' "$version" "$revision"
cd "$source_dir"
source ./activate --test-extras web
python -B -m pm.build_env --source "$source_dir" --out "$SHADOW_TEST_ROOT/test-venv" --group dev --group test --extra web
python="$SHADOW_TEST_ROOT/test-venv/bin/python"
export PYTHONPATH="$source_dir"
cd "$suites"
status=0
"$python" -B -m unittest discover -s . -p 'test_*.py' -v || status=1
# The outer suite captures the host subprocess output; expose its counts as well.
mkdir -p "$HERMES_HOME/plugins"
cp -R "$suites/shadow-army" "$HERMES_HOME/plugins/shadow-army"
HERMES_DASHBOARD_SESSION_TOKEN=synthetic-test-session \
    "$python" -B -m unittest discover -s . -p host_probe.py -v || status=1
node --check "$suites/shadow-army/desktop/plugin.js" || status=1
node --check "$suites/shadow-army/dashboard/dist/index.js" || status=1
result=PASS
[[ $status == 0 ]] || result=FAIL
printf '%s: plugin and host counts printed above; 2 JavaScript syntax checks; Hermes %s; %ss elapsed.\n' "$result" "$version" "$((SECONDS - started))"
exit "$status"
