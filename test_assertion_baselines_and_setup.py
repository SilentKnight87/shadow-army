"""Assertion baseline, setup, workspace, and trial regressions."""
import importlib
from contextvars import ContextVar
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_adapter import api
from test_gate import Gates, Store, seed, git

assertions = importlib.import_module(api.PACKAGE + '.assertions')
setup = importlib.import_module(api.PACKAGE + '.setup')
service = importlib.import_module(Gates.__module__)


class AssertionBaselineAndSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        seed(self.repo)
        self.test = self.repo / 'test_cart.py'
        self.test.write_text('assert total == 0\nassert count == 1\n')
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'tests')
        self.cfg = dict(repo=str(self.repo), check='true', verifier=dict(provider='anthropic', model='claude-test'))
        from hermes_cli.config import DEFAULT_CONFIG
        import copy
        self.host = copy.deepcopy(DEFAULT_CONFIG)
        self.host['delegation'] = dict(backend='shadow-army')
        self.gates = Gates(Store(self.root / 'data'), lambda k, d: self.cfg.get(k, d), host_config=lambda: self.host)
        self.key = self.gates.key_for('card')

    def test_rename_changes_assertions_without_removal(self):
        self.test.write_text('assert cart_total == 0\nassert cart_count == 1\n')
        evidence = self.gates.assertion_evidence(self.key, record=True)
        self.assertEqual(evidence['weakened_count'], 0)
        self.assertEqual(evidence['changed_count'], 2)
        self.assertEqual({r['kind'] for r in evidence['weakened']}, {'changed'})
        self.assertEqual(assertions.summary(evidence), '2 assertion lines changed')
        for _ in range(2):
            self.assertEqual(self.gates.assertion_evidence(self.key), evidence)
        receipt = dict(verdict='PASS', tree='abc', **evidence, accepted_removed=False)
        text = api.projection.receipt_markdown(receipt)
        self.assertIn('2 assertion lines changed', text)
        self.assertNotIn('accepted', text)
        self.assertNotIn('None', text)
        self.assertNotIn('Review:', text)

    def test_first_verify_sees_dirty_deletion_from_head(self):
        self.test.write_text('assert total == 0\n')
        self.gates.observe('parent', 'openai', 'gpt-test')
        task = self.gates.prepare([dict(card='card', duty='verify')], 'parent')[0]
        self.assertIn('1 assertion removed', task['goal'])
        evidence = self.gates.assertion_evidence(self.key)
        self.assertEqual(evidence['weakened_count'], 1)
        self.assertEqual(evidence['weakened'][0]['kind'], 'removed')

    def test_head_unavailable_falls_back_but_first_result_is_null(self):
        original = service.assertions.inventory
        def scan(repo, **kwargs):
            return None if kwargs.get('revision') else original(repo, **kwargs)
        with patch.object(service.assertions, 'inventory', side_effect=scan):
            self.assertIsNone(self.gates.assertion_evidence(self.key, record=True)['weakened'])
            self.test.write_text('assert total == 0\n')
            self.assertEqual(self.gates.assertion_evidence(self.key)['weakened_count'], 1)

    def test_failed_first_scan_recovers_and_failed_results_are_cached(self):
        with patch.object(service.assertions, 'inventory', return_value=None) as scan:
            self.gates.assertion_evidence(self.key, record=True, tree='overbudget')
            self.gates.assertion_evidence(self.key, record=True, tree='overbudget')
            self.gates.assertion_evidence(self.key, record=True, tree='overbudget')
            # HEAD may retry, but the working tree failed scan runs once.
            self.assertEqual(sum(call.kwargs.get('revision') is None for call in scan.call_args_list), 1)
        self.assertEqual(self.gates.assertion_evidence(self.key, record=True)['weakened_count'], 0)
        self.test.write_text('assert total == 0\n')
        self.assertEqual(self.gates.assertion_evidence(self.key)['weakened_count'], 1)

    def test_legacy_failed_start_keeps_a_successful_review_baseline(self):
        import json
        reviewed = [dict(file='test_cart.py', line=1, text='assert total == 0')]
        with self.gates.store.transaction() as db:
            self.gates.store.put(db, 'assertions', json.dumps(self.key.values()), dict(start=None, review=reviewed))
        self.test.write_text('assert total == 0\n')
        evidence = self.gates.assertion_evidence(self.key, record=True)
        self.assertEqual(evidence['weakened_count'], 0)
        self.assertEqual(evidence['weakened'], [])

    def test_empty_inventory_and_whole_test_words(self):
        for name in ('src/latest/file.py', 'inspector.ts'):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('assert False\n')
        self.test.write_text('shouldRetry(x)\nshouldComponentUpdate(x)\nthing.expect("x")\n')
        self.assertEqual(assertions.inventory(self.repo), [])
        self.assertEqual(assertions.removed([], []), [])

    def test_actual_repository_exceeds_default_budget(self):
        self.test.write_bytes(b'#' * (4 * 1024 * 1024 + 1))
        self.assertIsNone(assertions.inventory(self.repo))
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'large test')
        self.assertIsNone(assertions.inventory(self.repo, revision='HEAD'))

    def test_selftest_uses_real_arming_and_configuration(self):
        result = setup.selftest(self.gates)
        self.assertTrue(result['armed'])
        self.assertTrue(result['refused'])
        self.cfg['verifier'] = None
        self.assertFalse(setup.selftest(self.gates)['refused'])

    def test_session_folder_precedes_scope_and_settings_no_process_fallback(self):
        other = self.root / 'other'
        seed(other)
        self.gates.observe('parent', 'openai', 'gpt-test')
        from agent.runtime_cwd import _SESSION_CWD
        from tools.terminal_scope import install_and_reset_profile_terminal_scope
        profile = self.root / 'profile'
        profile.mkdir()
        (profile / 'config.yaml').write_text('terminal:\n  cwd: ' + str(self.repo) + '\n')
        token = _SESSION_CWD.set(str(other))
        try:
            with install_and_reset_profile_terminal_scope(profile):
                for _ in range(3):
                    task = self.gates.prepare([dict(duty='verify')], 'parent')[0]
                    self.assertIn(str(other), task['goal'])
                _SESSION_CWD.set(str(self.root))
                for _ in range(3):
                    task = self.gates.prepare([dict(duty='verify')], 'parent')[0]
                    self.gates.route(task, {})
                    self.assertIn(str(self.repo), task['goal'])
            self.cfg['repo'] = None
            (profile / 'config.yaml').write_text('{}')
            with install_and_reset_profile_terminal_scope(profile):
                for _ in range(3):
                    task = self.gates.prepare([dict(duty='verify')], 'parent')[0]
                    with self.assertRaisesRegex(ValueError, 'Git folder'):
                        self.gates.route(task, {})
        finally:
            _SESSION_CWD.reset(token)

    def test_workspace_board_slug_remains_a_real_board(self):
        import json
        import os
        import sqlite3
        board = self.root / 'kanban/boards/workspace'
        board.mkdir(parents=True)
        (board / 'board.json').write_text(json.dumps(dict(slug='workspace', name='Workspace board')))
        with sqlite3.connect(board / 'kanban.db') as db:
            db.execute('CREATE TABLE tasks (id TEXT, title TEXT, assignee TEXT, status TEXT, result TEXT, workspace_kind TEXT, workspace_path TEXT)')
            db.execute("INSERT INTO tasks VALUES ('scratch', 'Scratch', '', 'open', '', 'scratch', NULL)")
            db.execute("INSERT INTO tasks VALUES ('code', 'Code', '', 'open', '', 'worktree', ?)", (str(self.repo),))
        self.cfg['repo'] = None
        with patch.dict(os.environ, HERMES_KANBAN_HOME=str(self.root)):
            scratch = self.gates.key_for('scratch', 'workspace')
            self.assertIsNone(self.gates.close(scratch))
            self.gates.observe('parent', 'openai', 'gpt-test')
            task = self.gates.prepare([dict(card='code', board='workspace', duty='verify')], 'parent')[0]
            self.assertEqual(task['board'], 'workspace')
            self.assertEqual(self.gates.route(task, {})['model'], 'claude-test')

    def test_deleted_workspace_is_pruned_and_setup_failure_does_not_hide_cards(self):
        with self.gates.store.transaction() as db:
            self.gates.store.put(db, 'workspace', 'gone', dict(repo=str(self.root / 'gone'), title='gone'))
        with patch.object(api.boards, 'cards', return_value=([], [])), patch.object(api.projection.setup_service, 'status', side_effect=ValueError()), patch.object(api.projection.setup_service, 'actions', side_effect=ValueError()):
            value = api.projection.snapshot(self.gates)
        self.assertEqual(value['cards'], [])
        self.assertFalse(value['setup_actions']['can_arm'])

    def test_bad_optional_routes_and_unregistered_context(self):
        self.host.update(model=dict(provider='openai', default='gpt-test'), fallback_providers={'bad': 'mapping'})
        self.host['delegation'].update(provider='anthropic', model='claude-test', backends=dict(model_map=None))
        value = setup.actions(self.gates)
        self.assertFalse(value['can_arm'])
        self.assertIn('settings form', value['reason'])
        self.assertTrue(value['routes'])

    def test_atomic_route_save_failure_leaves_route_unchanged(self):
        from types import SimpleNamespace
        self.host['model'] = dict(provider='openai', default='gpt-test')
        save = unittest.mock.Mock(side_effect=OSError('failed'))
        setup.register_context(self.gates, SimpleNamespace(set_config=save))
        with self.assertRaises(OSError):
            setup.arm(self.gates, 'anthropic', 'claude-test')
        save.assert_called_once_with('verifier_provider', 'anthropic')
        self.assertEqual(self.gates.config()['verifier'], self.cfg['verifier'])

    def test_trial_checks_use_terminal_hooks_and_stale_receipt_is_absent(self):
        trial = api.trial_module.Trial(self.gates)
        trial.start()
        with patch.object(trial.gates, 'terminal_start', wraps=trial.gates.terminal_start) as start, patch.object(trial.gates, 'terminal_end', wraps=trial.gates.terminal_end) as end:
            for _ in range(6):
                trial.step()
            self.assertEqual(start.call_count, 2)
            self.assertEqual(end.call_count, 2)
        self.assertTrue(api.projection.snapshot(self.gates, True)['cards'][0]['receipt_markdown'].startswith('Simulated trial'))
        trial.tamper()
        self.assertIsNone(api.projection.snapshot(self.gates, True)['cards'][0]['receipt_markdown'])

    def test_reads_open_no_write_transactions(self):
        from unittest.mock import Mock
        keys = [self.gates.key_for('card' + str(i)) for i in range(15)]
        for key in keys:
            self.gates.assertion_evidence(key, record=True)
        rows = [dict(id=k.card_id, board=k.board, title='Code', status='open') for k in keys]
        with patch.object(api.boards, 'cards', return_value=(rows, [])), patch.object(self.gates.store, 'transaction', wraps=self.gates.store.transaction) as tx:
            for _ in range(3):
                api.projection.snapshot(self.gates)
            self.assertEqual(tx.call_count, 0)

    def test_workspace_defaults_placeholders_and_printed_id(self):
        for value in (object(), '.', './', 'auto', 'cwd', None):
            with self.subTest(value=value), patch.dict(service.os.environ, {}, clear=True):
                self.host['terminal']['cwd'] = value
                task = self.gates.prepare([dict(duty='verify')], 'parent')[0]
                key = self.gates.key_for(task['card'], task['board'])
                self.assertEqual(Path(self.gates.repository(key)).resolve(), self.repo.resolve())
                self.assertIn(task['card'], self.gates.inspect(key)['next_step'])

    def test_unobserved_builders_fall_back_to_requester(self):
        self.gates.observe('parent', 'openai', 'gpt-test')
        for scenario in ('other-profile', 'pruned', 'never-requested'):
            with self.subTest(scenario=scenario):
                with self.gates.store.transaction() as db:
                    if scenario == 'never-requested':
                        self.gates.store.put(db, 'binding', 'build', dict(board=self.key.board, card_id=self.key.card_id, duty='build', child_session=scenario))
                    if scenario == 'pruned':
                        self.gates.store.put(db, 'session', scenario, dict(routes=[]))
                with patch.object(service.boards, 'worker_sessions', return_value={scenario}):
                    task = self.gates.prepare([dict(card='card', duty='verify')], 'parent')[0]
                    self.assertEqual(self.gates.route(task, {})['model'], 'claude-test')

    def test_wrong_settings_shapes_leave_scratch_close_allowed(self):
        for field in (*service.SETTING_DEFAULTS, 'verifier_route', 'verifier_fallbacks', 'verifier_provider', 'verifier_model', 'pass_lifetime_minutes'):
            for value in ([], {}, ['x'], {'provider': 'x'}, 3, True):
                with self.subTest(field=field, value=value):
                    original = dict(self.cfg)
                    self.cfg.update(repo=None)
                    self.cfg[field] = value
                    with patch.object(service.boards, 'repository', return_value=None):
                        self.assertIsNone(self.gates.close(self.key))
                    setup.status(self.gates)
                    self.cfg.clear()
                    self.cfg.update(original)

    def test_form_route_overrides_legacy_pane_route(self):
        self.cfg.update(verifier_route={'provider': 'anthropic', 'model': 'claude-old'}, verifier_provider='google', verifier_model='gemini-new')
        self.assertEqual(self.gates.config()['verifier']['model'], 'gemini-new')

    def test_bad_route_settings_name_field_and_raw_arm_identity(self):
        from types import SimpleNamespace
        self.cfg.update(verifier_fallbacks=[{'provider': 'x', 'model': ['bad']}])
        self.assertEqual(self.gates.config()['verifier']['fallbacks'], [])
        self.assertIn('verifier_fallbacks', self.gates.config()['settings_errors'])
        self.assertTrue(any(item['label'] == 'verifier_fallbacks' and not item['ok'] for item in setup.status(self.gates)))
        save = unittest.mock.Mock()
        setup.register_context(self.gates, SimpleNamespace(set_config=save))
        self.host['model'] = dict(provider='openai', default='gpt-test')
        self.host['fallback_providers'] = [dict(provider='https://user@example.test', model='claude-test')]
        setup.arm(self.gates, 'https://user@example.test', 'claude-test')
        self.assertEqual(save.call_args_list[0].args, ('verifier_provider', 'https://user@example.test'))
    def test_trial_finishes_with_remote_profile_and_checks_once(self):
        with patch.dict(service.os.environ, TERMINAL_ENV='docker'):
            trial = api.trial_module.Trial(self.gates)
            with patch.object(trial, 'check', wraps=trial.check) as check:
                trial.start()
                for _ in range(6):
                    trial.step()
                trial.tamper()
                for _ in range(3):
                    trial.step()
                self.assertEqual(check.call_count, 10)

    def test_removed_assertions_are_paired_only_with_same_file_additions(self):
        def row(file, text):
            return dict(file=file, line=1, text=text)
        before = [row('test_a.py', 'assert old'), row('test_z.py', 'assert deleted')]
        after = [row('test_a.py', 'assert renamed'), row('test_new.py', 'assert extra'), row('test_new.py', 'assert another')]
        result = assertions.removed(before, after)
        self.assertEqual([r['kind'] for r in result], ['changed', 'removed'])
        self.assertEqual(assertions.removed([], []), [])

    def test_first_baseline_includes_uncommitted_tests_and_committed_deletions(self):
        new = self.repo / 'test_bug.py'
        new.write_text('assert bug == 0\n')
        self.test.write_text('assert total == 0\n')
        self.gates.assertion_evidence(self.key, record=True)
        new.unlink()
        result = self.gates.assertion_evidence(self.key)
        self.assertEqual(result['weakened_count'], 2)

    def test_fresh_real_web_server_offers_the_settings_form_before_the_first_chat(self):
        import json
        import os
        import shutil
        import subprocess
        import sys
        home = self.root / 'fresh-home'
        home.mkdir()
        shutil.copytree(Path(__file__).parent / 'shadow-army', home / 'plugins/shadow-army')
        (home / 'config.yaml').write_text(json.dumps(dict(
            plugins=dict(enabled=['shadow-army']), model=dict(provider='openai', default='gpt-test'),
            delegation=dict(provider='anthropic', model='claude-test'))))
        code = """
from fastapi.testclient import TestClient
from hermes_cli.web_server import app
client = TestClient(app)
headers = {'Authorization': 'Bearer synthetic'}
snapshot = client.get('/api/plugins/shadow-army/snapshot', headers=headers)
assert snapshot.status_code == 200, snapshot.text
actions = snapshot.json()['setup_actions']
assert not actions['can_arm'] and 'settings form' in actions['reason'], snapshot.text
assert actions['routes'], snapshot.text
response = client.post('/api/plugins/shadow-army/setup/arm', headers=headers, json={'provider': 'anthropic', 'model': 'claude-test'})
assert response.status_code == 400, response.text
"""
        env = dict(os.environ, HERMES_HOME=str(home), HERMES_KANBAN_HOME=str(home), HERMES_DASHBOARD_SESSION_TOKEN='synthetic')
        result = subprocess.run([sys.executable, '-B', '-c', code], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_receipt_uses_response_family_and_final_tokens(self):
        self.gates.observe('parent', 'openai', 'gpt-test')
        task = self.gates.prepare([dict(card='card', duty='verify')], 'parent')[0]
        self.gates.route(task, {})
        self.gates.start('parent', 'child', task['goal'])
        self.gates.observe('child', 'anthropic', 'claude-test', 'gemini-test', request_id='first', usage={'total_tokens': 10})
        self.gates.terminal_start('child', 'check', dict(command='true', workdir=str(self.repo)))
        self.gates.terminal_end('child', 'check', dict(exit_code=0))
        self.gates.verdict(self.key, 'PASS', [], 'child')
        self.gates.usage('child', 'final', {'total_tokens': 7})
        self.gates.stop('child')
        with self.gates.store.read() as db:
            receipt = self.gates.receipts(db, self.key)[0]
        self.assertEqual(receipt['verifier']['family'], 'google')
        self.assertEqual(receipt['tokens'], 17)
        receipt['check']['command'] = 'python /Users/someone/check.py /home/someone/test.py ~/test.py'
        markdown = api.projection.receipt_markdown(receipt)
        self.assertIn('Check: python ~/check.py ~/test.py ~/test.py (exit 0)', markdown)

    def test_unborn_repository_not_ready_and_selftest_does_not_broadcast(self):
        unborn = self.root / 'unborn'
        unborn.mkdir()
        git(unborn, 'init')
        self.cfg['repo'] = str(unborn)
        with patch.object(api.boards, 'cards', return_value=([], [])):
            value = api.projection.snapshot(self.gates)
        self.assertFalse(value['setup'][0]['ok'])
        self.gates.store.on_event = unittest.mock.Mock()
        setup.selftest(self.gates)
        self.gates.store.on_event.assert_not_called()
