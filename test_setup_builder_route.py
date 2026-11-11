"""Setup uses the configured builder, including delegated two-family profiles."""
import copy
import importlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hermes_cli.config import DEFAULT_CONFIG
from test_adapter import api
from test_gate import Gates, Store

setup = importlib.import_module(api.PACKAGE + '.setup')


class SetupBuilderRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = copy.deepcopy(DEFAULT_CONFIG)
        self.cfg = dict(verifier=dict(provider='anthropic', model='claude-test'))
        self.gates = Gates(Store(Path(self.temp.name) / 'data'), lambda k, d: self.cfg.get(k, d),
                           host_config=lambda: self.host)
        setup.register_context(self.gates, SimpleNamespace(set_config=lambda *args: None))
        self.addCleanup(setup.contexts().pop, str(self.gates.store.path.resolve()), None)

    def reads(self):
        for call in range(3):
            with self.subTest(call=call + 1):
                yield setup.status(self.gates)[0], setup.actions(self.gates)

    def test_main_can_coordinate_and_verify_delegated_builder(self):
        self.host['model'] = dict(provider='anthropic', default='claude-test')
        self.host['delegation'].update(provider='openai-codex', model='gpt-6-luna')
        for item, actions in self.reads():
            self.assertTrue(item['ok'])
            self.assertEqual(item['value'], 'openai builds (delegated), anthropic verifies')
            self.assertIn('only delegates and runs no terminal command or file edit itself', item['hint'])
            self.assertTrue(actions['can_arm'])
            self.assertEqual([(r['provider'], r['model']) for r in actions['routes']],
                             [('anthropic', 'claude-test')])
        snapshot = api.projection.snapshot(self.gates)
        self.assertTrue(next(row for row in snapshot['setup'] if row['id'] == 'families')['ok'])
        self.assertTrue(snapshot['setup_actions']['can_arm'])

    def test_main_builder_without_delegation(self):
        self.host['model'] = dict(provider='openai', default='gpt-test')
        self.host.pop('delegation', None)
        for item, actions in self.reads():
            self.assertTrue(item['ok'])
            self.assertEqual(item['value'], 'openai builds, anthropic verifies')
            self.assertTrue(actions['can_arm'])
            self.assertEqual([r['family'] for r in actions['routes']], ['anthropic'])

    def test_all_one_family(self):
        self.host['model'] = dict(provider='anthropic', default='claude-main')
        self.host['delegation'].update(provider='anthropic', model='claude-builder')
        for item, actions in self.reads():
            self.assertFalse(item['ok'])
            self.assertFalse(actions['can_arm'])
            self.assertEqual(actions['routes'], [])

    def test_related_delegation_family(self):
        self.host['model'] = dict(provider='meta', default='llama-main')
        self.host['delegation'].update(provider='nous', model='hermes-builder')
        self.cfg['verifier'] = dict(provider='meta', model='llama-verifier')
        for item, actions in self.reads():
            self.assertFalse(item['ok'])
            self.assertFalse(actions['can_arm'])
            self.assertEqual(actions['routes'], [])

    def test_malformed_delegation_settings(self):
        self.host['model'] = dict(provider='openai', default='gpt-test')
        for delegation in (None, [], 'bad', 42, {}, {'provider': [], 'model': {}},
                           {'provider': 'openai', 'model': ['gpt-test']},
                           {'provider': '', 'model': 'gpt-test'},
                           {'backends': []}, {'backends': {'model_map': []}},
                           {'fallback_providers': [{'provider': [], 'model': 'claude-test'}]},
                           {'backends': {'model_map': {'options': [{'provider': {}, 'model': ['claude-test']}]}}}):
            with self.subTest(delegation=delegation):
                self.host['delegation'] = delegation
                for item, actions in self.reads():
                    self.assertTrue(item['ok'])
                    self.assertTrue(actions['can_arm'])

    def test_unknown_delegation_family_is_not_ready(self):
        self.host['model'] = dict(provider='anthropic', default='claude-main')
        self.host['delegation'].update(provider='proxy', model='unknown-alias')
        for item, actions in self.reads():
            self.assertFalse(item['ok'])
            self.assertFalse(actions['can_arm'])
            self.assertEqual(actions['routes'], [])

    def test_closed_code_card_does_not_unconfigure_profile(self):
        self.host['model'] = dict(provider='anthropic', default='claude-test')
        self.host['delegation'].update(backend='shadow-army', provider='openai-codex', model='gpt-6-luna')
        row = dict(id='closed', board='default', title='Code', status='done')
        with patch.object(api.boards, 'cards', return_value=([row], [])), patch.object(
                self.gates, 'inspect', return_value=dict(state='PASS', repo=None, tree=None, reason='Closed')):
            snapshot = api.projection.snapshot(self.gates)
        self.assertTrue(snapshot['armed'])
        required = [r for r in snapshot['setup'] if r['id'] not in ('hook', 'hermes_version')]
        self.assertTrue(all(r['ok'] for r in required), required)
        self.assertIn('No open code card.', snapshot['setup'][0]['hint'])
        self.assertEqual(self.host['delegation']['model'], 'gpt-6-luna')
        self.assertTrue(next(r for r in required if r['id'] == 'families')['ok'])
        for repo, verifier in ((str(Path(self.temp.name) / 'missing'), self.cfg['verifier']),
                               (None, dict(provider='openai', model='gpt-test'))):
            with self.subTest(repo=repo, verifier=verifier):
                self.cfg.update(repo=repo, verifier=verifier)
                with patch.object(api.boards, 'cards', return_value=([], [])):
                    broken = api.projection.snapshot(self.gates)
                failures = [r for r in broken['setup'] if not r['ok'] and r['id'] not in ('hook', 'hermes_version')]
                self.assertTrue(failures)
                self.assertTrue(all(r['hint'] for r in failures))
