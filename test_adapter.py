from contextlib import closing
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

FILE = Path(__file__).parent / 'shadow-army/dashboard/plugin_api.py'
spec = importlib.util.spec_from_file_location('shadow_api', FILE)
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
spec.loader.exec_module(api)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'HERMES_KANBAN_HOME': str(self.root), 'HERMES_HOME': str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        with closing(sqlite3.connect(self.root / 'kanban.db')) as db:
            db.executescript('CREATE TABLE tasks(id,title,assignee,status,last_failure_error,result,workspace_path);')
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)', ('card1', 'Research evidence', 'researcher', 'blocked', '', 'NEEDS_DECISION: choose a route', None))
            db.commit()

    def test_snapshot_contract_and_read_only_board(self):
        before = (self.root / 'kanban.db').read_bytes()
        data = api.snapshot()
        self.assertEqual(data['schema_version'], 2)
        self.assertFalse(data['demo'])
        card = data['cards'][0]
        self.assertEqual(card['owner'], 'researcher')
        self.assertTrue(card['needs_you'])
        self.assertEqual(card['decision'], 'choose a route')
        self.assertEqual(card['gate']['state'], 'UNGATED')
        self.assertEqual(card['shadows'], [])
        self.assertEqual(card['history'], [])
        self.assertEqual(before, (self.root / 'kanban.db').read_bytes())

    def test_readonly_connection(self):
        with api.connect(self.root / 'kanban.db') as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE tasks SET status='done'")

    def test_duplicate_board_alias_deduped(self):
        alias = self.root / 'kanban/boards/alias'
        alias.mkdir(parents=True)
        (alias / 'kanban.db').symlink_to(self.root / 'kanban.db')
        self.assertEqual(len(api.snapshot()['cards']), 1)

    def test_redaction_without_hermes_internals(self):
        for value in ('Bearer synthetic', 'password=synthetic', 'https://u:p@example.test',
                      'sk-synthetic-key', 'person@example.test',
                      '/home/example/private', '~/private'):
            with self.subTest(value=value):
                self.assertEqual(api.clean(value), '')
        self.assertEqual(api.clean('\x1b[31mAdd rate limiter'), 'Add rate limiter')
        self.assertEqual(api.clean('abcdef', 3), 'abc')
        self.assertEqual(api.clean('host.example.ts.net'), 'host.example.ts.net')

    def test_api_writes_are_trial_only_and_raw_logs_removed(self):
        routes = {(r.path, tuple(sorted(r.methods))) for r in api.router.routes}
        self.assertEqual(routes, {('/snapshot', ('GET',)), ('/setup/arm', ('POST',)), ('/setup/selftest', ('POST',)), ('/demo/start', ('POST',)),
                                 ('/demo/step', ('POST',)), ('/demo/tamper', ('POST',)), ('/demo/reset', ('POST',))})

    def test_ambiguous_board_refuses_and_workspace_wins(self):
        other = self.root / 'kanban/boards/other'
        other.mkdir(parents=True)
        (other / 'kanban.db').write_bytes((self.root / 'kanban.db').read_bytes())
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            api.boards.key_for('card1')

    def test_empty_state_has_setup(self):
        (self.root / 'kanban.db').unlink()
        data = api.snapshot()
        self.assertEqual(data['cards'], [])
        self.assertEqual(len(data['setup']), 8)
        self.assertTrue(next(s for s in data['setup'] if s['id'] == 'repo')['ok'])
        self.assertFalse(next(s for s in data['setup'] if s['id'] == 'verifier')['ok'])
        self.assertIn('No open code card.', data['setup'][0]['hint'])


if __name__ == '__main__':
    unittest.main()
