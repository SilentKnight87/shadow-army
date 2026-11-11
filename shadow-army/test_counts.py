"""Counts explicitly stated by common runners; unrecognized output stays unknown."""
import re


def parse(output):
    if not isinstance(output, str) or len(output) > 256 * 1024:
        return None
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|\r", "", output)
    for pattern in (r"(?m)^Ran (\d+) tests? in ", r"(?m)^\s*(\d+) examples?, \d+ failures?",
                    r"(?m)^(?:\[INFO\]\s*)?Tests run: (\d+), Failures: \d+, Errors: \d+, Skipped: \d+\s*$"):
        matches = re.findall(pattern, output)
        if matches:
            return sum(map(int, matches))
    matches = re.findall(r"(?m)^\s*Tests\s*[: ]\s*(.+)$", output)
    if len(matches) > 1:
        return None
    if matches:
        totals = re.findall(r"(\d+) total|\((\d+)\)\s*$", matches[-1])
        if totals:
            return int(next(v for v in totals[-1] if v))
    cargo = re.findall(r"(?m)^test result: .*? (\d+) passed; (\d+) failed; (\d+) ignored;", output)
    if cargo:
        return sum(sum(map(int, row)) for row in cargo)
    summaries = re.findall(r"(?m)^(?:=+\s*)?((?:\d+ (?:passed|failed|skipped|xfailed|xpassed|errors?|deselected|warnings?)(?:, )?)+)(?: in [0-9.]+s(?:\s+\([\d:]+\))?)?(?:\s*=+)?$", output)
    if len(summaries) > 1:
        return None
    if summaries:
        counts = re.findall(r"(\d+) (passed|failed|skipped|xfailed|xpassed|errors?|deselected|warnings?)", summaries[-1])
        return sum(int(n) for n, label in counts if label not in ('deselected', 'warning', 'warnings'))
    # Go's ordinary 'ok package duration' has no test count. Do not count packages as tests.
    explicit_go = re.findall(r"(?m)^Tests run: (\d+)$", output)
    return int(explicit_go[-1]) if explicit_go else None
