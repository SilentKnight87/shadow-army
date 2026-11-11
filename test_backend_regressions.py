import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_adapter import api
from test_gate import seed, fingerprint, git, GateKey, Gates, Store


class WorkspaceScopeRegressions(unittest.TestCase):
    def test_filesystem_walk_stops_at_home_and_device_boundary(self):
        from types import SimpleNamespace
        import sys
        boards = sys.modules[Gates.__module__].boards
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            home = root / 'home'
            workspace = home / 'project' / 'nested'
            workspace.mkdir(parents=True)
            (root / '.git').mkdir()
            with patch.object(Path, 'home', return_value=home):
                self.assertFalse(boards.in_work_tree(workspace))
                git(home / 'project', 'init')
                self.assertTrue(boards.in_work_tree(workspace))
                original_stat = Path.stat
                def stat(path, *args, **kwargs):
                    if path == home / 'project':
                        return SimpleNamespace(st_dev=-1)
                    return original_stat(path, *args, **kwargs)
                with patch.object(Path, 'stat', stat):
                    self.assertFalse(boards.in_work_tree(workspace))


class FingerprintRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        seed(self.repo)

    def test_index_flags_include_worktree_bytes_after_tracked_edit(self):
        for flag in ('assume-unchanged', 'skip-worktree'):
            with self.subTest(flag=flag):
                before = fingerprint(self.repo)
                git(self.repo, 'update-index', '--' + flag, 'app.txt')
                (self.repo / 'app.txt').write_text('unchecked edit')
                self.assertNotEqual(fingerprint(self.repo), before)
                git(self.repo, 'update-index', '--no-' + flag, 'app.txt')
                self.assertNotEqual(fingerprint(self.repo), before)
                git(self.repo, 'checkout', '--', 'app.txt')

    def test_subdirectory_hashes_actual_files_across_root(self):
        (self.repo / 'pkg').mkdir()
        (self.repo / 'new.py').write_text('root decoy')
        (self.repo / 'pkg/new.py').write_text('nested original')
        before = fingerprint(self.repo / 'pkg')
        self.assertEqual(before, fingerprint(self.repo))
        (self.repo / 'pkg/new.py').write_text('nested changed')
        self.assertNotEqual(fingerprint(self.repo / 'pkg'), before)
        (self.repo / 'new.py').unlink()
        self.assertEqual(fingerprint(self.repo / 'pkg'), fingerprint(self.repo))

    def test_lfs_and_configured_filter_do_not_refuse(self):
        (self.repo / '.gitattributes').write_text('*.txt filter=lfs diff=lfs merge=lfs -text\n')
        git(self.repo, 'config', 'filter.lfs.clean', 'cat')
        self.assertTrue(fingerprint(self.repo))


class StorageAndDisplayRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        seed(self.root / 'repo')
        env = patch.dict(os.environ, HERMES_HOME=str(self.root), HERMES_KANBAN_HOME=str(self.root))
        env.start()
        self.addCleanup(env.stop)

    def test_subprocesses_do_not_hold_writer_lock(self):
        store = Store(self.root / 'data')
        cfg = dict(repo=str(self.root / 'repo'), check='true', verifier=dict(provider='anthropic', model='claude-test'))
        gates = Gates(store, lambda k, d: cfg.get(k, d), host_config=lambda: {'delegation': {'backend': 'shadow-army'}})
        gates.observe('parent', 'openai', 'gpt-test')
        original = gates.tree
        def unlocked(key):
            with store.transaction() as writer:
                store.put(writer, 'probe', 'key', {})
            return original(key)
        with patch.object(gates, 'tree', side_effect=unlocked):
            task = gates.prepare([dict(card='card', duty='verify')], 'parent')[0]
            gates.route(task, {})
            gates.start('parent', 'child', task['goal'])
            gates.observe('child', 'anthropic', 'claude-test', 'claude-test')
            gates.terminal_start('child', 'call', dict(command='true', workdir=cfg['repo']))
            gates.terminal_end('child', 'call', dict(exit_code=0, output='OK'))
            self.assertEqual(gates.verdict(GateKey('default', 'card'), 'PASS', [], 'child')['state'], 'PASS')
        trial = api.trial_module.Trial(gates)
        import importlib
        module = importlib.import_module(trial.__class__.__module__)
        original_git, original_run = module.git, module.subprocess.run
        def unlocked_git(*args, **kw):
            with store.transaction() as writer:
                store.put(writer, 'probe', 'git', {})
            return original_git(*args, **kw)
        def unlocked_run(*args, **kw):
            with store.transaction() as writer:
                store.put(writer, 'probe', 'run', {})
            return original_run(*args, **kw)
        with patch.object(module, 'git', side_effect=unlocked_git), patch.object(module.subprocess, 'run', side_effect=unlocked_run):
            trial.start()
            for _ in range(6):
                trial.step()

    def test_storage_error_keeps_last_good_cards(self):
        with sqlite3.connect(self.root / 'kanban.db') as db:
            db.executescript('CREATE TABLE tasks(id,title,assignee,status,result,workspace_path);')
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?)', ('c1', 'Keep visible', 'owner', 'ready', '', str(self.root / 'repo')))
        good = api.get_snapshot()
        self.assertEqual(len(good['cards']), 1)
        with patch.object(api.projection, 'snapshot', side_effect=sqlite3.OperationalError('database is locked')):
            failed = api.get_snapshot()
        self.assertEqual(failed['cards'], good['cards'])
        self.assertTrue(failed['errors'])
        with patch.object(api.boards, 'cards', return_value=([], ['Board default: read failed'])):
            board_error = api.get_snapshot()
        self.assertEqual(board_error['cards'], good['cards'])
        self.assertTrue(board_error['errors'])
        trial = api.start_demo()
        with patch.object(api.projection, 'snapshot', side_effect=sqlite3.OperationalError('database is locked')):
            trial_error = api.get_snapshot(True)
        self.assertEqual(trial_error['cards'], trial['cards'])
        self.assertTrue(trial_error['errors'])

    def test_display_preserves_unittest_and_model_ids_line_by_line(self):
        output = '\n'.join(['..', '-' * 70, 'Ran 2 tests in 0.001s', '', 'OK'])
        unsafe = '\n'.join(['password=synthetic', 'https://u:p@example.test', 'person@example.test', '/Users/person/private'])
        clean = api.projection.check_view(dict(command='python -m unittest', exit_code=0, output_excerpt=output + '\n' + unsafe))
        self.assertEqual(clean['output_excerpt'], output)
        for model in ('anthropic/claude-sonnet-4-5-20250929', 'moonshotai/kimi-k2-instruct-0905'):
            self.assertEqual(api.clean(model), model)

    def test_setup_truthfully_reports_default_and_per_card_hints(self):
        settings = dict(repo=str(self.root / 'repo'), verifier={'provider': 'anthropic', 'model': 'claude-test'})
        gates = Gates(Store(self.root / 'data'), lambda k, d: settings.get(k, d))
        with sqlite3.connect(self.root / 'kanban.db') as db:
            db.executescript('CREATE TABLE tasks(id,title,assignee,status,result,workspace_path);')
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?)', ('c1', 'Has repo', 'owner', 'ready', '', str(self.root / 'repo')))
        data = api.projection.snapshot(gates)
        self.assertFalse(data['armed'])
        setup = {s['id']: s for s in data['setup']}
        self.assertTrue(setup['repo']['ok'])
        self.assertTrue(setup['check']['ok'])
        self.assertEqual(setup['check']['hint'], 'Using the default: python3 -m unittest discover -s tests')
        self.assertEqual(data['setup_url'], '/settings?tab=plugins&agent=shadow-army')
        self.assertFalse(setup['backend']['ok'])
        self.assertEqual(data['cards'][0]['gate']['state'], 'UNARMED')
        self.assertIn('Finish setup', data['cards'][0]['hint'])


class DisclosureRegressions(unittest.TestCase):
    def test_readme_names_machine_effects_threat_and_limits(self):
        readme = (Path(__file__).parent / 'shadow-army/README.md').read_text()
        for phrase in ('What it does on your machine', 'Runs Git', 'Kanban databases read-only',
                       'SQLite file under profile plugin data', 'bundled unittest', 'no network requests',
                       'no telemetry', 'not a sandbox', 'Ignored files and submodule contents',
                       'sabotaged environment can fool a check', 'Local terminal only',
                       'not atomic', 'Session compaction can hide earlier routes', 'proxy'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, readme + (Path(__file__).parent / 'shadow-army/REFERENCE.md').read_text())
        self.assertNotIn('No index mutation, external diff, text conversion, Git shell command', readme)
