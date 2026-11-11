"""Real Hermes dispatch, with synthetic route/lifecycle events and no inference."""
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import hermes_yaml as yaml


def copy_db(source, target):
    """Copy a live SQLite database. A plain file copy misses rows still in the write-ahead log."""
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


class HostProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = Path(os.environ['HERMES_HOME'])
        cls.repo = cls.home / 'repo'
        cls.repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(cls.repo), *args], stderr=subprocess.STDOUT)
        git('init')
        (cls.repo / 'app.txt').write_text('synthetic\n')
        git('add', '.')
        git('-c', 'user.name=Probe', '-c', 'user.email=probe@example.invalid', '-c', 'commit.gpgsign=false', 'commit', '-m', 'seed')
        cls.config = {'plugins': {'enabled': ['shadow-army'], 'entries': {'shadow-army': {'settings': {
            'repo': str(cls.repo), 'check': 'printf gate-check',
            'verifier': {'provider': 'anthropic', 'model': 'claude-opus-4-6'}}}}},
            'delegation': {'backend': 'shadow-army'}, 'terminal': {'backend': 'local'}}
        (cls.home / 'config.yaml').write_text(yaml.safe_dump(cls.config))
        from hermes_cli.plugins import get_plugin_manager, invoke_hook, _dispatch_pre_tool_call_hooks
        cls.manager = get_plugin_manager()
        cls.manager.discover_and_load()
        cls.invoke = staticmethod(invoke_hook)
        cls.pre = staticmethod(_dispatch_pre_tool_call_hooks)
        import tools.kanban_tools
        import tools.delegate_tool
        import tools.terminal_tool
        import model_tools
        cls.dispatch = staticmethod(model_tools.handle_function_call)
        from agent.delegation_registry import get_active_provider
        cls.provider = get_active_provider(cls.config['delegation'])
        assert cls.provider.name == 'shadow-army'
        cls.gates = cls.provider.gates
        from tui_gateway.server import register_live_transport
        cls.frames = []
        class Sink:
            def write(self, frame):
                cls.frames.append(frame)
        cls.transport = Sink()
        register_live_transport(cls.transport)
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect
        db = connect()
        try:
            cls.card = kb.create_task(db, title='Synthetic gate probe', assignee='test')
        finally:
            db.close()
        if isinstance(cls.card, dict):
            cls.card = cls.card['id']

    @classmethod
    def tearDownClass(cls):
        from tui_gateway.server import unregister_live_transport
        unregister_live_transport(cls.transport)
        import tools.terminal_tool
        tools.terminal_tool._atexit_cleanup()
        cls.manager.unload()

    def call(self, name, args, sid='builder', call_id='call'):
        value = self.dispatch(name, args, session_id=sid, task_id='probe-task', tool_call_id=call_id, turn_id='probe-turn')
        self.assertIsInstance(value, str)
        return json.loads(value)

    def test_00_settings_schema_loaded_by_real_manager(self):
        loaded = self.manager._plugins['shadow-army']
        self.assertTrue(loaded.enabled)
        self.assertIsNone(loaded.error)
        schema = loaded.manifest.config_schema
        self.assertEqual(set(schema), {'verifier_route', 'verifier_provider', 'verifier_model', 'verifier_fallbacks', 'family_overrides', 'check', 'pass_lifetime_minutes', 'repo'})
        self.assertEqual(schema['verifier_fallbacks']['type'], 'list')
        self.assertEqual(schema['family_overrides']['type'], 'dict')
        self.assertEqual(schema['check']['default'], 'python3 -m unittest discover -s tests')
        self.assertIn('Warning', schema['repo']['description'])
        original = dict(self.config['plugins']['entries']['shadow-army']['settings'])
        settings = self.config['plugins']['entries']['shadow-army']['settings']
        try:
            settings.update(verifier_provider='openai', verifier_model='gpt-5.4', pass_lifetime_minutes=15)
            (self.home / 'config.yaml').write_text(yaml.safe_dump(self.config))
            cfg = self.gates.config()
            self.assertEqual(cfg['verifier']['provider'], 'openai')
            self.assertEqual(cfg['verifier']['model'], 'gpt-5.4')
            self.assertEqual(cfg['max_age_s'], 900)
        finally:
            settings.clear()
            settings.update(original)
            (self.home / 'config.yaml').write_text(yaml.safe_dump(self.config))
        self.assertEqual(self.gates.config()['max_age_s'], 3600)
        self.assertEqual(self.gates.config()['verifier'], original['verifier'])

    def test_01_dispatch_gate_and_observed_terminal(self):
        from tools.delegate_tool_routes import _route_one
        from tools.registry import registry
        gate_args = dict(card=self.card)
        close_args = dict(task_id=self.card, summary='Reviewed synthetic artifact')
        self.assertIn('UNVERIFIED', self.call('kanban_complete', close_args)['error'])
        self.assertIn('bound', self.call('army_verdict', dict(gate_args, verdict='PASS', findings=[]), 'unbound')['error'])
        self.invoke('pre_api_request', session_id='builder', provider='openai', model='gpt-5.4')
        declared = self.provider.task_parameters(self.config['delegation'])
        schema = registry.get_definitions({'delegate_task'})[0]['function']['parameters']['properties']['tasks']['items']['properties']
        self.assertEqual(schema['duty']['enum'], ['build', 'verify', 'repair'])
        self.assertIn('card', schema)
        def prepare():
            blocked, modified = self.pre('delegate_task', {'tasks': [dict(goal='Review the actual code', duty='verify', card=self.card)]}, session_id='builder')
            self.assertIsNone(blocked)
            self.assertIsInstance(modified, dict)
            return modified['tasks'][0]
        # A failed configured route is refused before constructing a child.
        self.config['plugins']['entries']['shadow-army']['settings']['verifier'] = {'provider': 'openai', 'model': 'gpt-5.4'}
        (self.home / 'config.yaml').write_text(yaml.safe_dump(self.config))
        task = prepare()
        with self.assertRaisesRegex(Exception, 'shares'):
            _route_one(self.provider, declared, 0, task, self.config['delegation'])
        self.config['plugins']['entries']['shadow-army']['settings']['verifier'] = {'provider': 'anthropic', 'model': 'claude-opus-4-6'}
        (self.home / 'config.yaml').write_text(yaml.safe_dump(self.config))
        task = prepare()
        route = _route_one(self.provider, declared, 0, task, self.config['delegation'])
        self.assertEqual(route['fallback_providers'], [])
        self.invoke('subagent_start', parent_session_id='builder', child_session_id='verifier', child_goal=task['goal'])
        blocked, _ = self.pre('delegate_task', {'tasks': [dict(goal='second', duty='verify', card=self.card)]}, session_id='builder')
        self.assertIsNone(blocked)
        with self.assertRaisesRegex(Exception, 'flight'):
            _route_one(self.provider, declared, 0, _['tasks'][0], self.config['delegation'])
        self.invoke('subagent_start', parent_session_id='wrong-parent', child_session_id='impostor', child_goal=task['goal'])
        self.assertIn('bound', self.call('army_verdict', dict(gate_args, verdict='PASS', findings=[]), 'impostor')['error'])
        self.invoke('subagent_start', parent_session_id='builder', child_session_id='verifier', child_goal=task['goal'])
        self.invoke('subagent_start', parent_session_id='builder', child_session_id='replay-child', child_goal=task['goal'])
        self.assertIn('bound', self.call('army_verdict', dict(gate_args, verdict='PASS', findings=[]), 'replay-child')['error'])
        self.invoke('pre_api_request', session_id='verifier', provider='anthropic', model='claude-opus-4-6')
        self.invoke('post_api_request', session_id='verifier', provider='anthropic', model='claude-opus-4-6', response_model='claude-opus-4-6')
        verdict_args = dict(gate_args, verdict='PASS', findings=[])
        self.assertIn('check', self.call('army_verdict', verdict_args, 'verifier')['error'])
        result = self.call('terminal', dict(command='printf gate-check', workdir=str(self.repo), timeout=5), 'verifier', 'real-terminal')
        self.assertEqual(result['exit_code'], 0, result)
        result = self.call('army_verdict', verdict_args, 'verifier')
        self.assertEqual(result['state'], 'PASS', result)
        self.assertIn('replay', self.call('army_verdict', verdict_args, 'verifier')['error'])
        self.assertEqual(self.pre('kanban_complete', close_args, session_id='kanban-worker'), (None, None))
        code = "from hermes_cli.plugins import get_plugin_manager; m=get_plugin_manager(); m.discover_and_load(); import model_tools,json; print(model_tools.handle_function_call('army_gate'," + repr(gate_args) + ",session_id='second-process')); m.unload()"
        result = subprocess.run([sys.executable, '-B', '-c', code], env=os.environ.copy(), text=True, capture_output=True, timeout=25)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"state": "PASS"', result.stdout)
        self.assertEqual(self.call('army_gate', gate_args, 'worker')['state'], 'PASS')
        completed = self.call('kanban_complete', close_args, 'kanban-worker')
        self.assertNotIn('error', completed, completed)
        self.assertEqual(self.call('army_gate', gate_args)['state'], 'PASS')
        from hermes_cli.kanban_db_connect import connect
        with connect() as db:
            db.execute("UPDATE tasks SET status='ready' WHERE id=?", (self.card,))
            db.commit()
        (self.repo / 'app.txt').write_text('edited after review\n')
        self.assertEqual(self.call('army_gate', gate_args)['state'], 'STALE')
        self.assertIn('STALE', self.call('kanban_complete', close_args)['error'])
        with self.gates.store.transaction() as db:
            kinds = [e['kind'] for e in self.gates.store.history(db, self.key())]
        for kind in ('close_refused', 'refused_same_family', 'refused_unbound', 'arose', 'verdict', 'close_permitted', 'closed'):
            self.assertIn(kind, kinds)
        # The real manager accepts **kwargs on every hook, and untouched tools pass through.
        tested_hooks = set()
        for hook_name, hooks in self.manager._hooks.items():
            for entry in hooks:
                callback = entry[1] if isinstance(entry, tuple) else entry
                if callable(callback) and callback.__module__.endswith('.bridge'):
                    tested_hooks.add(hook_name)
                    self.assertTrue(any(p.kind == p.VAR_KEYWORD for p in inspect.signature(callback).parameters.values()))
        self.assertEqual(tested_hooks, {'pre_tool_call', 'post_tool_call', 'subagent_start', 'subagent_stop', 'pre_api_request', 'post_api_request'})
        with patch.object(self.gates, 'tree', side_effect=RuntimeError('synthetic fingerprint fault')):
            block, _ = self.pre('kanban_complete', close_args, session_id='builder')
            self.assertIn('fingerprint fault', str(block))
        self.assertEqual(self.pre('unrelated', {}, session_id='builder'), (None, None))
        with patch.object(self.gates, 'terminal_start', side_effect=RuntimeError('evidence store unavailable')):
            self.assertEqual(self.pre('terminal', {'command': 'printf untouched'}, session_id='unbound'), (None, None))
        self.config['delegation']['backend'] = 'default'
        (self.home / 'config.yaml').write_text(yaml.safe_dump(self.config))
        self.assertEqual(self.pre('delegate_task', {'tasks': [{'goal': 'ordinary delegation'}]}, session_id='builder'), (None, None))
        self.config['delegation']['backend'] = 'shadow-army'
        (self.home / 'config.yaml').write_text(yaml.safe_dump(self.config))
        frames = [f if isinstance(f, dict) else json.loads(f) for f in self.frames]
        events = [f['params'] for f in frames if f.get('params', {}).get('type') == 'plugin.shadow-army.gate.changed']
        self.assertTrue(events, frames)
        self.assertTrue(any(e.get('payload', {}).get('card') == self.card for e in events))
        print('DISPATCH PROOF: real terminal exit=0; PASS; second process PASS; actual Kanban close; edit STALE; close refused. Route and child lifecycle observations are synthetic.')

    def test_03_non_code_and_broken_repository(self):
        settings = self.config['plugins']['entries']['shadow-army']['settings']
        repo = settings.pop('repo')
        (self.home / 'config.yaml').write_text(yaml.safe_dump(self.config))
        try:
            from hermes_cli import kanban_db as kb
            from hermes_cli.kanban_db_connect import connect
            db = connect()
            card = kb.create_task(db, title='Non-code planning', assignee='test')
            db.close()
            card = card['id'] if isinstance(card, dict) else card
            args = dict(task_id=card, summary='Planning complete')
            self.assertEqual(self.pre('kanban_complete', args), (None, None))
            self.assertEqual(self.call('army_gate', dict(card=card))['state'], 'UNGATED')
            self.assertNotIn('error', self.call('kanban_complete', args))
            self.assertEqual(self.call('army_gate', dict(card=card))['state'], 'UNGATED')
            card = self.fresh_card()
            settings['repo'] = str(self.home / 'broken-repository')
            (self.home / 'config.yaml').write_text(yaml.safe_dump(self.config))
            self.assertEqual(self.call('army_gate', dict(card=card))['state'], 'UNKNOWN')
            self.assertIn('UNKNOWN', self.call('kanban_complete', dict(task_id=card, summary='Must block'))['error'])
        finally:
            settings['repo'] = repo
            (self.home / 'config.yaml').write_text(yaml.safe_dump(self.config))

    def test_034_workspace_gate_scope(self):
        from hermes_cli.kanban_db_connect import connect
        settings = self.config['plugins']['entries']['shadow-army']['settings']
        repo = settings.pop('repo')
        self.configure()
        scratch = self.home / 'scratch'
        scratch.mkdir()
        nested = self.repo / 'scratch'
        nested.mkdir()
        plain = self.home / 'plain-dir'
        plain.mkdir()
        try:
            self.assertTrue(self.gates.armed())
            for kind, workspace in [('scratch', scratch), ('scratch', nested), ('dir', plain)]:
                with self.subTest(kind=kind, nested=workspace == nested):
                    card = self.fresh_card(workspace)
                    with connect() as db:
                        db.execute('UPDATE tasks SET workspace_kind=? WHERE id=?', (kind, card))
                        db.commit()
                    args = dict(task_id=card, summary='Ungated completion')
                    self.assertEqual(self.call('army_gate', dict(card=card))['state'], 'UNGATED')
                    self.assertEqual(self.pre('kanban_complete', args), (None, None))
                    self.assertNotIn('error', self.call('kanban_complete', args))
            card = self.fresh_card(self.repo)
            self.assertEqual(self.call('army_gate', dict(card=card))['state'], 'UNVERIFIED')
            self.assertIn('UNVERIFIED', self.call('kanban_complete', dict(task_id=card, summary='Must block'))['error'])
            with connect() as db:
                db.execute('UPDATE tasks SET workspace_path=? WHERE id=?', (str(self.home / 'missing-worktree'), card))
                db.commit()
            self.assertEqual(self.call('army_gate', dict(card=card))['state'], 'UNKNOWN')
            self.assertIn('worktree is missing', self.call('army_gate', dict(card=card))['reason'])
            self.assertIn('UNKNOWN', self.call('kanban_complete', dict(task_id=card, summary='Must block'))['error'])
            settings['repo'] = repo
            self.configure()
            card = self.fresh_card(scratch)
            with connect() as db:
                db.execute("UPDATE tasks SET workspace_kind='scratch' WHERE id=?", (card,))
                db.commit()
            self.assertEqual(self.call('army_gate', dict(card=card))['state'], 'UNVERIFIED')
            self.assertIn('UNVERIFIED', self.call('kanban_complete', dict(task_id=card, summary='Opted in'))['error'])
        finally:
            settings['repo'] = repo
            self.configure()
            nested.rmdir()

    def test_035_completion_uses_host_board_and_task_context(self):
        from agent.delegation_context import delegated_child_context, non_dispatcher_owned_context
        from hermes_cli import kanban_db as kb
        from contextlib import closing
        import sqlite3
        other = self.home / 'kanban/boards/other'
        other.mkdir(parents=True)
        (other / 'board.json').write_text(json.dumps(dict(slug='other', name='Other')))
        import shutil
        copy_db(self.home / 'kanban.db', other / 'kanban.db')
        with closing(sqlite3.connect(other / 'kanban.db')) as db:
            db.execute('UPDATE tasks SET title=? WHERE id=?', ('Other board card', self.card))
            db.commit()
        boards = sys.modules[self.gates.__class__.__module__].boards
        from tools.registry import registry
        def key(args):
            return boards.completion_key(args, registry.dispatch)
        with kb.scoped_current_board('other'):
            self.assertEqual(key(dict(task_id=self.card)).board, 'other')
        (self.home / 'kanban/current').write_text('OTHER\n')
        try:
            self.assertEqual(key(dict(task_id=self.card)).board, 'other')
            self.assertEqual(self.gates.key_for(self.card).board, 'other')
        finally:
            (self.home / 'kanban/current').unlink()
        with patch.dict(os.environ, {'HERMES_KANBAN_BOARD': 'other', 'HERMES_KANBAN_TASK': self.card}):
            self.assertEqual(key({}).board, 'other')
            self.assertEqual(key(dict(task_id='  ' + self.card + '  ', board='default')).board, 'default')
            with non_dispatcher_owned_context():
                self.assertIsNone(key({}))
            with delegated_child_context():
                self.assertIsNone(key({}))
        with patch.dict(os.environ, {'HERMES_KANBAN_DB': str(other / 'kanban.db'), 'HERMES_KANBAN_TASK': self.card}):
            self.assertEqual(key(dict(task_id=self.card, board='default')).board, 'other')
        self.assertEqual(self.pre('kanban_complete', dict(task_id='missing-card')), (None, None))
        with patch.object(registry, 'dispatch', return_value=json.dumps({'error': 'database is locked'})):
            self.assertEqual(self.pre('kanban_complete', dict(task_id=self.card)), (None, None))
        shutil.rmtree(other)

    def test_04_unbound_delegation_and_per_task_refusal(self):
        from tools.delegate_tool_routes import _route_one
        tasks = [dict(goal='ordinary'), dict(goal='build unbound', duty='build'),
                 dict(goal='repair unbound', duty='repair'), dict(goal='malformed verifier', duty='verify')]
        from agent.runtime_cwd import set_session_cwd, reset_session_cwd
        token = set_session_cwd(str(self.repo))
        try:
            blocked, modified = self.pre('delegate_task', dict(tasks=tasks), session_id='ordinary-parent')
        finally:
            reset_session_cwd(token)
        self.assertIsNone(blocked)
        self.assertEqual(modified['tasks'][:3], tasks[:3])
        self.assertEqual(modified['tasks'][3]['board'], 'workspace:')
        cfg = dict(self.config['delegation'], provider='openai', model='gpt-5.4')
        declared = self.provider.task_parameters(cfg)
        for i, task in enumerate(modified['tasks'][:3]):
            self.assertEqual(_route_one(self.provider, declared, i, task, cfg), dict(provider='openai', model='gpt-5.4'))
        with self.assertRaisesRegex(Exception, 'No builder model observed'):
            _route_one(self.provider, declared, 3, modified['tasks'][3], cfg)
        self.assertEqual(_route_one(self.provider, declared, 0, modified['tasks'][0], cfg)['model'], 'gpt-5.4')

        from tools.delegate_tool_routes import resolve_task_routes
        with patch('tools.delegate_tool._resolve_delegation_credentials', return_value={'model': 'gpt-5.4'}):
            routes, error = resolve_task_routes(modified['tasks'], cfg, object())
        self.assertIsNone(error)
        self.assertEqual([bool(r.error) for r in routes], [False, False, False, True])
        self.assertIn('refused', routes[3].refused_entry(3)['error'].lower())

    def test_05_bounded_observers_and_locked_database(self):
        import sqlite3
        from contextlib import contextmanager
        store = self.gates.store
        with patch.object(store, 'transaction', wraps=store.transaction) as writes:
            for _ in range(500):
                self.invoke('pre_api_request', session_id='repeat', provider='openai', model='gpt-repeat')
            self.assertEqual(writes.call_count, 1)
        for i in range(25):
            self.invoke('pre_api_request', session_id='overflow', provider='openai', model='gpt-' + str(i))
        with store.read() as db:
            row = store.get(db, 'session', 'overflow')
            self.assertEqual(len(row['routes']), 16)
            self.assertTrue(row['too_many_routes'])
            self.assertIsNone(self.gates.observations(db, 'overflow')[-1]['family'])
        task = self.gates.prepare([dict(goal='verify', duty='verify', card=self.card)], 'overflow')[0]
        with self.assertRaisesRegex(Exception, '[Tt]oo many routes'):
            self.provider.route_task(task, self.config['delegation'])
        with patch.object(store, 'transaction', side_effect=AssertionError('unexpected write')):
            self.pre('terminal', dict(command='ordinary'), session_id='ordinary-session')
            self.invoke('post_tool_call', tool_name='terminal', session_id='ordinary-session', result={})
        @contextmanager
        def locked():
            raise sqlite3.OperationalError('database is locked')
            yield
        callbacks = {}
        for name, hooks in self.manager._hooks.items():
            for entry in hooks:
                callback = entry[1] if isinstance(entry, tuple) else entry
                if callable(callback) and callback.__module__.endswith('.bridge'):
                    callbacks[name] = callback
        with patch.object(store, 'read', locked), patch.object(store, 'transaction', locked):
            for name in ('subagent_start', 'subagent_stop', 'pre_api_request', 'post_api_request', 'post_tool_call'):
                callbacks[name](parent_session_id='p', child_session_id='c', child_goal=task['goal'],
                                session_id='locked-new', provider='openai', model='gpt-new', tool_name='terminal', result={})
        lock = sqlite3.connect(store.path)
        try:
            lock.execute('BEGIN EXCLUSIVE')
            for name in ('subagent_start', 'subagent_stop', 'pre_api_request', 'post_api_request', 'post_tool_call'):
                callbacks[name](parent_session_id='p', child_session_id='c', child_goal=task['goal'],
                                session_id='locked-actual', provider='openai', model='gpt-new', tool_name='terminal', result={})
        finally:
            lock.rollback()
            lock.close()
        import time
        legacy = dict(provider='openai', model='gpt-legacy', family='openai', observed_at=time.time())
        with store.transaction() as db:
            store.put(db, 'session', 'legacy', [legacy] * 500)
        self.invoke('pre_api_request', session_id='legacy', provider='openai', model='gpt-legacy')
        with store.read() as db:
            self.assertEqual(len(store.get(db, 'session', 'legacy')['routes']), 1)
        with store.transaction() as db:
            for kind in ('session', 'terminal_pending', 'check'):
                store.put(db, kind, 'old', dict(observed_at=1))
        with store.transaction() as db:
            for kind in ('session', 'terminal_pending', 'check'):
                self.assertIsNone(store.get(db, kind, 'old'))

    def fresh_card(self, workspace=None, worker=None):
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect
        import uuid
        with connect() as db:
            card = kb.create_task(db, title='Regression ' + uuid.uuid4().hex, assignee='test')
            card = card['id'] if isinstance(card, dict) else card
            if workspace:
                db.execute("UPDATE tasks SET workspace_path=?,workspace_kind='worktree' WHERE id=?", (str(workspace), card))
                if worker:
                    db.execute('INSERT INTO task_runs(task_id,profile,status,started_at,metadata) VALUES(?,?,?,?,?)', (card, 'test', 'done', 1, json.dumps({'worker_session_id': worker})))
                db.commit()
        return card

    def configure(self):
        (self.home / 'config.yaml').write_text(yaml.safe_dump(self.config))

    def bind_card(self, card, command='printf gate-check', start=True):
        import uuid
        parent, child = 'parent-' + uuid.uuid4().hex, 'child-' + uuid.uuid4().hex
        self.config['plugins']['entries']['shadow-army']['settings']['check'] = command
        self.configure()
        self.invoke('pre_api_request', session_id=parent, provider='openai', model='gpt-5.4')
        blocked, modified = self.pre('delegate_task', dict(tasks=[dict(card=card, duty='verify', goal='Review')]), session_id=parent)
        self.assertIsNone(blocked)
        task = modified['tasks'][0]
        self.provider.route_task(task, self.config['delegation'])
        if start:
            self.invoke('subagent_start', parent_session_id=parent, child_session_id=child, child_goal=task['goal'])
            self.invoke('post_api_request', session_id=child, provider='anthropic', model='claude-opus-4-6', response_model='claude-opus-4-6')
        return parent, child, task

    def test_06_alias_identity_workspace_and_shared_home(self):
        import shutil
        from contextlib import closing
        import sqlite3
        from tools.registry import registry
        settings = self.config['plugins']['entries']['shadow-army']['settings']
        other = self.home / 'actual-repo'
        shutil.copytree(self.repo, other)
        card = self.fresh_card(other)
        boards = sys.modules[self.gates.__class__.__module__].boards
        key = self.gates.key_for('  ' + card + ' ', ' DEFAULT ')
        self.assertEqual(key.board, 'default')
        self.assertEqual(key.card_id, card)
        self.assertEqual(boards.repository(key, settings), str(other))
        self.assertEqual(key, boards.completion_key(dict(task_id=' ' + card + ' ', board='DEFAULT'), registry.dispatch))
        parent, child, task = self.bind_card(card)
        with patch.dict(os.environ, HERMES_KANBAN_TASK=card):
            self.assertEqual(boards.completion_key(dict(task_id='  ', board='DEFAULT'), registry.dispatch), key)
        self.call('terminal', dict(command='printf gate-check', workdir=str(self.repo)), child, 'wrong-fallback-repo')
        self.assertIn('check', self.call('army_verdict', dict(card=' ' + card + ' ', board='DEFAULT', verdict='PASS'), child)['error'])
        self.assertIn('UNVERIFIED', self.call('kanban_complete', dict(task_id=' ' + card + ' ', board='DEFAULT', summary='must refuse'))['error'])
        broken = self.home / 'broken-explicit-repo'
        (broken / '.git').mkdir(parents=True)
        with closing(sqlite3.connect(self.home / 'kanban.db')) as db:
            db.execute('UPDATE tasks SET workspace_path=? WHERE id=?', (str(broken), card))
            db.commit()
        unknown = self.call('army_gate', dict(card=card))
        self.assertEqual(unknown['state'], 'UNKNOWN')
        self.assertIn('fingerprint', unknown['reason'])
        with patch.dict(os.environ, {'HERMES_HOME': str(self.home / 'profiles/named'), 'HERMES_KANBAN_HOME': str(self.home)}):
            with patch.dict(os.environ):
                os.environ.pop('SHADOW_ARMY_KANBAN_HOME', None)
                self.assertEqual(boards.root(), self.home)
                self.assertIn(card, [row['id'] for row in boards.cards()[0]])
                self.assertEqual(boards.completion_key(dict(task_id=card, board='DEFAULT'), registry.dispatch), key)
                os.environ.pop('HERMES_KANBAN_HOME', None)
                from hermes_cli import kanban_db as kb
                self.assertEqual(boards.root(), kb.kanban_home())
                self.assertEqual(boards.root(), self.home)

    def test_07_background_promoted_and_noninteger_checks_refuse(self):
        cases = [(dict(background=True), dict(exit_code=0)),
                 ({}, dict(exit_code=0, pid=1234, session_id='bg')),
                 ({}, dict(exit_code=0, promoted_from_foreground=True)),
                 ({}, dict(exit_code=0, running=True)),
                 ({}, dict(exit_code=0, yielded_session_id='still-running')),
                 ({}, dict(exit_code=True)), ({}, dict(exit_code='0')), ({}, {})]
        for index, (extra, result) in enumerate(cases):
            with self.subTest(index=index):
                card = self.fresh_card()
                _, child, _ = self.bind_card(card)
                args = dict(command='printf gate-check', workdir=str(self.repo), **extra)
                self.pre('terminal', args, session_id=child, tool_call_id='bg')
                self.invoke('post_tool_call', tool_name='terminal', args=args, result=result, session_id=child, tool_call_id='bg')
                self.assertIn('check', self.call('army_verdict', dict(card=card, verdict='PASS'), child)['error'])
        card = self.fresh_card()
        _, child, _ = self.bind_card(card, 'printf gate-check &')
        blocked, _ = self.pre('terminal', dict(command='printf gate-check &', workdir=str(self.repo)), session_id=child, tool_call_id='ampersand')
        self.assertIn('foreground', str(blocked))

    def test_08_session_path_check_and_nonlocal_refusal(self):
        import shlex
        repo = self.repo
        venv = self.home / 'venv-bin'
        venv.mkdir()
        check = venv / 'projcheck'
        check.write_text('#!/bin/sh\nmkdir -p __pycache__\nprintf cache > __pycache__/check.pyc\nprintf "session path works"\n')
        check.chmod(0o755)
        card = self.fresh_card(repo)
        _, child, _ = self.bind_card(card, 'projcheck')
        exported = self.call('terminal', dict(command='export PATH=' + shlex.quote(str(venv)) + ':"$PATH"', workdir=str(repo)), child, 'venv-path')
        self.assertEqual(exported['exit_code'], 0)
        result = self.call('terminal', dict(command='projcheck', workdir=str(repo)), child, 'session-check')
        self.assertEqual(result['exit_code'], 0, result)
        self.assertIn('session path works', result['output'])
        self.assertTrue((repo / '__pycache__/check.pyc').is_file())
        self.assertEqual(self.call('army_verdict', dict(card=card, verdict='PASS'), child)['state'], 'PASS')
        card = self.fresh_card(repo)
        _, child, _ = self.bind_card(card, 'false')
        failed = self.call('terminal', dict(command='false', workdir=str(repo)), child, 'real-failure')
        self.assertEqual(failed['exit_code'], 1)
        self.assertIn('check', self.call('army_verdict', dict(card=card, verdict='PASS'), child)['error'])
        verdict = self.call('army_verdict', dict(card=card, verdict='FAIL', findings=[dict(file='app.txt', line=1, text='Configured check failed')]), child)
        self.assertEqual(verdict['state'], 'BLOCKED')
        card = self.fresh_card(repo)
        _, child, _ = self.bind_card(card)
        self.config['terminal']['backend'] = 'ssh'
        self.configure()
        from tools.terminal_scope import set_terminal_scope, reset_terminal_scope
        terminal_token = set_terminal_scope({'TERMINAL_ENV': 'ssh'})
        try:
            blocked, _ = self.pre('terminal', dict(command='printf gate-check', workdir=str(repo)), session_id=child, tool_call_id='remote')
            self.assertIn('local terminal', str(blocked))
            self.invoke('post_tool_call', tool_name='terminal', result=dict(exit_code=0), session_id=child, tool_call_id='remote')
            self.assertIn('check', self.call('army_verdict', dict(card=card, verdict='PASS'), child)['error'])
        finally:
            reset_terminal_scope(terminal_token)
            self.config['terminal']['backend'] = 'local'
            self.configure()

    def test_09_unarmed_completion_is_untouched(self):
        settings = self.config['plugins']['entries']['shadow-army']['settings']
        for backend, verifier in [('default', settings['verifier']), ('shadow-army', None), ('shadow-army', {})]:
            with self.subTest(backend=backend, verifier=verifier):
                old_backend, old_verifier = self.config['delegation']['backend'], settings['verifier']
                try:
                    self.config['delegation']['backend'], settings['verifier'] = backend, verifier
                    self.configure()
                    card = self.fresh_card(self.repo)
                    self.assertEqual(self.call('army_gate', dict(card=card))['state'], 'UNARMED')
                    self.assertEqual(self.pre('kanban_complete', dict(task_id=card, summary='Ungated')), (None, None))
                    self.assertTrue(self.call('kanban_complete', dict(task_id=card, summary='Ungated'))['ok'])
                finally:
                    self.config['delegation']['backend'], settings['verifier'] = old_backend, old_verifier
                    self.configure()

    def test_10_failed_or_unstarted_binding_retries_and_json_tasks(self):
        card = self.fresh_card()
        parent, _, task = self.bind_card(card, start=False)
        _, modified = self.pre('delegate_task', dict(tasks=json.dumps([dict(card=card, duty='verify')])), session_id=parent)
        retry = modified['tasks'][0]
        self.provider.route_task(retry, self.config['delegation'])
        self.invoke('post_tool_call', tool_name='delegate_task', args=modified, session_id=parent,
                    result=json.dumps(dict(results=[dict(task_index=0, status='error', error='Synthetic credentials unavailable', route={'refused': True})])))
        _, next_modified = self.pre('delegate_task', dict(tasks=[dict(card=card, duty='verify')]), session_id=parent)
        self.provider.route_task(next_modified['tasks'][0], self.config['delegation'])
        blocked, _ = self.pre('delegate_task', dict(tasks='{broken'), session_id=parent)
        self.assertIn('JSON array', str(blocked))

    def test_11_edit_during_check_cannot_pass_after_restore(self):
        card = self.fresh_card()
        _, child, _ = self.bind_card(card)
        before = (self.repo / 'app.txt').read_text()
        self.pre('terminal', dict(command='printf gate-check', workdir=str(self.repo)), session_id=child, tool_call_id='edit-check')
        (self.repo / 'app.txt').write_text('edited while checking')
        self.invoke('post_tool_call', tool_name='terminal', result=dict(exit_code=0, output='OK'), session_id=child, tool_call_id='edit-check')
        (self.repo / 'app.txt').write_text(before)
        result = self.call('army_verdict', dict(card=card, verdict='PASS'), child)
        self.assertIn('check', result['error'])
        self.assertNotIn('tree or check changed since dispatch', result['error'])

    def test_12_close_permission_and_actual_completion_are_distinct(self):
        card = self.fresh_card()
        _, child, _ = self.bind_card(card)
        self.call('terminal', dict(command='printf gate-check', workdir=str(self.repo)), child, 'close-check')
        self.assertEqual(self.call('army_verdict', dict(card=card, verdict='PASS'), child)['state'], 'PASS')
        failed = self.call('kanban_complete', dict(task_id=card))
        self.assertIn('provide at least one', failed['error'])
        key = self.gates.key_for(card)
        with self.gates.store.read() as db:
            history = self.gates.store.history(db, key)
        self.assertEqual(history[-1]['kind'], 'close_permitted')
        self.assertNotIn('closed', [e['kind'] for e in history])
        self.assertTrue(self.call('kanban_complete', dict(task_id=card, summary='Checked'))['ok'])
        with self.gates.store.read() as db:
            event = self.gates.store.history(db, key)[-1]
            self.assertEqual(event['kind'], 'closed')
            self.assertEqual(event['gate_at_close'], 'PASS')

    def test_13_card_worker_family_and_unknown_model_are_refused(self):
        for model, expected in [('claude-worker', 'shares'), ('custom-worker-alias', 'custom-worker-alias')]:
            with self.subTest(model=model):
                card = self.fresh_card(self.repo, worker='worker-' + model)
                self.invoke('pre_api_request', session_id='worker-' + model, provider='anthropic', model=model)
                self.invoke('pre_api_request', session_id='coordinator-' + model, provider='openai', model='gpt-coordinator')
                _, modified = self.pre('delegate_task', dict(tasks=[dict(card=card, duty='verify')]), session_id='coordinator-' + model)
                with self.assertRaisesRegex(Exception, expected):
                    self.provider.route_task(modified['tasks'][0], self.config['delegation'])
                with self.gates.store.read() as db:
                    binding = next(b for b in self.gates.store.rows(db, 'binding') if b['card_id'] == card)
                    self.assertTrue(binding['refused'])
                    if expected == 'shares':
                        event = self.gates.store.history(db, self.gates.key_for(card))[-1]
                        self.assertEqual(event['builder_family'], 'anthropic')
                        self.assertEqual(event['finding'], 'Verifier requested: Anthropic. An Anthropic session requested this review or built this card.')

    def test_14_flagged_edits_change_identity_through_manager(self):
        card = self.fresh_card(self.repo)
        before = (self.repo / 'app.txt').read_text()
        for flag in ('assume-unchanged', 'skip-worktree'):
            with self.subTest(flag=flag):
                subprocess.run(['git', '-C', str(self.repo), 'update-index', '--' + flag, 'app.txt'], check=True)
                try:
                    original = self.call('army_gate', dict(card=card))['tree']
                    (self.repo / 'app.txt').write_text('flagged edit')
                    result = self.call('army_gate', dict(card=card))
                    self.assertEqual(result['state'], 'UNVERIFIED')
                    self.assertNotEqual(result['tree'], original)
                finally:
                    (self.repo / 'app.txt').write_text(before)
                    subprocess.run(['git', '-C', str(self.repo), 'update-index', '--no-' + flag, 'app.txt'], check=True)

    def test_15_corrupt_unrelated_board_does_not_block_plain_card(self):
        settings = self.config['plugins']['entries']['shadow-army']['settings']
        repo = settings.pop('repo')
        bad = self.home / 'kanban/boards/unrelated'
        bad.mkdir(parents=True)
        (bad / 'kanban.db').write_bytes(b'not a sqlite database')
        self.configure()
        try:
            card = self.fresh_card()
            self.assertTrue(self.call('kanban_complete', dict(task_id=card, summary='Planning complete'))['ok'])
        finally:
            (bad / 'kanban.db').unlink()
            bad.rmdir()
            settings['repo'] = repo
            self.configure()

    def test_16_named_board_verdict_uses_child_binding(self):
        from hermes_cli import kanban_db as kb
        import shutil
        named = self.home / 'kanban/boards/feature'
        named.mkdir(parents=True)
        (named / 'board.json').write_text(json.dumps(dict(slug='feature', name='Feature')))
        try:
            for include_card in (True, False):
                with kb.scoped_current_board('feature'):
                    card = self.fresh_card(self.repo)
                self.assertEqual(self.call('army_gate', dict(card=card))['state'], 'UNVERIFIED')
                parent, child, task = self.bind_card(card)
                self.assertIn('on board feature', task['goal'])
                self.assertIn('in repository ' + str(self.repo), task['goal'])
                self.assertIn('does not match', self.call('army_verdict', dict(card='wrong-card', verdict='PASS'), child)['error'])
                self.assertIn('does not match', self.call('army_verdict', dict(board='default', verdict='PASS'), child)['error'])
                self.call('terminal', dict(command='printf gate-check', workdir=str(self.repo)), child, 'named-check')
                args = dict(verdict='PASS', findings=[])
                if include_card:
                    args['card'] = card
                self.assertEqual(self.call('army_verdict', args, child)['state'], 'PASS')
                with kb.scoped_current_board('feature'):
                    self.assertTrue(self.call('kanban_complete', dict(task_id=card, summary='Reviewed'))['ok'])
            duplicate = self.home / 'kanban/boards/duplicate'
            duplicate.mkdir(parents=True)
            (duplicate / 'board.json').write_text(json.dumps(dict(slug='duplicate', name='Duplicate')))
            copy_db(named / 'kanban.db', duplicate / 'kanban.db')
            self.assertIn('Ambiguous', self.call('army_gate', dict(card=card))['error'])
        finally:
            shutil.rmtree(named)
            if (self.home / 'kanban/boards/duplicate').exists():
                shutil.rmtree(self.home / 'kanban/boards/duplicate')

    def test_17_odd_workspace_snapshot_and_completion(self):
        from fastapi.testclient import TestClient
        from hermes_cli import web_server
        from hermes_cli.kanban_db_connect import connect
        from contextlib import nullcontext
        settings = self.config['plugins']['entries']['shadow-army']['settings']
        repo = settings.pop('repo')
        self.configure()
        file = self.home / 'workspace-file'
        file.write_text('not a directory')
        loop = self.home / 'workspace-loop'
        loop.symlink_to(loop)
        unreadable = self.home / 'unreadable'
        unreadable.mkdir()
        original = Path.stat
        def stat(path, *args, **kwargs):
            if path == unreadable:
                raise PermissionError('Synthetic unreadable directory')
            return original(path, *args, **kwargs)
        client = TestClient(web_server.app)
        headers = {'Authorization': 'Bearer synthetic-test-session'}
        try:
            for workspace in (file, loop, unreadable):
                for kind in ('scratch', 'dir', 'worktree'):
                    with self.subTest(workspace=workspace.name, kind=kind):
                        card = self.fresh_card()
                        with connect() as db:
                            db.execute('UPDATE tasks SET workspace_path=?,workspace_kind=? WHERE id=?', (str(workspace), kind, card))
                            db.commit()
                        with patch.object(Path, 'stat', stat) if workspace == unreadable else nullcontext():
                            expected = 'UNKNOWN' if kind == 'worktree' else 'UNGATED'
                            gate = self.call('army_gate', dict(card=card))
                            self.assertEqual(gate['state'], expected, gate)
                            response = client.get('/api/plugins/shadow-army/snapshot', headers=headers)
                            self.assertEqual(response.status_code, 200)
                            data = response.json()
                            self.assertEqual(data['errors'], [])
                            projected = next(c for c in data['cards'] if c['id'] == card)
                            self.assertEqual(projected['gate']['state'], expected)
                            if kind == 'worktree':
                                self.assertIn('restore', projected['gate']['reason'])
                                self.assertIn('UNKNOWN', self.call('kanban_complete', dict(task_id=card, summary='Must block'))['error'])
                            else:
                                self.assertEqual(self.pre('kanban_complete', dict(task_id=card, summary='Planning complete')), (None, None))
        finally:
            settings['repo'] = repo
            self.configure()
            loop.unlink()

    def test_18_refused_batch_finishes_only_matching_unstarted_bindings(self):
        for field, entry in [('results', dict(task_index=1, status='failed')),
                             ('refused', dict(index=1, status='error', error='Synthetic unavailable route')),
                             ('failed', dict(index=1))]:
            with self.subTest(field=field):
                first, second = self.fresh_card(), self.fresh_card()
                parent = 'batch-' + first
                self.invoke('pre_api_request', session_id=parent, provider='openai', model='gpt-5.4')
                _, modified = self.pre('delegate_task', dict(tasks=[dict(card=c, duty='verify') for c in (first, second)]), session_id=parent)
                for task in modified['tasks']:
                    self.provider.route_task(task, self.config['delegation'])
                child = 'running-' + first
                self.invoke('subagent_start', parent_session_id=parent, child_session_id=child, child_goal=modified['tasks'][0]['goal'])
                self.invoke('post_tool_call', tool_name='delegate_task', args=modified, session_id=parent,
                            result=json.dumps(dict(status='dispatched', dispatched=[dict(index=0)], **{field: [entry]})))
                with self.gates.store.read() as db:
                    bindings = {b['card_id']: b for b in self.gates.store.rows(db, 'binding') if b['parent_session'] == parent}
                self.assertTrue(bindings[second]['finished_at'])
                self.assertTrue(bindings[second]['refused'])
                self.assertIsNone(bindings[first]['finished_at'])
                self.invoke('subagent_stop', child_session_id=child)

    def test_19_trial_reason_and_unarmed_hint(self):
        from fastapi.testclient import TestClient
        from hermes_cli import web_server
        client = TestClient(web_server.app)
        prefix = '/api/plugins/shadow-army'
        headers = {'Authorization': 'Bearer synthetic-test-session'}
        self.assertEqual(client.post(prefix + '/demo/start', headers=headers).status_code, 200)
        for _ in range(6):
            self.assertEqual(client.post(prefix + '/demo/step', headers=headers).status_code, 200)
        response = client.post(prefix + '/demo/tamper', headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['cards'][0]['gate']['reason'],
                         'One line changed after the PASS. The old verdict no longer counts.')
        self.assertEqual(client.post(prefix + '/demo/reset', headers=headers).status_code, 200)
        settings = self.config['plugins']['entries']['shadow-army']['settings']
        verifier = settings.pop('verifier')
        self.configure()
        try:
            card = self.fresh_card(self.repo)
            data = client.get(prefix + '/snapshot', headers=headers).json()
            projected = next(c for c in data['cards'] if c['id'] == card)
            self.assertEqual(projected['gate']['state'], 'UNARMED')
            self.assertEqual(projected['hint'], 'Finish setup to arm the gate.')
        finally:
            settings['verifier'] = verifier
            self.configure()

    def pane_snapshot(self):
        from fastapi.testclient import TestClient
        from hermes_cli import web_server
        response = TestClient(web_server.app).get('/api/plugins/shadow-army/snapshot',
                                                 headers={'Authorization': 'Bearer synthetic-test-session'})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_20_closed_worktree_retains_review_without_fingerprinting(self):
        from hermes_cli.kanban_db_connect import connect
        source, worktree = self.home / 'closed-source', self.home / 'closed-worktree'
        subprocess.run(['git', 'clone', '--quiet', str(self.repo), str(source)], check=True)
        subprocess.run(['git', '-C', str(source), 'worktree', 'add', '--detach', str(worktree), 'HEAD'], check=True, capture_output=True)
        card = self.fresh_card(worktree)
        _, child, _ = self.bind_card(card)
        checked = self.call('terminal', dict(command='printf gate-check', workdir=str(worktree)), child, 'closed-check')
        self.assertEqual(checked['exit_code'], 0)
        reviewed = self.call('army_verdict', dict(card=card, verdict='PASS'), child)
        self.assertEqual(reviewed['state'], 'PASS')
        self.assertTrue(self.call('kanban_complete', dict(task_id=card, summary='Reviewed'))['ok'])
        self.assertFalse(worktree.exists(), 'Hermes must clean the actual worktree')
        with patch.object(self.gates, 'tree', side_effect=AssertionError('closed card fingerprinted')):
            for status in ('done', 'archived'):
                with connect() as db:
                    db.execute('UPDATE tasks SET status=? WHERE id=?', (status, card))
                    db.commit()
                gate = self.call('army_gate', dict(card=card))
                self.assertEqual(gate['state'], 'PASS', gate)
                self.assertEqual(gate['tree'], reviewed['tree'])
                self.assertEqual(gate['reason'], 'Closed after an independent PASS.')
        with connect() as db:
            db.execute("UPDATE tasks SET status='done' WHERE id=?", (card,))
            db.commit()
        projected = next(c for c in self.pane_snapshot()['cards'] if c['id'] == card)
        self.assertEqual(projected['gate']['state'], 'PASS')
        self.assertEqual(projected['hint'], 'Nothing to do. The card is closed.')
        self.config['delegation']['backend'] = 'default'
        self.configure()
        try:
            self.assertEqual(self.call('army_gate', dict(card=card))['state'], 'PASS')
            broken = self.fresh_card(self.home / 'missing-unarmed')
            self.assertEqual(self.call('army_gate', dict(card=broken))['state'], 'UNARMED')
            plain = self.fresh_card(self.repo)
            self.assertTrue(self.call('kanban_complete', dict(task_id=plain, summary='Unarmed close'))['ok'])
            self.assertEqual(self.call('army_gate', dict(card=plain))['state'], 'UNARMED')
        finally:
            self.config['delegation']['backend'] = 'shadow-army'
            self.configure()

    def test_21_effective_terminal_scope_controls_evidence_and_setup(self):
        from tools.terminal_scope import set_terminal_scope, reset_terminal_scope, build_profile_terminal_scope
        card = self.fresh_card(self.repo)
        _, child, _ = self.bind_card(card)
        token = set_terminal_scope({'TERMINAL_ENV': 'local'})
        try:
            with patch.dict(os.environ, TERMINAL_ENV='ssh'):
                self.assertTrue(self.gates.local_terminal())
                result = self.call('terminal', dict(command='printf gate-check', workdir=str(self.repo)), child, 'scoped-local')
                self.assertEqual(result['exit_code'], 0, result)
                self.assertEqual(self.call('army_verdict', dict(card=card, verdict='PASS'), child)['state'], 'PASS')
        finally:
            reset_terminal_scope(token)
        remote_home = self.home / 'env-remote-profile'
        remote_home.mkdir()
        (remote_home / '.env').write_text('TERMINAL_ENV=ssh\n')
        remote_scope = build_profile_terminal_scope(remote_home)
        self.assertEqual(remote_scope['TERMINAL_ENV'], 'ssh')
        card = self.fresh_card(self.repo)
        _, child, _ = self.bind_card(card)
        token = set_terminal_scope(remote_scope)
        try:
            with patch.dict(os.environ, TERMINAL_ENV='local'):
                blocked, _ = self.pre('terminal', dict(command='printf gate-check', workdir=str(self.repo)), session_id=child, tool_call_id='scoped-remote')
                self.assertIn('effective terminal', str(blocked))
                self.invoke('post_tool_call', tool_name='terminal', result=dict(exit_code=0), session_id=child, tool_call_id='scoped-remote')
                self.assertIn('check', self.call('army_verdict', dict(card=card, verdict='PASS'), child)['error'])
                setup = next(s for s in self.pane_snapshot()['setup'] if s['id'] == 'terminal')
                self.assertFalse(setup['ok'])
                self.assertIn('not local', setup['hint'])
        finally:
            reset_terminal_scope(token)

    def test_22_actionable_refusals_and_unbound_repairs(self):
        from hermes_cli.kanban_db_connect import connect
        unborn = self.home / 'unborn'
        unborn.mkdir()
        subprocess.run(['git', '-C', str(unborn), 'init'], check=True, capture_output=True)
        card = self.fresh_card(unborn)
        self.assertIn('make a first commit', self.call('army_gate', dict(card=card))['reason'])
        for duty in ('build', 'repair'):
            _, modified = self.pre('delegate_task', dict(tasks=[dict(card=card, duty=duty, goal='Fix the tree')]), session_id='repair-parent')
            self.assertEqual(modified['tasks'], [dict(duty=duty, goal='Fix the tree')])
            self.assertEqual(self.provider.route_task(modified['tasks'][0], {'model': 'gpt-builder'})['model'], 'gpt-builder')
        card = self.fresh_card(self.repo)
        outputs = [self.repo / f'generated-{i}' for i in range(6)]
        command = 'touch ' + ' '.join(path.name for path in outputs)
        _, child, _ = self.bind_card(card, command)
        try:
            self.assertEqual(self.call('terminal', dict(command=command, workdir=str(self.repo)), child, 'output-check')['exit_code'], 0)
            error = self.call('army_verdict', dict(card=card, verdict='PASS'), child)['error']
            self.assertIn('generated-0', error)
            self.assertIn('generated-4', error)
            self.assertNotIn('generated-5', error)
            self.assertIn('ignore generated check output', error)
        finally:
            for path in outputs:
                path.unlink(missing_ok=True)
        card = self.fresh_card(self.repo)
        parent = 'anthropic-coordinator'
        self.invoke('pre_api_request', session_id=parent, provider='anthropic', model='claude-coordinator')
        _, modified = self.pre('delegate_task', dict(tasks=[dict(card=card, duty='verify')]), session_id=parent)
        with self.assertRaisesRegex(Exception, 'An Anthropic session requested this review or built this card'):
            self.provider.route_task(modified['tasks'][0], self.config['delegation'])

    def test_23_snapshot_reuses_fingerprints_but_gate_decisions_do_not(self):
        import time
        source = self.home / 'polling-repo'
        subprocess.run(['git', 'clone', '--quiet', str(self.repo), str(source)], check=True)
        (source / 'nested').mkdir()
        cards = [self.fresh_card(source), self.fresh_card(source / 'nested'), self.fresh_card(source)]
        service = sys.modules[self.gates.__class__.__module__]
        original = service.fingerprint
        seen = []
        def count(repo):
            if Path(repo) == source:
                seen.append(repo)
            return original(repo)
        snapshot_service = sys.modules['_shadow_army_snapshot_backend.service']
        snapshot_module = sys.modules['_shadow_army_snapshot_backend.snapshot']
        from types import SimpleNamespace
        now = [time.monotonic()]
        clock = SimpleNamespace(monotonic=lambda: now[0], time=time.time)
        with patch.object(service, 'time', clock), patch.object(snapshot_service, 'time', clock), patch.object(snapshot_module, 'time', clock), patch.object(service, 'fingerprint', side_effect=count), patch.object(snapshot_service, 'fingerprint', side_effect=count):
            first = self.pane_snapshot()
            self.pane_snapshot()
            self.assertEqual(len(seen), 1)
            before = next(c for c in first['cards'] if c['id'] == cards[0])['gate']['tree']
            (source / 'app.txt').write_text('new code during cache window')
            current = self.call('army_gate', dict(card=cards[0]))
            self.assertNotEqual(current['tree'][:12], before)
            self.assertEqual(len(seen), 2)
            self.assertIn('UNVERIFIED', self.call('kanban_complete', dict(task_id=cards[0], summary='No review'))['error'])
            self.assertEqual(len(seen), 3)
            now[0] += 2.05
            self.pane_snapshot()
            self.assertEqual(len(seen), 4)

    def test_24_snapshot_copy_and_closed_ungated_state(self):
        from hermes_cli.kanban_db_connect import connect
        settings = self.config['plugins']['entries']['shadow-army']['settings']
        repo = settings.pop('repo')
        self.configure()
        try:
            card = self.fresh_card()
            with connect() as db:
                db.execute("UPDATE tasks SET status='blocked',result='NEEDS_DECISION: Choose a window' WHERE id=?", (card,))
                db.commit()
            projected = next(c for c in self.pane_snapshot()['cards'] if c['id'] == card)
            self.assertEqual(projected['hint'], 'Waiting on your decision.')
            self.assertTrue(self.call('kanban_complete', dict(task_id=card, summary='Decision made'))['ok'])
            self.assertEqual(self.call('army_gate', dict(card=card))['state'], 'UNGATED')
            settings['repo'] = str(self.home / 'missing-after-close')
            self.configure()
            self.assertEqual(self.call('army_gate', dict(card=card))['state'], 'UNGATED')
        finally:
            settings['repo'] = repo
            self.configure()
        card = self.fresh_card(self.repo)
        _, child, _ = self.bind_card(card, 'false')
        self.call('terminal', dict(command='false', workdir=str(self.repo)), child, 'wording-fail')
        verdict = self.call('army_verdict', dict(card=card, verdict='FAIL', findings=[dict(file='app.txt', line=1, text='Bad code')]), child)
        self.assertEqual(verdict['reason'], 'The independent review failed this code.')
        before = (self.repo / 'app.txt').read_text()
        try:
            (self.repo / 'app.txt').write_text('changed after failure')
            self.assertEqual(self.call('army_gate', dict(card=card))['reason'], 'The code changed after the last review, so that verdict no longer counts.')
        finally:
            (self.repo / 'app.txt').write_text(before)

    def test_25_bypassed_close_is_unverified_without_fingerprinting(self):
        from hermes_cli.kanban_db_connect import connect
        self.assertTrue(self.gates.armed())
        card = self.fresh_card(self.repo)
        with connect() as db:
            db.execute("UPDATE tasks SET status='done' WHERE id=?", (card,))
            db.commit()
        reason = 'Closed without an independent review. The gate did not see this close.'
        with patch.object(self.gates, 'tree', side_effect=AssertionError('closed card fingerprinted')):
            gate = self.call('army_gate', dict(card=card))
            self.assertEqual(gate['state'], 'UNVERIFIED', gate)
            self.assertEqual(gate['reason'], reason)
            self.assertIsNone(gate['tree'])
            projected = next(c for c in self.pane_snapshot()['cards'] if c['id'] == card)
            self.assertEqual(projected['gate']['state'], 'UNVERIFIED')
            self.assertEqual(projected['gate']['reason'], reason)
            self.assertEqual(projected['hint'], 'Nothing to do. The card is closed.')
        self.config['delegation']['backend'] = 'default'
        self.configure()
        try:
            unarmed = self.fresh_card(self.repo)
            self.assertTrue(self.call('kanban_complete', dict(task_id=unarmed, summary='Unarmed close'))['ok'])
        finally:
            self.config['delegation']['backend'] = 'shadow-army'
            self.configure()
        gate = self.call('army_gate', dict(card=unarmed))
        self.assertEqual(gate['state'], 'UNARMED')
        self.assertEqual(gate['reason'], 'Closed while the gate was not armed.')

    def test_26_direct_close_with_pass_receipt_keeps_review(self):
        from hermes_cli.kanban_db_connect import connect
        card = self.fresh_card(self.repo)
        _, child, _ = self.bind_card(card)
        self.call('terminal', dict(command='printf gate-check', workdir=str(self.repo)), child, 'direct-close-check')
        reviewed = self.call('army_verdict', dict(card=card, verdict='PASS'), child)
        self.assertEqual(reviewed['state'], 'PASS')
        with connect() as db:
            db.execute("UPDATE tasks SET status='done' WHERE id=?", (card,))
            db.commit()
        with patch.object(self.gates, 'tree', side_effect=AssertionError('closed card fingerprinted')):
            gate = self.call('army_gate', dict(card=card))
            self.assertEqual(gate['state'], 'PASS', gate)
            self.assertEqual(gate['tree'], reviewed['tree'])
            self.assertEqual(gate['reason'], 'Closed after an independent PASS.')

    def key(self):
        module = sys.modules[self.gates.__class__.__module__]
        return module.boards.key_for(self.card)

    def test_02_auth_mount_and_trial_routes(self):
        from fastapi.testclient import TestClient
        from hermes_cli import web_server
        client = TestClient(web_server.app)
        prefix = '/api/plugins/shadow-army'
        for path in ('/snapshot', '/demo/start', '/demo/step', '/demo/tamper', '/demo/reset'):
            call = client.get if path == '/snapshot' else client.post
            self.assertEqual(call(prefix + path).status_code, 401, path)
            self.assertEqual(call(prefix + path, headers={'Authorization': 'Bearer wrong'}).status_code, 401, path)
        headers = {'Authorization': 'Bearer synthetic-test-session'}
        response = client.get(prefix + '/snapshot', headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['schema_version'], 2)
        response = client.post(prefix + '/demo/start', headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['demo'])
