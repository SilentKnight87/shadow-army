"""Bounded, advisory assertion inventory; moves and renames preserve assertions."""
from collections import Counter
import os
from pathlib import Path
import re
import stat
import time

from .policy import git

ASSERT = re.compile(r'\b(?:assert\w*|Assert\w*|(?<!\.)expect|EXPECT_\w+|ASSERT_\w+|require\.(?:\w+)|should(?:Be|Equal|Have|Contain|Match)\w*|refute\w*)\s*[!(.]')


def inventory(repo, *, revision=None, max_files=2000, max_bytes=4 * 1024 * 1024, timeout=1.0, mode="assertions", root=None):
    try:
        deadline = time.monotonic() + timeout
        root = Path(root) if root is not None else Path(os.fsdecode(git(repo, 'rev-parse', '--show-toplevel', timeout=timeout)).strip())
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        if revision is not None:
            return committed_inventory(root, revision, max_files, max_bytes, deadline, mode)
        names = git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z',
                    cap=4 * 1024 * 1024, timeout=remaining).split(b'\0')
        names = sorted(set(names) - {b''})
        result, budget, files = [], max_bytes, 0
        for name in names:
            if time.monotonic() >= deadline:
                return None
            relative = os.fsdecode(name)
            path = Path(relative)
            if not test_path(path):
                continue
            if path.suffix.lower() not in ('.py', '.js', '.jsx', '.ts', '.tsx', '.go', '.rs', '.java', '.rb'):
                continue
            files += 1
            if files > max_files:
                return None
            if any((root / parent).is_symlink() for parent in path.parents if parent != Path('.')):
                continue
            try:
                fd = os.open(root / path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    if info.st_size > budget:
                        return None
                    content = stream.read(budget + 1)
            except OSError:
                continue
            budget -= len(content)
            if budget < 0:
                return None
            result.extend(scan(content, relative, mode, deadline))
            if len(result) >= 10000:
                return None
        return result if time.monotonic() < deadline else None
    except Exception:
        return None


def removed(before, after, since='start'):
    if before is None or after is None:
        return None
    # The same file first, so a line edited in one file never marks its untouched twin in another file.
    left = Counter((row['file'], ' '.join(row['text'].split())) for row in after)
    rest = []
    for row in before:
        identity = (row['file'], ' '.join(row['text'].split()))
        if left[identity]:
            left[identity] -= 1
        else:
            rest.append(row)
    # Then the same text in any other file: a moved or renamed test is neither removed nor changed.
    elsewhere = {}
    for (file, text), count in left.items():
        if count:
            elsewhere.setdefault(text, []).append(file)
    missing = []
    for row in rest:
        text = ' '.join(row['text'].split())
        files = [f for f in elsewhere.get(text, ()) if left[(f, text)]]
        if files:
            left[(files[0], text)] -= 1
        else:
            missing.append(dict(row, since=since))
    # What is left in "after" is genuinely new; only a new line in the same file can pair a missing one as changed.
    additions = Counter()
    for (file, text), count in left.items():
        additions[file] += count
    result = []
    for row in missing:
        changed = additions[row['file']] > 0
        if changed:
            additions[row['file']] -= 1
        result.append(dict(row, kind='changed' if changed else 'removed'))
    return result


def union(committed, current):
    if committed is None:
        return current
    result = list(committed)
    available = Counter((r['file'], ' '.join(r['text'].split())) for r in committed)
    unmatched = []
    for row in current:
        identity = (row['file'], ' '.join(row['text'].split()))
        if available[identity]:
            available[identity] -= 1
        else:
            unmatched.append(row)
    # A moved or renamed test keeps its text: match it to the committed line in another file first, so the
    # baseline does not hold the same assertion twice.
    elsewhere = {}
    for (file, text), count in available.items():
        if count:
            elsewhere.setdefault(text, []).append(file)
    edited = []
    for row in unmatched:
        text = ' '.join(row['text'].split())
        files = [f for f in elsewhere.get(text, ()) if available[(f, text)]]
        if files:
            available[(files[0], text)] -= 1
        else:
            edited.append(row)
    unmatched = edited
    missing = Counter()
    for (file, text), count in available.items():
        missing[file] += count
    for row in unmatched:
        if missing[row['file']]:
            missing[row['file']] -= 1
        else:
            result.append(row)
    return result


def test_path(path):
    words = re.split(r'[^a-z0-9]+', str(path).lower())
    return any(word in ('test', 'tests', 'spec') for word in words) or bool(re.search(r'(?:Test|Tests|Spec)\.', str(path)))


def committed_inventory(root, revision, max_files, max_bytes, deadline, mode="assertions"):
    names = git(root, 'ls-tree', '-r', '-z', revision, cap=4 * 1024 * 1024,
                timeout=max(0.001, deadline - time.monotonic())).split(b'\0')
    entries = []
    for entry in names:
        if not entry:
            continue
        meta, name = entry.split(b'\t', 1)
        file_mode, kind, oid = meta.split()
        path = Path(os.fsdecode(name))
        if kind == b'blob' and file_mode in (b'100644', b'100755') and test_path(path) and path.suffix.lower() in ('.py', '.js', '.jsx', '.ts', '.tsx', '.go', '.rs', '.java', '.rb'):
            entries.append((oid, str(path)))
    if len(entries) > max_files or time.monotonic() >= deadline:
        return None
    import io
    import tarfile
    output = git(root, 'archive', '--format=tar', revision, '--', *[name for _, name in entries],
                 cap=max_bytes + len(entries) * 2048 + 10240,
                 timeout=max(0.001, deadline - time.monotonic())) if entries else b''
    archive = tarfile.open(fileobj=io.BytesIO(output)) if entries else None
    result, offset, budget = [], 0, max_bytes
    for oid, name in entries:
        member = archive.getmember(name)
        budget -= member.size
        if budget < 0:
            return None
        content = archive.extractfile(member).read()
        result.extend(scan(content, name, mode, deadline))
        if len(result) >= 10000 or time.monotonic() >= deadline:
            return None
    return result


def summary(evidence):
    removed = evidence.get('weakened_count') or 0
    changed = evidence.get('changed_count') or 0
    parts = []
    if any(key in evidence and evidence[key] is None for key in ('weakened_count', 'skipped_count')):
        parts.append('Test scan unavailable: review the test diff by hand')
    if removed:
        parts.append(f'{removed} assertion' + (' removed' if removed == 1 else 's removed'))
    if changed:
        parts.append(f'{changed} assertion lines changed')
    if evidence.get('skipped_count'):
        parts.append(f"{evidence['skipped_count']} skipped or disabled tests")
    if evidence.get('tests_ran'):
        parts.append('Fewer tests ran')
    return '; '.join(parts)


DISABLED = re.compile(r"(?:@(?:unittest\.)?(?:skip\w*|expectedFailure)\b|pytest\.mark\.(?:skip\w*|xfail)\b|\b(?:it|test|describe)\.(?:skip|todo|fails)\b|(?<!\.)\b(?:xit|xtest|xdescribe|pending|skip)\s*\(|\bt\.Skip(?:f|Now)?\s*\(|#\[ignore\b|@(?:Disabled|Ignore)\b|\b(?:pending|skip)\s+['\"])")
FUNCTION = re.compile(r"(?:async\s+)?def\s+(\w+)|func\s+(?:\([^)]*\)\s*)?(\w+)|fn\s+(\w+)|(?:public\s+)?void\s+(\w+)")


def scan(content, name, mode, deadline):
    lines = content.decode('utf-8', 'replace').splitlines()
    result = []
    consumed = 0
    for index, text in enumerate(lines):
        if len(result) >= 10000 or (deadline is not None and time.monotonic() >= deadline):
            raise TimeoutError('Advisory inventory budget exceeded.')
        stripped = text.strip()
        comment = stripped.startswith(('#', '//', '/*', '*')) and not stripped.startswith('#[')
        if mode in ('disabled', 'all'):
            declaration = FUNCTION.match(stripped)
            if DISABLED.search(stripped) or (comment and re.search(r"\b(?:def test_|func Test|fn test_|(?:it|test|describe)\s*\(|void test)", stripped)):
                result.append(dict(file=name, line=index + 1, text=stripped, kind='disabled'))
            if declaration:
                function = next(v for v in declaration.groups() if v)
                body = []
                indent = len(text) - len(text.lstrip())
                for following in lines[index + 1:index + 25]:
                    if following.strip() and len(following) - len(following.lstrip()) <= indent:
                        break
                    body.append(following.strip())
                result.append(dict(file=name, line=index + 1, text=stripped,
                                   kind='test' if function.startswith(('test', 'Test')) else 'function',
                                   body=' '.join(body)))
        if index >= consumed and mode != 'disabled' and not comment and (ASSERT.search(stripped) or re.match(r'(?:assert\w*|refute\w*)\s+', stripped) or re.search(r'\b(?:t|testing)\.(?:Errorf?|Fatalf?)\(', stripped)):
            captured = statement(lines, index)
            consumed = index + len(captured.splitlines())
            row = dict(file=name, line=index + 1, text=captured)
            if mode == 'all':
                row['kind'] = 'assertion'
            result.append(row)
    return result


def skipped(before, after, since):
    if before is None or after is None:
        return None
    old_markers = [r for r in before if r.get('kind') == 'disabled']
    new_markers = [r for r in after if r.get('kind') == 'disabled']
    added = removed(new_markers, old_markers, since)
    missing = removed([r for r in before if r.get('kind') == 'test'],
                      [r for r in after if r.get('kind') == 'test'], since)
    additions = removed([r for r in after if r.get('kind') == 'function'],
                        [r for r in before if r.get('kind') == 'function'], since)
    renamed = [r for r in missing if r['kind'] == 'removed' and r.get('body') and any(
        candidate.get('body') == r['body'] for candidate in additions)]
    return [dict(file=r['file'], line=r['line'], text=r['text'], since=since) for r in added + renamed]


def statement(lines, start):
    depth, quote = 0, None
    captured = []
    for index in range(start, min(len(lines), start + 12)):
        line = lines[index]
        captured.append(line.strip())
        escaped = False
        position = 0
        while position < len(line):
            char = line[position]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            elif char == '#' or line[position:position + 2] == '//':
                break
            elif char == "'" and re.match(r"'[A-Za-z_]\w*(?![\w'])", line[position:]) and not re.match(r"'[A-Za-z_]\w*'", line[position:]):
                # Rust lifetime, rather than a quoted character or string.
                pass
            elif char in ("'", '"', '`'):
                quote = char
            elif char in '([{':
                depth += 1
            elif char in ')]}':
                depth -= 1
            position += 1
        if quote != '`':
            quote = None
        continuation = index + 1 < len(lines) and lines[index + 1].lstrip().startswith('.')
        if depth <= 0 and not continuation:
            break
    return '\n'.join(captured)
