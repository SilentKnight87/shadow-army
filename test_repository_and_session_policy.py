from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import unittest
from unittest.mock import patch

from test_adapter import api
import test_gate
from test_gate import fingerprint, git, Gates, Store

boards = sys.modules[Gates.__module__].boards
store_module = sys.modules[Store.__module__]


class RepositoryAndSessionPolicyTests(unittest.TestCase):
    setUp = test_gate.ServiceTests.setUp
    bound = test_gate.ServiceTests.bound

    def card(self, kind='worktree', path=None, creator=None):
        with sqlite3.connect(self.root / 'kanban.db') as db:
            db.executescript('CREATE TABLE IF NOT EXISTS tasks(id,title,assignee,status,result,workspace_path,workspace_kind,session_id);')
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?)',
                       ('card', 'Code', 'owner', 'ready', '', str(path or self.repo), kind, creator))

    def pass_check(self):
        self.bound()
        self.gates.terminal_start('child', 'call', dict(command='true', workdir=str(self.repo)))
        self.gates.terminal_end('child', 'call', dict(exit_code=0, output='OK'))
        self.assertEqual(self.gates.verdict(self.key, 'PASS', [], 'child')['state'], 'PASS')

    def test_A_large_tracked_repository_fingerprints_and_gates(self):
        for size in (6, 250):
            with self.subTest(mebibytes=size):
                with (self.repo / 'tracked.bin').open('wb') as stream:
                    stream.truncate(size * 1024 * 1024)
                git(self.repo, 'add', 'tracked.bin', timeout=10)
                git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    '-c', 'commit.gpgsign=false', 'commit', '-m', 'large fixture', timeout=5)
                self.assertTrue(fingerprint(self.repo))
                (self.repo / 'app.txt').write_text('small diff')
                self.gates.store = Store(self.root / ('data-' + str(size)))
                self.gates.observe('parent', 'openai', 'gpt-test')
                self.pass_check()
                self.assertIsNone(self.gates.close(self.key))

    def test_A_global_ignore_and_interpreter_caches_leave_identity_unchanged(self):
        config = self.root / 'gitconfig'
        excludes = self.root / 'global-ignore'
        excludes.write_text('globally-ignored\n')
        config.write_text('[core]\n excludesFile = ' + str(excludes) + '\n')
        with patch.dict(os.environ, GIT_CONFIG_GLOBAL=str(config)):
            before = fingerprint(self.repo)
            (self.repo / 'globally-ignored').write_text('private')
            (self.repo / '__pycache__').mkdir()
            (self.repo / '__pycache__/app.cpython-311.pyc').write_bytes(b'cache')
            (self.repo / '.pytest_cache').mkdir()
            (self.repo / '.pytest_cache/state').write_text('cache')
            (self.repo / 'module.pyc').write_bytes(b'cache')
            (self.repo / '.DS_Store').write_bytes(b'finder')
            self.assertEqual(fingerprint(self.repo), before)

    def test_A_untracked_nested_repo_is_head_pointer(self):
        from test_gate import seed
        nested = self.repo / 'nested'
        seed(nested)
        before = fingerprint(self.repo)
        (nested / 'app.txt').write_text('nested working edit is outside pointer')
        self.assertEqual(fingerprint(self.repo), before)
        git(nested, 'add', '.')
        git(nested, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '-m', 'nested commit')
        self.assertNotEqual(fingerprint(self.repo), before)
        unborn = self.repo / 'unborn'
        unborn.mkdir()
        git(unborn, 'init')
        (unborn / 'app.txt').write_text('uncommitted nested content')
        pointer = fingerprint(self.repo)
        (unborn / 'app.txt').write_text('dirty unborn content')
        self.assertEqual(fingerprint(self.repo), pointer)

    def test_B_exact_terminal_args_are_untouched_and_cache_creation_can_pass(self):
        self.bound()
        args = dict(command='true', workdir=str(self.repo))
        self.assertIsNone(self.gates.terminal_start('child', 'call', args))
        self.assertEqual(args, dict(command='true', workdir=str(self.repo)))
        (self.repo / '__pycache__').mkdir()
        (self.repo / '__pycache__/check.pyc').write_bytes(b'created by check')
        self.gates.terminal_end('child', 'call', dict(exit_code=0))
        self.assertEqual(self.gates.verdict(self.key, 'PASS', [], 'child')['state'], 'PASS')

    def test_C_unobserved_creator_and_blank_response_do_not_poison_builder(self):
        self.card(creator='never-observed-creator')
        self.gates.observe('parent', 'openai', 'gpt-test', '  ')
        self.pass_check()
        self.gates.observe('never-observed-creator', 'anthropic', 'claude-creator')
        with self.gates.store.read() as db:
            self.assertEqual(self.gates.builders(db, self.key, 'parent'), {'openai'})

    def test_C_unknown_model_names_exact_override_and_recovers(self):
        self.gates.observe('parent', 'proxy', 'custom-worker')
        task = self.gates.prepare([dict(card='card', duty='verify')], 'parent')[0]
        with self.assertRaisesRegex(ValueError, 'family_overrides:.*proxy/custom-worker'):
            self.gates.route(task, {})
        self.cfg['family_overrides'] = {'proxy/custom-worker': 'openai'}
        self.pass_check()

    def test_C_session_retention_is_seven_days(self):
        store = self.gates.store
        now = time.time()
        with store.transaction() as db:
            for key, age in [('recent', 2), ('old', 8)]:
                store.put(db, 'session', key, dict(routes=[], observed_at=now - age * 86400))
        with store.transaction() as db:
            self.assertIsNotNone(store.get(db, 'session', 'recent'))
            self.assertIsNone(store.get(db, 'session', 'old'))

    def test_D_journal_mode_matches_hermes_version_rule(self):
        for version, mode in [((3, 50, 4), 'delete'), ((3, 51, 3), 'wal'), ((3, 50, 7), 'wal'),
                              ((3, 44, 6), 'wal'), ((3, 51, 2), 'delete')]:
            with self.subTest(version=version), patch.object(sqlite3, 'sqlite_version_info', version):
                store = Store(self.root / ('data-' + str(version)))
                with store.transaction() as db:
                    self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], mode)

    def test_D_pass_permission_write_failure_does_not_block(self):
        self.pass_check()
        with patch.object(self.gates.store, 'transaction', side_effect=sqlite3.OperationalError('database is locked')):
            self.assertIsNone(self.gates.close(self.key))

    def test_D_read_retries_a_brief_exclusive_lock(self):
        store = self.gates.store
        lock = sqlite3.connect(store.path, check_same_thread=False)
        self.addCleanup(lock.close)
        lock.execute('BEGIN EXCLUSIVE')
        release = threading.Timer(0.08, lock.rollback)
        release.start()
        try:
            with store.read() as db:
                self.assertTrue(store.rows(db, 'session'))
        finally:
            release.join()

    def test_E_host_selected_board_only_and_unreadable_target_passes_through(self):
        self.card(kind='scratch')
        self.cfg.pop('repo')
        bad = self.root / 'kanban/boards/unrelated'
        bad.mkdir(parents=True)
        (bad / 'kanban.db').write_bytes(b'foreign db')
        dispatch = lambda name, args: json.dumps({'task': {'id': 'card'}})
        self.assertEqual(boards.completion_key({'task_id': 'card'}, dispatch), self.key)
        self.assertIsNone(self.gates.close(self.key))
        with patch.object(boards, 'card_row', side_effect=sqlite3.DatabaseError('unreadable')):
            self.assertIsNone(boards.completion_key({'task_id': 'card'}, dispatch))

    def test_F_home_dotfiles_does_not_gate_notes_or_fallback_broken_worktree(self):
        from test_gate import seed
        home = self.root / 'home'
        seed(home)
        notes = home / 'notes'
        notes.mkdir()
        self.card(kind='dir', path=notes)
        self.cfg.pop('repo')
        with patch.object(Path, 'home', return_value=home):
            self.assertFalse(boards.in_work_tree(notes))
            result = self.gates.inspect(self.key)
            self.assertEqual(result['state'], 'UNGATED')
            self.assertEqual(result['reason'], "This card's folder is not a git work tree.")
        self.cfg['repo'] = str(self.repo)
        with sqlite3.connect(self.root / 'kanban.db') as db:
            db.execute("UPDATE tasks SET workspace_kind='worktree',workspace_path=?", (str(self.root / 'missing'),))
        result = self.gates.inspect(self.key)
        self.assertEqual(result['state'], 'UNKNOWN')
        self.assertIn('worktree is missing', result['reason'])
        self.assertIn('restore', result['reason'])

    def test_G_ungated_build_repair_are_default_unbound_and_retry_supersedes(self):
        self.card(kind='scratch')
        repo = self.cfg.pop('repo')
        for duty in ('build', 'repair'):
            task = self.gates.prepare([dict(card='card', duty=duty, goal='ordinary')], 'parent')[0]
            self.assertEqual(task['goal'], 'ordinary')
            self.assertEqual(self.gates.route(task, {'model': 'gpt-default'}), {'model': 'gpt-default'})
            with self.gates.store.read() as db:
                self.assertEqual(self.gates.store.rows(db, 'binding'), [])
        self.cfg['repo'] = repo
        old = self.gates.prepare([dict(card='card', duty='verify')], 'parent')[0]
        self.gates.route(old, {})
        replacement = self.gates.prepare([dict(card='card', duty='verify')], 'parent')[0]
        self.assertEqual(self.gates.route(replacement, {})['provider'], 'anthropic')
        self.gates.start('parent', 'obsolete-child', old['goal'])
        with self.gates.store.read() as db:
            self.assertFalse(any(b.get('child_session') == 'obsolete-child' for b in self.gates.store.rows(db, 'binding')))
        self.assertIn(str(self.repo), replacement['goal'])
        self.assertIn(self.cfg['check'], replacement['goal'])
        status = self.gates.inspect(self.key)
        self.assertEqual(status['repo'], str(self.repo))
        self.assertIn('delegate_task(tasks=', status['next_step'])
        self.assertIn('delegate_task(tasks=', self.gates.close(self.key)['message'])

    def test_H_terminal_backend_setting_and_session_environment(self):
        self.gates.host_config = lambda: {'terminal': {'backend': 'ssh'}}
        with patch.dict(os.environ):
            os.environ.pop('TERMINAL_ENV', None)
            self.assertFalse(self.gates.local_terminal())
        self.gates.host_config = lambda: {'terminal': {'backend': 'local'}}
        with patch.dict(os.environ, TERMINAL_ENV='docker'):
            self.assertFalse(self.gates.local_terminal())


class BoardSnapshotAndDisplayTests(unittest.TestCase):
    setUp = test_gate.ServiceTests.setUp

    def test_E_snapshot_keeps_failed_board_only_while_healthy_updates(self):
        with patch.dict(os.environ, HERMES_HOME=str(self.root)):
            source = dict(schema_version=2, rev=1, cards=[{'board': 'broken', 'id': 'old'}, {'board': 'default', 'id': 'healthy-old'}], errors=[])
            with patch.object(api, 'snapshot', return_value=source):
                api.get_snapshot()
            updated = dict(schema_version=2, rev=2, cards=[{'board': 'default', 'id': 'healthy-new'}], errors=['Board broken: read failed'])
            with patch.object(api, 'snapshot', return_value=updated):
                result = api.get_snapshot()
            self.assertEqual([c['id'] for c in result['cards']], ['healthy-new', 'old'])

    def test_I_rev_human_copy_setup_labels_and_specific_ungated_reasons(self):
        with patch.object(api.boards, 'cards', return_value=([], [])):
            real = api.projection.snapshot(self.gates)
        self.assertIsInstance(real['rev'], int)
        self.assertTrue(all(s['label'] for s in real['setup']))
        for item in real['setup']:
            if item['ok']:
                self.assertFalse(item['hint'].startswith(('Set ', 'Configure ', 'Choose ')))
        with patch.dict(os.environ, HERMES_HOME=str(self.root)):
            previous = api.start_demo()
            for _ in range(6):
                current = api.step_demo()
                self.assertGreater(current['rev'], previous['rev'])
                card = current['cards'][0]
                self.assertNotEqual(card['hint'], card['gate']['reason'])
                for text in (card['hint'], card['gate']['reason']):
                    for name in ('army_verdict', 'delegate_task', 'duty=', 'card='):
                        self.assertNotIn(name, text)
                previous = current
            self.assertGreater(api.reset_demo()['rev'], previous['rev'])
        with sqlite3.connect(self.root / 'kanban.db') as db:
            db.execute('CREATE TABLE tasks(id,title,assignee,status,result,workspace_path,workspace_kind)')
            db.executemany('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)', [
                ('scratch', 'Notes', 'owner', 'ready', '', str(self.repo), 'scratch'),
                ('dir', 'Notes', 'owner', 'ready', '', str(self.root), 'dir')])
        self.cfg.pop('repo')
        data = api.projection.snapshot(self.gates)
        self.assertEqual({c['gate']['reason'] for c in data['cards']}, {
            'Scratch cards are never gated.', "This card's folder is not a git work tree."})

    def test_I_unreadable_path_keeps_a_human_reason_after_display_filter(self):
        status = dict(state='UNKNOWN', reason='Permission denied: /Users/synthetic/private', tree=None)
        with sqlite3.connect(self.root / 'kanban.db') as db:
            db.execute('CREATE TABLE tasks(id,title,assignee,status,result,workspace_path,workspace_kind)')
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)', ('c', 'Code', 'owner', 'ready', '', str(self.repo), 'worktree'))
        with patch.object(self.gates, 'inspect', return_value=status):
            card = api.projection.snapshot(self.gates)['cards'][0]
        self.assertIn('restore repository access', card['gate']['reason'])

    def test_J_readme_defines_cooperative_coverage(self):
        readme = (Path(__file__).parent / 'shadow-army/README.md').read_text()
        for phrase in ('requesting session', 'bound build and repair children', 'workers observed in this profile',
                       'normal configuration', 'filters', 'another profile is not observed here', 'fresh terminal session', 'every such card'):
            self.assertIn(phrase, readme + (Path(__file__).parent / 'shadow-army/REFERENCE.md').read_text())
        for phrase in ('variables are scrubbed', 'reads their bytes directly', '4 MiB content budget'):
            self.assertNotIn(phrase, readme)
