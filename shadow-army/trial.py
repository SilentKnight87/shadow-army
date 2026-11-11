"""Deliberately started, isolated trial with simulated reviewers and real checks."""
import os
import subprocess
import sys
import time

from .boards import clean
from .policy import GateKey, fingerprint, git
from .service import Gates
from .store import Store

KEY = GateKey('trial', 'empty-cart')
COMMAND = 'python -m unittest discover -s . -p test_cart.py'
BUG = 'def total(prices):\n    return prices[0] + sum(prices[1:])\n'
FIX = 'def total(prices):\n    return sum(prices)\n'
TEST = '''import unittest
from cart import total

class CartTests(unittest.TestCase):
    def test_nonempty(self):
        self.assertEqual(total([2, 3]), 5)

    def test_empty(self):
        self.assertEqual(total([]), 0, "Empty cart should total 0")
'''

WEAK_TEST = TEST.replace('        self.assertEqual(total([]), 0, "Empty cart should total 0")', '        pass')


class Trial:
    def __init__(self, real, namespace='trial'):
        self.repo = real.store.path.parent.resolve() / (namespace + '-repo')
        self.store = Store(real.store.path.parent, namespace, on_event=real.store.on_event if namespace == 'trial' else None)
        self.cfg = cfg = dict(repo=str(self.repo), check=COMMAND, max_age_s=3600, verifier=dict(provider='anthropic', model='claude-simulated'))
        self.gates = Gates(self.store, lambda k, v: cfg.get(k, v), host_config=lambda: {'delegation': {'backend': 'shadow-army'}})
        self.gates.local_terminal = lambda: True
        self.gates.repository = lambda key: str(self.repo)
        self.gates.key_for = lambda card, board=None: KEY
        # Trial routing never consults real Kanban workspaces.
        self.gates.tree = lambda key: fingerprint(self.repo)

    def validate_paths(self):
        if self.repo.is_symlink() or ((self.repo / '.git').exists() and not (self.repo / '.git').is_dir()):
            raise ValueError('Trial repository must stay inside plugin data.')
        if any((self.repo / name).is_symlink() for name in ('.git', 'cart.py', 'test_cart.py')):
            raise ValueError('Trial assets cannot be symlinks.')

    def initialize(self):
        self.validate_paths()
        self.repo.mkdir(parents=True, exist_ok=True)
        git(self.repo, 'init')
        for name, body in [('cart.py', BUG), ('test_cart.py', TEST)]:
            path = self.repo / name
            if path.is_symlink():
                raise ValueError('Trial assets cannot be symlinks.')
            path.write_text(body)
        self.commit('Seed empty cart bug')

    def commit(self, message):
        git(self.repo, 'add', '--', 'cart.py', 'test_cart.py')
        git(self.repo, '-c', 'user.name=Shadow Army Trial', '-c', 'user.email=trial@example.invalid',
            '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-m', message)

    def start(self):
        self.initialize()
        with self.store.transaction() as db:
            self.store.reset(db)
        self.gates.observe('trial-parent', 'openai', 'gpt-simulated', 'gpt-simulated')
        self.gates.assertion_evidence(KEY, record=True)
        check = self.check()
        if check['exit_code'] != 1:
            raise ValueError('The opening empty-cart test must fail.')
        with self.store.transaction() as db:
            self.store.put(db, 'trial', 'step', {'step': 1})
            self.shadow(db, 'builder', 'build', 'openai', 'gpt-simulated', None, state='running')
            self.gates.event(db, KEY, 'arose', duty='build', family='openai', model='gpt-simulated',
                             tree=check['tree'], check=check, simulated=True)

    def shadow(self, db, sid, duty, family, model, verdict, finding=None, state='finished'):
        self.store.put(db, 'shadow', sid, dict(id=sid, duty=duty, family=family, model=model,
                       state=state, verdict=verdict, finding=finding, elapsed_s=0, simulated=True))

    def check(self):
        self.validate_paths()
        argv = [sys.executable, '-I', '-B', '-m', 'unittest', 'discover', '-s', str(self.repo), '-p', 'test_cart.py']
        env = {'PATH': os.defpath, 'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1'}
        tree = fingerprint(self.repo)
        result = subprocess.run(argv, cwd=self.repo, env=env, capture_output=True, text=True, timeout=3)
        # The trial always runs its own local test process.
        # Show paths relative to the trial repository, never the machine's folders.
        output = (result.stdout + result.stderr).replace(str(self.repo) + os.sep, '')
        excerpt = clean(output, 1200)
        return dict(command=COMMAND, exit_code=result.returncode, output_excerpt=excerpt[-1200:],
                    tree=tree, end_tree=fingerprint(self.repo), observed_at=time.time())

    def review_task(self):
        return self.gates.prepare([dict(card=KEY.card_id, board=KEY.board, duty='verify', goal='Review trial')], 'trial-parent')[0]

    def receipt(self, verdict, check, findings=None):
        task = self.review_task()
        route = self.gates.route(task, {})
        if route['model'] != 'claude-simulated':
            raise ValueError('Unexpected trial verifier route.')
        child = 'trial-' + str(time.time_ns())
        self.gates.start('trial-parent', child, task['goal'])
        self.gates.observe(child, route['provider'], route['model'], route['model'])
        call_id = 'trial-check'
        self.gates.terminal_start(child, call_id, dict(command=COMMAND, workdir=str(self.repo)))
        self.gates.terminal_end(child, call_id, dict(exit_code=check['exit_code'], output=check['output_excerpt']))
        self.gates.verdict(KEY, verdict, findings or [], child)
        with self.store.transaction() as db:
            self.shadow(db, child, 'verify', 'anthropic', route['model'], verdict,
                        findings[0]['text'] if findings else None)

    def step(self):
        with self.store.read() as db:
            row = self.store.get(db, 'trial', 'step')
        if row is None or row.get('closed'):
            raise ValueError('Start or reset the trial first; the card is not open.')
        step = row['step'] + 1
        tampered = row.get('tampered', False)
        self.validate_paths()
        if step == 2:
            (self.repo / 'test_cart.py').write_text(WEAK_TEST)
        elif step == 6:
            (self.repo / 'test_cart.py').write_text(TEST)
            (self.repo / 'cart.py').write_text(FIX)
        check = self.check()
        if check['exit_code'] != 0 or check['tree'] != check['end_tree']:
            raise ValueError('Unexpected real trial check outcome; reset the trial.')
        evidence = self.gates.evidence(KEY)
        tree, closed = check['tree'], False
        if step in (3, 8, 10):
            decision = self.gates.close(KEY)
            should_close = step == 10 or (step == 8 and not tampered)
            if (decision is None) != should_close:
                raise ValueError('Unexpected real close decision; reset the trial.')
            closed = should_close
            if closed:
                with self.store.transaction() as db:
                    self.gates.event(db, KEY, 'closed', tree=tree, gate_at_close='PASS',
                                     gate=dict(state='PASS', tree=tree), check=check)
        elif step == 4:
            self.cfg['verifier'] = dict(provider='openai', model='gpt-simulated')
            try:
                try:
                    self.gates.route(self.review_task(), {})
                except ValueError as exc:
                    if 'shares a builder family' not in str(exc):
                        raise
                else:
                    raise ValueError('Real same-family route unexpectedly accepted.')
            finally:
                self.cfg['verifier'] = dict(provider='anthropic', model='claude-simulated')
            with self.store.transaction() as db:
                self.shadow(db, 'same-family', 'verify', 'openai', 'gpt-simulated', None, state='refused')
        elif step == 5:
            if not evidence['weakened']:
                raise ValueError('Real removed-assertion check did not find the deleted assertion.')
            findings = [dict(file=r['file'], line=r['line'], text=r['text']) for r in evidence['weakened']]
            self.receipt('FAIL', check, findings)
        elif step in (7, 9):
            if evidence['weakened'] != []:
                raise ValueError('Restored assertions must have no removals.')
            self.receipt('PASS', check)
        elif step in (2, 6):
            if (step == 2 and not evidence['weakened']) or (step == 6 and evidence['weakened'] != []):
                raise ValueError('Unexpected real assertion inventory.')
            duty = 'build' if step == 2 else 'repair'
            with self.store.transaction() as db:
                # One builder row for the build; a second row only for the real repair at step 6.
                self.shadow(db, 'builder' if step == 2 else 'repair-6', duty, 'openai', 'gpt-simulated', None)
                self.gates.event(db, KEY, 'finished', duty=duty, family='openai', model='gpt-simulated',
                                 tree=tree, check=check, **evidence, simulated=True)
        else:
            raise ValueError('Unexpected trial step.')
        with self.store.transaction() as db:
            self.store.put(db, 'trial', 'step', dict(step=step, closed=closed, tampered=tampered))

    def edit(self):
        self.validate_paths()
        path = self.repo / 'cart.py'
        if path.is_symlink():
            raise ValueError('Trial file cannot be a symlink.')
        with path.open('a') as stream:
            stream.write('\n# Edited after review\n')

    def tamper(self):
        with self.store.read() as db:
            row = self.store.get(db, 'trial', 'step')
        if not row or row['step'] != 7 or row.get('closed'):
            raise ValueError('Reach the trial PASS before tampering with reviewed code.')
        if row.get('tampered'):
            raise ValueError('The reviewed code was already changed; reset the trial before tampering again.')
        self.edit()
        tree = fingerprint(self.repo)
        with self.store.transaction() as db:
            if self.store.get(db, 'trial', 'step') != row:
                raise ValueError('Trial changed while tampering; refresh it.')
            self.store.put(db, 'trial', 'step', dict(row, tampered=True))
            self.gates.event(db, KEY, 'edited', tree=tree, finding='Tracked file cart.py edited after review.', simulated=True)

    def reset(self):
        with self.store.transaction() as db:
            self.store.reset(db)
            self.gates.event(db, KEY, 'finished', **{'finding': 'Trial reset.'})
