"""Release-candidate regressions using Hermes defaults and terminal scope."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_adapter import api
from test_gate import Gates, Store, seed, git
from hermes_cli.config import DEFAULT_CONFIG
from tools.terminal_scope import install_and_reset_profile_terminal_scope
from agent.runtime_cwd import _SESSION_CWD


class ReviewEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / 'repo'
        seed(self.repo)
        self.test = self.repo / 'test_cart.py'
        self.test.write_text('assert total == 0\nassert count == 1\n')
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '-m', 'tests')
        self.host = copy.deepcopy(DEFAULT_CONFIG)
        self.host['delegation'] = dict(backend='shadow-army')
        self.cfg = dict(repo=str(self.repo), check='true', verifier=dict(provider='anthropic', model='claude-test'))
        self.gates = Gates(Store(self.root / 'data'), lambda k, d: self.cfg.get(k, d), host_config=lambda: self.host)
        self.profile = self.root / 'profile'
        self.profile.mkdir()
        (self.profile / 'config.yaml').write_text('{}')
        self.enterContext(install_and_reset_profile_terminal_scope(self.profile))
        token = _SESSION_CWD.set(str(Path.home()))
        self.addCleanup(_SESSION_CWD.reset, token)
        self.enterContext(patch.object(api.boards, 'cards', return_value=([dict(id='card', board='default', title='Code', status='open')], [])))
        self.key = self.gates.key_for('card')
        # Use the actual resolved board identity in the pane as well.
        api.boards.cards.return_value[0][0]['board'] = self.key.board
        self.gates.observe('parent', 'openai', 'gpt-test')

    def card(self):
        return api.projection.snapshot(self.gates)['cards'][0]

    def prepare(self):
        task = self.gates.prepare([dict(card='card', duty='verify')], 'parent')[0]
        self.gates.route(task, {})
        self.gates.start('parent', 'child', task['goal'])
        self.gates.observe('child', 'anthropic', 'claude-test', 'gemini-test', request_id='first', usage={'total_tokens': 100})
        return task

    def verdict(self):
        self.gates.terminal_start('child', 'check', dict(command='true', workdir=str(self.repo)))
        self.gates.terminal_end('child', 'check', dict(exit_code=0))
        return self.gates.verdict(self.key, 'PASS', [], 'child')

    def check_counts(self, value, changed, removed):
        self.assertEqual(value['changed_count'], changed)
        self.assertEqual(value['weakened_count'], removed)

    def review_sequence(self, changed, removed):
        task = self.prepare()
        self.assertIn(f'{changed} assertion lines changed' if changed else f'{removed} assertion removed', task['goal'])
        self.check_counts(self.card()['gate'], changed, removed)
        self.check_counts(self.verdict(), changed, removed)
        for _ in range(2):
            card = self.card()
            self.check_counts(card['gate'], changed, removed)
            self.assertEqual('Reviewer accepted the removed assertions.' in card['receipt_markdown'], bool(removed))
            event = next(row for row in card['history'] if row['kind'] == 'verdict')
            self.assertEqual(event['accepted_removed'], bool(removed))

    def test_edit_in_one_file_never_marks_the_same_line_in_another(self):
        import sys
        assertions = sys.modules[Gates.__module__].assertions
        before = [dict(file='test_a.py', line=1, text='assert status == 200'),
                  dict(file='test_b.py', line=1, text='assert status == 200')]
        after = [dict(file='test_a.py', line=1, text='assert status == 201'),
                 dict(file='test_b.py', line=1, text='assert status == 200')]
        for _ in range(3):
            rows = assertions.removed(before, after)
            self.assertEqual([(r['file'], r['kind']) for r in rows], [('test_a.py', 'changed')])
        # A pure move still matches across files.
        moved = [dict(file='test_c.py', line=4, text='assert status == 200'),
                 dict(file='test_b.py', line=1, text='assert status == 200')]
        self.assertEqual(assertions.removed(before, moved), [])

    def test_moved_test_file_is_neither_removed_nor_changed(self):
        # An uncommitted rename keeps every assertion; nothing may read as removed on any later call.
        self.test.rename(self.repo / 'test_moved.py')
        task = self.prepare()
        self.assertNotIn('assertion removed', task['goal'])
        self.assertNotIn('assertions removed', task['goal'])
        self.check_counts(self.card()['gate'], 0, 0)
        self.check_counts(self.verdict(), 0, 0)
        for _ in range(2):
            card = self.card()
            self.check_counts(card['gate'], 0, 0)
            self.assertNotIn('Reviewer accepted the removed assertions.', card['receipt_markdown'])
            event = next(row for row in card['history'] if row['kind'] == 'verdict')
            self.assertFalse(event['accepted_removed'])

    def test_edits_stay_changed_through_review_and_snapshots(self):
        self.test.write_text('assert cart_total == 0\nassert cart_count == 1\n')
        self.review_sequence(2, 0)

    def test_deletion_stays_removed_through_review_and_snapshots(self):
        self.test.write_text('assert total == 0\n')
        self.review_sequence(0, 1)

    def test_uncommitted_test_seen_before_first_review_survives_baseline(self):
        new = self.repo / 'test_bug.py'
        new.write_text('assert bug == 0\n')
        self.gates.prepare([dict(card='card', duty='build')], 'parent')
        new.unlink()
        self.review_sequence(0, 1)

    def test_home_scope_falls_through_to_configured_repository(self):
        for _ in range(3):
            task = self.gates.prepare([dict(duty='verify')], 'parent')[0]
            self.gates.route(task, {})
            key = self.gates.key_for(task['card'], task['board'])
            self.assertEqual(Path(self.gates.repository(key)).resolve(), self.repo.resolve())

    def test_relative_folder_never_anchors_to_process_directory(self):
        (self.profile / 'config.yaml').write_text('terminal:\n  cwd: repo\n')
        token = _SESSION_CWD.set('repo')
        self.addCleanup(_SESSION_CWD.reset, token)
        self.cfg['repo'] = None
        import os
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        with install_and_reset_profile_terminal_scope(self.profile):
            for _ in range(3):
                task = self.gates.prepare([dict(duty='verify')], 'parent')[0]
                with self.assertRaisesRegex(ValueError, 'Git folder'):
                    self.gates.route(task, {})

    def test_proxy_identity_and_final_tokens_match_all_receipt_views(self):
        self.prepare()
        self.verdict()
        for tokens in (100, 175, 175):
            if tokens == 175:
                self.gates.usage('child', 'final', {'total_tokens': 75})
                self.gates.stop('child')
            card = self.card()
            event = next(row for row in card['history'] if row['kind'] == 'verdict')
            self.assertEqual((event['family'], event['model'], event['tokens']), ('google', 'gemini-test', tokens))
            self.assertEqual(card['gate']['verifier'], dict(family='google', model='gemini-test'))
            self.assertIn('Verifier: google / gemini-test', card['receipt_markdown'])
            self.assertIn(f'Tokens: {tokens}', card['receipt_markdown'])

    def test_removed_lines_precede_changed_lines_before_five_item_limit(self):
        self.test.write_text(''.join(f'assert old_{i}\n' for i in range(5)))
        deleted = self.repo / 'test_z.py'
        deleted.write_text('assert deleted\n')
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '-m', 'six assertions')
        self.test.write_text(''.join(f'assert new_{i}\n' for i in range(5)))
        deleted.unlink()
        task = self.gates.prepare([dict(card='card', duty='verify')], 'parent')[0]
        self.assertIn('assert deleted', task['goal'])
        for _ in range(3):
            gate = self.card()['gate']
            self.check_counts(gate, 5, 1)
            self.assertEqual(gate['weakened'][0]['text'], 'assert deleted')
            self.assertEqual(gate['weakened'][0]['kind'], 'removed')

    def test_duplicate_invalid_settings_produce_one_setup_item(self):
        self.cfg['verifier_fallbacks'] = [1, 2]
        for _ in range(3):
            items = api.projection.snapshot(self.gates)['setup']
            self.assertEqual(sum(row['id'] == 'setting_verifier_fallbacks' for row in items), 1)
