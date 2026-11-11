"""Schema version 2 pane projection. No raw prompts or logs leave this module."""
import datetime as dt
import re
import time

from . import boards, setup as setup_service
from .policy import GateKey, family
from .trial import KEY, Trial

FINGERPRINT_CACHE = {}


def weakened_view(rows):
    return None if rows is None else [dict(file=boards.clean(r['file'], 500), line=r['line'],
        text=boards.clean(r['text'], 1000), since=r['since'], kind=r.get('kind', 'changed')) for r in rows[:5]]


def command_view(value, limit=500):
    value = re.sub(r'(?:/Users/[^/\s]+|/home/[^/\s]+|~)(?=[/\\])', '<home>', str(value or ''))
    return boards.clean(value, limit).replace('<home>', '~')


def check_view(value):
    if not value:
        return None
    return {'command': command_view(value.get('command')), 'exit_code': value.get('exit_code'), 'tests_ran': value.get('tests_ran'),
            'output_excerpt': boards.clean(value.get('output_excerpt'), 1200)}


def receipt_markdown(receipt):
    if not receipt:
        return None
    check, verifier = receipt.get('check') or {}, receipt.get('verifier') or {}
    lines = [('Simulated trial — ' if receipt.get('synthetic') else '') + 'Shadow Army: ' + receipt['verdict'],
             'Tree: ' + receipt['tree'][:12]]
    identity = [str(value) for value in (verifier.get('family'), verifier.get('response_model') or verifier.get('model')) if value is not None]
    if identity:
        lines.append('Verifier: ' + ' / '.join(identity))
    if check.get('command') is not None:
        lines.append('Check: ' + re.sub(r'(?:/Users/[^/\s]+|/home/[^/\s]+|~)(?=[/\\])', '<home>', str(check['command'])) + (' (exit ' + str(check['exit_code']) + ')' if check.get('exit_code') is not None else ''))
    if receipt.get('elapsed_s') is not None:
        lines.append('Review: ' + str(receipt['elapsed_s']) + 's')
    if receipt.get('tokens') is not None:
        lines.append('Tokens: ' + str(receipt['tokens']))
    from .assertions import summary
    description = summary(receipt)
    if description:
        lines.append(description)
        lines.extend(f"- {r['file']}:{r['line']}: {r['text']}" for r in receipt.get('weakened') or [])
    if receipt.get('accepted_removed'):
        lines.append('Reviewer accepted the removed assertions.')
    if receipt.get('tests_ran'):
        drop = receipt['tests_ran']
        lines.append(f"Fewer tests ran: {drop['before']} -> {drop['now']}")
    if receipt.get('accepted_skipped'):
        lines.append('Reviewer accepted the skipped tests.')
    if receipt.get('skipped'):
        lines.append('Skipped or disabled tests:')
    lines.extend(f"- {r['file']}:{r['line']}: {r['text']}" for r in receipt.get('skipped') or [])
    return boards.clean('\n'.join(lines), 4000).replace('<home>', '~')


def snapshot(real, demo=False):
    trial = Trial(real) if demo else None
    gates = trial.gates if trial else real
    with gates.store.read() as db:
        rev = gates.store.revision(db)
    if trial:
        with gates.store.read() as db:
            progress = gates.store.get(db, 'trial', 'step')
            started = progress is not None
            reset = not started and bool(gates.store.history(db, KEY))
        rows, errors = ([dict(id=KEY.card_id, board=KEY.board, title='Empty cart crashes instead of totaling 0',
                              assignee='Trial', status='done' if progress.get('closed') else 'in_progress', result='')] if started else []), []
    else:
        rows, errors = boards.cards()
        with gates.store.read() as db:
            workspaces = gates.store.rows(db, 'workspace')
        import hashlib
        import os
        rows.extend(dict(id=hashlib.sha256(os.fsencode(w['repo'])).hexdigest()[:12], board='workspace:',
                         title=w['title'], status='open', assignee='Workspace', result='') for w in workspaces if os.path.isdir(w['repo']))
    cfg = gates.config()
    armed = gates.armed()
    verifier = cfg['verifier']
    configured_check = gates.settings('check', None)
    setup = [{'id': 'repo', 'label': 'Code cards', 'ok': False,
              'hint': 'Give a code card a Git workspace, or set the optional repository in Settings, Plugins, Shadow Army.'},
             {'id': 'check', 'label': 'Check command', 'ok': True,
              'hint': ((command_view(cfg['check']) or 'Command evidence disabled by owner.') if configured_check is not None else
                       'Using the default: ' + command_view(cfg['check']))},
             {'id': 'verifier', 'label': 'Verifier model', 'ok': bool(isinstance(verifier, dict) and verifier.get('provider') and verifier.get('model')),
              'hint': (str(verifier['model']) + ' through ' + str(verifier['provider']) if isinstance(verifier, dict) and verifier.get('provider') and verifier.get('model')
                       else 'Choose a verifier provider and model from a different known family in Settings, Plugins, Shadow Army.')},
             {'id': 'backend', 'label': 'Routing', 'ok': gates.host_config().get('delegation', {}).get('backend') == 'shadow-army',
              'hint': ('Shadow Army routing is enabled.' if gates.host_config().get('delegation', {}).get('backend') == 'shadow-army'
                       else 'Set delegation.backend to shadow-army.')}]
    local = gates.local_terminal()
    setup.append(dict(id='terminal', label='Terminal', ok=local,
                      hint='The effective terminal is local.' if local else
                      'The effective terminal is not local; select a local terminal in this profile before verifying.'))
    values = dict(repo=cfg['repo'], check=cfg['check'], verifier=(verifier or {}).get('model'),
                  backend=gates.host_config().get('delegation', {}).get('backend', 'default'), terminal='local' if local else 'remote')
    for item in setup:
        raw = values.get(item['id'])
        item['value'] = (command_view(raw) if item['id'] == 'check' else boards.clean(raw)) if raw is not None else None
    try:
        setup.extend(setup_service.status(gates))
    except Exception:
        pass
    try:
        setup_actions = setup_service.actions(gates)
    except Exception:
        setup_actions = dict(can_arm=False, routes=[], can_set_routing=False, reason='Setup routes unavailable; use the plugin settings form.')
    trial_view = None
    if trial:
        step = progress['step'] if progress else 0
        tampered = bool(progress and progress.get('tampered'))
        messages = {
            1: 'The empty-cart test fails for real, exit 1. Next: the builder reports done.',
            2: 'The builder deleted the failing assertion, so the real check now passes. Next: the gate is asked to close the card.',
            3: 'The gate refused to close the card: nobody has reviewed this code. Next: a reviewer from the builder\'s own family is requested.',
            4: 'The same-family reviewer was refused by the real routing. Next: a different family reviews, with the removed line in its brief.',
            5: 'The tests pass. The simulated reviewer, shown the removed line, still fails it. Next: the builder restores the assertion and fixes the code.',
            6: 'The assertion is restored and the fixed code passes. Next: a fresh review.',
            7: 'A fresh simulated PASS for this exact code. Next: the card closes, or tamper with one line first.',
            8: 'The gate allowed the close and the card is closed. Next is off until you reset.',
        }
        if tampered:
            messages.update({
                7: 'One line changed after the PASS, so the gate is STALE. Next: the gate is asked to close the changed code.',
                8: 'The gate refused to close the changed code. Next: a fresh simulated review and a real check.',
                9: 'The real check passes and a fresh simulated review records PASS. Next: the card closes.',
                10: 'The gate allowed the close and the card is closed. Next is off until you reset.',
            })
        message = messages[step] if started else 'The trial has not started; Next is disabled until Start seeds the bug.'
        if reset:
            message = 'The trial was reset; Next is disabled until Start seeds the bug.'
        closed = bool(progress and progress.get('closed'))
        trial_view = dict(real=['gate decisions', 'routing refusal', 'test runs', 'fingerprint', 'removed-assertion check', 'verdict acceptance'],
                          simulated=['model identities and what the two models say'], step=step, total=10 if tampered else 8, finished=closed,
                          can_step=bool(started and not closed),
                          can_tamper=bool(started and step == 7 and not tampered and not closed), message=message)
    cards = []
    repositories = {cfg['repo']} if cfg['repo'] else set()
    for repo, cached in list(FINGERPRINT_CACHE.items()):
        if time.monotonic() - cached[0] >= 2:
            FINGERPRINT_CACHE.pop(repo, None)
    for raw in rows:
        key = GateKey(raw['board'], raw['id'])
        status = gates.inspect(key, fingerprint_cache=FINGERPRINT_CACHE if not demo else None)
        if status.get('repo'):
            repositories.add(status['repo'])
        with gates.store.read() as db:
            receipts = gates.receipts(db, key)
            history = gates.store.history(db, key)
            if trial:
                shadows = gates.store.rows(db, 'shadow')
            else:
                shadows = []
                for b in gates.store.rows(db, 'binding'):
                    if (b['board'], b['card_id']) != key.values():
                        continue
                    observations = gates.observations(db, b.get('child_session') or '')
                    model = observations[-1] if observations else b.get('route', {})
                    receipt = next((r for r in reversed(receipts) if r['verifier']['session_id'] == b.get('child_session')), None)
                    shadows.append(dict(id=b.get('child_session') or b['nonce'][:12], duty=b['duty'], family=model.get('response_family') or model.get('family') or family(model.get('provider'), model.get('model'), cfg['family_overrides']),
                        model=model.get('response_model') or model.get('model'), state='refused' if b.get('refused') else ('finished' if b.get('finished_at') else ('running' if b.get('used_at') else 'pending')),
                        verdict=receipt['verdict'] if receipt else None,
                        finding=receipt['findings'][0]['text'] if receipt and receipt['findings'] else b.get('finding'),
                        elapsed_s=int((b.get('finished_at') or time.time()) - b['created_at'])))
        for shadow in shadows:
            for field in ('id', 'model', 'family', 'finding'):
                if shadow.get(field) is not None:
                    shadow[field] = boards.clean(shadow[field])
        latest = receipts[-1] if receipts else None
        builder = next((s for s in reversed(shadows) if s['duty'] in ('build', 'repair')), None) or (latest.get('builder') if latest else None)
        verifier = latest['verifier'] if latest else None
        decision = str(raw.get('result') or '')
        needs_decision = decision.startswith('NEEDS_DECISION')
        public_history = []
        for event in history:
            if event.get('kind') not in {
                'arose', 'finished', 'verdict', 'refused_same_family', 'refused_unbound',
                'close_permitted', 'close_refused', 'closed', 'edited',
            }:
                continue
            item = {k: v for k, v in event.items() if k in {
                'at', 'kind', 'duty', 'family', 'model', 'verdict', 'finding', 'tree', 'check',
                'builder_family', 'findings', 'check_passed_but_failed', 'accepted_skipped',
                'accepted_removed', 'elapsed_s', 'tokens', 'simulated', 'receipt_id',
                'weakened', 'weakened_count', 'changed_count', 'skipped', 'skipped_count',
                'tests_ran', 'gate_at_close', 'gate', 'command', 'output_excerpt', 'error',
            }}
            if item.get('kind') == 'verdict' and item.get('receipt_id'):
                with gates.store.read() as db:
                    receipt = gates.store.get(db, 'receipt', item['receipt_id'])
                if receipt:
                    item['tokens'] = receipt.get('tokens')
            item['tree'] = (item.get('tree') or '')[:12] or None
            item['check'] = check_view(item.get('check'))
            if 'skipped' in item:
                item['skipped'] = weakened_view(item['skipped'])
            if 'weakened' in item:
                item['weakened'] = weakened_view(item['weakened'])
            if item.get('findings'):
                item['findings'] = [dict(file=boards.clean(f['file']), line=f['line'], text=boards.clean(f['text'])) for f in item['findings']]
            for field in ('finding', 'model', 'family', 'builder_family', 'command', 'output_excerpt', 'error'):
                if item.get(field) is not None:
                    value = item[field]
                    if field == 'command':
                        item[field] = command_view(value, 240)
                        continue
                    if field == 'error':
                        value = re.sub(r'(?:/Users/[^/\s]+|/home/[^/\s]+|~)(?=[/\\])', '<home>', str(value))
                    item[field] = boards.clean(value, 1200 if field == 'output_excerpt' else 240)
            public_history.append(item)
        hint = {
            'UNGATED': 'Nothing to do. The gate leaves this card alone.',
            'UNARMED': 'Finish setup to arm the gate.',
            'UNKNOWN': 'Restore the card worktree or commit or ignore the files named in the reason, then retry.',
            'UNVERIFIED': 'Waiting for a review by a different model family.',
            'BLOCKED': 'The builder must fix the finding, then ask for a new review.',
            'STALE': 'Ask for a new independent review of the current code and check.',
            'PASS': 'Nothing to do. The card can close.',
        }[status['state']]
        if raw.get('status') in ('done', 'archived'):
            hint = 'Nothing to do. The card is closed.'
        elif status['state'] == 'UNGATED':
            hint = 'Waiting on your decision.' if raw.get('status') == 'blocked' or needs_decision else 'Nothing to do. The gate leaves this card alone.'
        reason = ('One line changed after the PASS. The old verdict no longer counts.'
                  if trial and tampered and status['state'] == 'STALE' else status['reason'])
        cards.append(dict(receipt_markdown=receipt_markdown(latest) if latest and status['state'] in ('PASS', 'BLOCKED') and status['tree'] == latest['tree'] else None, kind='trial' if trial else ('workspace' if key.board == 'workspace:' else 'card'), id=boards.clean(key.card_id, 128), board=boards.clean('Workspace' if key.board == 'workspace:' else key.board, 128), title=boards.clean(raw.get('title')),
            hint=boards.clean(hint, 500), owner=boards.clean(raw.get('assignee') or 'unassigned', 80), state=boards.clean(raw.get('status'), 40),
            needs_you=raw.get('status') == 'blocked' or needs_decision,
            decision=boards.clean(decision[len('NEEDS_DECISION'):].lstrip(': \n')) if needs_decision else '',
            gate=dict(state=status['state'], reason=boards.clean(reason) or 'The code identity could not be read; restore repository access and retry.', tree=(status['tree'] or '')[:12] or None,
                tests_ran=status.get('tests_ran'), skipped=weakened_view(status.get('skipped')), skipped_count=status.get('skipped_count'),
                weakened=weakened_view(status.get('weakened')), weakened_count=status.get('weakened_count'), changed_count=status.get('changed_count'),
                age_s=max(0, int(time.time() - latest['created_at'])) if latest else None,
                builder={k: boards.clean(builder.get(k)) for k in ('family', 'model')} if builder else None,
                verifier=dict(family=boards.clean(verifier.get('family')), model=boards.clean(verifier.get('response_model') or verifier.get('model'))) if verifier else None),
            shadows=shadows, history=public_history))
    from .policy import git
    if trial:
        repositories.add(str(trial.repo))
    for repo in repositories - {None, ''}:
        try:
            if not boards.in_work_tree(repo):
                continue
            git(repo, 'rev-parse', 'HEAD^{tree}')
            setup[0].update(ok=True, hint='A code repository is available.')
            break
        except (ValueError, OSError):
            pass
    with gates.store.read() as db:
        selftest = gates.store.get(db, 'setup', 'selftest')
    return dict(setup_actions=setup_actions, selftest=selftest, schema_version=2, rev=rev, observed_at=dt.datetime.now(dt.timezone.utc).isoformat(), demo=demo,
                armed=armed, setup=setup, setup_url='/settings?tab=plugins&agent=shadow-army', trial=trial_view, cards=cards, errors=errors)
