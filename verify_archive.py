"""Validate the committed plugin against a disposable copy of Hermes, never its live tree."""
import argparse
import io
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import tempfile


def archive(repo, destination, subtree=None):
    args = ['git', '--no-optional-locks', '-C', str(repo), 'archive', 'HEAD'] + ([subtree] if subtree else [])
    data = subprocess.check_output(args, env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'))
    with tarfile.open(fileobj=io.BytesIO(data)) as stream:
        stream.extractall(destination, filter='data')


def run(argv, env, cwd):
    with subprocess.Popen(argv, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, start_new_session=True) as proc:
        try:
            output, _ = proc.communicate(timeout=150)
        except BaseException:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
            raise
        return proc.returncode, output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-source', type=Path, required=True)
    parser.add_argument('--receipts', type=Path, required=True)
    args = parser.parse_args()
    args.receipts.mkdir(parents=True, exist_ok=True)
    # The plugin sits beside this script; find its path inside whichever repository holds it.
    here = Path(__file__).resolve().parent / 'shadow-army'
    env_git = dict(os.environ, GIT_OPTIONAL_LOCKS='0')
    repo = Path(subprocess.check_output(['git', '--no-optional-locks', '-C', str(here), 'rev-parse', '--show-toplevel'],
                                        env=env_git, text=True).strip())
    subtree = subprocess.check_output(['git', '--no-optional-locks', '-C', str(here), 'rev-parse', '--show-prefix'],
                                      env=env_git, text=True).strip().rstrip('/')
    with tempfile.TemporaryDirectory(prefix='shadow-admission-') as tmp:
        tmp = Path(tmp)
        source = tmp / 'hermes-source'
        source.mkdir()
        archive(args.hermes_source, source)
        archive(repo, tmp / 'candidate', subtree)
        plugin = tmp / 'candidate' / subtree
        # Reuse installed dependencies without allowing PM to mutate the reference checkout.
        shutil.copytree(args.hermes_source / '.venv', source / '.venv', symlinks=True)
        env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT') if k in os.environ}
        env.update(HOME=str(tmp / 'os-home'), HERMES_HOME=str(tmp / 'home'), PYTHONPATH=str(source),
                   PYTHONDONTWRITEBYTECODE='1', UV_CACHE_DIR=str(tmp / 'uv-cache'), NO_COLOR='1')
        (tmp / 'os-home').mkdir()
        for name, tail in [('validate', ['--install-deps']), ('doctor', [])]:
            argv = [str(source / '.venv/bin/python'), '-B', '-m', 'hermes_cli.main', 'plugins', name, str(plugin), *tail]
            code, output = run(argv, env, source)
            text = '$ hermes plugins ' + name + ' <clean-archive>' + (' --install-deps' if tail else '') + '\n' + output + f'\nexit={code}\n'
            (args.receipts / (name + '.txt')).write_text(text)
            print(text, flush=True)
            if code:
                raise SystemExit(code)
        # Also expose machine counts; CLI success alone can conceal warnings or dependency errors.
        argv = [str(source / '.venv/bin/python'), '-B', '-c',
                'from hermes_cli.plugin_validate import validate_plugin_dir; import json,sys; r=validate_plugin_dir(__import__("pathlib").Path(sys.argv[1])); print(json.dumps({"failures":r.failures,"warnings":r.warnings}))', str(plugin)]
        code, output = run(argv, env, source)
        (args.receipts / 'counts.txt').write_text(output)
        print(output, flush=True)
        if code or '"failures": []' not in output or '"warnings": []' not in output:
            raise SystemExit(1)
    print('Cleanup: disposable source, dependency copy, home and trial artifacts removed; child processes exited.')


if __name__ == '__main__':
    main()
