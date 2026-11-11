"""Review regressions against real repository files and real runner output."""
import sys
import subprocess
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import test_review_evidence
from test_gate import git

module = sys.modules[test_review_evidence.Gates.__module__]


class AdvisoryResilienceTests(unittest.TestCase):
    setUp = test_review_evidence.ReviewEvidenceTests.setUp
    card = test_review_evidence.ReviewEvidenceTests.card
    prepare = test_review_evidence.ReviewEvidenceTests.prepare

    def test_repeated_real_runner_drop_survives_receipt(self):
        self.test.write_text('import unittest\nclass Cart(unittest.TestCase):\n    def test_one(self): self.assertEqual(1, 1)\n    def test_two(self): self.assertEqual(2, 2)\n    def test_three(self): self.assertEqual(3, 3)\n')
        command = sys.executable + ' -m unittest discover'
        self.cfg['check'] = command
        baseline = subprocess.run([sys.executable, '-m', 'unittest', 'discover'], cwd=self.repo, capture_output=True, text=True)
        def run(call):
            result = subprocess.run([sys.executable, '-m', 'unittest', 'discover'], cwd=self.repo, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.gates.terminal_start('child', call, dict(command=command, workdir=str(self.repo)))
            self.gates.terminal_end('child', call, dict(exit_code=result.returncode, output=result.stdout + result.stderr))
        self.test.write_text('import unittest\nclass Cart(unittest.TestCase):\n    def test_one(self): self.assertEqual(1, 1)\n')
        self.prepare()
        self.gates.terminal_start('child', 'baseline', dict(command=command, workdir=str(self.repo)))
        self.gates.terminal_end('child', 'baseline', dict(exit_code=baseline.returncode, output=baseline.stdout + baseline.stderr))
        for n in range(3):
            run(str(n))
            self.assertEqual(self.card()['gate']['tests_ran'], dict(before=3, now=1))
        self.gates.verdict(self.key, 'PASS', [], 'child')
        for _ in range(3):
            self.assertIn('Fewer tests ran: 3 -> 1', self.card()['receipt_markdown'])

    def test_advisory_read_lock_never_raises(self):
        import sqlite3
        @contextmanager
        def locked():
            raise sqlite3.OperationalError('database is locked')
            yield
        for _ in range(3):
            with patch.object(self.gates.store, 'read', locked):
                self.assertEqual(self.gates.test_count_evidence(self.key), dict(tests_ran=None))

    def test_retired_profile_data_is_not_projected(self):
        self.cfg['retired_setting'] = True
        with self.gates.store.transaction() as db:
            self.gates.store.put(db, 'retired_probe', 'old', dict(result=True))
            self.gates.event(db, self.key, 'retired_event', retired_result=True)
            self.gates.event(db, self.key, 'finished', retired_result=True, finding='Done')
        for _ in range(3):
            card = self.card()
            self.assertEqual([row['kind'] for row in card['history']], ['finished'])
            self.assertNotIn('retired_result', card['history'][0])
            self.assertEqual(card['history'][0]['finding'], 'Done')

    def home_command(self, home):
        command = home + '/project/.venv/bin/python -m unittest'
        self.cfg['check'] = command
        self.prepare()
        self.gates.terminal_start('child', home, dict(command=command, workdir=str(self.repo)))
        self.gates.terminal_end('child', home, dict(exit_code=0, output='Ran 3 tests in 0.1s\nOK'))
        self.gates.verdict(self.key, 'PASS', [], 'child')
        with self.gates.store.transaction() as db:
            self.gates.event(db, self.key, 'finished', command=command)
        card = self.card()
        event = next(row for row in reversed(card['history']) if row['kind'] == 'verdict')
        self.assertEqual(event['check']['command'], '~/project/.venv/bin/python -m unittest')
        self.assertEqual(event['check']['exit_code'], 0)
        self.assertEqual(event['check']['tests_ran'], 3)
        self.assertIn('Ran 3 tests', event['check']['output_excerpt'])
        self.assertEqual(card['history'][-1]['command'], '~/project/.venv/bin/python -m unittest')
        self.assertIn('Check: ~/project/.venv/bin/python -m unittest (exit 0)', card['receipt_markdown'])
        self.assertEqual(test_review_evidence.api.projection.check_view(dict(command='echo password=private'))['command'], '')

    def test_macos_home_command(self):
        self.home_command('/Users/example')

    def test_linux_home_command(self):
        self.home_command('/home/example')

    def test_tilde_home_command(self):
        self.home_command('~')

    def test_disabled_false_positives_and_multiline_inventory(self):
        self.test.write_text('# Setup test_user fixture\nquery.skip(10)\ndef helper():\n    assert 1 == 1\ndef test_one():\n    assert 1 == 1\n')
        before = module.assertions.inventory(self.repo, mode='disabled')
        self.test.write_text('# Setup test_user fixture\nquery.skip(10)\ndef helper():\n    assert 1 == 1\ndef test_one(fix):\n    assert 1 == 1\n')
        js = self.repo / 'cart.test.ts'
        js.write_text("expect(\n total(cart), // user's empty cart\n).toBe(0);\nexpect(total([]))\n .toBe(0);\nexpect(foo(expect.arrayContaining([1]))).toBe(1);\n")
        rust = self.repo / 'cart_test.rs'
        rust.write_text("assert_eq!(\n parse::<'a>(x),\n y\n);\n")
        for _ in range(3):
            after = module.assertions.inventory(self.repo, mode='disabled')
            self.assertEqual(module.assertions.skipped(before, after, 'start'), [])
            rows = module.assertions.inventory(self.repo)
            jsrows = [r for r in rows if r['file'] == js.name]
            self.assertEqual(len(jsrows), 3)
            self.assertIn('.toBe(0)', jsrows[1]['text'])
            self.assertEqual(len([r for r in rows if r['file'] == rust.name]), 1)
