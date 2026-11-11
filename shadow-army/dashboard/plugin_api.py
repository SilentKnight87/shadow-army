"""Authenticated by the Hermes plugin mount; mutations stay in plugin-owned state."""
import copy
import importlib.util
from pathlib import Path
import sys

from fastapi import APIRouter, HTTPException

# Dashboard API files are loaded as standalone modules by Hermes.
PACKAGE = '_shadow_army_snapshot_backend'
if PACKAGE not in sys.modules:
    spec = importlib.util.spec_from_file_location(PACKAGE, Path(__file__).resolve().parents[1] / '__init__.py',
                                                 submodule_search_locations=[str(Path(__file__).resolve().parents[1])])
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = package
    spec.loader.exec_module(package)
backend = importlib.import_module(PACKAGE + '.bridge')
projection = importlib.import_module(PACKAGE + '.snapshot')
trial_module = importlib.import_module(PACKAGE + '.trial')
boards = importlib.import_module(PACKAGE + '.boards')
clean, connect, databases = boards.clean, boards.connect, boards.databases
router = APIRouter()
LAST_GOOD = {}


def snapshot(demo=False):
    return projection.snapshot(backend.current_gates(), demo)


@router.get('/snapshot')
def get_snapshot(demo: bool = False):
    from hermes_constants import get_hermes_home
    key = (str(get_hermes_home()), demo)
    try:
        data = snapshot(demo)
        if data['errors'] and key in LAST_GOOD:
            failed = {error[6:-13] for error in data['errors'] if error.startswith('Board ') and error.endswith(': read failed')}
            data['cards'].extend(copy.deepcopy(card) for card in LAST_GOOD[key]['cards'] if card['board'] in failed)
        LAST_GOOD[key] = copy.deepcopy(data)
        return data
    except Exception:
        data = copy.deepcopy(LAST_GOOD.get(key))
        if data is None:
            data = dict(schema_version=2, rev=0, demo=demo, observed_at=None, armed=False, setup=[], trial=None, cards=[], errors=[])
        data['errors'] = ['Gate storage unavailable; showing the last good cards if available. Check profile plugin data permissions.']
        return data


def trial_action(action):
    try:
        trial = trial_module.Trial(backend.current_gates())
        getattr(trial, action)()
        return get_snapshot(True)
    except Exception as exc:
        raise HTTPException(400, clean(str(exc))) from exc


@router.post('/demo/start')
def start_demo():
    return trial_action('start')


@router.post('/demo/step')
def step_demo():
    return trial_action('step')


@router.post('/demo/tamper')
def tamper_demo():
    return trial_action('tamper')


@router.post('/demo/reset')
def reset_demo():
    return trial_action('reset')


@router.post('/setup/arm')
def arm_setup(body: dict):
    try:
        setup = importlib.import_module(PACKAGE + '.setup')
        setup.arm(backend.current_gates(), body.get('provider'), body.get('model'))
        return get_snapshot()
    except Exception as exc:
        raise HTTPException(400, clean(str(exc))) from exc


@router.post('/setup/selftest')
def selftest_setup():
    try:
        setup = importlib.import_module(PACKAGE + '.setup')
        return setup.selftest(backend.current_gates())
    except Exception as exc:
        raise HTTPException(400, clean(str(exc))) from exc
