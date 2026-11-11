import os
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch

from test_adapter import api
import test_backend_regressions
from test_gate import fingerprint, git

policy = sys.modules[fingerprint.__module__]


class GitTimeoutTests(unittest.TestCase):
    setUp = test_backend_regressions.FingerprintRegressions.setUp

    def test_1_touched_index_timeout_leaves_git_writable(self):
        for i in range(2000):
            (self.repo / f'file-{i}').write_text('same bytes\n')
        git(self.repo, 'add', '.', timeout=5)
        git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            '-c', 'commit.gpgsign=false', 'commit', '-m', 'many files', timeout=5)
        for path in self.repo.glob('file-*'):
            os.utime(path, None)
        original = policy.git
        def limited(repo, *args, **kwargs):
            if args[0] == 'diff':
                kwargs['timeout'] = 0.0001
            return original(repo, *args, **kwargs)
        with patch.object(policy, 'git', side_effect=limited):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                fingerprint(self.repo)
        self.assertFalse((self.repo / '.git/index.lock').exists())
        git(self.repo, 'add', 'app.txt')

    def test_1_timeout_terminates_git_before_kill(self):
        real_git = shutil.which('git')
        wrapper = self.repo.parent / 'git'
        wrapper.write_text('''#!''' + sys.executable + '''
import os, signal, sys, time
from pathlib import Path
assert '--no-optional-locks' in sys.argv
assert os.environ['GIT_OPTIONAL_LOCKS'] == '0'
if 'diff' not in sys.argv:
    os.execv(''' + repr(real_git) + ''', ['git', *sys.argv[1:]])
lock = Path(sys.argv[sys.argv.index('-C') + 1]) / '.git/index.lock'
lock.touch()
def stopped(*args):
    lock.unlink()
    sys.exit(0)
signal.signal(signal.SIGTERM, stopped)
time.sleep(10)
''')
        wrapper.chmod(0o755)
        with patch.dict(os.environ, PATH=str(wrapper.parent) + os.pathsep + os.environ['PATH']):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                fingerprint(self.repo)
        self.assertFalse((self.repo / '.git/index.lock').exists())
        git(self.repo, 'add', 'app.txt')


    def test_1_git_receives_a_minimal_environment(self):
        real_git = shutil.which('git')
        wrapper = self.repo.parent / 'git'
        seen = self.repo.parent / 'git-env.jsonl'
        wrapper.write_text("""#!""" + sys.executable + """
import json, os, sys
with open(""" + repr(str(seen)) + """, 'a') as log:
    log.write(json.dumps(dict(os.environ)) + '\\n')
os.execv(""" + repr(real_git) + """, ['git', *sys.argv[1:]])
""")
        wrapper.chmod(0o755)
        secrets = {'OPENAI_API_KEY': 'synthetic-openai', 'ANTHROPIC_API_KEY': 'synthetic-anthropic',
                   'HERMES_DASHBOARD_SESSION_TOKEN': 'synthetic-session', 'GIT_DIR': '/nonexistent',
                   'GIT_CONFIG_GLOBAL': '/nonexistent', 'SSH_AUTH_SOCK': '/nonexistent'}
        with patch.dict(os.environ, dict(secrets, PATH=str(wrapper.parent) + os.pathsep + os.environ['PATH'])):
            parent = set(os.environ)
            self.assertTrue(fingerprint(self.repo))
        import json
        calls = [json.loads(line) for line in seen.read_text().splitlines()]
        self.assertTrue(calls)
        allowed = {'PATH', 'HOME', 'LANG', 'LC_ALL', 'GIT_TERMINAL_PROMPT', 'GIT_OPTIONAL_LOCKS'}
        for env in calls:
            # macOS adds __CF_USER_TEXT_ENCODING to every process by itself; nothing passes it on.
            self.assertEqual((set(env) & parent) - allowed - {'__CF_USER_TEXT_ENCODING'}, set())
            self.assertFalse(set(secrets) & set(env))
            self.assertFalse(set(secrets.values()) & set(env.values()))
            self.assertEqual((env['LC_ALL'], env['GIT_TERMINAL_PROMPT'], env['GIT_OPTIONAL_LOCKS']), ('C', '0', '0'))

class ConfigurationDocumentationTests(unittest.TestCase):
    def test_4_repo_is_only_explicit_opt_in(self):
        # The settings example lives in the reference page; the catalog page links to it.
        docs = ''.join((Path(__file__).parent / 'shadow-army' / name).read_text() for name in ('README.md', 'REFERENCE.md'))
        example = docs.split('```yaml')[1].split('```')[0]
        self.assertNotIn('repo:', example)
        self.assertIn('every scratch card', docs)
        skill = (Path(__file__).parent / 'shadow-army/skills/verify-loop/SKILL.md').read_text()
        self.assertNotIn('set the repo', skill)
        self.assertIn('first commit', skill)
        self.assertIn('UNGATED and UNARMED cards close normally', skill)

    def test_7_other_profile_worker_limit_is_explicit(self):
        readme = (Path(__file__).parent / 'shadow-army/README.md').read_text()
        self.assertIn('A worker that ran in another profile is not observed here.', (Path(__file__).parent / 'shadow-army/REFERENCE.md').read_text())

    def test_9_no_committed_receipt_claims(self):
        root = Path(__file__).parent
        self.assertFalse(any((root / 'verification').rglob('*.*')))
        doc = (root / 'shadow-army/VERIFICATION.md').read_text()
        self.assertIn('mktemp -d', doc)
        self.assertNotIn('../verification/', doc)
        self.assertNotIn('f526a30', doc)
        readme = (root / 'shadow-army/README.md').read_text()
        self.assertIn('Check output and findings are stored verbatim', (Path(__file__).parent / 'shadow-army/REFERENCE.md').read_text())
        self.assertNotIn('No raw\nprompt, conversation, credential', readme)
        self.assertNotIn('SHADOW_ARMY_CLI', (root / 'README.md').read_text())


if __name__ == '__main__':
    unittest.main()
