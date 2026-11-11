"""Profile setup through supported plugin settings; no global config writes."""
import time
import importlib
import sys
import types
from pathlib import Path
from . import boards
from .policy import family, related

ROUTING_COMMAND = 'hermes config set delegation.backend shadow-army'
HEARTBEATS = {}
LAST_FLUSH = {}


def contexts():
    # The host loads the dashboard backend under its own package name.
    name = '_shadow_army_runtime_contexts'
    if name not in sys.modules:
        module = types.ModuleType(name)
        module.contexts = {}
        sys.modules[name] = module
    return sys.modules[name].contexts


def register_context(gates, ctx):
    contexts()[str(gates.store.path.resolve())] = ctx


def heartbeat(gates):
    key = str(gates.store.path)
    HEARTBEATS[key] = time.time()


def flush_heartbeat(gates):
    key = str(gates.store.path)
    seen = HEARTBEATS.get(key)
    if seen is None:
        return
    now = time.monotonic()
    if now - LAST_FLUSH.get(key, float('-inf')) < 60:
        return
    LAST_FLUSH[key] = now
    with gates.store.transaction() as db:
        gates.store.put(db, 'setup', 'hook', dict(at=seen))


def builder_route(host):
    model = host.get('model')
    if isinstance(model, str):
        return dict(provider=host.get('provider', ''), model=model)
    return model if isinstance(model, dict) else {}


def ensure_context(gates):
    # Hermes loads a plugin's backend with the first chat, not when the server starts. Until then there is no
    # settings context here, and the pane offers the settings form instead (no private Hermes call to force it).
    return str(gates.store.path.resolve()) in contexts()


def actions(gates, *, raw=False):
    host, cfg = gates.host_config(), gates.config()
    builder = builder_route(host)
    builder_family = family(builder.get('provider'), builder.get('default', builder.get('model')), cfg['family_overrides'])
    candidates = [host.get('delegation', {}), cfg.get('verifier') or {}]
    candidates.extend(host.get('fallback_providers', [])[:24] if isinstance(host.get('fallback_providers'), list) else [])
    fallbacks = host.get('delegation', {}).get('fallback_providers')
    candidates.extend(fallbacks[:24] if isinstance(fallbacks, list) else [])
    options = host.get('delegation', {}).get('backends', {}).get('model_map') or {}
    options = options.get('options', []) if isinstance(options, dict) else []
    candidates.extend(options[:24] if isinstance(options, list) else [])
    routes, seen = [], set()
    for route in candidates:
        if not isinstance(route, dict):
            continue
        provider, model = route.get('provider'), route.get('model')
        f = family(provider, model, cfg['family_overrides'])
        if not provider or not model or not builder_family or not f or related(f, builder_family) or (provider, model) in seen:
            continue
        seen.add((provider, model))
        routes.append(dict(provider=provider if raw else boards.clean(provider), model=model if raw else boards.clean(model), family=f,
                           label=boards.clean(str(model) + ' through ' + str(provider))))
        if len(routes) == 12:
            break
    registered = ensure_context(gates)
    return dict(reason=None if registered else 'Hermes has not loaded this plugin yet in this run. Send Hermes any message and arm here, or set the verifier in the settings form.', can_arm=bool(routes and registered), routes=routes, can_set_routing=False, routing_command=ROUTING_COMMAND)


def arm(gates, provider, model):
    if not any(r['provider'] == provider and r['model'] == model for r in actions(gates, raw=True)['routes']):
        raise ValueError('Choose an already configured route independent of the profile main model.')
    ctx = contexts().get(str(gates.store.path.resolve()))
    if ctx is None:
        raise ValueError('Hermes has not loaded this plugin yet in this run. Send Hermes any message and arm again, or set the verifier in the settings form.')
    ctx.set_config('verifier_provider', provider)
    ctx.set_config('verifier_model', model)


def missing_host_contexts():
    expected = {
        'agent.runtime_cwd': ('HERMES_SESSION_CWD',),
        'tools.terminal_scope': ('hermes_terminal_scope',),
        'agent.delegation_context': ('hermes_delegated_child_context',),
        'hermes_cli.kanban_db': ('hermes_kanban_current_board_override', 'hermes_kanban_pin_first_board_resolution'),
    }
    missing = []
    for module_name, names in expected.items():
        try:
            module = importlib.import_module(module_name)
            available = {getattr(value, 'name', None) for value in vars(module).values()}
        except Exception:
            available = set()
        missing.extend(name for name in names if name not in available)
    return missing


def status(gates):
    cfg, host = gates.config(), gates.host_config()
    builder = builder_route(host)
    b = family(builder.get('provider'), builder.get('default', builder.get('model')), cfg['family_overrides'])
    verifier = cfg['verifier'] or {}
    v = family(verifier.get('provider'), verifier.get('model'), cfg['family_overrides'])
    with gates.store.read() as db:
        saved = gates.store.get(db, 'setup', 'hook') or {}
    stamp = HEARTBEATS.get(str(gates.store.path), saved.get('at'))
    seen = max(0, int(time.time() - stamp)) if stamp else None
    missing = missing_host_contexts()
    compatibility = ([dict(id='hermes_version', label='Hermes version', ok=False, value=None,
                           hint='This Hermes version lacks expected session context names: ' + ', '.join(missing) +
                                '. Update to a compatible Hermes version; terminal or board selection may use fallback context.')] if missing else [])
    return [dict(id='families', label='Two families', ok=bool(b and v and not related(b, v)),
                 value=f'{b} builds, {v} verifies' if b and v else None,
                 hint='The main model and verifier must have known independent families.'),
            dict(id='hook', label='Gate hook', ok=seen is not None and seen < 24 * 60 * 60, seen_s=seen,
                 value=f'Seen {seen}s ago' if seen is not None else None,
                 hint='Run a tool in this profile to observe the real pre-tool hook.'),
            dict(id='lifetime', label='PASS lifetime', ok=True, value=str(cfg['max_age_s'] / 60).removesuffix('.0') + ' minutes',
                 hint='Effective PASS lifetime in this profile.')] + compatibility + [dict(id="setting_" + field, label=field, ok=False, value=None,
        hint="Invalid settings field: " + field + "; set it in the plugin settings form.") for field in cfg["settings_errors"]]


def selftest(gates):
    from .trial import Trial, KEY
    trial = Trial(gates, namespace='selftest')
    trial.initialize()
    cfg = gates.config()
    trial.gates.settings = lambda k, d: cfg.get(k, d)
    trial.gates.host_config = gates.host_config
    with trial.store.transaction() as db:
        trial.store.reset(db)
    armed = gates.armed()
    decision = trial.gates.close(KEY)
    result = dict(armed=armed, refused=bool(decision and decision.get('action') == 'block'),
                  message=(boards.clean(str((decision or {}).get('message', 'Close unexpectedly allowed.')).split(' Next:')[0]) if armed else
                           'This profile is not armed, so real closes are allowed. Choose an independent verifier and enable Shadow Army routing to finish setup.'),
                  checked_at=time.time())
    with gates.store.transaction() as db:
        gates.store.put(db, 'setup', 'selftest', result)
    return result
