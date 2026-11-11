"""Advisory evidence survives the real default scope and repeated pane reads."""
import unittest

import test_review_evidence
from test_gate import git


class DisabledTestInventoryTests(unittest.TestCase):
    setUp = test_review_evidence.ReviewEvidenceTests.setUp
    card = test_review_evidence.ReviewEvidenceTests.card
    prepare = test_review_evidence.ReviewEvidenceTests.prepare
    verdict = test_review_evidence.ReviewEvidenceTests.verdict

    def sequence(self, before, after):
        self.test.write_text(before)
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '-m', 'baseline')
        self.test.write_text(after)
        task = self.prepare()
        self.assertIn('1 skipped or disabled tests (advisory; review intent)', task['goal'])
        self.assertEqual(self.card()['gate']['skipped_count'], 1)
        self.assertEqual(self.verdict()['skipped_count'], 1)
        for _ in range(3):
            card = self.card()
            self.assertEqual(card['gate']['skipped_count'], 1)
            event = next(r for r in card['history'] if r['kind'] == 'verdict')
            self.assertTrue(event['accepted_skipped'])
            self.assertEqual(event['skipped_count'], 1)
            self.assertIn('Reviewer accepted the skipped tests.', card['receipt_markdown'])

    def test_python_skip(self):
        self.sequence('def test_total():\n    assert total == 0\n', '@unittest.skip("later")\ndef test_total():\n    assert total == 0\n')

    def test_python_xfail(self):
        self.sequence('assert total == 0\n', '@pytest.mark.xfail\nassert total == 0\n')

    def test_renamed(self):
        self.sequence('def test_total():\n    assert total == 0\n', 'def disabled_total():\n    assert total == 0\n')

    def test_commented(self):
        self.sequence('def test_total():\n    assert total == 0\n', '# def test_total():\n#    assert total == 0\n')

    def test_js(self):
        self.test = self.repo / 'cart.test.ts'
        self.sequence('it("total", () => expect(total).toBe(0));\n', 'it.skip("total", () => expect(total).toBe(0));\n')

    def test_go(self):
        self.test = self.repo / 'cart_test.go'
        self.sequence('func TestTotal(t *testing.T) {\n}\n', 'func TestTotal(t *testing.T) {\n t.Skip("later")\n}\n')

    def test_rust(self):
        self.test = self.repo / 'tests.rs'
        self.sequence('#[test]\nfn test_total() {}\n', '#[test]\n#[ignore]\nfn test_total() {}\n')

    def test_java(self):
        self.test = self.repo / 'CartTest.java'
        self.sequence('@Test\nvoid testTotal() {}\n', '@Disabled\n@Test\nvoid testTotal() {}\n')

    def test_ruby(self):
        self.test = self.repo / 'cart_spec.rb'
        self.sequence('it "total" do\nend\n', 'it "total" do\n skip "later"\nend\n')


class RunnerTestCountTests(unittest.TestCase):
    setUp = test_review_evidence.ReviewEvidenceTests.setUp
    card = test_review_evidence.ReviewEvidenceTests.card
    prepare = test_review_evidence.ReviewEvidenceTests.prepare

    def sequence(self, before, after, expected):
        self.prepare()
        self.assertIsNone(self.card()['gate']['tests_ran'])
        for call, output in [('before', before), ('after', after)]:
            self.gates.terminal_start('child', call, dict(command='true', workdir=str(self.repo)))
            self.gates.terminal_end('child', call, dict(exit_code=0, output=output))
        self.assertEqual(self.card()['gate']['tests_ran'], expected)
        self.assertEqual(self.gates.verdict(self.key, 'PASS', [], 'child')['tests_ran'], expected)
        for _ in range(2):
            card = self.card()
            self.assertEqual(card['gate']['tests_ran'], expected)
            event = next(r for r in card['history'] if r['kind'] == 'verdict')
            self.assertEqual(event['tests_ran'], expected)
            self.assertEqual(event['check']['tests_ran'], expected['now'] if expected else None)

    def test_unittest(self):
        self.sequence('Ran 3 tests in 0.1s\nOK', 'Ran 1 test in 0.1s\nOK', dict(before=3, now=1))

    def test_pytest(self):
        self.sequence('=== 3 passed in 0.2s ===', '=== 1 passed in 0.2s ===', dict(before=3, now=1))

    def test_jest(self):
        self.sequence('Tests: 3 passed, 3 total', 'Tests: 1 passed, 1 total', dict(before=3, now=1))

    def test_vitest(self):
        self.sequence(' Tests  3 passed (3)', ' Tests  1 passed (1)', dict(before=3, now=1))

    def test_cargo(self):
        self.sequence('test result: ok. 3 passed; 0 failed; 0 ignored;', 'test result: ok. 1 passed; 0 failed; 0 ignored;', dict(before=3, now=1))

    def test_rspec(self):
        self.sequence('3 examples, 0 failures', '1 example, 0 failures', dict(before=3, now=1))

    def test_junit(self):
        self.sequence('Tests run: 3, Failures: 0, Errors: 0, Skipped: 0', 'Tests run: 1, Failures: 0, Errors: 0, Skipped: 0', dict(before=3, now=1))

    def test_maven_surefire_results(self):
        before = """[INFO] Running example.FooTest
[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.01 s -- in example.FooTest
[INFO] Running example.BarTest
[INFO] Tests run: 2, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.02 s -- in example.BarTest
[INFO]
[INFO] Results:
[INFO]
[INFO] Tests run: 5, Failures: 0, Errors: 0, Skipped: 0
[INFO]
[INFO] BUILD SUCCESS
"""
        after = """[INFO] Running example.FooTest
[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0, Time elapsed: 0.01 s -- in example.FooTest
[INFO]
[INFO] Results:
[INFO]
[INFO] Tests run: 3, Failures: 0, Errors: 0, Skipped: 0
[INFO]
[INFO] BUILD SUCCESS
"""
        self.sequence(before, after, dict(before=5, now=3))

    def test_go_unknown(self):
        self.sequence('ok example.test/project 0.01s', 'ok example.test/project 0.01s', None)


class MultilineAssertionTests(unittest.TestCase):
    setUp = test_review_evidence.ReviewEvidenceTests.setUp
    card = test_review_evidence.ReviewEvidenceTests.card
    prepare = test_review_evidence.ReviewEvidenceTests.prepare
    verdict = test_review_evidence.ReviewEvidenceTests.verdict

    def test_later_argument_changes_survive_all_views(self):
        self.test.write_text('self.assertEqual(\n    total([]),\n    0,\n)\n')
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '-m', 'multiline baseline')
        self.test.write_text('self.assertEqual(\n    total([]),\n    1,\n)\n')
        task = self.prepare()
        self.assertIn('0,', task['goal'])
        for value in [self.card()['gate'], self.verdict(), self.card()['gate'], self.card()['gate']]:
            self.assertEqual(value['changed_count'], 1)
            self.assertEqual(value['weakened_count'], 0)
            self.assertEqual(value['weakened'][0]['text'], 'self.assertEqual(\ntotal([]),\n0,\n)')
