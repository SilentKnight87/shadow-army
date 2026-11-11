"""Pure receipt policy and bounded git artifact identity."""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import selectors
import stat
import subprocess
import time
from typing import TypedDict

FAMILIES = {'anthropic', 'openai', 'google', 'xai', 'meta', 'mistral', 'deepseek',
            'qwen', 'moonshot', 'zhipu', 'nous', 'cohere'}
PREFIXES = {
    'anthropic': ('claude',), 'openai': ('gpt-', 'o1', 'o3', 'o4', 'codex-', 'chatgpt-'),
    'google': ('gemini', 'gemma'), 'xai': ('grok',), 'meta': ('llama',),
    'mistral': ('mistral', 'mixtral', 'magistral', 'codestral', 'devstral', 'ministral'),
    'deepseek': ('deepseek',), 'qwen': ('qwen', 'qwq'), 'moonshot': ('kimi', 'moonshot'),
    'zhipu': ('glm',), 'nous': ('hermes', 'deephermes'), 'cohere': ('command-',),
}

@dataclass(frozen=True)
class GateKey:
    board: str
    card_id: str

    def values(self):
        return self.board, self.card_id


@dataclass(frozen=True)
class GateState:
    state: str
    reason: str
    tree: str | None


class Receipt(TypedDict):
    board: str
    card_id: str
    tree: str
    check_hash: str
    verdict: str
    findings: list
    check: dict | None
    verifier: dict
    builder_families: list
    created_at: float
    synthetic: bool


class Binding(TypedDict):
    nonce: str
    board: str
    card_id: str
    parent_session: str
    child_session: str | None
    duty: str
    tree: str
    created_at: float
    used_at: float | None


def family(provider, model, overrides=None):
    provider, model = str(provider or '').lower(), str(model or '').lower()
    overrides = {k.lower(): v.lower() for k, v in overrides.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(overrides, dict) else {}
    override = overrides.get(provider + '/' + model)
    if override is not None:
        return override if override in FAMILIES else None
    # Provider names alone cannot establish a model's identity through aliases/proxies.
    leaf = model.rsplit('/', 1)[-1]
    for name, prefixes in PREFIXES.items():
        if leaf.startswith(prefixes):
            return name
    return None


def related(left, right):
    return left == right or ('nous' in (left, right) and bool({left, right} & {'meta', 'qwen'}))


def check_hash(command):
    return hashlib.sha256(str(command or '').encode()).hexdigest()


def state(receipts, tree, now, settings, synthetic=False):
    if not tree:
        return GateState('UNKNOWN', 'Give this code card a readable Git workspace, then retry verification.', None)
    rows = [r for r in receipts if r['synthetic'] == synthetic]
    matching = [r for r in rows if r['tree'] == tree and r['check_hash'] == check_hash(settings.get('check'))]
    if not matching:
        return GateState('STALE' if rows else 'UNVERIFIED',
                         'The code changed after the last review, so that verdict no longer counts.' if rows else 'No receipt for this tree.', tree)
    latest = max(matching, key=lambda r: r['created_at'])
    if latest['verdict'] == 'FAIL':
        return GateState('BLOCKED', 'The independent review failed this code.', tree)
    age = now - latest['created_at']
    if not 0 <= age < float(settings.get('max_age_s', 3600)):
        return GateState('STALE', 'PASS expired; request fresh verification.', tree)
    return GateState('PASS', 'Fresh independent PASS with evidence for this tree and check.', tree)


def accept_verdict(binding, observations, builders, current_tree, command, checks, verdict, findings, now):
    if not binding or binding['duty'] != 'verify' or not binding.get('child_session') or binding.get('finished_at'):
        raise ValueError('Verdict refused: caller is not an active bound verifier child (or verdict replay).')
    if now - binding['created_at'] > 3600:
        raise ValueError('Verdict refused: binding expired; delegate a new verifier.')
    if current_tree != binding['tree'] or check_hash(command) != binding['check_hash']:
        raise ValueError('Verdict refused: tree or check changed since dispatch; re-verify.')
    if not builders or None in builders:
        raise ValueError('Verdict refused: builder model family is unknown.')
    if not observations or not any(o.get('response_model') for o in observations):
        raise ValueError('Verdict refused: no observed verifier response model.')
    for obs in observations:
        if obs.get('model') == 'too many routes':
            raise ValueError('Verdict refused: too many routes observed in verifier session; family is unknown.')
        families = [obs.get('family')]
        if obs.get('response_model'):
            families.append(obs.get('response_family'))
        if any(f is None or any(related(f, b) for b in builders) for f in families):
            raise ValueError('Verdict refused: unknown or builder family observed for model ' + str(obs.get('response_model') or obs.get('model')))
    if verdict not in ('PASS', 'FAIL'):
        raise ValueError('Verdict must be PASS or FAIL.')
    if verdict == 'FAIL' and not findings:
        raise ValueError('FAIL requires at least one finding with file, line and text.')
    eligible = [c for c in checks if c['command'] == command and c['tree'] == current_tree
                and c.get('end_tree') == current_tree and type(c.get('exit_code')) is int and c['exit_code'] == 0]
    if verdict == 'PASS' and command and not eligible:
        raise ValueError('PASS refused: run the exact configured check through terminal in this child, in the repository, and wait for exit 0: ' + command)
    return eligible[-1] if eligible else (checks[-1] if checks else None)


def git(repo, *args, cap=2 * 1024 * 1024, timeout=0.6, consume=None, ok_codes=(0,)):
    env = dict(os.environ, LC_ALL='C', GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0')
    argv = ['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', '-C', str(repo), *args]
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) as proc:
        try:
            chunks = {proc.stdout: bytearray(), proc.stderr: bytearray()}
            with selectors.DefaultSelector() as selector:
                for stream in chunks:
                    selector.register(stream, selectors.EVENT_READ)
                deadline = time.monotonic() + timeout
                size = 0
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError('Git fingerprint timed out; let other Git or build work finish, then retry, or commit pending changes to reduce the diff.')
                    for key, _ in selector.select(remaining):
                        block = os.read(key.fileobj.fileno(), 65536)
                        if not block:
                            selector.unregister(key.fileobj)
                        size += len(block)
                        if cap is not None and size > cap:
                            raise ValueError('Git fingerprint exceeds size cap.')
                        if consume and key.fileobj is proc.stdout:
                            consume(block)
                        else:
                            chunks[key.fileobj].extend(block)
            proc.wait(timeout=max(0.01, deadline - time.monotonic()))
            if proc.returncode not in ok_codes:
                raise ValueError('Cannot read git work tree; restore the repository path and ensure Git can read it.')
            return bytes(chunks[proc.stdout])
        except subprocess.TimeoutExpired as exc:
            raise ValueError('Git fingerprint timed out; let other Git or build work finish, then retry.') from exc
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            proc.wait()


def fingerprint(repo):
    deadline = time.monotonic() + 1.8
    untracked_budget = 4 * 1024 * 1024
    def read(*args, consume=None, ok_codes=(0,)):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('Fingerprint timed out; commit pending changes or ignore large untracked folders, then retry.')
        return git(repo, *args, cap=None, timeout=remaining, consume=consume, ok_codes=ok_codes)
    root = Path(os.fsdecode(read('rev-parse', '--show-toplevel')).strip()).resolve()
    repo = root
    digest = hashlib.sha256(os.fsencode(root))
    def frame(value):
        digest.update(len(value).to_bytes(8, 'big') + value)
    head = read('rev-parse', '--verify', '--quiet', 'HEAD^{tree}', ok_codes=(0, 1))
    if not head:
        raise ValueError('This repository has no commit yet; make a first commit, then retry verification.')
    frame(head)
    diff = hashlib.sha256()
    read('diff', '--binary', '--no-ext-diff', '--no-textconv', 'HEAD', '--', consume=diff.update)
    frame(diff.digest())
    flagged = {entry[2:] for entry in read('ls-files', '-v', '-z').split(b'\0')
               if entry and (entry[:1].islower() or entry[:1] == b'S')}
    others = {entry for entry in read('ls-files', '--others', '--exclude-standard',
              '--exclude=__pycache__/', '--exclude=*.pyc', '--exclude=.pytest_cache/',
              '--exclude=.DS_Store', '-z').split(b'\0') if entry}
    for name in sorted(flagged | others):
        path = root / os.fsdecode(name)
        frame(name)
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            frame(b'missing')
            continue
        if stat.S_ISDIR(mode):
            frame(read('-C', str(path), 'rev-parse', '--verify', '--quiet', 'HEAD', ok_codes=(0, 1)) or b'unborn')
            continue
        frame(str(stat.S_IFMT(mode) | (mode & 0o111)).encode())
        if stat.S_ISLNK(mode):
            frame(os.fsencode(os.readlink(path)))
            continue
        content = hashlib.sha256()
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Cannot fingerprint ' + os.fsdecode(name) + '; remove or ignore this non-file entry.')
            while block := stream.read(65536):
                if name in others:
                    untracked_budget -= len(block)
                    if untracked_budget < 0:
                        raise ValueError('Untracked content exceeds the 4 MiB fingerprint budget; commit or ignore ' + os.fsdecode(name) + ' before retrying.')
                if time.monotonic() > deadline:
                    raise ValueError('Fingerprint timed out at ' + os.fsdecode(name) + '; commit pending changes or ignore large untracked folders, then retry.')
                content.update(block)
        frame(content.digest())
    return digest.hexdigest()


def worktree_changes(repo):
    root = Path(os.fsdecode(git(repo, 'rev-parse', '--show-toplevel')).strip())
    tracked = git(root, 'diff', '--name-only', '-z', 'HEAD', '--').split(b'\0')
    untracked = git(root, 'ls-files', '--others', '--exclude-standard',
                    '--exclude=__pycache__/', '--exclude=*.pyc', '--exclude=.pytest_cache/',
                    '--exclude=.DS_Store', '-z').split(b'\0')
    flagged = [entry[2:] for entry in git(root, 'ls-files', '-v', '-z').split(b'\0')
               if entry and (entry[:1].islower() or entry[:1] == b'S')]
    result = {}
    for name in set(tracked + untracked + flagged) - {b''}:
        path = root / os.fsdecode(name)
        try:
            info = path.lstat()
            result[os.fsdecode(name)] = [info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        except FileNotFoundError:
            result[os.fsdecode(name)] = None
    return result
