"""Publish approval heuristics and read-only receipt decisions; no network calls."""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import tempfile
import sqlite3
import time
import unittest
from unittest.mock import patch
from contextvars import ContextVar

from test_gate import Gates, Store, seed, fingerprint, check_hash
from gate_test_plugin.service import publish_targets


class PublishMatcherTests(unittest.TestCase):
    def test_publish_forms(self):
        for command, expected in [
            ('git push', ['/repo']),
            ('git push origin HEAD:main', ['/repo']),
            ('gh pr create --title "Ship code" --body "Ready"', ['/repo']),
            ('CI=1 TOKEN="a b" git push', ['/repo']),
            ('env CI=1 gh pr create', ['/repo']),
            ('cd child && git push', ['/repo/child']),
            ('cd "child folder"; gh pr create', ['/repo/child folder']),
            ('git -C child push', ['/repo/child']),
            ('git -c push.default=current -C /other push', ['/other']),
            ('git -C child -C nested push', ['/repo/child/nested']),
            ('git status && git push; gh pr create', ['/repo']),
            ('cd /other; git push; cd /third && gh pr create', ['/other', '/third']),
            ('git push 2>&1', ['/repo']),
            ('git push origin HEAD 2>&1 | tail -5', ['/repo']),
            ('git push > /dev/null', ['/repo']),
            ('gh pr create --fill 2>&1 | cat', ['/repo']),
            ('git add -A && git commit -m "x" && git push 2>&1', ['/repo']),
            ('git status || git push', ['/repo']),
        ]:
            with self.subTest(command=command):
                self.assertEqual(publish_targets(command, '/repo'), expected)

    def test_non_matches_and_unknown_syntax(self):
        for command in ['git fetch', 'git pull', 'git push --dry-run', 'git push -n origin',
                        'echo "git push"', 'printf "%s" "gh pr create"',
                        'echo "x; git push"', "echo ';' git push", r'echo \; git push',
                        './publish.sh', 'bash publish.sh', 'python publish.py',
                        '# git push', 'echo hi # words; git push',
                        'echo "unterminated', 'git status | cat', 'echo hi | grep push', 'git log | grep "git push"',
                        'git stash push', 'cat notes > push', 'echo git push > out.txt', '(git push)', 'git push <<EOF', '', ' ']:
            with self.subTest(command=command):
                self.assertEqual(publish_targets(command, '/repo'), [])
        self.assertEqual(publish_targets('x' * 16385, '/repo'), [])


class PublishDecisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        seed(self.repo)
        self.cfg = dict(repo=str(self.repo), check='printf checked',
                        verifier=dict(provider='anthropic', model='claude-opus-4-6'))
        self.gates = Gates(Store(self.root / 'state'), lambda k, d=None: self.cfg.get(k, d),
                           host_config=lambda: {'delegation': {'backend': 'shadow-army'}})
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'HERMES_KANBAN_HOME': str(self.root), 'TERMINAL_ENV': 'local', 'TERMINAL_CWD': ''}).start()
        patch.object(self.gates, 'local_terminal', return_value=True).start()
        self.args = dict(command='git push', workdir=str(self.repo))

    def receipt(self, **changes):
        wid = hashlib.sha256(os.fsencode(str(self.repo.resolve()))).hexdigest()[:12]
        row = dict(board='workspace:', card_id=wid, tree=fingerprint(self.repo),
                   check_hash=check_hash(self.cfg['check']), created_at=time.time(),
                   verdict='PASS', synthetic=False)
        row.update(changes)
        with self.gates.store.transaction() as db:
            self.gates.store.put(db, 'receipt', 'review', row)
        return row

    def test_unverified_asks_without_writing(self):
        result = self.gates.publish_decision(self.args)
        self.assertEqual(result['action'], 'approve')
        self.assertEqual(result['rule_key'], 'shadow-army:publish-unreviewed')
        self.assertIn('(UNVERIFIED)', result['message'])
        self.assertFalse(self.gates.store.path.exists())

    def test_unarmed_setting_off_or_malformed_passes_through(self):
        self.cfg['verifier'] = None
        self.assertIsNone(self.gates.publish_decision(self.args))
        self.cfg['verifier'] = dict(provider='anthropic', model='claude-opus')
        for value in (False, 'true', 1, None):
            self.cfg['guard_push'] = value
            self.assertIsNone(self.gates.publish_decision(self.args))

    def test_fresh_workspace_pass(self):
        self.receipt()
        before = self.gates.store.path.read_bytes()
        self.assertIsNone(self.gates.publish_decision(self.args))
        self.assertEqual(before, self.gates.store.path.read_bytes())

    def test_fresh_card_pass_including_closed_card_and_subdirectory(self):
        subdir = self.repo / 'src'
        subdir.mkdir()
        self.receipt(board='default', card_id='card')
        with closing(sqlite3.connect(self.root / 'kanban.db')) as db:
            db.execute('CREATE TABLE tasks (id TEXT, status TEXT, workspace_kind TEXT, workspace_path TEXT)')
            db.execute('INSERT INTO tasks VALUES (?, ?, ?, ?)', ('card', 'archived', 'dir', str(subdir)))
            db.commit()
        self.assertIsNone(self.gates.publish_decision(self.args))

    def test_deleted_card_receipt_does_not_authorize(self):
        self.receipt(board='default', card_id='missing-card')
        self.assertEqual(self.gates.publish_decision(self.args)['action'], 'approve')

    def test_all_publish_targets_need_a_pass(self):
        self.receipt()
        other = self.root / 'other'
        seed(other)
        self.assertEqual(self.gates.publish_decision(dict(self.args,
            command='git push; git -C "' + str(other) + '" push'))['action'], 'approve')

    def test_stale_tree_check_or_age_asks(self):
        for changes in (dict(tree='older-tree'), dict(check_hash='other-check'), dict(created_at=time.time() - 3700)):
            with self.subTest(changes=changes):
                self.receipt(**changes)
                self.assertIn('(STALE)', self.gates.publish_decision(self.args)['message'])

    def test_synthetic_receipt_does_not_authorize(self):
        self.receipt(synthetic=True)
        self.assertIn('(UNVERIFIED)', self.gates.publish_decision(self.args)['message'])

    def test_no_git_or_unknown_folder_passes_through(self):
        for workdir in (str(self.root), str(self.root / 'missing'), 'relative'):
            self.assertIsNone(self.gates.publish_decision(dict(self.args, workdir=workdir)))

    def test_cd_or_git_C_from_non_git_parent_uses_target(self):
        for command in ('cd repo && git push', 'git -C repo push'):
            self.assertEqual(self.gates.publish_decision(dict(command=command, workdir=str(self.root)))['action'], 'approve')

    def test_session_folder_precedes_configured_repo(self):
        other = self.root / 'other'
        seed(other)
        self.receipt()
        token_var = ContextVar('HERMES_SESSION_CWD')
        token = token_var.set(str(other))
        try:
            self.assertIn('(UNVERIFIED)', self.gates.publish_decision(dict(command='git push'))['message'])
        finally:
            token_var.reset(token)

    def test_ordinary_commands_skip_fingerprint(self):
        with patch('gate_test_plugin.service.fingerprint', side_effect=AssertionError('should not run')) as tree:
            self.assertIsNone(self.gates.publish_decision(dict(self.args, command='git status')))
            tree.assert_not_called()
        with patch('gate_test_plugin.service.boards.in_work_tree', side_effect=AssertionError('should not run')) as repo:
            self.assertIsNone(self.gates.publish_decision(dict(self.args, command='echo hello')))
            repo.assert_not_called()

    def test_error_timeout_and_size_limit_pass_through(self):
        for error in (RuntimeError('fault'), ValueError('Git fingerprint timed out'), ValueError('size cap')):
            with self.subTest(error=error), patch('gate_test_plugin.service.fingerprint', side_effect=error):
                self.assertIsNone(self.gates.publish_decision(self.args))
        with patch.object(self.gates, 'armed', side_effect=RuntimeError('config fault')):
            self.assertIsNone(self.gates.publish_decision(self.args))
