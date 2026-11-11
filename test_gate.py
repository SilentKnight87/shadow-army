import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PLUGIN = Path(__file__).parent / 'shadow-army'
spec = importlib.util.spec_from_file_location('gate_test_plugin', PLUGIN / '__init__.py', submodule_search_locations=[str(PLUGIN)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
from gate_test_plugin.policy import GateKey, accept_verdict, check_hash, family, fingerprint, git, state
from gate_test_plugin.service import Gates
from gate_test_plugin.store import Store


def seed(repo):
    repo.mkdir()
    git(repo, 'init')
    (repo / 'app.txt').write_text('one\n')
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-m', 'seed')


class PolicyTests(unittest.TestCase):
    def test_family_table_and_unknown_alias(self):
        models = dict(anthropic='anthropic/claude-opus', openai='openai/gpt-5', google='google/gemini-3',
                      xai='x-ai/grok-4', meta='meta-llama/llama-4', mistral='mistralai/mistral-large',
                      deepseek='deepseek/deepseek-v3', qwen='qwen/qwen3', moonshot='moonshotai/kimi-k2',
                      zhipu='z-ai/glm-4', nous='nousresearch/hermes-4', cohere='cohere/command-r')
        for f, m in models.items():
            self.assertEqual(family('openrouter', m), f)
        self.assertIsNone(family('openai', 'custom-alias'))
        self.assertEqual(family('proxy', 'custom-alias', {'proxy/custom-alias': 'google'}), 'google')
        self.assertIsNone(family('proxy', 'custom-alias', {'proxy/custom-alias': 'invented'}))

    def test_fingerprint_tracks_dirty_staged_untracked_symlink_ignored_and_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            seed(repo)
            initial = fingerprint(repo)
            (repo / 'app.txt').write_text('two\n')
            dirty = fingerprint(repo)
            self.assertNotEqual(dirty, initial)
            git(repo, 'add', '.')
            self.assertEqual(fingerprint(repo), dirty)
            git(repo, 'reset', '--hard', 'HEAD')
            self.assertEqual(fingerprint(repo), initial)
            (repo / 'new').symlink_to('missing')
            untracked = fingerprint(repo)
            self.assertNotEqual(untracked, initial)
            (repo / 'new').unlink()
            with (repo / '.git/info/exclude').open('a') as f:
                f.write('\nignored\n')
            (repo / 'ignored').write_text('excluded')
            self.assertEqual(fingerprint(repo), initial)
            (repo / 'large').write_bytes(b'x' * (4 * 1024 * 1024 + 1))
            with self.assertRaisesRegex(ValueError, 'budget'):
                fingerprint(repo)
            (repo / 'large').unlink()
            os.mkfifo(repo / 'pipe')
            self.assertEqual(fingerprint(repo), initial)
            (repo / 'pipe').unlink()
            git(repo, 'add', 'app.txt')
            (repo / 'app.txt').write_text('three\n')
            git(repo, 'add', 'app.txt')
            git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-m', 'changed')
            self.assertNotEqual(fingerprint(repo), initial)

    def test_state_machine_and_check_definition(self):
        row = dict(tree='a', check_hash=check_hash('check'), verdict='PASS', synthetic=False, created_at=90)
        cfg = dict(check='check', max_age_s=20)
        self.assertEqual(state([], None, 100, cfg).state, 'UNKNOWN')
        self.assertEqual(state([], 'a', 100, cfg).state, 'UNVERIFIED')
        self.assertEqual(state([row], 'a', 100, cfg).state, 'PASS')
        self.assertEqual(state([row], 'b', 100, cfg).state, 'STALE')
        self.assertEqual(state([row], 'a', 111, cfg).state, 'STALE')
        self.assertEqual(state([row], 'a', 100, dict(check='weaker')).state, 'STALE')
        self.assertEqual(state([dict(row, verdict='FAIL')], 'a', 100, cfg).state, 'BLOCKED')
        self.assertEqual(state([dict(row, synthetic=True)], 'a', 100, cfg).state, 'UNVERIFIED')

    def test_verdict_acceptance_requires_all_attempts_response_check_binding_and_findings(self):
        binding = dict(duty='verify', child_session='v', tree='a', check_hash=check_hash('check'), created_at=1)
        obs = [dict(model='claude-test', family='anthropic', response_model='claude-test', response_family='anthropic')]
        check = dict(command='check', tree='a', end_tree='a', exit_code=0)
        def accept(b=binding, o=obs, tree='a', checks=None, verdict='PASS', findings=None):
            return accept_verdict(b, o, {'openai'}, tree, 'check', [check] if checks is None else checks, verdict, findings or [], 2)
        self.assertEqual(accept(), check)
        for kw in [dict(b=None), dict(b=dict(binding, finished_at=2)), dict(tree='b'), dict(checks=[]),
                   dict(o=obs + [dict(model='gpt-test', family='openai')]),
                   dict(o=obs + [dict(model='mystery', family=None)]),
                   dict(o=[dict(obs[0], response_model='mystery', response_family=None)]),
                   dict(o=[dict(model='claude-test', family='anthropic')]), dict(verdict='FAIL')]:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                accept(**kw)
        self.assertIsNone(accept(checks=[], verdict='FAIL', findings=[dict(file='app.py', line=1, text='bug')]))

    def test_trial_reset_still_broadcasts_every_new_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            store = Store(tmp, 'trial', on_event=events.append)
            key = GateKey('trial', 'card')
            for _ in range(3):
                with store.transaction() as db:
                    store.reset(db)
                    store.event(db, key, {'kind': 'finished'})
            self.assertEqual(events, [dict(board='trial', card='card', demo=True)] * 3)

    def test_store_is_durable_across_processes_and_namespaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            key = GateKey('default', 'card')
            with store.transaction() as db:
                store.put(db, 'receipt', 'r', {'verdict': 'PASS'})
                store.event(db, key, {'kind': 'verdict'})
            code = f"import sys; sys.path.insert(0,{str(PLUGIN)!r}); from store import Store; s=Store({tmp!r});\nwith s.transaction() as d: print(s.rows(d,'receipt')[0]['verdict'])"
            result = subprocess.check_output([sys.executable, '-B', '-c', code], text=True)
            self.assertEqual(result.strip(), 'PASS')
            with Store(tmp, 'trial').transaction() as db:
                self.assertEqual(Store(tmp, 'trial').rows(db, 'receipt'), [])
            with store.transaction() as db:
                self.assertEqual(store.history(db, key), [{'kind': 'verdict'}])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        seed(self.repo)
        self.env = patch.dict(os.environ, {'HERMES_KANBAN_HOME': str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cfg = dict(repo=str(self.repo), check='true', verifier=dict(provider='anthropic', model='claude-test'))
        self.gates = Gates(Store(self.root / 'data'), lambda k, d: self.cfg.get(k, d), host_config=lambda: {'delegation': {'backend': 'shadow-army'}})
        self.key = GateKey('default', 'card')
        self.gates.observe('parent', 'openai', 'gpt-test')

    def bound(self):
        task = self.gates.prepare([dict(card='card', duty='verify', goal='Review')], 'parent')[0]
        route = self.gates.route(task, {})
        self.assertEqual(route['fallback_providers'], [])
        self.gates.start('parent', 'child', task['goal'])
        self.gates.observe('child', 'anthropic', 'claude-test')
        self.gates.observe('child', 'anthropic', 'claude-test', 'claude-test')
        return task

    def test_complete_flow_with_serialization_replay_check_change_and_staleness(self):
        self.assertEqual(self.gates.close(self.key)['action'], 'block')
        task = self.bound()
        with self.assertRaisesRegex(ValueError, 'flight'):
            self.gates.route(self.gates.prepare([dict(card='card', duty='verify')], 'parent')[0], {})
        self.gates.start('parent', 'replay', task['goal'])
        with self.assertRaisesRegex(ValueError, 'bound'):
            self.gates.verdict(self.key, 'PASS', [], 'replay')
        with self.assertRaisesRegex(ValueError, 'check'):
            self.gates.verdict(self.key, 'PASS', [], 'child')
        self.gates.terminal_start('child', 'call', dict(command='true', workdir=str(self.repo)))
        self.gates.terminal_end('child', 'call', json.dumps(dict(exit_code=0, output='checked')))
        self.assertEqual(self.gates.verdict(self.key, 'PASS', [], 'child')['state'], 'PASS')
        self.assertIsNone(self.gates.close(self.key))
        with self.assertRaisesRegex(ValueError, 'replay'):
            self.gates.verdict(self.key, 'PASS', [], 'child')
        self.cfg['check'] = 'changed'
        self.assertEqual(self.gates.inspect(self.key)['state'], 'STALE')
        self.cfg['check'] = 'true'
        (self.repo / 'app.txt').write_text('edited')
        self.assertEqual(self.gates.inspect(self.key)['state'], 'STALE')
        self.assertEqual(self.gates.close(self.key)['action'], 'block')

    def test_check_cannot_transfer_between_children_commands_or_workspaces(self):
        self.bound()
        self.gates.terminal_start('child', 'wrong-dir', dict(command='true', workdir=str(self.root)))
        self.gates.terminal_end('child', 'wrong-dir', dict(exit_code=0, output=''))
        self.gates.terminal_start('child', 'wrong-command', dict(command='echo true', workdir=str(self.repo)))
        self.gates.terminal_end('child', 'wrong-command', dict(exit_code=0, output=''))
        self.gates.terminal_start('child', 'error', dict(command='true', workdir=str(self.repo)))
        self.gates.terminal_end('child', 'error', dict(exit_code=0, output='', error='failed'))
        with self.assertRaisesRegex(ValueError, 'check'):
            self.gates.verdict(self.key, 'PASS', [], 'child')
        self.gates.terminal_start('child', 'changed', dict(command='true', workdir=str(self.repo)))
        (self.repo / 'app.txt').write_text('changed during check')
        self.gates.terminal_end('child', 'changed', dict(exit_code=0, output=''))
        with self.assertRaisesRegex(ValueError, 'tree or check changed'):
            self.gates.verdict(self.key, 'PASS', [], 'child')

    def test_unknown_same_family_fallback_and_wrong_lineage(self):
        self.cfg['verifier'] = dict(provider='openai', model='gpt-test')
        task = self.gates.prepare([dict(card='card', duty='verify')], 'parent')[0]
        with self.assertRaisesRegex(ValueError, 'shares'):
            self.gates.route(task, {})
        self.cfg['verifier']['fallbacks'] = [dict(provider='anthropic', model='claude-test')]
        task = self.gates.prepare([dict(card='card', duty='verify')], 'parent')[0]
        self.assertEqual(self.gates.route(task, {})['provider'], 'anthropic')
        self.gates.start('wrong-parent', 'intruder', task['goal'])
        with self.assertRaisesRegex(ValueError, 'bound'):
            self.gates.verdict(self.key, 'FAIL', [dict(file='app', line=1, text='bug')], 'intruder')


if __name__ == '__main__':
    unittest.main()
