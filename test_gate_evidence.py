import importlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from test_adapter import api

assertions = importlib.import_module(api.PACKAGE + '.assertions')


class AssertionInventoryTests(unittest.TestCase):
    def test_removed_and_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            test = repo / 'test_a.py'
            test.write_text('assert total([]) == 0\n')
            before = assertions.inventory(repo)
            self.assertEqual(len(before), 1)
            test.rename(repo / 'test_b.py')
            self.assertEqual(assertions.removed(before, assertions.inventory(repo)), [])
            (repo / 'test_b.py').unlink()
            missing = assertions.removed(before, assertions.inventory(repo))
            self.assertEqual(missing, [dict(before[0], since='start', kind='removed')])
            test.write_text('assert True\n')
            self.assertIsNone(assertions.inventory(repo, max_files=0))
            self.assertIsNone(assertions.inventory(repo, max_bytes=1))
            self.assertIsNone(assertions.inventory(repo, timeout=0))

    def test_ordinary_files_do_not_consume_test_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            for index in range(1500):
                (repo / f'ordinary_{index}.txt').touch()
            (repo / 'test_a.py').write_text('assert True\n')
            self.assertEqual(len(assertions.inventory(repo)), 1)

    def test_symlinked_and_unreadable_tests_are_skipped(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            (repo / 'test_a.py').write_text('assert True\n')
            (repo / 'test_link.py').symlink_to(repo / 'test_a.py')
            (repo / 'test_denied.py').touch()
            original = assertions.os.open
            def opened(path, flags):
                if Path(path).name == 'test_denied.py':
                    raise PermissionError('denied')
                return original(path, flags)
            with patch.object(assertions.os, 'open', side_effect=opened):
                self.assertEqual(len(assertions.inventory(repo)), 1)

    def test_common_languages(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            for ext, text in [('py', 'self.assertEqual(x, y)'), ('ts', 'expect(x).toBe(y)'),
                              ('go', 'assert.Equal(t, x, y)'), ('rs', 'assert_eq!(x, y);'),
                              ('java', 'assertEquals(x, y);'), ('rb', 'assert_equal(x, y)')]:
                (repo / ('test_a.' + ext)).write_text(text)
            self.assertEqual(len(assertions.inventory(repo)), 6)

class TrialRoutingTests(unittest.TestCase):
    def test_trial_uses_real_routing_and_removed_lines(self):
        from unittest.mock import patch
        from test_gate import Gates, Store
        with tempfile.TemporaryDirectory() as tmp:
            real = Gates(Store(Path(tmp)), lambda k, d: d)
            trial = api.trial_module.Trial(real)
            trial.start()
            with trial.store.read() as db:
                self.assertEqual(trial.store.history(db, api.trial_module.KEY)[0]['check']['exit_code'], 1)
            trial.step()
            missing = trial.gates.assertion_evidence(api.trial_module.KEY)['weakened']
            self.assertEqual(missing[0]['text'], 'self.assertEqual(total([]), 0, "Empty cart should total 0")')
            trial.step()
            with patch.object(trial.gates, 'route', return_value={}):
                with self.assertRaisesRegex(ValueError, 'unexpectedly accepted'):
                    trial.step()
            trial.step()
            trial.step()
            with trial.store.read() as db:
                event = trial.store.history(db, api.trial_module.KEY)[-1]
            self.assertEqual(event['findings'][0]['text'], missing[0]['text'])
            self.assertEqual(event['weakened'], missing)
            self.assertEqual(trial.gates.assertion_evidence(api.trial_module.KEY)['weakened'], missing)
            trial.step()
            self.assertEqual(trial.gates.assertion_evidence(api.trial_module.KEY)['weakened'], [])

class SetupReadinessTests(unittest.TestCase):
    def gates(self, tmp, host):
        from test_gate import Gates, Store
        return Gates(Store(Path(tmp)), lambda k, d: d, host_config=lambda: host)

    def test_missing_main_and_configured_routes(self):
        setup = importlib.import_module(api.PACKAGE + '.setup')
        host = dict(fallback_providers=[dict(provider='anthropic', model='claude-test')])
        with tempfile.TemporaryDirectory() as tmp:
            gates = self.gates(tmp, host)
            self.assertFalse(setup.actions(gates)['can_arm'])
            self.assertFalse(setup.status(gates)[0]['ok'])
            host['model'] = dict(provider='openai', default='gpt-test')
            self.assertFalse(setup.actions(gates)['can_arm'])
            self.assertEqual(setup.actions(gates)['routes'][0]['family'], 'anthropic')
            self.assertFalse(setup.actions(gates)['can_set_routing'])
            from unittest.mock import patch
            from types import SimpleNamespace
            from unittest.mock import Mock
            save = Mock()
            setup.register_context(gates, SimpleNamespace(set_config=save))
            self.assertTrue(setup.actions(gates)['can_arm'])
            setup.arm(gates, 'anthropic', 'claude-test')
            self.assertEqual(save.call_count, 2)
            self.assertEqual(save.call_args_list[0].args, ('verifier_provider', 'anthropic'))
            self.assertEqual(save.call_args_list[1].args, ('verifier_model', 'claude-test'))
            with self.assertRaises(ValueError):
                setup.arm(gates, 'openai', 'gpt-unconfigured')

    def test_heartbeat_and_real_selftest_are_separate(self):
        setup = importlib.import_module(api.PACKAGE + '.setup')
        with tempfile.TemporaryDirectory() as tmp:
            gates = self.gates(tmp, {})
            trial = api.trial_module.Trial(gates)
            trial.start()
            result = setup.selftest(gates)
            self.assertFalse(result['refused'])
            self.assertFalse(result['armed'])
            self.assertIn('real closes are allowed', result['message'])
            self.assertTrue(result['message'])
            self.assertFalse(setup.status(gates)[1]['ok'])
            with trial.store.read() as db:
                self.assertEqual(trial.store.get(db, 'trial', 'step')['step'], 1)
            setup.heartbeat(gates)
            self.assertTrue(setup.status(gates)[1]['ok'])
            setup.flush_heartbeat(gates)
            from unittest.mock import patch
            with patch.object(gates.store, 'transaction', side_effect=AssertionError('extra write')), patch.object(gates.store, 'read', side_effect=AssertionError('extra read')):
                setup.flush_heartbeat(gates)

    def test_hook_stays_seen_for_a_day(self):
        from unittest.mock import patch
        setup = importlib.import_module(api.PACKAGE + '.setup')
        with tempfile.TemporaryDirectory() as tmp:
            gates = self.gates(tmp, {})
            setup.heartbeat(gates)
            with patch.object(setup.time, 'time', return_value=setup.HEARTBEATS[str(gates.store.path)] + 3600):
                self.assertTrue(setup.status(gates)[1]['ok'])
            with patch.object(setup.time, 'time', return_value=setup.HEARTBEATS[str(gates.store.path)] + 86401):
                self.assertFalse(setup.status(gates)[1]['ok'])

    def test_missing_context_name_warns_without_blocking(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from contextvars import ContextVar
        setup = importlib.import_module(api.PACKAGE + '.setup')
        modules = {
            'tools.terminal_scope': SimpleNamespace(scope=ContextVar('renamed_scope')),
            'agent.delegation_context': SimpleNamespace(child=ContextVar('hermes_delegated_child_context')),
            'hermes_cli.kanban_db': SimpleNamespace(board=ContextVar('hermes_kanban_current_board_override'),
                                                  pin=ContextVar('hermes_kanban_pin_first_board_resolution')),
        }
        with tempfile.TemporaryDirectory() as tmp:
            gates = self.gates(tmp, {})
            with patch.object(setup.importlib, 'import_module', side_effect=modules.__getitem__):
                status = setup.status(gates)
                warning = next(row for row in status if row['id'] == 'hermes_version')
                self.assertFalse(warning['ok'])
                self.assertIn('hermes_terminal_scope', warning['hint'])
                self.assertFalse(gates.armed())
            self.assertFalse(any(row['id'] == 'hermes_version' for row in setup.status(gates)))

class ReceiptUsageTests(unittest.TestCase):
    def test_tokens_duration_and_markdown(self):
        from test_gate import Gates, Store
        with tempfile.TemporaryDirectory() as tmp:
            gates = Gates(Store(Path(tmp)), lambda k, d: d)
            with gates.store.transaction() as db:
                gates.store.put(db, 'binding', 'usage-child', dict(child_session='child', duty='build'))
            gates.usage('child', 'a', dict(input_tokens=10, output_tokens=5))
            gates.usage('child', 'a', dict(total_tokens=15))
            gates.usage('child', 'b', dict(total_tokens=7))
            with gates.store.read() as db:
                self.assertEqual(gates.store.get(db, 'usage', 'child')['tokens'], 22)
            trial = api.trial_module.Trial(gates)
            trial.start()
            for _ in range(6):
                trial.step()
            with trial.store.read() as db:
                receipt = trial.gates.receipts(db, api.trial_module.KEY)[-1]
                event = trial.store.history(db, api.trial_module.KEY)[-1]
            self.assertEqual(event['elapsed_s'], receipt['elapsed_s'])
            self.assertIsNone(event['tokens'])
            text = api.projection.receipt_markdown(receipt)
            self.assertIn('PASS', text)
            self.assertIn('claude-simulated', text)
            self.assertIn('exit 0', text)

    def test_usage_only_bound_children_and_shares_observation_transaction(self):
        from unittest.mock import patch
        from test_gate import Gates, Store
        with tempfile.TemporaryDirectory() as tmp:
            gates = Gates(Store(Path(tmp)), lambda k, d: d)
            gates.observe('parent', 'openai', 'gpt-test', 'gpt-test')
            with patch.object(gates.store, 'transaction', side_effect=AssertionError('unrelated write')):
                gates.usage('parent', 'a', dict(total_tokens=5))
                gates.observe('parent', 'openai', 'gpt-test', 'gpt-test', request_id='a', usage=dict(total_tokens=5))
            with gates.store.transaction() as db:
                gates.store.put(db, 'binding', 'b', dict(child_session='child', duty='repair'))
            with patch.object(gates.store, 'transaction', wraps=gates.store.transaction) as transaction:
                gates.observe('child', 'openai', 'gpt-test', 'gpt-test', request_id='b', usage=dict(total_tokens=12))
                self.assertEqual(transaction.call_count, 1)
            with gates.store.read() as db:
                self.assertIsNone(gates.store.get(db, 'usage', 'parent'))
                self.assertEqual(gates.store.get(db, 'usage', 'child')['tokens'], 12)

    def test_pass_can_accept_removed_assertions_with_observed_usage(self):
        from test_gate import Gates, Store, seed
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            seed(repo)
            test = repo / 'test_total.py'
            test.write_text('assert total([]) == 0\n')
            from test_gate import git
            git(repo, 'add', '.')
            git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'test baseline')
            cfg = dict(repo=str(repo), check='true', verifier=dict(provider='anthropic', model='claude-test'))
            gates = Gates(Store(Path(tmp) / 'data'), lambda k, d: cfg.get(k, d), host_config=lambda: dict(delegation=dict(backend='shadow-army')))
            key = gates.key_for('card')
            gates.inspect(key)
            test.unlink()
            gates.observe('parent', 'openai', 'gpt-test')
            task = gates.prepare([dict(card='card', duty='verify')], 'parent')[0]
            self.assertIn('1 assertion removed', task['goal'])
            gates.route(task, {})
            gates.start('parent', 'child', task['goal'])
            gates.observe('child', 'anthropic', 'claude-test', 'claude-test')
            gates.usage('child', 'request', dict(total_tokens=42))
            gates.terminal_start('child', 'check', dict(command='true', workdir=str(repo)))
            gates.terminal_end('child', 'check', dict(exit_code=0, output=''))
            self.assertEqual(gates.verdict(key, 'PASS', [], 'child')['state'], 'PASS')
            with gates.store.read() as db:
                event = gates.store.history(db, key)[-1]
            self.assertTrue(event['accepted_removed'])
            self.assertEqual(event['tokens'], 42)
            self.assertEqual(event['weakened_count'], 1)
            self.assertGreaterEqual(event['elapsed_s'], 0)

class ModelFamilyTests(unittest.TestCase):
    def test_nous_base_families_both_directions_and_override(self):
        policy = importlib.import_module(api.PACKAGE + '.policy')
        for a, b in [('nous', 'meta'), ('meta', 'nous'), ('nous', 'qwen'), ('qwen', 'nous')]:
            self.assertTrue(policy.related(a, b))
        self.assertFalse(policy.related('meta', 'qwen'))
        overridden = policy.family('local', 'hermes-test', {'local/hermes-test': 'mistral'})
        self.assertFalse(policy.related(overridden, 'meta'))
        from test_gate import Gates, Store, seed
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            seed(repo)
            for builder, verifier in [('hermes-test', 'llama-test'), ('qwen-test', 'hermes-test')]:
                cfg = dict(repo=str(repo), verifier=dict(provider='local', model=verifier))
                gates = Gates(Store(Path(tmp) / builder), lambda k, d: cfg.get(k, d))
                gates.observe('parent', 'local', builder)
                task = gates.prepare([dict(card='test', duty='verify')], 'parent')[0]
                with self.assertRaisesRegex(ValueError, 'shares a builder family'):
                    gates.route(task, {})

    def test_orchestrator_parent_exclusion_and_changed_parent_inclusion(self):
        from unittest.mock import patch
        from test_gate import Gates, Store, seed
        for tool in (None, 'read_file', 'search_files', 'delegate_task', 'terminal', 'write_file', 'patch'):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp) / 'repo'
                seed(repo)
                cfg = dict(repo=str(repo), verifier=dict(provider='anthropic', model='claude-test'))
                gates = Gates(Store(Path(tmp) / 'data'), lambda k, d: cfg.get(k, d))
                gates.observe('parent', 'anthropic', 'claude-test')
                key = gates.key_for('card')
                build = gates.prepare([dict(card='card', duty='build')], 'parent')[0]
                gates.route(build, dict(provider='openai', model='gpt-test'))
                gates.start('parent', 'builder', build['goal'])
                gates.observe('builder', 'openai', 'gpt-test')
                gates.repository_tool_seen('parent', tool)
                with patch.object(gates.store, 'transaction', side_effect=AssertionError('repeat write')):
                    gates.repository_tool_seen('parent', tool)
                task = gates.prepare([dict(card='card', duty='verify')], 'parent')[0]
                if tool in ('terminal', 'write_file', 'patch'):
                    with self.assertRaisesRegex(ValueError, 'parent counts.*repository-changing'):
                        gates.route(task, {})
                else:
                    self.assertEqual(gates.route(task, {})['model'], 'claude-test')

    def test_related_worker_family_still_refused_and_lone_parent_explained(self):
        from unittest.mock import patch
        from test_gate import Gates, Store, seed
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            seed(repo)
            cfg = dict(repo=str(repo), verifier=dict(provider='local', model='llama-test'))
            gates = Gates(Store(Path(tmp) / 'data'), lambda k, d: cfg.get(k, d))
            gates.observe('parent', 'anthropic', 'claude-test')
            gates.observe('worker', 'local', 'hermes-test')
            with patch.object(importlib.import_module(Gates.__module__.rsplit('.', 1)[0] + '.boards'), 'worker_sessions', return_value={'worker'}):
                task = gates.prepare([dict(card='card', duty='verify')], 'parent')[0]
                with self.assertRaisesRegex(ValueError, 'related to the nous builder'):
                    gates.route(task, {})
            cfg['verifier'] = dict(provider='anthropic', model='claude-test')
            task = gates.prepare([dict(card='card', duty='verify')], 'parent')[0]
            with self.assertRaisesRegex(ValueError, 'parent counts.*no other'):
                gates.route(task, {})

class WorkspaceAdvisoryResilienceTests(unittest.TestCase):
    def test_card_free_review_pass_then_edit_stales_without_blocking(self):
        from test_gate import Gates, Store, seed
        policy = importlib.import_module(api.PACKAGE + '.policy')
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'repo'
            seed(repo)
            cfg = dict(repo=str(repo), check='true', verifier=dict(provider='anthropic', model='claude-test'))
            gates = Gates(Store(Path(tmp) / 'data'), lambda k, d: cfg.get(k, d), host_config=lambda: dict(delegation=dict(backend='shadow-army')))
            gates.observe('parent', 'openai', 'gpt-test')
            task = gates.prepare([dict(duty='verify', goal='Review this workspace')], 'parent')[0]
            self.assertEqual(task['board'], 'workspace:')
            gates.route(task, {})
            gates.start('parent', 'child', task['goal'])
            gates.observe('child', 'anthropic', 'claude-test', 'claude-test')
            gates.terminal_start('child', 'check', dict(command='true', workdir=str(repo)))
            gates.terminal_end('child', 'check', dict(exit_code=0, output=''))
            key = gates.verdict_key('child')
            self.assertEqual(gates.verdict(key, 'PASS', [], 'child')['state'], 'PASS')
            from unittest.mock import patch
            with patch.object(api.boards, 'cards', return_value=([], [])):
                card = api.projection.snapshot(gates)['cards'][0]
            self.assertEqual(card['kind'], 'workspace')
            (repo / 'app.txt').write_text('edited')
            self.assertEqual(gates.inspect(key)['state'], 'STALE')

class SetupArmingApiTests(unittest.TestCase):
    def test_arm_api_saves_only_plugin_settings_in_disposable_profile(self):
        import json
        import os
        from unittest.mock import patch
        from hermes_cli.config import load_config_readonly
        with tempfile.TemporaryDirectory() as tmp:
            host = dict(model=dict(provider='openai', default='gpt-test'),
                        delegation=dict(backend='default'),
                        fallback_providers=[dict(provider='anthropic', model='claude-test')])
            (Path(tmp) / 'config.yaml').write_text(json.dumps(host))
            with patch.dict(os.environ, HERMES_HOME=tmp, HERMES_KANBAN_HOME=tmp):
                from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
                setup = importlib.import_module(api.PACKAGE + '.setup')
                setup.register_context(api.backend.current_gates(), PluginContext(PluginManifest(name='shadow-army'), get_plugin_manager()))
                result = api.arm_setup(dict(provider='anthropic', model='claude-test'))
                settings = load_config_readonly()['plugins']['entries']['shadow-army']['settings']
                self.assertEqual(settings['verifier_provider'], 'anthropic')
                self.assertEqual(settings['verifier_model'], 'claude-test')
                self.assertEqual(load_config_readonly()['delegation']['backend'], 'default')
                self.assertFalse(result['armed'])
                self.assertFalse(result['setup_actions']['can_set_routing'])
