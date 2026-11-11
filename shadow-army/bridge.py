"""Public Hermes hooks and delegation-provider adapter."""
import json
from pathlib import Path
from agent.delegation_provider import DelegationProvider, DelegationRouteError

from . import boards, setup
from .service import Gates
from .store import Store

LIMITS = ('Gates only the agent kanban_complete tool, not manual dashboard edits, direct database writes or shell commands. '
          'Route identity can be blurred by proxies; unknown families refuse. Fingerprinting and completion are not atomic.')


def publish(event):
    from hermes_cli.plugin_events import broadcast_plugin_event
    broadcast_plugin_event('shadow-army', 'gate.changed', event)


def current_gates():
    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins_state import PluginState
    def setting(key, default):
        cfg = load_config_readonly() or {}
        return cfg.get('plugins', {}).get('entries', {}).get('shadow-army', {}).get('settings', {}).get(key, default)
    return Gates(Store(PluginState('shadow-army').data_dir, on_event=publish), setting,
                 host_config=load_config_readonly)


class Army(DelegationProvider):
    def __init__(self, gates):
        self.gates = gates

    @property
    def name(self):
        return 'shadow-army'

    def task_parameters(self, config):
        return {'duty': {'type': 'string', 'enum': ['build', 'verify', 'repair'], 'description': 'Verifier requires independent route and observed check.'},
                'card': {'type': 'string', 'description': 'Kanban card id to build or verify.'},
                'board': {'type': 'string', 'description': 'Board name when card ids are ambiguous.'}}

    def route_task(self, task, config):
        try:
            return self.gates.route(task, {k: v for k, v in config.items() if k not in ('backend', 'backends')})
        except Exception as exc:
            raise DelegationRouteError(str(exc)) from exc


def register_backend(ctx):
    from hermes_cli.plugins_state import PluginState
    from hermes_cli.config import load_config_readonly
    gates = Gates(Store(PluginState('shadow-army').data_dir, on_event=publish), ctx.get_config,
                  host_config=load_config_readonly, dispatch=ctx.dispatch_tool)
    setup.register_context(gates, ctx)
    ctx.on_unload(lambda: setup.contexts().pop(str(gates.store.path.resolve()), None))
    ctx.register_delegation_provider(Army(gates))

    def before(tool_name='', args=None, session_id='', tool_call_id='', **kwargs):
        setup.heartbeat(gates)
        if tool_name not in ('kanban_complete', 'delegate_task', 'terminal'):
            return None
        args = args or {}
        try:
            if tool_name == 'kanban_complete':
                key = boards.completion_key(args, ctx.dispatch_tool)
                return gates.close(key) if key else None
            if tool_name == 'delegate_task' and args.get('tasks'):
                from hermes_cli.config import load_config_readonly
                active = (load_config_readonly() or {}).get('delegation', {}).get('backend', 'default')
                if active != 'shadow-army':
                    return None
                return {'action': 'modify', 'args': {'tasks': gates.prepare(args['tasks'], session_id)}}
            if tool_name == 'terminal':
                approval = gates.publish_decision(args)
                observed = gates.terminal_start(session_id, tool_call_id, args)
                return approval or observed
        except Exception as exc:
            if tool_name == 'terminal':
                return None
            return {'action': 'block', 'message': 'Gate refused: ' + str(exc) + '. Configure repo/check/verifier and delegate duty="verify" for this card.'}
        return None

    def started(parent_session_id='', child_session_id='', child_goal='', **kwargs):
        try:
            gates.start(parent_session_id, child_session_id, child_goal)
        except Exception:
            pass

    def stopped(child_session_id='', **kwargs):
        try:
            gates.stop(child_session_id)
        except Exception:
            pass

    def request(session_id='', provider='', model='', **kwargs):
        try:
            gates.observe(session_id, provider, model)
        except Exception:
            pass

    def response(session_id='', provider='', model='', response_model='', usage=None, api_request_id='', **kwargs):
        try:
            gates.observe(session_id, provider, model, response_model, request_id=api_request_id, usage=usage)
        except Exception:
            pass

    def after(tool_name='', args=None, result=None, session_id='', tool_call_id='', **kwargs):
        try:
            setup.flush_heartbeat(gates)
            gates.repository_tool_seen(session_id, tool_name)
        except Exception:
            pass
        try:
            if tool_name == 'terminal':
                gates.terminal_end(session_id, tool_call_id, result)
            elif tool_name == 'delegate_task':
                gates.delegation_end((args or {}).get('tasks'), session_id, result)
            elif tool_name == 'kanban_complete':
                outcome = json.loads(result) if isinstance(result, str) else result
                if not isinstance(outcome, dict) or outcome.get('ok') is not True or outcome.get('error'):
                    return
                key = boards.completion_key(args or {}, ctx.dispatch_tool)
                if key and outcome.get('task_id') == key.card_id:
                    row = boards.card_row(key)
                    if row and row.get('status') == 'done':
                        current = gates.inspect(key)
                        with gates.store.transaction() as db:
                            permission = next((e for e in reversed(gates.store.history(db, key)) if e['kind'] == 'close_permitted'), {})
                            gates.event(db, key, 'closed', tree=permission.get('tree'), gate=permission.get('gate') or {k: current[k] for k in ('state', 'reason', 'tree')},
                                        finding='Host reported successful completion for this card.')
        except Exception:
            pass

    for name, callback in [('pre_tool_call', before), ('post_tool_call', after), ('subagent_start', started),
                           ('subagent_stop', stopped), ('pre_api_request', request), ('post_api_request', response)]:
        ctx.register_hook(name, callback)

    common = {'card': {'type': 'string', 'description': 'Kanban card id'}, 'board': {'type': 'string'}}
    for name in ('army_gate', 'army_verdict'):
        props = dict(common)
        if name == 'army_verdict':
            props.update(verdict={'type': 'string', 'enum': ['PASS', 'FAIL']}, findings={
                'type': 'array', 'items': {'type': 'object', 'properties': {'file': {'type': 'string'},
                    'line': {'type': 'integer'}, 'text': {'type': 'string'}}, 'required': ['file', 'line', 'text']}})
        def handler(args, session_id='', _name=name, **kwargs):
            try:
                key = (gates.key_for(args.get('card'), args.get('board')) if _name == 'army_gate' else
                       gates.verdict_key(session_id, args.get('card'), args.get('board')))
                result = gates.inspect(key) if _name == 'army_gate' else gates.verdict(key, args.get('verdict'), args.get('findings', []), session_id)
            except Exception as exc:
                result = {'error': str(exc)}
            return json.dumps(result)
        description = ('Read current code-bound gate and next step. ' if name == 'army_gate' else
                       'Record a bound verifier verdict. PASS requires all observed families known and independent, unchanged tree/check, and the exact configured terminal check with exit 0. FAIL requires findings. ')
        ctx.register_tool(name, 'shadow-army', {'name': name, 'description': description + LIMITS,
            'parameters': {'type': 'object', 'properties': props, 'required': ['verdict', 'findings'] if name == 'army_verdict' else ['card']}}, handler)
    ctx.register_skill('verify-loop', Path(__file__).parent / 'skills/verify-loop/SKILL.md', 'Build, independently verify, repair and close a card, or verify any Git workspace with Shadow Army when there is no card.')
