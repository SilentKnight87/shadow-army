import asyncio
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from test_adapter import api


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_patch = patch.dict(os.environ, {'HERMES_HOME': self.tmp.name, 'HERMES_KANBAN_HOME': self.tmp.name})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_touchable_trial_real_check_and_tamper(self):
        app = FastAPI()
        app.include_router(api.router)
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                self.assertEqual((await client.get('/snapshot?demo=1')).json()['cards'], [])
                self.assertEqual((await client.post('/demo/step')).status_code, 400)
                start = (await client.post('/demo/start')).json()
                self.assertTrue(start['demo'])
                self.assertEqual(start['cards'][0]['history'][-1]['check']['exit_code'], 1)
                states = {2: 'UNVERIFIED', 3: 'UNVERIFIED', 4: 'UNVERIFIED', 5: 'BLOCKED', 6: 'STALE', 7: 'PASS'}
                for step, expected in states.items():
                    response = await client.post('/demo/step')
                    self.assertEqual(response.status_code, 200, response.text)
                    data = response.json()
                    card = data['cards'][0]
                    self.assertEqual(data['trial']['step'], step)
                    self.assertEqual(card['gate']['state'], expected)
                    self.assertEqual(card['state'], 'in_progress')
                    event = card['history'][-1]
                    if step == 2:
                        self.assertEqual(event['check']['exit_code'], 0)
                        self.assertEqual(event['weakened'][0]['line'], 9)
                    elif step == 3:
                        self.assertEqual(event['kind'], 'close_refused')
                    elif step == 4:
                        self.assertEqual(event['kind'], 'refused_same_family')
                        self.assertEqual(event['builder_family'], 'openai')
                    elif step == 5:
                        self.assertEqual(event['verdict'], 'FAIL')
                        self.assertEqual(event['check']['exit_code'], 0)
                        self.assertIn('OK', event['check']['output_excerpt'])
                        self.assertTrue(event['check_passed_but_failed'])
                        self.assertEqual(event['findings'][0]['file'], 'test_cart.py')
                        self.assertEqual(event['findings'][0]['line'], 9)
                        self.assertIn('self.assertEqual', event['finding'])
                    elif step == 6:
                        self.assertEqual(event['check']['exit_code'], 0)
                        self.assertEqual(event['weakened'], [])
                    elif step == 7:
                        self.assertEqual(event['verdict'], 'PASS')
                        self.assertEqual(event['check']['exit_code'], 0)
                        self.assertEqual(card['gate']['tree'], event['tree'])
                self.assertTrue(all(s['simulated'] for s in card['shadows']))
                repo = api.trial_module.Trial(api.backend.current_gates()).repo
                reviewed_source = (repo / 'cart.py').read_text()
                tracked = subprocess.check_output(['git', '-C', str(repo), 'ls-files', '--', 'cart.py'], text=True)
                self.assertEqual(tracked.strip(), 'cart.py')
                edited = (await client.post('/demo/tamper')).json()
                self.assertEqual((repo / 'cart.py').read_text(), reviewed_source + '\n# Edited after review\n')
                self.assertEqual(edited['cards'][0]['gate']['state'], 'STALE')
                self.assertEqual(edited['cards'][0]['history'][-1]['kind'], 'edited')
                self.assertTrue(edited['trial']['can_step'])
                refused = (await client.post('/demo/step')).json()['cards'][0]
                self.assertEqual(refused['gate']['state'], 'STALE')
                self.assertEqual(refused['history'][-1]['kind'], 'close_refused')
                reviewed = (await client.post('/demo/step')).json()['cards'][0]
                self.assertEqual(reviewed['gate']['state'], 'PASS')
                self.assertEqual(reviewed['history'][-1]['check']['exit_code'], 0)
                closed = (await client.post('/demo/step')).json()
                self.assertTrue(closed['trial']['finished'])
                self.assertEqual(closed['cards'][0]['state'], 'done')
                self.assertEqual(closed['cards'][0]['gate']['state'], 'PASS')
                self.assertEqual([e['kind'] for e in closed['cards'][0]['history'][-2:]], ['close_permitted', 'closed'])
                closed_source = (repo / 'cart.py').read_bytes()
                self.assertEqual((await client.post('/demo/tamper')).status_code, 400)
                self.assertEqual((repo / 'cart.py').read_bytes(), closed_source)
                self.assertEqual((await client.post('/demo/step')).status_code, 400)
                self.assertEqual((await client.get('/snapshot')).json()['cards'], [])
                self.assertEqual((await client.post('/demo/reset')).json()['cards'], [])
        asyncio.run(run())

    def test_trial_contract_controls_refusal_and_post_errors(self):
        from fastapi import HTTPException
        data = api.start_demo()
        self.assertEqual(data['trial']['total'], 8)
        for step in range(1, 9):
            if step > 1:
                data = api.step_demo()
            self.assertEqual(data['trial']['step'], step)
            self.assertEqual(data['trial']['can_tamper'], step == 7)
            if step != 7:
                with self.assertRaises(HTTPException) as error:
                    api.tamper_demo()
                self.assertEqual(error.exception.status_code, 400)
            self.assertEqual(data['trial']['can_step'], step < 8)
            self.assertEqual(data['trial']['finished'], step == 8)
        self.assertEqual(data['cards'][0]['state'], 'done')
        self.assertEqual(data['cards'][0]['hint'], 'Nothing to do. The card is closed.')
        with self.assertRaises(HTTPException):
            api.step_demo()

    def test_trial_messages_and_controls_at_every_step(self):
        from fastapi import HTTPException
        initial = api.reset_demo()['trial']
        self.assertIn('trial was reset', initial['message'])
        self.assertFalse(initial['can_step'])
        self.assertFalse(initial['can_tamper'])
        messages = []
        outcomes = ['empty-cart test fails', 'deleted the failing assertion', 'refused to close',
                    'same-family reviewer was refused', 'tests pass', 'assertion is restored', 'fresh simulated PASS', 'card is closed']
        for step in range(1, 9):
            trial = (api.start_demo() if step == 1 else api.step_demo())['trial']
            self.assertIn(outcomes[step - 1], trial['message'])
            self.assertIn('Next', trial['message'])
            messages.append(trial['message'])
        self.assertEqual(len(set(messages)), 8)
        api.start_demo()
        for _ in range(6):
            api.step_demo()
        changed = api.tamper_demo()['trial']
        self.assertEqual(changed['total'], 10)
        self.assertFalse(changed['can_tamper'])
        self.assertTrue(changed['can_step'])
        self.assertIn('STALE', changed['message'])
        with self.assertRaises(HTTPException):
            api.tamper_demo()
        for step in (8, 9, 10):
            trial = api.step_demo()['trial']
            self.assertEqual(trial['step'], step)
            self.assertEqual(trial['finished'], step == 10)
            self.assertEqual(trial['can_step'], step < 10)
            self.assertFalse(trial['can_tamper'])
            self.assertIn('Next', trial['message'])
        self.assertEqual(api.reset_demo()['trial'], initial)

    def test_real_hermes_manager_dispatch_and_auth_mount(self):
        with tempfile.TemporaryDirectory(prefix='shadow-host-') as tmp:
            home = Path(tmp)
            plugin = Path(__file__).parent / 'shadow-army'
            shutil.copytree(plugin, home / 'plugins/shadow-army', ignore=shutil.ignore_patterns('__pycache__'))
            env = dict(os.environ, HERMES_HOME=tmp, HERMES_KANBAN_HOME=tmp,
                       HERMES_DASHBOARD_SESSION_TOKEN='synthetic-test-session',
                       PYTHONDONTWRITEBYTECODE='1', HERMES_DISABLE_LAZY_INSTALLS='1')
            result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', str(Path(__file__).parent),
                                     '-p', 'host_probe.py', '-v'], env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
