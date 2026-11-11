"""Gate operations independent of Hermes delegation and verdict transport."""
from contextvars import copy_context
from dataclasses import asdict
import json
import hashlib
import os
import re
import sqlite3
import shlex
from pathlib import Path
import time
import uuid

from . import boards, assertions, test_counts
from .policy import GateKey, accept_verdict, check_hash, family, related, fingerprint, git, state, worktree_changes

MARKER = re.compile(r'\[shadow-army:([a-f0-9]{32})\]')
ROUTE_CAP = 16
ASSERTION_CACHE = {}
SETTING_DEFAULTS = {'repo': None, 'check': 'python3 -m unittest discover -s tests',
                    'verifier': None, 'family_overrides': {}, 'max_age_s': 3600}


def publish_targets(command, cwd):
    """Heuristically locate ordinary publish commands; never evaluate shell text."""
    if not isinstance(command, str) or len(command) > 16384:
        return []
    # Keep operators distinct from quoted/escaped words before unquoting each word.
    pattern = r"""(?:[^\s;&|<>()'"\\]|\\.|"(?:[^"\\]|\\.)*"|'[^']*')+|&&|\n+|[;&|<>()]+"""
    segments = [[]]
    end = 0
    for match in re.finditer(pattern, command):
        if command[end:match.start()].strip():
            return []
        end = match.end()
        raw = match.group()
        if raw.startswith('#'):
            return []
        if raw in ('&&', ';', '|', '||', '&') or raw[0] == '\n':
            # A new line starts a new command, like a semicolon.
            segments.append([])
        elif raw[0] in ';&|<>()':
            # A redirection (2>&1, > file) does not change what the command is; its words are harmless extras.
            if set(raw) <= set('<>&') and ('<' in raw or '>' in raw) and '<<' not in raw:
                continue
            return []
        else:
            try:
                segments[-1].extend(shlex.split(raw))
            except ValueError:
                return []
    if command[end:].strip() or sum(map(len, segments)) > 512:
        return []
    if len(segments) > 32:
        return []
    targets = []
    for words in segments:
        words = list(words)
        if words[:1] == ['env']:
            words.pop(0)
        while words and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*=.*', words[0], re.S):
            words.pop(0)
        if not words:
            continue
        if words[0] == 'cd':
            directory = words[1:] if words[1:2] != ['--'] else words[2:]
            if len(directory) != 1 or any(c in directory[0] for c in '$`'):
                cwd = None
            else:
                path = Path(directory[0]).expanduser()
                cwd = str(path if path.is_absolute() else Path(cwd) / path) if cwd or path.is_absolute() else None
            continue
        tool = words.pop(0)
        folder = cwd
        if tool == 'git':
            while words and words[0].startswith('-'):
                option = words.pop(0)
                if option in ('-C', '-c') and words:
                    value = words.pop(0)
                    if option == '-C':
                        if any(c in value for c in '$`'):
                            folder = None
                        else:
                            path = Path(value).expanduser()
                            folder = str(path if path.is_absolute() else Path(folder) / path) if folder or path.is_absolute() else None
                elif option not in ('--no-pager', '--no-optional-locks'):
                    words = []
            if words[:1] == ['push'] and not any(w in ('--dry-run', '-n') for w in words[1:]):
                targets.append(folder)
        elif tool == 'gh' and words[:2] == ['pr', 'create']:
            targets.append(folder)
    return list(dict.fromkeys(targets))


class Gates:
    def __init__(self, store, settings, host_config=None, dispatch=None):
        self.store = store
        self.settings = settings
        self.host_config = host_config or (lambda: {})
        self.dispatch = dispatch
        self.changed_sessions = set()

    def config(self):
        cfg = {k: self.settings(k, v) for k, v in SETTING_DEFAULTS.items()}
        errors = []
        def shape(name, value, expected, default):
            if value is not None and not isinstance(value, expected):
                errors.append(name)
                return default
            return value if value is not None else default
        verifier = dict(shape('verifier', cfg['verifier'], dict, {}))
        saved_route = shape('verifier_route', self.settings('verifier_route', None), dict, {})
        verifier.update(saved_route)
        for field in ('provider', 'model'):
            value = shape('verifier_' + field, self.settings('verifier_' + field, None), str, '')
            if value:
                verifier[field] = value
            if not isinstance(verifier.get(field, ''), str):
                errors.append('verifier.' + field)
                verifier.pop(field, None)
        fallbacks = self.settings('verifier_fallbacks', verifier.get('fallbacks'))
        if fallbacks is not None:
            routes = shape('verifier_fallbacks', fallbacks, list, [])
            verifier['fallbacks'] = []
            for route in routes:
                if not isinstance(route, dict) or any(not isinstance(route.get(field), str) for field in ('provider', 'model')):
                    errors.append('verifier_fallbacks')
                    continue
                verifier['fallbacks'].append(route)
        cfg['verifier'] = verifier if verifier.get('provider') or verifier.get('model') else None
        overrides = shape('family_overrides', cfg['family_overrides'], dict, {})
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in overrides.items()):
            errors.append('family_overrides')
        cfg['family_overrides'] = {k.lower(): v.lower() for k, v in overrides.items() if isinstance(k, str) and isinstance(v, str)}
        cfg['repo'] = shape('repo', cfg['repo'], str, None)
        cfg['check'] = shape('check', cfg['check'], str, SETTING_DEFAULTS['check'])
        minutes = self.settings('pass_lifetime_minutes', None)
        if minutes is not None:
            if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 0 < minutes < float('inf'):
                errors.append('pass_lifetime_minutes')
            else:
                cfg['max_age_s'] = minutes * 60
        if isinstance(cfg['max_age_s'], bool) or not isinstance(cfg['max_age_s'], (int, float)) or not 0 < cfg['max_age_s'] < float('inf'):
            errors.append('max_age_s')
            cfg['max_age_s'] = SETTING_DEFAULTS['max_age_s']
        cfg['settings_errors'] = list(dict.fromkeys(errors))
        return cfg

    def armed(self):
        verifier = self.config()['verifier']
        return bool(isinstance(verifier, dict) and verifier.get('provider') and verifier.get('model')
                    and self.host_config().get('delegation', {}).get('backend') == 'shadow-army')

    def local_terminal(self):
        scope = next((value for var, value in copy_context().items() if var.name == 'hermes_terminal_scope'), None)
        if scope is not None:
            return not getattr(scope, 'reason', None) and scope.get('TERMINAL_ENV', 'local') == 'local'
        configured = self.host_config().get('terminal', {}).get('backend', 'local')
        return (os.environ.get('TERMINAL_ENV') or configured) == 'local'

    def key_for(self, card, board=None):
        if board == 'workspace:' and isinstance(card, str) and card.strip():
            return GateKey('workspace:', card.strip())
        return boards.key_for(card, board, self.dispatch)

    def verdict_key(self, session, card=None, board=None):
        with self.store.read() as db:
            bindings = [b for b in self.store.rows(db, 'binding') if b.get('child_session') == session]
        if len(bindings) != 1:
            raise ValueError('Verdict refused: caller is not a bound verifier child; delegate a new verifier for the card.')
        binding = bindings[0]
        key = GateKey(binding['board'], binding['card_id'])
        if card is not None and str(card).strip() != key.card_id:
            raise ValueError('Verdict refused: card does not match the calling child binding; use card ' + key.card_id + '.')
        if board is not None and str(board).strip().lower() != key.board:
            raise ValueError('Verdict refused: board does not match the calling child binding; use board ' + key.board + '.')
        return key

    def repository(self, key):
        if key.board == 'workspace:':
            with self.store.read() as db:
                row = self.store.get(db, 'workspace', key.card_id)
            if not row:
                raise ValueError('Unknown workspace review.')
            return row['repo']
        return boards.repository(key, self.config())

    def tree(self, key):
        repo = self.repository(key)
        if not repo:
            raise ValueError('No Git workspace; give the code card an existing Git workspace before verifying.')
        return fingerprint(repo)

    def event(self, db, key, kind, **values):
        if kind == 'closed':
            values.setdefault('gate_at_close', (values.get('gate') or {}).get('state', 'UNVERIFIED'))
        self.store.event(db, key, dict(at=time.time(), kind=kind, duty=None, family=None,
                                     model=None, verdict=None, finding=None, tree=None, check=None) | values)

    def receipts(self, db, key):
        return [r for r in self.store.rows(db, 'receipt') if (r['board'], r['card_id']) == key.values()]

    def inspect(self, key, *, fingerprint_cache=None, advisory=True):
        with self.store.read() as db:
            receipts = self.receipts(db, key)
            history = self.store.history(db, key)
        row = {} if key.board == 'workspace:' else (boards.card_row(key) or {})
        if row.get('status') in ('done', 'archived'):
            closed = next((e for e in reversed(history) if e['kind'] == 'closed'), None)
            permitted = next((e for e in reversed(history) if e['kind'] == 'close_permitted'
                              and (not closed or e['at'] <= closed['at'])), None)
            recorded = (closed or {}).get('gate') or (permitted or {}).get('gate')
            if not recorded and permitted and any(r['tree'] == permitted.get('tree') and r['verdict'] == 'PASS' for r in receipts):
                recorded = dict(state='PASS', tree=permitted['tree'])
            if not recorded and receipts and receipts[-1]['verdict'] == 'PASS':
                recorded = dict(state='PASS', tree=receipts[-1]['tree'])
            if not recorded:
                ungated = not self.config()['repo'] and row.get('workspace_kind') in ('scratch', None)
                recorded = dict(state='UNGATED' if ungated else ('UNVERIFIED' if self.armed() else 'UNARMED'), tree=None)
            result = dict(recorded, receipts=[], next_step='', repo=None)
            result['reason'] = ('Closed after an independent PASS.' if result['state'] == 'PASS' else
                                ('The card closed without a completion gate.' if result['state'] == 'UNGATED' else
                                 'Closed without an independent review. The gate did not see this close.' if result['state'] == 'UNVERIFIED' else
                                 'Closed while the gate was not armed.'))
            return result
        error, repo = None, None
        armed = self.armed()
        try:
            if not armed:
                try:
                    repo = self.repository(key)
                except (ValueError, OSError):
                    return dict(state='UNARMED', reason='The completion gate is not armed.', tree=None, receipts=[], next_step='', repo=None)
            else:
                repo = self.repository(key)
            if not repo:
                return dict(state='UNGATED', reason=boards.ungated_reason(key), tree=None, receipts=[], next_step='', repo=None)
            if not armed:
                return dict(state='UNARMED', reason='The completion gate is not armed.', tree=None, receipts=[], next_step='Configure verifier.provider/model and delegation.backend shadow-army.', repo=repo)
            cache_key = os.fsdecode(git(repo, 'rev-parse', '--show-toplevel')).strip() if fingerprint_cache is not None else None
            cached = fingerprint_cache.get(cache_key) if fingerprint_cache is not None else None
            if cached and time.monotonic() - cached[0] < 2:
                tree = cached[1]
            else:
                tree = self.tree(key)
                if fingerprint_cache is not None:
                    fingerprint_cache[cache_key] = (time.monotonic(), tree)
        except Exception as exc:
            tree, error = None, str(exc)
            if isinstance(exc, OSError):
                error += '; restore read access to the repository files, then retry.'
        result = asdict(state(receipts, tree, time.time(), self.config(), self.store.namespace in ('trial', 'selftest')))
        if error:
            result['reason'] = 'Cannot fingerprint: ' + error
        result['repo'] = repo
        next_task = dict(duty='verify', card=key.card_id, goal='Review the current code and run the configured check.')
        if not key.board.startswith('path:'):
            next_task['board'] = key.board
        if advisory:
            latest = receipts[-1] if receipts else None
            if latest and latest['tree'] == tree and result['state'] in ('PASS', 'BLOCKED'):
                result.update({k: latest[k] for k in ('weakened', 'weakened_count', 'changed_count', 'skipped', 'skipped_count', 'tests_ran') if k in latest})
            else:
                result.update(self.evidence(key, tree=tree))
        result.update(receipts=[{k: r[k] for k in ('verdict', 'tree', 'created_at', 'findings', 'synthetic')} for r in receipts],
                      next_step='' if result['state'] == 'PASS' else 'delegate_task(tasks=' + json.dumps([next_task]) + ')')
        return result

    def evidence(self, key, **kwargs):
        if not kwargs.get('tree'):
            try:
                kwargs['tree'] = self.tree(key)
            except Exception:
                pass
        try:
            kwargs['root'] = os.fsdecode(git(self.repository(key), 'rev-parse', '--show-toplevel')).strip()
        except Exception:
            pass
        return self.assertion_evidence(key, **kwargs) | self.skipped_evidence(key, **kwargs) | self.test_count_evidence(key)

    def test_count_evidence(self, key):
        try:
            with self.store.read() as db:
                row = self.store.get(db, 'test_count', json.dumps(key.values())) or {}
            return dict(tests_ran=row.get('drop'))
        except Exception:
            return dict(tests_ran=None)

    def inventory(self, repo, root, tree, *, revision=None, disabled=False):
        cache_key = ('HEAD', root, tree) if revision else (root, tree)
        cached = ASSERTION_CACHE.get(cache_key) if tree else None
        if cached and time.monotonic() - cached[0] < 2:
            rows = cached[1]
        else:
            rows = assertions.inventory(repo, revision=revision, mode='all', root=root)
            if len(ASSERTION_CACHE) >= 64:
                ASSERTION_CACHE.clear()
            ASSERTION_CACHE[cache_key] = (time.monotonic(), rows)
        if rows is None:
            return None
        if disabled:
            return [r for r in rows if r.get('kind') != 'assertion']
        return [{k: v for k, v in r.items() if k != 'kind'} for r in rows if r.get('kind') == 'assertion']

    def assertion_evidence(self, key, *, record=False, verdict=False, tree=None, root=None):
        # Advisory evidence never participates in the close decision.
        try:
            repo = self.repository(key)
            root = root or os.fsdecode(git(repo, 'rev-parse', '--show-toplevel')).strip()
            current = self.inventory(repo, root, tree, disabled=False)
            if current is None:
                return dict(weakened=None, weakened_count=None, changed_count=None)
            identity = json.dumps(key.values())
            with self.store.read() as db:
                baseline = saved = self.store.get(db, 'assertions', identity)
            if not baseline or (baseline.get('start') is None and baseline.get('review') is None):
                committed = self.inventory(repo, root, tree, revision='HEAD', disabled=False)
                baseline = {'start': committed} if committed is not None else None
            if baseline and baseline.get('review', False) is None:
                baseline.pop('review')
            previous = (baseline or {}).get('review', (baseline or {}).get('start'))
            since = 'review' if baseline and 'review' in baseline else 'start'
            missing = assertions.removed(previous, current, since)
            if missing is not None:
                missing.sort(key=lambda row: row['kind'] != 'removed')
            count = sum(row['kind'] == 'removed' for row in missing) if missing is not None else None
            changed = max(0, len(missing) - count) if missing is not None else None
            if current is not None and ((record and saved is None) or verdict):
                with self.store.transaction() as db:
                    saved = self.store.get(db, 'assertions', identity)
                    baseline = saved if saved and saved.get('start') is not None else {'start': assertions.union((baseline or {}).get('start'), current)}
                    if baseline.get('review', False) is None:
                        baseline.pop('review')
                    if verdict:
                        baseline['review'] = current
                    self.store.put(db, 'assertions', identity, baseline)
            elif record and current is not None and baseline is None:
                with self.store.transaction() as db:
                    self.store.put(db, 'assertions', identity, {'start': current})
            return dict(weakened=[dict(r, file=boards.clean(r['file'], 500), text=boards.clean(r['text'], 1000)) for r in missing[:5]] if missing is not None else None,
                        weakened_count=count, changed_count=changed)
        except Exception:
            return dict(weakened=None, weakened_count=None, changed_count=None)

    def skipped_evidence(self, key, *, record=False, verdict=False, tree=None, root=None):
        # Advisory evidence never participates in the close decision.
        try:
            repo = self.repository(key)
            root = root or os.fsdecode(git(repo, 'rev-parse', '--show-toplevel')).strip()
            current = self.inventory(repo, root, tree, disabled=True)
            if current is None:
                return dict(skipped=None, skipped_count=None)
            identity = json.dumps(key.values())
            with self.store.read() as db:
                baseline = saved = self.store.get(db, 'disabled_tests', identity)
            if not baseline or (baseline.get('start') is None and baseline.get('review') is None):
                committed = self.inventory(repo, root, tree, revision='HEAD', disabled=True)
                baseline = {'start': committed} if committed is not None else None
            if baseline and baseline.get('review', False) is None:
                baseline.pop('review')
            previous = (baseline or {}).get('review', (baseline or {}).get('start'))
            since = 'review' if baseline and 'review' in baseline else 'start'
            missing = assertions.skipped(previous, current, since)
            if current is not None and ((record and saved is None) or verdict):
                with self.store.transaction() as db:
                    saved = self.store.get(db, 'disabled_tests', identity)
                    baseline = saved if saved and saved.get('start') is not None else {'start': assertions.union(baseline['start'], [r for r in current if r.get('kind') != 'disabled']) if baseline else current}
                    if baseline.get('review', False) is None:
                        baseline.pop('review')
                    if verdict:
                        baseline['review'] = current
                    self.store.put(db, 'disabled_tests', identity, baseline)
            elif record and current is not None and baseline is None:
                with self.store.transaction() as db:
                    self.store.put(db, 'disabled_tests', identity, {'start': current})
            return dict(skipped=[dict(r, file=boards.clean(r['file'], 500), text=boards.clean(r['text'], 1000)) for r in missing[:5]] if missing is not None else None,
                        skipped_count=len(missing) if missing is not None else None)
        except Exception:
            return dict(skipped=None, skipped_count=None)

    def observations(self, db, session):
        row = self.store.get(db, 'session', session) or {}
        routes = row if isinstance(row, list) else row.get('routes', [])
        result = []
        for route in routes:
            obs = dict(route, family=family(route.get('provider'), route.get('model'), self.config()['family_overrides']))
            if obs.get('response_model'):
                obs['response_family'] = family(obs.get('provider'), obs['response_model'], self.config()['family_overrides'])
            else:
                obs.pop('response_model', None)
                obs.pop('response_family', None)
            result.append(obs)
        if isinstance(row, dict) and row.get('too_many_routes'):
            result.append(dict(model='too many routes', family=None))
        return result

    def require_known(self, observations):
        for obs in observations:
            for model in (obs.get('model'), obs.get('response_model')):
                if model == 'too many routes':
                    raise ValueError('Too many routes observed; start a fresh session and request verification again.')
                if model and family(obs.get('provider'), model, self.config()['family_overrides']) is None:
                    key = str(obs.get('provider') or '').lower() + '/' + model.lower()
                    raise ValueError('Unknown model family for ' + model + '; add family_overrides: {"' + key + '": "<family>"} to plugins.entries.shadow-army.settings, replacing <family> with a known family from policy.py, then retry.')

    def builder_observations(self, db, key, parent):
        workers = set() if key.board == 'workspace:' else boards.worker_sessions(key)
        sessions = workers - {parent}
        for b in self.store.rows(db, 'binding'):
            if (b['board'], b['card_id']) == key.values() and b['duty'] in ('build', 'repair'):
                if b.get('child_session'):
                    sessions.add(b['child_session'])
        sessions.discard(parent)
        observations = [o for sid in sorted(sessions) for o in self.observations(db, sid)]
        if not observations or self.store.get(db, 'session_change', parent):
            observations.extend(self.observations(db, parent))
        return observations

    def repository_tool_seen(self, session, tool_name):
        if not session or tool_name not in ('terminal', 'write_file', 'patch') or session in self.changed_sessions:
            return
        with self.store.read() as db:
            changed = self.store.get(db, 'session_change', session)
        if not changed:
            with self.store.transaction() as db:
                self.store.put(db, 'session_change', session, True)
        self.changed_sessions.add(session)

    def builders(self, db, key, parent):
        observations = self.builder_observations(db, key, parent)
        families = {o.get('family') for o in observations}
        families.update(o.get('response_family') for o in observations if o.get('response_model'))
        return families

    def observe(self, session, provider, model, response_model=None, *, request_id=None, usage=None):
        if not session:
            return
        response_model = str(response_model or '').strip() or None
        cfg = self.config()
        obs = dict(session_id=session, provider=provider, model=model,
                   family=family(provider, model, cfg['family_overrides']), observed_at=time.time())
        if response_model:
            obs.update(response_model=response_model,
                       response_family=family(provider, response_model, cfg['family_overrides']))
        def known(row):
            if not row or isinstance(row, list):
                return False
            routes = row.get('routes', [])
            return time.time() - row.get('observed_at', 0) < 3600 and (row.get('too_many_routes') or any(
                (r.get('provider'), r.get('model'), r.get('response_model')) == (provider, model, response_model or None) for r in routes))
        with self.store.read() as db:
            record_usage = self.bound_child(db, session) and request_id and self.usage_total(usage) is not None
            if known(self.store.get(db, 'session', session)) and not record_usage:
                return
        with self.store.transaction() as db:
            if record_usage:
                self.record_usage(db, session, request_id, self.usage_total(usage))
            row = self.store.get(db, 'session', session)
            if known(row):
                return
            previous = row if isinstance(row, list) else (row or {}).get('routes', [])
            routes, seen = [], set()
            overflow = False
            for observed in [*previous, obs]:
                identity = (observed.get('provider'), observed.get('model'), observed.get('response_model'))
                if identity in seen:
                    continue
                seen.add(identity)
                if len(routes) < ROUTE_CAP:
                    routes.append(observed)
                else:
                    overflow = True
            self.store.put(db, 'session', session, dict(routes=routes, too_many_routes=overflow, observed_at=time.time()))

    def bound_child(self, db, session):
        return any(b.get('child_session') == session and b['duty'] in ('verify', 'build', 'repair')
                   for b in self.store.rows(db, 'binding'))

    @staticmethod
    def usage_total(usage):
        if not isinstance(usage, dict):
            return None
        total = usage.get('total_tokens')
        if type(total) is not int:
            parts = (usage.get('input_tokens', usage.get('prompt_tokens')),
                     usage.get('output_tokens', usage.get('completion_tokens')))
            if not all(type(v) is int and v >= 0 for v in parts):
                return None
            total = sum(parts)
        return total if total >= 0 else None

    def record_usage(self, db, session, request_id, total):
        row = self.store.get(db, 'usage', session) or dict(tokens=0, requests=[])
        if request_id in row['requests'] or len(row['requests']) >= 1000:
            return
        row['requests'].append(request_id)
        row['tokens'] += total
        row['observed_at'] = time.time()
        self.store.put(db, 'usage', session, row)

    def usage(self, session, request_id, usage):
        total = self.usage_total(usage)
        if not session or not request_id or total is None:
            return
        with self.store.read() as db:
            if not self.bound_child(db, session):
                return
        with self.store.transaction() as db:
            self.record_usage(db, session, request_id, total)

    def prepare(self, tasks, parent):
        if isinstance(tasks, str):
            try:
                tasks = json.loads(tasks)
            except ValueError as exc:
                raise ValueError('tasks must be a JSON array of task objects.') from exc
        if not isinstance(tasks, list) or any(not isinstance(t, dict) for t in tasks):
            raise ValueError('tasks must be an array of task objects.')
        prepared = []
        cfg = self.config()
        for raw in tasks:
            task = dict(raw)
            if not task.get('card') and task.get('duty') != 'verify':
                prepared.append(task)
                continue
            try:
                if not parent:
                    raise ValueError('Delegation refused: missing parent session identity.')
                duty = task.get('duty', 'build')
                if duty not in ('build', 'repair', 'verify'):
                    raise ValueError('Duty must be build, repair or verify.')
                if not task.get('card'):
                    scope = next((v for var, v in copy_context().items() if var.name == 'hermes_terminal_scope'), None)
                    cwd = scope.get('TERMINAL_CWD') if scope is not None else os.environ.get('TERMINAL_CWD')
                    session_cwd = next((v for var, v in copy_context().items() if var.name == 'HERMES_SESSION_CWD'), None)
                    candidates = (session_cwd, cwd, self.host_config().get('terminal', {}).get('cwd'), cfg['repo'])
                    repo = next((str(Path(v).expanduser()) for v in candidates
                                 if isinstance(v, str) and v.strip().lower() not in ('', '.', './', 'auto', 'cwd')
                                 and Path(v).expanduser().is_absolute() and boards.in_work_tree(str(Path(v).expanduser()))), None)
                    try:
                        if not repo or not boards.in_work_tree(repo):
                            raise ValueError('No workspace selected.')
                        repo = os.fsdecode(git(repo, 'rev-parse', '--show-toplevel')).strip()
                    except (ValueError, OSError) as exc:
                        raise ValueError('Start in a Git folder or set the optional repository.') from exc
                    wid = hashlib.sha256(os.fsencode(repo)).hexdigest()[:12]
                    with self.store.transaction() as db:
                        self.store.put(db, 'workspace', wid, dict(repo=repo, title=Path(repo).name))
                    task.update(card=wid, board='workspace:')
                key = GateKey('workspace:', task['card']) if task.get('board') == 'workspace:' else self.key_for(task.get('card'), task.get('board'))
                try:
                    repo = self.repository(key)
                    tree = self.tree(key) if repo else None
                except (ValueError, OSError):
                    if duty == 'verify':
                        raise
                    repo, tree = None, None
                if duty in ('build', 'repair') and not tree:
                    task.pop('card', None)
                    task.pop('board', None)
                    prepared.append(task)
                    continue
                if not tree:
                    raise ValueError('No Git workspace; give the code card an existing Git workspace before verifying.')
                evidence = self.evidence(key, record=True, tree=tree)
                workers = set() if key.board == 'workspace:' else boards.worker_sessions(key)
                with self.store.transaction() as db:
                    if duty == 'verify':
                        for b in self.store.rows(db, 'binding'):
                            if (b['board'], b['card_id']) != key.values() or b['duty'] != 'verify' or b.get('finished_at'):
                                continue
                            if b.get('used_at') and time.time() - b['created_at'] < 3600:
                                raise ValueError('Verification already in flight for this card; wait for that child to finish or retry after its one-hour expiry.')
                            b.update(finished_at=time.time(), refused=True, finding='Superseded before child start, or expired before completion.')
                            self.store.put(db, 'binding', b['nonce'], b)
                    nonce = uuid.uuid4().hex
                    binding = dict(nonce=nonce, board=key.board, card_id=key.card_id, parent_session=parent,
                                   child_session=None, duty=duty, tree=tree, check_hash=check_hash(cfg['check']),
                                   worker_sessions=sorted(workers), created_at=time.time(), used_at=None, finished_at=None)
                    self.store.put(db, 'binding', nonce, binding)
                task['card'] = key.card_id
                if not key.board.startswith('path:'):
                    task['board'] = key.board
                else:
                    task.pop('board', None)
                task['goal'] = MARKER.sub('', str(task.get('goal', ''))) + '\n[shadow-army:' + nonce + ']'
                if duty == 'verify':
                    task['goal'] += f'\nVerify card {key.card_id} on board {key.board} in repository {repo}. Run exactly: {cfg["check"] or "(no check configured)"}. Use terminal directly with workdir set to {repo}, then call army_verdict directly for this card. Green tests alone do not establish correctness: assess any removed or switched-off test against the requirement.'
                    description = assertions.summary(evidence)
                    if description:
                        task['goal'] += '\n' + description + ' (advisory; review intent): ' + json.dumps(evidence)
                    if task.get('toolsets'):
                        task['toolsets'] = list(dict.fromkeys([*task['toolsets'], 'shadow-army', 'terminal']))
            except Exception as exc:
                nonce = uuid.uuid4().hex
                with self.store.transaction() as db:
                    self.store.put(db, 'refusal', nonce, dict(error=str(exc), observed_at=time.time()))
                task['goal'] = MARKER.sub('', str(task.get('goal', ''))) + '\n[shadow-army:' + nonce + ']'
            prepared.append(task)
        return prepared

    def route(self, task, default):
        if not task.get('card'):
            tokens = MARKER.findall(task.get('goal') or '')
            if len(tokens) == 1:
                with self.store.read() as db:
                    refusal = self.store.get(db, 'refusal', tokens[0])
                if refusal:
                    raise ValueError(refusal['error'])
            if task.get('duty') == 'verify':
                raise ValueError('Verification refused: duty verify requires a card.')
            return dict(default)
        tokens = MARKER.findall(task.get('goal') or '')
        if len(tokens) != 1:
            raise ValueError('Delegation refused: no bound parent nonce; use delegate_task through the agent.')
        with self.store.transaction() as db:
            binding = self.store.get(db, 'binding', tokens[0])
            refusal = self.store.get(db, 'refusal', tokens[0])
            if refusal:
                raise ValueError(refusal['error'])
            if not binding or binding.get('used_at') or binding.get('finished_at'):
                raise ValueError('Delegation refused: expired, consumed or unbound nonce.')
            key = GateKey(binding['board'], binding['card_id'])
            if binding['duty'] != task.get('duty', 'build') or binding['card_id'] != task.get('card'):
                raise ValueError('Delegation refused: task does not match binding.')
            if binding['duty'] != 'verify':
                binding['route'] = {k: default[k] for k in ('provider', 'model') if k in default}
                self.store.put(db, 'binding', binding['nonce'], binding)
                return dict(default)
            cfg = self.config()
            builder_observations = self.builder_observations(db, key, binding['parent_session'])
            builders = self.builders(db, key, binding['parent_session'])
            verifier = cfg['verifier']
            routes = [verifier] + (verifier.get('fallbacks', []) if isinstance(verifier, dict) else [])
            problems = []
            for route in routes:
                if not isinstance(route, dict) or not route.get('provider') or not route.get('model'):
                    problems.append('configure verifier.provider and verifier.model')
                    continue
                f = family(route['provider'], route['model'], cfg['family_overrides'])
                try:
                    self.require_known(builder_observations + [route])
                    if not builders or None in builders:
                        raise ValueError('No builder model observed; send a message in the requesting session and request verification again.')
                except ValueError as exc:
                    problems.append(str(exc))
                    continue
                if any(related(f, b) for b in builders):
                    builder_family = next(b for b in builders if related(f, b))
                    name = f.title() if f != "openai" else "OpenAI"
                    article = "An" if name[0].lower() in "aeiou" else "A"
                    finding = f'Verifier requested: {name}. {article} {name} session requested this review or built this card.'
                    if builder_family != f:
                        finding = f'Verifier requested: {name}. This route is related to the {builder_family} builder family.'
                    binding.update(refused=True, finding=finding, route=route)
                    self.event(db, key, 'refused_same_family', duty='verify', family=f, model=route['model'], tree=binding['tree'], builder_family=builder_family, finding=finding)
                    parent_counted = any(o.get('session_id') == binding['parent_session'] for o in builder_observations)
                    if parent_counted:
                        reason = ('it made a repository-changing tool call (terminal, write_file or patch)'
                                  if self.store.get(db, 'session_change', binding['parent_session']) else
                                  'no other build/repair child or dispatcher worker exists for this card')
                        finding += ' The parent counts as a builder because ' + reason + '.'
                    problems.append(finding + ' This shares a builder family; choose an independent verifier family. A parent that only orchestrates is excluded once a build/repair child or dispatcher worker exists.')
                    continue
                result = {k: route[k] for k in ('provider', 'model', 'reasoning_effort') if k in route}
                result['fallback_providers'] = []
                binding.pop('refused', None)
                binding.pop('finding', None)
                binding['route'] = result
                self.store.put(db, 'binding', binding['nonce'], binding)
                return result
            binding.update(finished_at=time.time(), refused=True)
            binding.setdefault('finding', '; '.join(problems))
            self.store.put(db, 'binding', binding['nonce'], binding)
        raise ValueError('Verification refused: ' + '; '.join(problems) + '. Configure a known different-family verifier route.')

    def start(self, parent, child, goal):
        tokens = MARKER.findall(goal or '')
        if len(tokens) != 1 or not child:
            return
        with self.store.transaction() as db:
            b = self.store.get(db, 'binding', tokens[0])
            if not b or b.get('error'):
                return
            key = GateKey(b['board'], b['card_id'])
            if b['parent_session'] != parent or b.get('used_at') or b.get('finished_at') or time.time() - b['created_at'] >= (3600 if b.get('used_at') else 120) or (b['duty'] == 'verify' and not b.get('route')):
                self.event(db, key, 'refused_unbound', **{'duty': b['duty'], 'finding': 'Parent/child nonce mismatch or replay.'})
                return
            if any(x.get('child_session') == child for x in self.store.rows(db, 'binding')):
                self.event(db, key, 'refused_unbound', **{'finding': 'Child already bound.'})
                return
            b.update(child_session=child, used_at=time.time())
            self.store.put(db, 'binding', b['nonce'], b)
            route = b.get('route', {})
            self.event(db, key, 'arose', **{'duty': b['duty'], 'model': route.get('model'),
                       'family': family(route.get('provider'), route.get('model'), self.config()['family_overrides']), 'tree': b['tree']})

    def stop(self, child):
        with self.store.transaction() as db:
            usage = self.store.get(db, 'usage', child) or {}
            for binding in self.store.rows(db, 'binding'):
                if binding.get('child_session') == child:
                    receipt = self.store.get(db, 'receipt', binding['nonce'])
                    if receipt and receipt.get('tokens') != usage.get('tokens'):
                        self.store.put(db, 'receipt', binding['nonce'], dict(receipt, tokens=usage.get('tokens')))
        with self.store.read() as db:
            if not any(b.get('child_session') == child and not b.get('finished_at') for b in self.store.rows(db, 'binding')):
                return
        with self.store.read() as db:
            binding = next((b for b in self.store.rows(db, 'binding') if b.get('child_session') == child), None)
        evidence = self.evidence(GateKey(binding['board'], binding['card_id'])) if binding else {}
        with self.store.transaction() as db:
            for b in self.store.rows(db, 'binding'):
                if b.get('child_session') == child:
                    b['finished_at'] = time.time()
                    self.store.put(db, 'binding', b['nonce'], b)
                    observations = self.observations(db, child)
                    latest = observations[-1] if observations else b.get('route', {})
                    self.event(db, GateKey(b['board'], b['card_id']), 'finished',
                               **{'duty': b['duty'], 'tree': b['tree'], 'family': latest.get('response_family') or latest.get('family'),
                                  'model': latest.get('response_model') or latest.get('model')}, **evidence)

    def publish_decision(self, args):
        """Read-only approval advice. Unknown or failed inspection always passes through."""
        try:
            if not publish_targets(args.get('command'), None):
                return None
            if self.settings('guard_push', True) is not True or not self.armed() or not self.local_terminal():
                return None
            deadline = time.monotonic() + 3
            scope = next((v for var, v in copy_context().items() if var.name == 'hermes_terminal_scope'), None)
            session_cwd = next((v for var, v in copy_context().items() if var.name == 'HERMES_SESSION_CWD'), None)
            cfg = self.config()
            explicit = args.get('workdir')
            candidates = (explicit,) if explicit else (
                session_cwd, scope.get('TERMINAL_CWD') if scope is not None else os.environ.get('TERMINAL_CWD'),
                self.host_config().get('terminal', {}).get('cwd'), cfg['repo'])
            cwd = next((str(Path(v).expanduser()) for v in candidates
                        if isinstance(v, str) and v.strip().lower() not in ('', '.', './', 'auto', 'cwd')
                        and Path(v).expanduser().is_absolute()
                        and (Path(v).expanduser().is_dir() if explicit else boards.in_work_tree(str(Path(v).expanduser())))), None)
            targets = publish_targets(args.get('command'), cwd)
            if not targets or None in targets or len(targets) > 8:
                return None
            with self.store.read() as db:
                receipts = self.store.rows(db, 'receipt')
            receipts = receipts[-512:]  # the most recent; an old store must not switch the guard off
            card_keys = {(r['board'], r['card_id']) for r in receipts if r['board'] != 'workspace:'}
            states = []
            for folder in targets:
                if not boards.in_work_tree(folder):
                    return None
                root = os.fsdecode(git(folder, 'rev-parse', '--show-toplevel')).strip()
                keys = {GateKey('workspace:', hashlib.sha256(os.fsencode(root)).hexdigest()[:12]).values()}
                for values in card_keys:
                    if time.monotonic() > deadline:
                        return None
                    key = GateKey(*values)
                    if not boards.card_row(key):
                        continue
                    repo = boards.repository(key, cfg)
                    if repo and os.fsdecode(git(repo, 'rev-parse', '--show-toplevel')).strip() == root:
                        keys.add(key.values())
                relevant = [r for r in receipts if (r['board'], r['card_id']) in keys]
                result = state(relevant, fingerprint(root), time.time(), cfg)
                if time.monotonic() > deadline:
                    return None
                states.append(result.state)
            if all(s == 'PASS' for s in states):
                return None
            status = 'STALE' if 'STALE' in states else 'BLOCKED' if 'BLOCKED' in states else 'UNVERIFIED'
            return dict(action='approve', rule_key='shadow-army:publish-unreviewed', message=(
                'Shadow Army: this code has no fresh independent PASS (' + status + '). '
                'Approve to push anyway, or first ask the agent: "Use the verify-loop skill to verify this workspace with Shadow Army."'))
        except Exception:
            return None

    def terminal_start(self, session, call_id, args):
        with self.store.read() as db:
            bindings = [b for b in self.store.rows(db, 'binding') if b.get('child_session') == session
                        and b['duty'] == 'verify' and not b.get('finished_at')]
        if len(bindings) != 1:
            return
        b = bindings[0]
        key = GateKey(b['board'], b['card_id'])
        repo = self.repository(key)
        cwd = args.get('workdir')
        command = args.get('command')
        foreground = not args.get('background') and not str(command or '').rstrip().endswith('&')
        local = self.local_terminal()
        if not local:
            if command == self.config()['check']:
                return dict(action='block', message='Verification checks require a local terminal; the effective terminal for this command is not local. Set this profile to local and start a fresh terminal session.')
            return
        observation = dict(command=command, tree=self.tree(key), foreground=foreground, local=local,
                           paths=worktree_changes(repo), repo_cwd=bool(cwd and Path(cwd).resolve() == Path(repo).resolve()),
                           observed_at=time.time(), nonce=b['nonce'])
        with self.store.transaction() as db:
            current = self.store.get(db, 'binding', b['nonce'])
            if current != b:
                return
            self.store.put(db, 'terminal_pending', session + ':' + call_id, observation)
        if command == self.config()['check']:
            if not foreground:
                return dict(action='block', message='Verification checks must run in the foreground and finish with an integer exit status.')

    def terminal_end(self, session, call_id, result):
        record = session + ':' + call_id
        with self.store.read() as db:
            obs = self.store.get(db, 'terminal_pending', record)
            b = self.store.get(db, 'binding', obs['nonce']) if obs else None
        if not obs or not b or b.get('finished_at'):
            return
        result = json.loads(result) if isinstance(result, str) else result
        if not isinstance(result, dict):
            return
        key = GateKey(b['board'], b['card_id'])
        end_paths = worktree_changes(self.repository(key))
        start_paths = obs.pop('paths')
        missing = object()
        changed_paths = sorted(name for name in start_paths.keys() | end_paths.keys()
                               if start_paths.get(name, missing) != end_paths.get(name, missing))
        obs.update(end_tree=self.tree(key), changed_paths=changed_paths[:5], exit_code=result.get('exit_code'),
                   output_excerpt=str(result.get('output', ''))[-1200:], tests_ran=test_counts.parse(result.get('output')))
        running = any(result.get(k) is not None for k in ('pid', 'session_id', 'yielded_session_id')) or any(
            result.get(k) for k in ('promoted_from_foreground', 'running', 'background')) or result.get('status') in ('running', 'pending')
        if not obs['repo_cwd'] or not obs['foreground'] or not obs['local'] or running or result.get('error') or type(obs['exit_code']) is not int:
            obs['exit_code'] = None
        with self.store.transaction() as db:
            if self.store.get(db, 'binding', b['nonce']) != b:
                return
            if obs['command'] == self.config()['check'] and type(obs['exit_code']) is int:
                identity = json.dumps(key.values())
                previous = self.store.get(db, 'test_count', identity) or {}
                before, now = (previous.get('drop') or {}).get('before', previous.get('count')), obs['tests_ran']
                drop = dict(before=before, now=now) if before is not None and now is not None and now < before else None
                self.store.put(db, 'test_count', identity, dict(count=now if now is not None else before, drop=drop))
            self.store.put(db, 'check', record, obs)
            db.execute('DELETE FROM records WHERE ns=? AND kind=? AND key=?', (self.store.namespace, 'terminal_pending', record))

    def delegation_end(self, tasks, parent, result):
        tasks = json.loads(tasks) if isinstance(tasks, str) else tasks
        result = json.loads(result) if isinstance(result, str) else result
        if not isinstance(tasks, list) or not isinstance(result, dict):
            return
        entries = list(result.get('results', []))
        for field in ('refused', 'failed'):
            entries.extend(dict(entry, status=field) for entry in result.get(field, []))
        if result.get('error'):
            entries = [dict(task_index=i, error=result['error']) for i in range(len(tasks))]
        with self.store.transaction() as db:
            for entry in entries:
                index = entry.get('task_index', entry.get('index'))
                if type(index) is not int or not 0 <= index < len(tasks):
                    continue
                if not (entry.get('error') or entry.get('status') in ('error', 'failed', 'refused') or entry.get('route', {}).get('refused')):
                    continue
                for nonce in MARKER.findall(tasks[index].get('goal', '')):
                    b = self.store.get(db, 'binding', nonce)
                    if not b or b['parent_session'] != parent or b.get('finished_at') or b.get('used_at'):
                        continue
                    b.update(finished_at=time.time(), refused=True, finding=str(entry.get('error') or 'Delegate task failed.'))
                    self.store.put(db, 'binding', nonce, b)
                    self.event(db, GateKey(b['board'], b['card_id']), 'refused_unbound', duty=b['duty'], finding=b['finding'])

    def verdict(self, key, verdict, findings, session):
        if not isinstance(findings, list) or len(findings) > 50:
            raise ValueError('findings must be a list (at most 50).')
        for item in findings:
            if not isinstance(item, dict) or not isinstance(item.get('file'), str) or type(item.get('line')) is not int or item['line'] < 1 or not isinstance(item.get('text'), str) or not item['text'].strip():
                raise ValueError('Each finding requires file, line >= 1, and nonempty text.')
            if len(item['text']) > 2000 or len(item['file']) > 500:
                raise ValueError('Finding is too long.')
        cfg = self.config()
        current_tree = self.tree(key)
        evidence = self.evidence(key, tree=current_tree)
        with self.store.transaction() as db:
            bindings = [b for b in self.store.rows(db, 'binding') if b.get('child_session') == session
                        and (b['board'], b['card_id']) == key.values()]
            b = bindings[0] if len(bindings) == 1 else None
            checks = [c for c in self.store.rows(db, 'check') if b and c['nonce'] == b['nonce']]
            try:
                observations = self.observations(db, session)
                builders = self.builders(db, key, b['parent_session']) if b else set()
                self.require_known(observations + (self.builder_observations(db, key, b['parent_session']) if b else []))
                check = accept_verdict(b, observations, builders, current_tree, cfg['check'], checks, verdict, findings, time.time())
            except ValueError as exc:
                error = str(exc)
                changed = sorted({name for c in checks if c.get('tree') != c.get('end_tree') for name in c.get('changed_paths', [])})[:5]
                if changed:
                    error += ' Changed paths: ' + ', '.join(changed) + '. Commit source changes or ignore generated check output, then request a new review.'
                self.event(db, key, 'refused_unbound', duty='verify', finding=error)
            else:
                actual = next(o for o in reversed(observations) if o.get('response_model'))
                builder_observations = self.builder_observations(db, key, b['parent_session'])
                builder = builder_observations[-1] if builder_observations else {}
                usage = self.store.get(db, 'usage', session) or {}
                elapsed_s = max(0, int(time.time() - (b.get('used_at') or b['created_at'])))
                receipt = dict(id=b['nonce'], elapsed_s=elapsed_s, tokens=usage.get('tokens'), builder={k: builder.get(k) for k in ('family', 'model')}, board=key.board, card_id=key.card_id, tree=b['tree'], check_hash=b['check_hash'],
                               verdict=verdict, findings=findings, check=check, **evidence,
                               accepted_skipped=verdict == 'PASS' and bool(evidence.get('skipped_count')),
                               accepted_removed=verdict == 'PASS' and bool(evidence['weakened_count']),
                               verifier=dict({k: actual.get(k) for k in ('session_id', 'provider', 'model', 'response_model')}, family=actual.get('response_family') or actual.get('family')),
                               builder_families=sorted(builders), created_at=time.time(), synthetic=self.store.namespace in ('trial', 'selftest'))
                if verdict == 'PASS':
                    identity = json.dumps(key.values())
                    count = self.store.get(db, 'test_count', identity)
                    if count:
                        self.store.put(db, 'test_count', identity, dict(count, drop=None))
                self.store.put(db, 'receipt', b['nonce'], receipt)
                b['finished_at'] = time.time()
                self.store.put(db, 'binding', b['nonce'], b)
                self.event(db, key, 'verdict', **{'duty': 'verify', 'family': receipt['verifier']['family'], 'model': actual.get('response_model') or actual.get('model'),
                           'verdict': verdict, 'finding': findings[0]['text'] if findings else None, 'tree': b['tree'], 'check': check,
                           'findings': findings, 'check_passed_but_failed': verdict == 'FAIL' and bool(check) and check['exit_code'] == 0,
                           'accepted_skipped': receipt['accepted_skipped'], 'accepted_removed': receipt['accepted_removed'], 'elapsed_s': elapsed_s, 'tokens': receipt['tokens'],
                           'simulated': self.store.namespace == 'trial', 'receipt_id': b['nonce']}, **evidence)
                error = None
        if error:
            raise ValueError(error)
        if verdict == 'PASS':
            self.evidence(key, verdict=True, tree=current_tree)
        return self.inspect(key)

    def close(self, key):
        result = self.inspect(key, advisory=False)
        try:
            with self.store.transaction() as db:
                self.event(db, key, 'close_permitted' if result['state'] in ('PASS', 'UNGATED', 'UNARMED') else 'close_refused',
                       tree=result['tree'], finding=result['reason'], gate={k: result[k] for k in ('state', 'reason', 'tree')})
        except (OSError, sqlite3.Error):
            pass
        if result['state'] not in ('PASS', 'UNGATED', 'UNARMED'):
            latest_fail = next((r for r in reversed(result['receipts']) if r['verdict'] == 'FAIL'), None)
            findings = ' Findings: ' + json.dumps(latest_fail['findings']) if latest_fail else ''
            return dict(action='block', message=f"Gate {result['state']}: {result['reason']}{findings} Next: {result['next_step']}")
        return None
