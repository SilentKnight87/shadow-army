import { host, PALETTE_AREA, ROUTES_AREA, SIDEBAR_NAV_AREA, useValue } from '@hermes/plugin-sdk'
// Optional surfaces are read off the namespace so an older Desktop that lacks one still loads the rest.
import * as sdk from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'shadow-army'
const POLL_MS = 5000
// With no pane or page open, the pushed gate.changed event carries the news; the poll only reconciles once a minute.
const IDLE_POLL_MS = 60000
const STEP_MS = 4000
const GATES = ['UNVERIFIED', 'BLOCKED', 'STALE', 'PASS', 'UNKNOWN', 'UNGATED', 'UNARMED']
const QUIET = ['UNGATED', 'UNARMED']
const GATE_WORD = { UNGATED: 'NOT GATED', UNARMED: 'NOT ARMED' }
const FAMILY = {
  anthropic: 'Anthropic', openai: 'OpenAI', google: 'Google', xai: 'xAI', meta: 'Meta', mistral: 'Mistral',
  deepseek: 'DeepSeek', qwen: 'Qwen', moonshot: 'Moonshot', zhipu: 'Zhipu', nous: 'Nous', cohere: 'Cohere'
}
const DUTY = { build: 'Builder', verify: 'Verifier', repair: 'Repair' }
const FAILED_ACTION = {
  start: 'The trial could not start on this backend. Enable the shadow-army plugin for this profile and restart the backend.',
  step: 'The next trial step did not run. Auto-play is paused.',
  tamper: 'The tamper edit did not run. Try again.',
  reset: 'The trial reset did not finish on the backend. Showing this connection again.'
}

const list = value => (Array.isArray(value) ? value : [])
const str = value => (value == null ? '' : String(value))
const cardKey = card => `${str(card.kind)}:${str(card.board)}/${str(card.id)}`
// A card on a Kanban board (kind "card", or no kind from an older backend), as opposed to a workspace or the trial.
const kanbanCard = card => !['workspace', 'trial'].includes(card?.kind)
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`
const family = id => FAMILY[str(id).toLowerCase()] || str(id) || 'unknown family'
const gateOf = card => (GATES.includes(card.gate?.state) ? card.gate.state : 'UNKNOWN')
const gateWord = gate => GATE_WORD[gate] || gate
const human = value => str(value).replace(/[_-]+/g, ' ').trim()
const isPass = row => row.kind === 'verdict' && str(row.verdict).toUpperCase() === 'PASS'
// v2.3: a review that recorded FAIL although the real check exited 0.
const greenFail = row => row?.kind === 'verdict' && row.check_passed_but_failed === true
const pinsOf = row => list(row?.findings).filter(f => f && typeof f === 'object' && str(f.file).trim())
const pinText = f => (f.line != null && str(f.line) !== '' ? `${str(f.file)}:${str(f.line)}` : str(f.file))

// The bridge flattens a refusal to "Error invoking remote method 'hermes:api': Error: 400: {"detail": …}".
function refusalOf(error) {
  const raw = str(error?.message ?? error)
  const text = (raw.match(/Error invoking remote method '[^']+': Error: (.+)$/s)?.[1] ?? raw).replace(/^Error:\s*/, '')
  const status = text.match(/^(4\d\d):\s*(.*)$/s)
  if (!status) return ''
  try {
    const detail = JSON.parse(status[2]).detail
    return typeof detail === 'string' ? detail : str(detail?.message)
  } catch (_) {
    return status[2].trim()
  }
}

function seconds(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value > 1e12 ? value / 1000 : value
  const parsed = Date.parse(str(value))
  return Number.isNaN(parsed) ? null : parsed / 1000
}

function ago(delta) {
  if (delta == null || !Number.isFinite(delta)) return ''
  if (delta < 45) return 'just now'
  if (delta < 3600) return `${Math.max(1, Math.round(delta / 60))}m ago`
  if (delta < 86400) return `${Math.round(delta / 3600)}h ago`
  return `${Math.round(delta / 86400)}d ago`
}

// A measure is shown only when it measured something: never "review 0s", "0 tokens" or a missing value.
const measured = value => typeof value === 'number' && Number.isFinite(value) && Math.round(value) >= 1

// A review's own duration: seconds stay exact under a minute.
function took(s) {
  const n = Math.max(0, Math.round(s))
  if (n < 60) return `${n}s`
  return n < 3600 ? `${Math.floor(n / 60)}m ${n % 60}s` : `${Math.floor(n / 3600)}h ${Math.round((n % 3600) / 60)}m`
}

function duration(s) {
  if (s < 60) return '<1m'
  return s < 3600 ? `${Math.round(s / 60)}m` : `${Math.round(s / 3600)}h`
}

function clock(at) {
  const t = seconds(at)
  return t == null ? '' : new Date(t * 1000).toLocaleString()
}

function historyOf(card) {
  return list(card.history).slice().sort((a, b) => (seconds(a.at) ?? 0) - (seconds(b.at) ?? 0))
}

const lastActivity = card => Math.max(0, ...list(card.history).map(row => seconds(row.at) ?? 0))
const hasActivity = card => list(card.history).length > 0 || ['BLOCKED', 'STALE', 'PASS'].includes(gateOf(card))
const verdictRows = card => historyOf(card).filter(row => row.kind === 'verdict')
// Permission to close is not a close: only the Kanban state or a "closed" event proves it.
const isDone = card => str(card.state).toLowerCase() === 'done'
const closed = card => isDone(card) || list(card.history).some(row => row.kind === 'closed')

// The edit that voided the latest PASS, if one came after it and the gate no longer passes.
function editAfterPass(card) {
  if (gateOf(card) === 'PASS') return null
  const rows = historyOf(card)
  const pass = rows.findLastIndex(isPass)
  const edit = rows.findLastIndex(row => row.kind === 'edited')
  return pass >= 0 && edit > pass ? rows[edit] : null
}

// Why and Next are the backend's own sentences, shown as sent. Next is dropped when it only repeats Why.
const TAMPERED = 'One line changed after the PASS. The old verdict no longer counts.'
const whyOf = card => str(card.gate?.reason).trim() || (editAfterPass(card) ? TAMPERED : 'No gate reason reported.')
const hintOf = card => {
  const hint = str(card.hint).trim()
  return hint === whyOf(card) ? '' : hint
}
const ownerOf = card => {
  const owner = str(card.owner).trim()
  return owner.toLowerCase() === 'unassigned' ? '' : owner
}
const sentence = value => {
  const text = str(value).trim()
  return text.charAt(0).toUpperCase() + text.slice(1)
}

// Done cards sort after every open card and lead only when no open card is left.
function orderCards(cards) {
  const sorted = cards.slice().sort((a, b) =>
    Number(isDone(a)) - Number(isDone(b)) || Number(QUIET.includes(gateOf(a))) - Number(QUIET.includes(gateOf(b))) ||
    Number(hasActivity(b)) - Number(hasActivity(a)) || lastActivity(b) - lastActivity(a) || str(a.title).localeCompare(str(b.title)))
  const open = sorted.filter(card => !isDone(card))
  const pool = open.length ? open : sorted
  const lead = pool[0] && !QUIET.includes(gateOf(pool[0])) && hasActivity(pool[0]) ? pool[0] : null
  const hero = pool.find(card => gateOf(card) === 'BLOCKED') || lead
  return { hero, rest: sorted.filter(card => card !== hero) }
}

function problemWith(snap) {
  if (!snap || typeof snap !== 'object') return 'The backend answered without a snapshot.'
  if (snap.schema_version !== 2) {
    return `This backend speaks Shadow Army API version ${str(snap.schema_version) || 'unknown'}; this pane needs version 2. Update the shadow-army plugin on the backend, then restart it.`
  }
  if (!Array.isArray(snap.cards)) return 'The snapshot has no card list.'
  return ''
}

function sealLines(card) {
  const gate = card.gate || {}
  const last = verdictRows(card).at(-1)
  switch (gateOf(card)) {
    case 'UNGATED': return ['gate does', 'not apply']
    case 'UNARMED': return ['completion', 'untouched']
    case 'UNVERIFIED': return ['no verdict', 'for this tree']
    case 'BLOCKED': return [greenFail(last) ? 'FAIL on exit 0' : 'FAIL', str(gate.tree)]
    case 'STALE': return editAfterPass(card) ? ['edited', 'after PASS'] : ['tree changed', `since ${last?.verdict || 'verdict'}`]
    case 'PASS': return [str(gate.tree), ago(gate.age_s)]
    default: return isDone(card) ? ['card closed'] : ['cannot read', 'the tree']
  }
}

const still = () => {
  try { return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches } catch (_) { return false }
}

// A snapshot carries "rev", the newest gate event id. Never show an older one for the same mode.
const older = (snap, shown) =>
  Boolean(shown) && shown.demo === snap.demo && Number.isFinite(snap.rev) && Number.isFinite(shown.rev) && snap.rev < shown.rev

function createFeed(ctx) {
  let state = { scope: null, snap: null, status: 'connecting', problem: '', demo: false, auto: true, pending: '', notice: '' }
  // users keep the poll alive; viewers are the pane and page, the only ones that may drive auto-play.
  let users = 0, viewers = 0, busy = false, again = false, generation = 0, actions = 0, stopPoll = null, stopStep = null
  // The newest live (non-trial) snapshot, kept through a trial, for the toast and badge registrations.
  let real = null
  const listeners = new Set()
  const realListeners = new Set()
  // Per card, the gate every other surface keeps showing until the seal's verdict lands (see holdsOf).
  let holds = new Map()
  const set = patch => {
    state = { ...state, ...patch }
    for (const listener of listeners) listener(state)
  }
  const scopeNow = () => `${host.activeConnectionId() || ''}|${host.state.profile.get() || ''}`

  function apply(snap) {
    const problem = problemWith(snap)
    if (problem) { set({ status: 'incompatible', problem }); return }
    if (older(snap, state.snap)) return
    // Only an open pane or page plays the seal, and reduced motion shows its verdict at once: only then do the other
    // surfaces wait for it.
    holds = viewers && !still() ? holdsOf(state.snap, snap, holds) : new Map()
    set({ status: 'live', problem: '', snap })
    if (snap.demo !== true) {
      const prev = real
      real = snap
      for (const listener of realListeners) {
        try { listener(snap, prev) } catch (_) { /* a broken listener never stops the feed */ }
      }
    }
  }

  // Reads never overlap each other or a trial action. A read that an action overtook is dropped:
  // the action's own snapshot is newer. A read asked for during an action runs after it.
  async function refresh() {
    if (!users) return
    if (busy || state.pending) { again = true; return }
    busy = true
    try {
      do {
        again = false
        const gen = generation, scope = scopeNow(), demo = state.demo, sent = actions
        try {
          const snap = await ctx.rest(demo ? '/snapshot?demo=1' : '/snapshot', { timeoutMs: 15000 })
          if (gen !== generation || scope !== scopeNow() || sent !== actions || state.pending) continue
          apply(snap)
        } catch (_) {
          if (gen === generation && scope === scopeNow() && sent === actions && !state.pending) set({ status: 'offline' })
        }
      } while (again && users && !state.pending)
    } finally {
      busy = false
    }
    scheduleAuto()
  }

  // Auto-play runs only while the backend says a step can run.
  function canAutoStep() {
    const trial = state.snap?.demo ? state.snap.trial : null
    return Boolean(trial && trial.can_step === true && !trial.finished)
  }
  function stopAuto() { stopStep?.(); stopStep = null }
  function scheduleAuto() {
    if (stopStep) return
    if (viewers && state.demo && state.auto && !state.pending && canAutoStep()) {
      stopStep = ctx.setTimeout(() => { stopStep = null; void step() }, STEP_MS)
    }
  }

  function discard(patch) {
    generation++
    stopAuto()
    holds = new Map()
    set({ snap: null, status: 'connecting', problem: '', pending: '', ...patch })
  }

  // Returns true when the backend ran the action. The returned snapshot is shown as is.
  async function post(action) {
    const gen = generation
    actions++
    stopAuto()
    set({ pending: action, notice: '' })
    try {
      const snap = await ctx.rest(`/demo/${action}`, { method: 'POST', body: {}, timeoutMs: 20000 })
      if (gen === generation && snap && typeof snap === 'object' && Array.isArray(snap.cards)) apply(snap)
      return true
    } catch (error) {
      if (gen === generation) set({ notice: refusalOf(error) || FAILED_ACTION[action] })
      return false
    } finally {
      if (gen === generation) set({ pending: '' })
      if (again && !busy) void refresh()
    }
  }

  // The shown snapshot stays up until the first trial snapshot replaces it: no "Connecting" in between.
  async function startTrial() {
    generation++
    stopAuto()
    set({ demo: true, auto: true, notice: '' })
    if (!await post('start')) {
      generation++
      set({ demo: false, notice: state.notice || FAILED_ACTION.start })
      void refresh()
      return
    }
    if (!state.snap?.demo) await refresh()
    scheduleAuto()
  }

  async function step() {
    if (!await post('step')) set({ auto: false })
    scheduleAuto()
  }

  async function tamper() {
    set({ auto: false })
    await post('tamper')
  }

  async function reset() {
    const ok = await post('reset')
    discard({ demo: false, notice: ok ? '' : state.notice || FAILED_ACTION.reset })
    void refresh()
  }

  // Setup actions (v2.4). Each returns '' or the backend's refusal sentence; the arm reply is a snapshot.
  async function setupPost(path, body, pending) {
    const gen = generation
    actions++
    set({ pending, notice: '' })
    try {
      return { result: await ctx.rest(path, { method: 'POST', body, timeoutMs: 20000 }), error: '', gen }
    } catch (error) {
      return { result: null, error: refusalOf(error) || '', gen }
    } finally {
      if (gen === generation) set({ pending: '' })
      if (again && !busy) void refresh()
    }
  }
  async function arm(route) {
    const { result, error, gen } = await setupPost('/setup/arm', { provider: str(route?.provider), model: str(route?.model) }, 'arm')
    if (result && gen === generation && typeof result === 'object' && Array.isArray(result.cards)) apply(result)
    return result ? '' : error || 'The verifier was not saved. Try again.'
  }
  async function selftest() {
    const { result, error } = await setupPost('/setup/selftest', {}, 'selftest')
    void refresh()
    return result && typeof result === 'object' ? { result, error: '' } : { result: null, error: error || 'The self-test did not run. Try again.' }
  }

  function toggleAuto() {
    set({ auto: !state.auto })
    if (state.auto) scheduleAuto()
    else stopAuto()
  }

  function rescope() {
    const scope = scopeNow()
    if (scope === state.scope) return false
    real = null
    discard({ scope, demo: false, notice: '' })
    void refresh()
    return true
  }

  // A pushed event refreshes only the mode it is about.
  function onEvent(payload) {
    if (typeof payload?.demo === 'boolean' && payload.demo !== state.demo) return
    void refresh()
  }

  // The poll runs every 5 seconds while a pane or page is open, and once a minute for passive users alone.
  function repoll() {
    stopPoll?.()
    stopPoll = users ? ctx.setInterval(() => void refresh(), viewers ? POLL_MS : IDLE_POLL_MS) : null
  }

  // A passive user (the status bar, a transcript card) keeps a slow poll alive but never drives the trial.
  function retain(passive = false) {
    users++
    if (!passive) viewers++
    if (users === 1) {
      repoll()
      if (!rescope()) void refresh()
    } else if (!passive && viewers === 1) {
      repoll()
      void refresh()
      scheduleAuto()
    }
    return () => {
      users--
      if (!passive) viewers--
      if (!viewers) stopAuto()
      if (!users || (!passive && !viewers)) repoll()
    }
  }

  return {
    get: () => state,
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    onReal(listener) { realListeners.add(listener); return () => realListeners.delete(listener) },
    real: () => real,
    // The gate a surface may show for a card now, and how long (ms) until the last held verdict lands.
    shown: card => {
      const hold = holds.get(cardKey(card))
      return hold && hold.until > Date.now() ? hold.was : gateOf(card)
    },
    holdLeft: card => {
      const now = Date.now()
      const all = card ? [holds.get(cardKey(card))].filter(Boolean) : [...holds.values()]
      return Math.max(0, ...all.map(hold => hold.until - now))
    },
    holding: () => [...holds.values()].find(hold => hold.until > Date.now()) || null,
    retain, rescope, refresh, onEvent, startTrial, step, tamper, reset, toggleAuto, arm, selftest,
    // The SDK's clipboard door (ctx.os.writeClipboard) resolves false where it is unavailable.
    copy: text => {
      try { return Promise.resolve(ctx.os?.writeClipboard?.(str(text)) ?? false).then(Boolean, () => false) } catch (_) { return Promise.resolve(false) }
    }
  }
}

function useFeed(feed, passive = false) {
  const [state, setState] = useState(feed.get)
  const connection = useValue(host.state.connectionId)
  const profile = useValue(host.state.profile)
  useEffect(() => {
    const unsubscribe = feed.subscribe(setState)
    setState(feed.get())
    const release = feed.retain(passive)
    return () => { unsubscribe(); release() }
  }, [feed, passive])
  useEffect(() => { feed.rescope() }, [feed, connection, profile])
  return state
}

const css = `
/* The pane carries its own night skin in any host theme: a gate field in electric blue and violet. */
.sa-root {
  --sa-bg: #070a17; --sa-panel: #0b1124; --sa-raised: #111a36; --sa-inset: #04060f;
  --sa-line: rgb(122 162 255 / .16); --sa-line-2: rgb(122 162 255 / .32);
  --sa-text: #eaf0ff; --sa-text-2: #b3c0e2; --sa-muted: #8e9bc2;
  --sa-sys: #66cfff; --sa-sys-2: #3f7fff; --sa-violet: #b08cff;
  --sa-fail: #ff6283; --sa-pass: #45e8b2; --sa-warn: #ffbb52; --sa-quiet: #8e9bc2;
  --sa-sans: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  --sa-mono: ui-monospace, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  --tone: var(--sa-sys);
  container: sa / inline-size;
  color: var(--sa-text); height: 100%; overflow: auto; color-scheme: dark;
  font: 13px/1.5 var(--sa-sans); overflow-wrap: anywhere; box-sizing: border-box;
  background-color: var(--sa-bg);
  background-image:
    radial-gradient(110% 55% at 100% 0%, rgb(124 82 255 / .17), transparent 62%),
    radial-gradient(80% 45% at 0% 100%, rgb(40 120 255 / .13), transparent 60%),
    linear-gradient(rgb(122 162 255 / .035) 1px, transparent 1px),
    linear-gradient(90deg, rgb(122 162 255 / .035) 1px, transparent 1px);
  background-size: auto, auto, 28px 28px, 28px 28px;
}
.sa-root.sa-inline { height: auto; overflow: visible; padding: 12px; border-radius: 4px; max-width: 680px; margin: 4px 0 }
.sa-root *, .sa-root *::before, .sa-root *::after { box-sizing: border-box }
.sa-root h1, .sa-root h2, .sa-root h3, .sa-root p, .sa-root ol, .sa-root ul, .sa-root pre, .sa-root dl { margin: 0 }
.sa-root ol, .sa-root ul { list-style: none; padding: 0 }
.sa-root button { font: inherit; color: inherit }
.sa-root ::selection { background: rgb(102 207 255 / .35) }
.sa-root :focus-visible { outline: 2px solid var(--sa-sys); outline-offset: 2px; border-radius: 3px; box-shadow: 0 0 0 4px rgb(102 207 255 / .18) }
/* overflow-x: clip keeps a mid-animation shake or stamp from ever opening a horizontal scrollbar; sticky still works. */
.sa-wrap { padding: 16px 14px 28px; display: flex; flex-direction: column; gap: 14px; min-width: 0; overflow-x: clip }
.sa-sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap }
.sa-mono, .sa-hash, .sa-model { font-family: var(--sa-mono) }

/* Header: the emblem is a gate ring with a shadow rising out of it. */
.sa-head { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; animation: sa-window .6s ease-out both }
.sa-emblem { width: 30px; height: 30px; flex: none; color: var(--sa-sys); filter: drop-shadow(0 0 6px rgb(102 207 255 / .55)) }
.sa-emblem .fig { fill: var(--sa-violet) }
.sa-head h1 { font: 800 16px/1.2 var(--sa-sans); letter-spacing: .2em; text-transform: uppercase; color: var(--sa-text); text-shadow: 0 0 14px rgb(102 207 255 / .55) }
.sa-tag { font: 12px/1.4 var(--sa-mono); color: var(--sa-text-2); letter-spacing: .02em }
.sa-meta { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: flex-start; color: var(--sa-muted); font-size: 12px; margin-top: -6px }
.sa-live { display: inline-flex; align-items: center; gap: 6px; color: var(--sa-text-2) }
.sa-meta > span:not(.sa-live) { flex: 1 1 0; min-width: 9em }
.sa-live i { width: 7px; height: 7px; border-radius: 50%; border: 1.5px solid var(--sa-sys); flex: none }
.sa-live[data-status="live"] i { background: var(--sa-sys); box-shadow: 0 0 8px var(--sa-sys); animation: sa-pulse 2.4s ease-in-out infinite }
.sa-live[data-status="offline"] i, .sa-live[data-status="incompatible"] i { border-color: var(--sa-muted) }
@keyframes sa-pulse { 50% { opacity: .35 } }
.sa-synth { border: 1px dashed var(--sa-line-2); border-radius: 3px; padding: 0 6px; color: var(--sa-text-2); font: 11px/1.6 var(--sa-mono); white-space: nowrap }
.sa-link { background: none; border: 0; padding: 0; color: var(--sa-sys); cursor: pointer; text-decoration: underline; text-decoration-color: rgb(102 207 255 / .45); text-underline-offset: 3px }
.sa-link:hover:not(:disabled) { text-decoration-color: currentColor; text-shadow: 0 0 10px rgb(102 207 255 / .6) }
.sa-link.sa-unarmed { color: var(--sa-warn); font-weight: 600; text-decoration-color: rgb(255 187 82 / .5) }

/* Buttons: dark glass; the primary one is lit from inside. */
.sa-btn { position: relative; cursor: pointer; border: 1px solid var(--sa-line-2); background-color: var(--sa-raised); color: var(--sa-text); border-radius: 4px; padding: 5px 12px; font-size: 12.5px; line-height: 1.4; white-space: nowrap; transition: border-color .2s, box-shadow .2s, background-color .2s }
.sa-btn:hover:not(:disabled) { border-color: var(--sa-sys); box-shadow: 0 0 14px -4px var(--sa-sys) }
.sa-btn:disabled { cursor: default; opacity: .42 }
.sa-btn.primary { background-color: #6fd3ff; background-image: linear-gradient(180deg, #8adcff, #52b8ff); color: #03101f; border-color: #a6e6ff; font-weight: 700; box-shadow: 0 0 18px -4px rgb(102 207 255 / .8), inset 0 1px 0 rgb(255 255 255 / .5) }
.sa-btn.primary:hover:not(:disabled) { box-shadow: 0 0 26px -2px rgb(102 207 255 / .95), inset 0 1px 0 rgb(255 255 255 / .6) }
.sa-btn.quiet { background-color: transparent }
.sa-btn.tamper { border-color: rgb(255 187 82 / .7); color: #ffd38f; box-shadow: 0 0 14px -6px var(--sa-warn) }
.sa-btn.tamper:hover:not(:disabled) { border-color: var(--sa-warn); box-shadow: 0 0 18px -4px var(--sa-warn) }
.sa-btn[aria-pressed="true"] { border-color: var(--sa-sys); color: var(--sa-sys) }
.sa-btn.big { padding: 10px 22px; font-size: 14px; letter-spacing: .04em }
.sa-btn.big::after { content: ""; position: absolute; inset: -6px; border-radius: 8px; border: 1px solid rgb(102 207 255 / .6); opacity: 0; animation: sa-beckon 2.6s ease-out infinite; pointer-events: none }
@keyframes sa-beckon { 0% { opacity: .9; transform: scale(.94) } 70%, 100% { opacity: 0; transform: scale(1.12) } }
.sa-label { font: 700 10.5px/1.3 var(--sa-mono); letter-spacing: .14em; text-transform: uppercase; color: var(--sa-muted) }
.sa-empty { color: var(--sa-muted); font-size: 12px }
.sa-note { color: var(--sa-text-2); font-size: 12px; border-left: 2px solid var(--sa-warn); background-color: var(--sa-panel); padding: 6px 10px; border-radius: 0 4px 4px 0 }

/* The System window: corner brackets on a dark glass frame. */
.sa-panel, .sa-tile, .sa-needs, .sa-setup-line { position: relative; background-color: var(--sa-panel); background-image: linear-gradient(180deg, rgb(70 110 255 / .07), transparent 60%); border: 1px solid var(--sa-line-2); border-radius: 3px; box-shadow: inset 0 1px 0 rgb(150 190 255 / .07), 0 14px 32px -22px #000 }
.sa-panel::before, .sa-tile::before, .sa-panel::after, .sa-tile::after { content: ""; position: absolute; width: 12px; height: 12px; pointer-events: none; border: 0 solid var(--tone); filter: drop-shadow(0 0 4px var(--tone)) }
.sa-panel::before, .sa-tile::before { top: -1px; left: -1px; border-top-width: 2px; border-left-width: 2px }
.sa-panel::after, .sa-tile::after { right: -1px; bottom: -1px; border-right-width: 2px; border-bottom-width: 2px }

/* The trial bar is a System window that stays on screen while the story plays. */
.sa-trial { position: sticky; top: 0; z-index: 4; margin: 0 -14px; padding: 12px 14px 10px; background-color: rgb(7 10 23 / .9); backdrop-filter: blur(10px); border-bottom: 1px solid var(--sa-line-2); display: flex; flex-direction: column; gap: 8px; box-shadow: 0 1px 0 rgb(102 207 255 / .25), 0 10px 24px -16px #000 }
.sa-trial-top { display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: center }
.sa-trial-top b { font: 800 11px/1.4 var(--sa-mono); letter-spacing: .22em; color: var(--sa-sys); text-shadow: 0 0 10px rgb(102 207 255 / .6) }
.sa-count { font: 700 11.5px/1.4 var(--sa-mono); color: var(--sa-text) }
.sa-trial-top span.why { color: var(--sa-text); font-size: 12.5px; flex: 1 1 100%; min-width: 0 }
.sa-trial-top .msg { display: block; border-left: 2px solid var(--sa-sys); padding-left: 9px; animation: sa-type .55s steps(14, end) both }
@keyframes sa-type { from { clip-path: inset(0 100% 0 0) } to { clip-path: inset(0 0 0 0) } }
.sa-steps { display: flex; flex-wrap: wrap; gap: 4px; align-items: center }
.sa-steps i { width: 28px; height: 3px; border-radius: 2px; background-color: rgb(122 162 255 / .18); transition: background-color .4s, box-shadow .4s }
.sa-steps i[data-next="true"] { background-color: transparent; border: 1px dashed rgb(102 207 255 / .55); height: 5px }
.sa-steps i[data-end="true"] { width: 7px; height: 7px; transform: rotate(45deg); background-color: var(--sa-pass); box-shadow: 0 0 8px var(--sa-pass) }
.sa-steps i[data-on="true"] { background-color: var(--sa-sys); box-shadow: 0 0 8px rgb(102 207 255 / .7) }
.sa-steps i[data-now="true"] { background-color: #d8f3ff; box-shadow: 0 0 10px var(--sa-sys) }
.sa-trial-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center }
.sa-trial-row .grow { flex: 1 }

.sa-panel h2:focus { outline: none }
.sa-panel { padding: 18px; display: flex; flex-direction: column; gap: 10px; max-width: 640px }
.sa-panel h2 { font-size: 16px; font-weight: 700; letter-spacing: .01em }
.sa-panel p { color: var(--sa-text-2) }
.sa-panel h3 { font: 700 11px/1.3 var(--sa-mono); letter-spacing: .14em; text-transform: uppercase; color: var(--sa-sys); margin-top: 8px }
.sa-row { display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: center }
.sa-hint { color: var(--sa-muted); font-size: 12px }
.sa-hint.sa-advisory { font: 11px/1.5 var(--sa-mono); color: var(--sa-text-2); border-left: 2px solid var(--sa-line-2); padding: 1px 0 1px 9px }
.sa-checklist { display: flex; flex-direction: column; gap: 8px }
.sa-checklist li { display: grid; grid-template-columns: 16px 1fr; gap: 10px; align-items: start; color: var(--sa-text-2) }
.sa-box { width: 13px; height: 13px; margin-top: 3px; border: 1.5px solid var(--sa-muted); transform: rotate(45deg) scale(.86); position: relative }
.sa-box[data-ok="true"] { border-color: var(--sa-pass); background-color: rgb(69 232 178 / .22); box-shadow: 0 0 8px rgb(69 232 178 / .5) }
.sa-box[data-ok="true"]::after { content: ""; position: absolute; inset: 3px; background-color: var(--sa-pass) }
.sa-item { display: block; font-weight: 600; color: var(--sa-text) }
.sa-item-hint { display: block; font-size: 12px; overflow-wrap: break-word }
.sa-key { overflow-wrap: normal; word-break: normal; font-family: var(--sa-mono); font-size: 11.5px; color: var(--sa-text) }
.sa-key > span { display: inline-block; white-space: nowrap }
.sa-panel-head { display: flex; flex-wrap: wrap; gap: 4px 12px; align-items: baseline; justify-content: space-between }
.sa-panel-body { display: flex; flex-direction: column; gap: 10px }
.sa-setup-line { display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: center; padding: 7px 12px; color: var(--sa-text-2); font-size: 12.5px }
.sa-owner { font-size: 12px; color: var(--sa-muted) }
.sa-errors { display: flex; flex-direction: column; gap: 2px; margin-top: 2px }
.sa-panel .sa-errors { color: var(--sa-text) }

/* First run: the gate itself, waiting to be opened. */
.sa-portal { position: relative; width: 176px; aspect-ratio: 1; justify-self: center; isolation: isolate; color: var(--sa-sys); animation: sa-materialize 1s cubic-bezier(.2, .8, .2, 1) both }
.sa-portal::before { content: ""; position: absolute; inset: -18%; border-radius: 50%; z-index: -2; background: radial-gradient(closest-side, rgb(176 140 255 / .32), rgb(63 127 255 / .14) 55%, transparent) }
.sa-portal > * { position: absolute; inset: 0 }
.sa-portal > svg { width: 100%; height: 100% }
.sa-portal .swirl { inset: 14%; border-radius: 50%; background: conic-gradient(from 0deg, rgb(102 207 255 / .0), rgb(102 207 255 / .55), rgb(176 140 255 / .7), rgb(63 127 255 / .1), rgb(176 140 255 / .55), rgb(102 207 255 / .0)); filter: blur(9px); animation: sa-spin 14s linear infinite; z-index: -1 }
.sa-portal .void { inset: 25%; border-radius: 50%; background: radial-gradient(closest-side, #02030a 62%, rgb(20 16 60 / .9) 80%, transparent); box-shadow: 0 0 22px 4px rgb(176 140 255 / .35) inset }
.sa-portal .r1 { animation: sa-spin 48s linear infinite }
.sa-portal .r2 { animation: sa-spin 30s linear infinite reverse; color: var(--sa-violet) }
@keyframes sa-spin { to { transform: rotate(1turn) } }

/* Needs you */
.sa-needs { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: baseline; padding: 9px 12px }
.sa-needs[data-count]:not([data-count="0"]) { border-color: rgb(255 187 82 / .55); border-left: 3px solid var(--sa-warn) }
.sa-needs-tag { font: 700 10.5px/1.5 var(--sa-mono); letter-spacing: .14em; color: var(--sa-muted); white-space: nowrap }
.sa-needs[data-count]:not([data-count="0"]) .sa-needs-tag { color: var(--sa-warn) }
.sa-needs ul { display: flex; flex-direction: column; gap: 4px; flex: 1 1 260px; min-width: 0 }
.sa-needs-item { display: block; width: 100%; text-align: left; background: none; border: 0; padding: 2px 0; cursor: pointer; border-radius: 3px }
.sa-needs-item b { font-weight: 600; color: var(--sa-text) }
.sa-needs-item span { color: var(--sa-muted); font-size: 12px }
.sa-needs-item:hover b { text-decoration: underline; text-underline-offset: 2px }

/* Card tiles */
.sa-tiles { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px }
.sa-tile { isolation: isolate; display: grid; grid-template-columns: 100px minmax(0, 1fr); gap: 12px; padding: 14px 12px; min-width: 0; align-items: start }
.sa-tile > :is(.sa-mark, .sa-body, .sa-side, .sa-roster) { animation: sa-window .5s cubic-bezier(.2, .8, .2, 1) both }
@keyframes sa-window { from { opacity: 0; transform: scaleY(.92); filter: blur(3px) } }
.sa-tile[data-gate="UNVERIFIED"] { --tone: var(--sa-sys) }
.sa-tile[data-gate="BLOCKED"] { --tone: var(--sa-fail); border-color: rgb(255 98 131 / .5); box-shadow: inset 0 0 40px -22px var(--sa-fail), 0 0 26px -16px var(--sa-fail) }
.sa-tile[data-gate="PASS"] { --tone: var(--sa-pass); border-color: rgb(69 232 178 / .45); box-shadow: inset 0 0 40px -24px var(--sa-pass), 0 0 26px -18px var(--sa-pass) }
.sa-tile[data-gate="STALE"] { --tone: var(--sa-warn); border-color: rgb(255 187 82 / .45); box-shadow: inset 0 0 40px -26px var(--sa-warn) }
.sa-tile:is([data-gate="UNGATED"], [data-gate="UNARMED"], [data-gate="UNKNOWN"]) { --tone: var(--sa-quiet); border-color: var(--sa-line) }
.sa-tile:is([data-gate="UNGATED"], [data-gate="UNARMED"], [data-gate="UNKNOWN"])::before, .sa-tile:is([data-gate="UNGATED"], [data-gate="UNARMED"], [data-gate="UNKNOWN"])::after { filter: none; opacity: .6 }
.sa-tile[data-closed="true"]:not(.hero) { background-color: transparent; background-image: none; border-style: dashed; box-shadow: none }
.sa-tile[data-closed="true"]:not(.hero)::before, .sa-tile[data-closed="true"]:not(.hero)::after { display: none }
.sa-tile.sa-m-fail.sa-p0 { animation: sa-hit0 .6s var(--hit, 0s) cubic-bezier(.3, .7, .4, 1) }
.sa-tile.sa-m-fail.sa-p1 { animation: sa-hit1 .6s var(--hit, 0s) cubic-bezier(.3, .7, .4, 1) }
@keyframes sa-hit0 { 0% { transform: translateX(0) } 15% { transform: translateX(-6px) } 30% { transform: translateX(5px) } 45% { transform: translateX(-3px) } 60% { transform: translateX(2px) } 100% { transform: none } }
@keyframes sa-hit1 { 0% { transform: translateX(0) } 15% { transform: translateX(-6px) } 30% { transform: translateX(5px) } 45% { transform: translateX(-3px) } 60% { transform: translateX(2px) } 100% { transform: none } }
.sa-flash { position: absolute; inset: 0; pointer-events: none; border-radius: inherit; opacity: 0; z-index: -1; animation-delay: var(--hit, 0s) !important }
.sa-flash[data-tone="fail"] { background: radial-gradient(120% 90% at 50% 50%, transparent 40%, rgb(255 98 131 / .45)); animation: sa-flash .8s ease-out }
.sa-flash[data-tone="pass"] { background: linear-gradient(100deg, transparent 30%, rgb(69 232 178 / .35) 50%, transparent 70%) no-repeat; background-size: 250% 100%; animation: sa-sweep .85s ease-out }
.sa-flash[data-tone="warn"] { background: radial-gradient(120% 90% at 50% 50%, transparent 45%, rgb(255 187 82 / .38)); animation: sa-flash .8s ease-out }
.sa-flash[data-tone="arise"] { background: radial-gradient(70% 60% at 50% 100%, rgb(176 140 255 / .4), transparent 70%); animation: sa-flash .85s ease-out }
@keyframes sa-flash { 0% { opacity: 0 } 15% { opacity: 1 } 100% { opacity: 0 } }
@keyframes sa-sweep { 0% { opacity: 1; background-position: 100% 0 } 100% { opacity: 0; background-position: 0 0 } }

/* The moment banner: a short System line above the tile, repeating the label of the event that just landed. */
.sa-moment { position: absolute; z-index: 5; left: 50%; top: -12px; transform: translateX(-50%); display: grid; pointer-events: none; max-width: calc(100% - 24px) }
/* Each label holds fully opaque until the next one replaces it in the same frame; the last one stays at rest until
   the moment ends. Nothing fades, so no still ever catches a ghost label. */
.sa-moment span { grid-area: 1 / 1; justify-self: center; white-space: nowrap; font: 800 11px/1 var(--sa-mono); letter-spacing: .24em; padding: 6px 12px 6px 14px; color: var(--c); background-color: #060915; border: 1px solid var(--c); box-shadow: 0 0 18px -4px var(--c), inset 0 0 12px -6px var(--c); text-shadow: 0 0 8px var(--c); visibility: hidden; animation: sa-hold var(--t, 0s) var(--d, 0s) step-end, sa-open .2s var(--d, 0s) cubic-bezier(.2, .8, .2, 1) backwards }
.sa-moment span.last { visibility: visible; animation: sa-on 1ms var(--d, 0s) both, sa-open .2s var(--d, 0s) cubic-bezier(.2, .8, .2, 1) both }
@keyframes sa-open { from { transform: scaleX(.15) } }
.sa-moment span[data-tone="fail"] { --c: var(--sa-fail) }
.sa-moment span[data-tone="pass"] { --c: var(--sa-pass) }
.sa-moment span[data-tone="warn"] { --c: var(--sa-warn) }
.sa-moment span[data-tone="arise"] { --c: var(--sa-violet) }
.sa-moment span[data-tone="sys"] { --c: var(--sa-sys) }

.sa-mark { position: relative; display: flex; flex-direction: column; align-items: center; gap: 8px; min-width: 0 }
/* ARISE: a shadow soldier rises where the seal stands, then the new seal slams in over it. */
.sa-summon { position: absolute; z-index: 0; top: 0; left: 0; width: 100%; aspect-ratio: 1; pointer-events: none; opacity: 0; color: var(--sa-violet); animation: sa-summon .9s cubic-bezier(.2, .8, .2, 1) both }
.sa-summon::before { content: ""; position: absolute; inset: 8%; border-radius: 50%; background: radial-gradient(closest-side, rgb(176 140 255 / .45), rgb(63 127 255 / .12) 65%, transparent) }
.sa-summon::after { content: ""; position: absolute; left: 18%; right: 18%; bottom: 12%; height: 10%; border-radius: 50%; background: radial-gradient(closest-side, rgb(176 140 255 / .9), transparent); filter: blur(2px) }
.sa-summon svg { position: absolute; left: 31%; width: 38%; bottom: 16%; height: 58%; fill: currentColor; filter: drop-shadow(0 0 10px var(--sa-violet)); mask-image: linear-gradient(#000 50%, transparent 100%); transform-origin: 50% 100% }
.sa-summon svg .eyes { fill: #e6f7ff }
@keyframes sa-summon { 0% { opacity: 0; transform: translateY(18%) scaleY(.4) } 28% { opacity: 1; transform: translateY(-3%) scaleY(1.06) } 62% { opacity: 1; transform: none } 100% { opacity: 0; transform: scale(1.12) } }
.sa-mark .sa-seal { width: 100% }
.sa-closed { font: 800 10px/1 var(--sa-mono); letter-spacing: .2em; color: var(--sa-pass); border: 1.5px solid var(--sa-pass); border-radius: 2px; padding: 4px 7px 4px 9px; white-space: nowrap; box-shadow: 0 0 10px -3px var(--sa-pass); animation: sa-stamp .5s var(--hit, 0s) cubic-bezier(.2, .9, .3, 1.4) both }
.sa-tile[data-gate="STALE"] .sa-closed, .sa-ledger-head .sa-closed { color: var(--sa-text-2); border-color: var(--sa-line-2); box-shadow: none }
@keyframes sa-stamp { 0% { opacity: 0; transform: scale(1.8) } 60% { opacity: 1; transform: scale(.94) } 100% { transform: none } }
.sa-body { min-width: 0; display: flex; flex-direction: column; gap: 7px }
.sa-title-row { display: flex; flex-wrap: wrap; gap: 2px 10px; align-items: baseline }
.sa-title { font-size: 15px; font-weight: 700; text-align: left; background: none; border: 0; padding: 0; cursor: pointer; line-height: 1.3; color: var(--sa-text) }
.sa-title:hover { text-decoration: underline; text-decoration-color: var(--tone); text-underline-offset: 3px }
.sa-lane { font: 700 10px/1.2 var(--sa-mono); letter-spacing: .12em; text-transform: uppercase; color: var(--sa-muted) }
.sa-why { color: var(--sa-text-2) }
.sa-next { color: var(--sa-text-2); font-size: 12px }
.sa-next .sa-label { font-size: 10px }
.sa-tile[data-gate="BLOCKED"] .sa-why { color: var(--sa-text) }
.sa-qa { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 4px 12px; align-items: baseline }
.sa-qa dt { font: 700 10px/1.7 var(--sa-mono); letter-spacing: .16em; text-transform: uppercase; color: var(--tone, var(--sa-sys)) }
.sa-qa dd { margin: 0; color: var(--sa-text-2) }
.sa-qa dd.why { color: var(--sa-text); font-weight: 500 }
.sa-side { grid-column: 1 / -1; min-width: 0; border-top: 1px solid var(--sa-line); padding-top: 12px; display: flex; flex-direction: column; gap: 9px }
.sa-side > .sa-label { color: var(--sa-text-2) }
.sa-more { align-self: flex-start }

/* The gate seal: a portal ring. Locked crossbars on BLOCKED, a fracture on STALE, rays on PASS. */
.sa-seal { --seal: var(--sa-quiet); position: relative; aspect-ratio: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; color: var(--seal); isolation: isolate }
.sa-seal::before { content: ""; position: absolute; inset: 16%; border-radius: 50%; z-index: -2; background: radial-gradient(closest-side, color-mix(in srgb, var(--seal) 26%, transparent), color-mix(in srgb, var(--seal) 7%, transparent) 70%, transparent) }
.sa-seal .art, .sa-seal .runes { position: absolute; inset: 0; width: 100%; height: 100%; z-index: -1; overflow: visible; fill: none; stroke: currentColor }
.sa-seal .art .halo { stroke-width: 1; opacity: .3 }
.sa-seal .art .ticks { stroke-width: 2.6; stroke-dasharray: .7 6.53; opacity: .5 }
.sa-seal .art .ring { stroke-width: 1.6; filter: drop-shadow(0 0 3px currentColor) }
.sa-seal .art .bars, .sa-seal .art .crack, .sa-seal .art .rays { stroke-dasharray: 1; stroke-dashoffset: 0; stroke-linecap: round }
.sa-seal .art .bars { stroke-width: 2.4; opacity: .42 }
.sa-seal .art .crack { stroke-width: 1.7; stroke: #ffd9a0; filter: drop-shadow(0 0 3px var(--sa-warn)) }
.sa-seal .art .rays { stroke-width: 1.6; opacity: .75 }
.sa-seal .art .shard { stroke-width: 2.2; stroke: #ffd9a0; stroke-linecap: round }
.sa-seal .runes { stroke-width: 1.2; stroke-dasharray: 5 3 1 3; opacity: .7 }
.sa-seal[data-gate="UNVERIFIED"] .runes { animation: sa-spin 40s linear infinite }
/* The band: crossbars, cracks and rays stop at its edge, so they never cross the word. */
.sa-seal .plate { display: flex; flex-direction: column; align-items: center; gap: 2px; width: 86%; padding: 5px 4px 6px; background-color: rgb(4 6 15 / .92); border-block: 1px solid color-mix(in srgb, var(--seal) 50%, transparent); box-shadow: 0 0 16px -6px var(--seal), inset 0 0 12px -8px var(--seal) }
.sa-seal .k { font: 700 8px/1 var(--sa-mono); letter-spacing: .26em; color: var(--sa-muted); text-transform: uppercase; padding-left: .26em }
.sa-seal .w { font: 900 12.5px/1.05 var(--sa-sans); letter-spacing: .04em; white-space: nowrap; text-shadow: 0 0 10px color-mix(in srgb, var(--seal) 70%, transparent) }
.sa-seal[data-gate="UNVERIFIED"] .w:not(.was), .sa-seal .was[data-gate="UNVERIFIED"] { font-size: 10px; letter-spacing: 0 }
.sa-seal .h { position: absolute; left: 50%; bottom: 0; transform: translateX(-50%); font: 8.5px/1.3 var(--sa-mono); color: var(--sa-text-2); display: flex; flex-direction: column; align-items: center; white-space: nowrap; padding: 2px 6px; background-color: var(--sa-inset); border: 1px solid color-mix(in srgb, var(--seal) 40%, transparent); border-radius: 2px }
.sa-seal[data-gate="UNVERIFIED"] { --seal: var(--sa-sys) }
.sa-seal[data-gate="BLOCKED"] { --seal: var(--sa-fail) }
.sa-seal[data-gate="PASS"] { --seal: var(--sa-pass) }
.sa-seal[data-gate="STALE"] { --seal: var(--sa-warn) }
.sa-seal:is([data-gate="UNGATED"], [data-gate="UNARMED"], [data-gate="UNKNOWN"]) .w:not(.was), .sa-seal .was:is([data-gate="UNGATED"], [data-gate="UNARMED"], [data-gate="UNKNOWN"]) { font-size: 10px; letter-spacing: .03em; text-shadow: none }
.sa-seal:is([data-gate="UNGATED"], [data-gate="UNARMED"], [data-gate="UNKNOWN"]) .plate { box-shadow: none; background-color: rgb(4 6 15 / .7) }
.sa-seal:is([data-gate="UNGATED"], [data-gate="UNARMED"]) .art .ring { stroke-dasharray: 1.5 4; filter: none }
.sa-seal:is([data-gate="UNGATED"], [data-gate="UNARMED"], [data-gate="UNKNOWN"])::before { opacity: .45 }
.sa-seal[data-closed="true"]:is([data-gate="BLOCKED"], [data-gate="UNKNOWN"]) { --seal: var(--sa-quiet) }
.sa-seal[data-closed="true"]:is([data-gate="BLOCKED"], [data-gate="UNKNOWN"]) .art .ring { stroke-dasharray: 1.5 4 }
.sa-seal::after { content: ""; position: absolute; inset: 10%; border-radius: 50%; border: 2px solid var(--seal); opacity: 0; pointer-events: none; z-index: -1 }
/* The word on the band is never dimmed, covered or faded. While a moment holds the verdict back (--hit), the band
   keeps the gate word that stood before; at the hit the new word replaces it in one frame. Only the ring art moves. */
.sa-seal .words { display: grid; justify-items: center }
.sa-seal .words > * { grid-area: 1 / 1 }
.sa-seal .was { visibility: hidden }
.sa-seal .was[data-gate="UNVERIFIED"] { color: var(--sa-sys) }
.sa-seal .was[data-gate="BLOCKED"] { color: var(--sa-fail) }
.sa-seal .was[data-gate="PASS"] { color: var(--sa-pass) }
.sa-seal .was[data-gate="STALE"] { color: var(--sa-warn) }
.sa-seal .was:is([data-gate="UNGATED"], [data-gate="UNARMED"], [data-gate="UNKNOWN"]) { color: var(--sa-quiet) }
.sa-seal.flip .was { animation: sa-hold var(--hit, 0s) step-end }
.sa-seal.flip .w:not(.was), .sa-seal.flip .h { animation: sa-on 1ms var(--hit, 0s) both }
.sa-seal.flip .plate { animation: sa-plate var(--hit, 0s) step-end }
@keyframes sa-hold { from, to { visibility: visible } }
@keyframes sa-on { from { visibility: hidden } }
@keyframes sa-plate { from, to { border-color: var(--sa-line-2); box-shadow: none } }
.sa-seal.flip > :is(.art, .runes), .sa-seal.flip::before { animation: sa-materialize .6s var(--hit, 0s) cubic-bezier(.2, .8, .2, 1) both }
.sa-seal.flip::after { animation: sa-wave .8s calc(var(--hit, 0s) + .1s) ease-out }
/* BLOCKED: the ring slams down and the crossbars lock. */
.sa-seal.flip[data-gate="BLOCKED"] > :is(.art, .runes) { animation: sa-slam .6s var(--hit, 0s) cubic-bezier(.2, .9, .3, 1.25) both }
.sa-seal.flip[data-gate="BLOCKED"] .bars { animation: sa-draw .35s calc(var(--hit, 0s) + .25s) ease-in both }
/* PASS: the seal is stamped. The ring drops onto the gate, flares once, and the rays burst from the impact. */
.sa-seal.flip[data-gate="PASS"] > :is(.art, .runes) { animation: sa-pass .7s var(--hit, 0s) cubic-bezier(.2, .9, .3, 1.2) both }
.sa-seal.flip[data-gate="PASS"] .rays { animation: sa-burst .5s calc(var(--hit, 0s) + .25s) ease-out both }
.sa-seal.flip[data-gate="PASS"]::after { animation: sa-wave .6s calc(var(--hit, 0s) + .22s) ease-out }
/* STALE: the crack runs through the ring first, then the gate jolts. */
.sa-seal.flip[data-gate="STALE"] > :is(.art, .runes) { animation: sa-jolt .45s calc(var(--hit, 0s) + .35s) ease-out both }
.sa-seal.flip[data-gate="STALE"] .crack { animation: sa-draw .35s var(--hit, 0s) cubic-bezier(.6, 0, .4, 1) both, sa-glint .5s calc(var(--hit, 0s) + .3s) ease-out }
.sa-seal.flip[data-gate="STALE"] .shard { animation: sa-shard .45s calc(var(--hit, 0s) + .35s) ease-out both }
/* While a moment holds the verdict back, the ring shows its 0% frame: a dim grey ring awaiting judgment. */
@keyframes sa-materialize { 0% { opacity: .3; filter: grayscale(1) brightness(.7) } 1% { opacity: 0; transform: scale(.7); filter: blur(4px) } }
@keyframes sa-slam { 0% { opacity: .3; filter: grayscale(1) brightness(.7) } 1% { opacity: 0; transform: scale(1.6); filter: none } 50% { opacity: 1; transform: scale(.92) } 75% { transform: scale(1.04) } 100% { transform: none } }
@keyframes sa-pass { 0% { opacity: .3; filter: grayscale(1) brightness(.7) } 1% { opacity: 0; transform: scale(1.35); filter: none } 35% { opacity: 1; transform: scale(.95); filter: brightness(2) drop-shadow(0 0 12px var(--sa-pass)) } 55% { transform: scale(1.02) } 100% { transform: none; filter: none } }
@keyframes sa-jolt { 0% { transform: none } 25% { transform: translate(-3px, 1px) rotate(-2deg) } 50% { transform: translate(3px, -1px) rotate(1.5deg) } 75% { transform: translate(-1px, 0) rotate(-.5deg) } 100% { transform: none } }
@keyframes sa-glint { 0% { stroke: #fff4dc; stroke-width: 3 } 100% { stroke: #ffd9a0 } }
@keyframes sa-shard { 0% { transform: none } 40% { transform: translate(2.5px, -1.5px) } 100% { transform: translate(1.2px, -.6px) } }
@keyframes sa-draw { from { stroke-dashoffset: 1 } }
@keyframes sa-burst { from { opacity: 0; transform: scale(.7) } }
@keyframes sa-wave { 0% { opacity: .9; transform: scale(.75) } 100% { opacity: 0; transform: scale(1.45) } }
.sa-seal .rays { transform-origin: 50px 50px }
/* A refused close: a red seal slams onto the gate behind the word band, then lifts. It never covers the word. */
.sa-stamp { position: absolute; z-index: -1; inset: 4%; pointer-events: none; opacity: 0; color: var(--sa-fail); animation: sa-stampin 1.2s cubic-bezier(.2, .9, .3, 1.2) both }
.sa-stamp svg { width: 100%; height: 100%; fill: none; stroke: currentColor; filter: drop-shadow(0 0 6px var(--sa-fail)) }
@keyframes sa-stampin { 0% { opacity: 0; transform: scale(1.7) } 25% { opacity: 1; transform: scale(.95) } 35% { transform: none } 85% { opacity: 1 } 100% { opacity: 0 } }

.sa-badge { display: inline-flex; align-items: center; font: 800 10px/1 var(--sa-mono); letter-spacing: .12em; padding: 4px 7px; border-radius: 2px; border: 1px solid var(--sa-line-2); color: var(--sa-text-2); white-space: nowrap }
.sa-badge[data-gate="UNVERIFIED"] { color: var(--sa-sys); border-style: dashed; border-color: rgb(102 207 255 / .55) }
.sa-badge[data-gate="BLOCKED"] { color: var(--sa-fail); border-color: rgb(255 98 131 / .6); background-color: rgb(255 98 131 / .1) }
.sa-badge[data-gate="PASS"] { color: var(--sa-pass); border-color: rgb(69 232 178 / .55); background-color: rgb(69 232 178 / .09) }
.sa-badge[data-gate="STALE"] { color: var(--sa-warn); border-color: rgb(255 187 82 / .55); background-color: rgb(255 187 82 / .08) }

/* Shadows: each row is a soldier that rises when it is called, or is turned away. */
.sa-shadows { display: flex; flex-direction: column }
.sa-shadow { display: grid; grid-template-columns: 16px 52px minmax(0, 1fr) auto; gap: 2px 8px; align-items: center; padding: 6px 0; border-top: 1px solid var(--sa-line); min-width: 0; animation: sa-arise .7s cubic-bezier(.2, .8, .2, 1) both; animation-delay: calc(var(--i, 0) * 70ms) }
@keyframes sa-arise { from { opacity: 0; transform: translateY(10px); filter: blur(4px) } }
.sa-sigil { width: 16px; height: 20px; color: var(--sa-sys); fill: currentColor; overflow: visible; mask-image: linear-gradient(#000 55%, transparent 100%); filter: drop-shadow(0 0 3px currentColor); transform-origin: 50% 100%; animation: sa-rise .8s cubic-bezier(.2, .8, .2, 1) both; animation-delay: calc(var(--i, 0) * 70ms) }
.sa-sigil .eyes { fill: #e6f7ff }
.sa-shadow[data-duty="verify"] .sa-sigil { color: var(--sa-violet) }
.sa-shadow[data-state="refused"] .sa-sigil { color: #5d6788; filter: none }
.sa-shadow[data-state="refused"] .sa-sigil .eyes { fill: var(--sa-fail) }
.sa-sigil .slash { stroke: var(--sa-fail); stroke-width: 1.8; fill: none; stroke-linecap: round; stroke-dasharray: 1; animation: sa-draw .3s .5s ease-in both }
@keyframes sa-rise { 0% { opacity: 0; transform: translateY(8px) scaleY(.15) } 55% { opacity: 1; transform: translateY(-2px) scaleY(1.12) } 100% { transform: none } }
.sa-shadow[data-state="refused"] { animation-name: sa-turned; animation-duration: .8s }
@keyframes sa-turned { 0% { opacity: 0; transform: translateY(10px) } 35% { opacity: 1; transform: none } 52% { transform: translateX(-7px) } 64% { transform: translateX(5px) } 76% { transform: translateX(-2px) } 100% { transform: none } }
.sa-duty { font: 700 10px/1 var(--sa-mono); letter-spacing: .12em; text-transform: uppercase; color: var(--sa-text-2) }
.sa-who { display: flex; flex-wrap: wrap; gap: 4px 6px; align-items: center; min-width: 0 }
.sa-fam { display: inline-flex; align-items: center; gap: 5px; padding: 1px 7px 1px 6px; border-radius: 2px; background-color: rgb(122 162 255 / .1); border: 1px solid var(--sa-line); color: var(--sa-text-2); font-size: 11.5px; white-space: nowrap }
.sa-fam::before { content: ""; width: 6px; height: 6px; border: 1.5px solid currentColor }
.sa-fam[data-fam="openai"]::before { border-radius: 50% }
.sa-fam[data-fam="anthropic"]::before { transform: rotate(45deg) }
.sa-fam[data-fam="google"]::before { border-radius: 50% 50% 0 50% }
.sa-model { font-size: 11.5px; color: var(--sa-muted); min-width: 0 }
.sa-sim { font: 10.5px/1.6 var(--sa-mono); color: var(--sa-muted); border: 1px dashed var(--sa-line-2); border-radius: 2px; padding: 0 5px; white-space: nowrap }
.sa-state { justify-self: end; font-size: 12px; color: var(--sa-muted); white-space: nowrap; display: inline-flex; align-items: center; gap: 5px }
.sa-state i { width: 6px; height: 6px; border-radius: 50%; background-color: var(--sa-violet); box-shadow: 0 0 8px var(--sa-violet); animation: sa-pulse 1.6s ease-in-out infinite }
.sa-state.run { color: var(--sa-text-2) }
.sa-state.refused { color: var(--sa-fail); font: 800 10px/1 var(--sa-mono); letter-spacing: .14em; text-transform: uppercase; border: 1px solid rgb(255 98 131 / .6); border-radius: 2px; padding: 4px 6px; background-color: rgb(255 98 131 / .08); animation: sa-stamp .5s .6s cubic-bezier(.2, .9, .3, 1.4) both }
.sa-shadow[data-state="refused"] .sa-model { text-decoration: line-through; text-decoration-color: rgb(255 98 131 / .75) }
.sa-finding { grid-column: 3 / -1; color: var(--sa-text-2); font-size: 12px }
.sa-finding[data-bad="true"] { color: var(--sa-fail) }

.sa-v { font: 800 10px/1 var(--sa-mono); letter-spacing: .14em; padding: 4px 6px; border-radius: 2px; white-space: nowrap; border: 1px solid transparent }
.sa-v[data-v="FAIL"] { color: var(--sa-fail); background-color: rgb(255 98 131 / .12); border-color: rgb(255 98 131 / .45); box-shadow: 0 0 10px -4px var(--sa-fail) }
.sa-v[data-v="PASS"] { color: var(--sa-pass); background-color: rgb(69 232 178 / .1); border-color: rgb(69 232 178 / .45); box-shadow: 0 0 10px -4px var(--sa-pass) }
.sa-v[data-v="EDITED"] { color: var(--sa-warn); background-color: rgb(255 187 82 / .1); border-color: rgb(255 187 82 / .45) }
.sa-hash { font-size: 11.5px; color: var(--sa-text-2) }
.sa-older { font-size: 11px; color: var(--sa-muted); border: 1px solid var(--sa-line-2); border-radius: 2px; padding: 0 5px; white-space: nowrap }
.sa-verdicts { display: flex; flex-direction: column; gap: 12px }
.sa-verdict { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 4px 10px; align-items: start }
.sa-verdict .meta { display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: center; color: var(--sa-muted); font-size: 12px }
.sa-verdict .note { grid-column: 2; color: var(--sa-text); font-size: 12.5px }
.sa-verdict.old .note { color: var(--sa-text-2) }
.sa-verdict .sa-check { grid-column: 1 / -1 }
/* Evidence is a terminal readout of the real check run. */
.sa-check { border: 1px solid var(--sa-line); border-left: 2px solid var(--sa-sys); border-radius: 2px; background-color: var(--sa-inset); min-width: 0; animation: sa-window .45s ease-out both }
.sa-check[data-ok="false"] { border-left-color: var(--sa-fail); box-shadow: inset 14px 0 24px -22px var(--sa-fail) }
.sa-check[data-ok="true"] { border-left-color: var(--sa-pass); box-shadow: inset 14px 0 24px -22px var(--sa-pass) }
.sa-check-cmd { display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: baseline; padding: 7px 10px; font: 11.5px/1.45 var(--sa-mono); color: var(--sa-sys) }
.sa-check-cmd code { flex: 1 1 200px; min-width: 0; font: inherit }
.sa-exit { font: 800 10.5px/1.4 var(--sa-mono); letter-spacing: .08em; white-space: nowrap }
.sa-exit[data-ok="true"] { color: var(--sa-pass) }
.sa-exit[data-ok="false"] { color: var(--sa-fail); text-shadow: 0 0 8px rgb(255 98 131 / .6) }
.sa-out { border-top: 1px solid var(--sa-line); padding: 7px 10px; font: 11px/1.5 var(--sa-mono); color: var(--sa-text-2); white-space: pre-wrap; overflow-wrap: anywhere; max-height: 6.6em; overflow: auto; animation: sa-scan .9s .15s steps(9, end) both }
@keyframes sa-scan { from { clip-path: inset(0 0 100% 0) } to { clip-path: inset(0 0 0 0) } }
.sa-refusal { display: flex; flex-wrap: wrap; gap: 2px 8px; align-items: baseline; font-size: 12px; color: var(--sa-text-2); padding: 7px 10px; border: 1px solid rgb(255 98 131 / .5); border-left-width: 3px; border-radius: 2px; background-color: var(--sa-panel); animation: sa-window .45s ease-out both }
.sa-refusal.dashed { border-style: dashed; border-left-style: solid }
.sa-refusal b { color: var(--sa-fail); font: 800 10px/1.7 var(--sa-mono); letter-spacing: .12em; white-space: nowrap }

/* Ledger */
.sa-detail { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; align-items: start }
.sa-index { display: none; flex-direction: column; gap: 4px }
.sa-index-item { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 3px 8px; text-align: left; cursor: pointer; background-color: transparent; border: 1px solid transparent; border-radius: 3px; padding: 8px 10px; width: 100% }
.sa-index-item:hover { background-color: rgb(122 162 255 / .07) }
.sa-index-item[aria-current="true"] { background-color: var(--sa-raised); border-color: var(--sa-line-2); box-shadow: inset 2px 0 0 var(--sa-sys) }
.sa-index-item .t { font-weight: 600; min-width: 0 }
.sa-index-item > .sa-badge { align-self: start; margin-top: 1px }
.sa-index-item .s { grid-column: 1 / -1; color: var(--sa-muted); font-size: 12px }
.sa-ledger { min-width: 0; display: flex; flex-direction: column; gap: 12px }
.sa-back { align-self: flex-start }
.sa-ledger-head { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center }
.sa-ledger-head h2 { font-size: 18px; font-weight: 700; letter-spacing: .005em }
.sa-ledger-head h2:focus { outline: none }
.sa-summary { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; padding: 8px 0 12px; border-bottom: 1px solid var(--sa-line) }
.sa-summary .arrow { color: var(--sa-muted) }
.sa-summary .by { margin-left: auto; display: inline-flex; flex-wrap: wrap; gap: 4px 6px; align-items: center; color: var(--sa-muted); font-size: 12px }
.sa-root .sa-rail { position: relative; padding-left: 22px; display: flex; flex-direction: column; gap: 2px }
.sa-rail::before { content: ""; position: absolute; left: 5px; top: 12px; bottom: 12px; width: 2px; background: linear-gradient(var(--sa-sys), var(--sa-violet) 60%, rgb(176 140 255 / .2)); box-shadow: 0 0 8px rgb(102 207 255 / .5) }
.sa-ev { position: relative; padding: 7px 0 7px 6px; display: grid; grid-template-columns: minmax(0, 1fr); gap: 2px 10px }
.sa-ev::before { content: ""; position: absolute; left: -20px; top: 13px; width: 8px; height: 8px; transform: rotate(45deg); background-color: var(--sa-bg); border: 2px solid var(--sa-sys) }
.sa-ev time, .sa-bar time { color: var(--sa-muted); font: 11px/1.6 var(--sa-mono); white-space: nowrap }
.sa-ev .who { display: flex; flex-wrap: wrap; gap: 4px 6px; align-items: center }
.sa-ev .txt { color: var(--sa-text-2) }
.sa-ev.verdict { padding: 10px; margin: 3px 0 3px -5px; border-radius: 2px; gap: 6px 10px; border: 1px solid var(--sa-line) }
.sa-ev.verdict::before { left: -16px; top: 16px }
.sa-ev.verdict .txt { color: var(--sa-text) }
.sa-ev[data-v="FAIL"] { background-color: rgb(255 98 131 / .06); border-color: rgb(255 98 131 / .3) }
.sa-ev[data-v="FAIL"]::before { border-color: var(--sa-fail); background-color: var(--sa-fail); box-shadow: 0 0 8px var(--sa-fail) }
.sa-ev[data-v="PASS"] { background-color: rgb(69 232 178 / .06); border-color: rgb(69 232 178 / .3) }
.sa-ev[data-v="PASS"]::before { border-color: var(--sa-pass); background-color: var(--sa-pass); box-shadow: 0 0 8px var(--sa-pass) }
.sa-bar { margin: 5px 0 5px -22px; padding: 8px 11px; border: 1px solid rgb(255 98 131 / .6); border-left-width: 3px; border-radius: 2px; display: flex; flex-wrap: wrap; gap: 3px 10px; align-items: baseline; color: var(--sa-text); background-color: var(--sa-panel); position: relative }
.sa-bar b { color: var(--sa-fail); font: 800 10px/1.7 var(--sa-mono); letter-spacing: .12em; white-space: nowrap }
.sa-bar .txt { flex: 1 1 220px; min-width: 0 }
.sa-bar.dashed { border-style: dashed; border-left-style: solid }
.sa-bar.permitted, .sa-refusal.permitted { border-color: rgb(102 207 255 / .55); border-style: dashed; border-left-style: solid }
.sa-bar.permitted b, .sa-refusal.permitted b { color: var(--sa-sys) }
.sa-bar.neutral, .sa-refusal.neutral { border-color: var(--sa-line-2); background-color: var(--sa-panel) }
.sa-bar.neutral b, .sa-refusal.neutral b, .sa-refusal.neutral span { color: var(--sa-text-2) }
.sa-bar.closed, .sa-refusal.closed { border-color: var(--sa-pass); background-color: #0b1d26; box-shadow: 0 0 16px -8px var(--sa-pass) }
.sa-bar.closed b, .sa-refusal.closed b { color: var(--sa-pass) }
.sa-bar.edited, .sa-refusal.edited { border-color: var(--sa-warn); background-color: #1a1720; box-shadow: 0 0 16px -8px var(--sa-warn) }
.sa-bar.edited b, .sa-refusal.edited b { color: var(--sa-warn) }
.sa-refusal.edited span, .sa-refusal.closed span { color: var(--sa-text) }

/* First run: a hero. The gate holds two shadows, and the only bright control on screen is "Play the trial". */
.sa-hero { display: flex; flex-direction: column; gap: 26px; width: 100%; max-width: 1080px; margin: 0 auto }
.sa-stage { display: grid; grid-template-columns: minmax(0, 1fr); justify-items: center; text-align: center; gap: 18px; padding: 6px 0 0 }
.sa-hero .sa-portal { width: min(250px, 66cqi) }
.sa-pitch { display: flex; flex-direction: column; align-items: center; gap: 12px; min-width: 0; max-width: 620px; animation: sa-window .7s .15s cubic-bezier(.2, .8, .2, 1) both }
.sa-eyebrow { font: 700 11px/1.3 var(--sa-mono); letter-spacing: .3em; text-transform: uppercase; color: var(--sa-sys); text-shadow: 0 0 12px rgb(102 207 255 / .55) }
.sa-pitch h2 { font: 800 clamp(28px, 7.2cqi, 52px)/1.04 var(--sa-sans); letter-spacing: -.02em; color: #f4f7ff; text-wrap: balance; text-shadow: 0 0 30px rgb(124 82 255 / .5) }
.sa-sub { color: var(--sa-text-2); font-size: 14px; line-height: 1.5 }
.sa-cta { display: flex; flex-direction: column; align-items: center; gap: 8px; margin-top: 8px }
.sa-cta .sa-hint { max-width: 340px }
.sa-preview-wrap { display: flex; flex-direction: column; align-items: center; gap: 8px; margin-top: 10px; width: 100% }
.sa-preview { display: grid; grid-template-columns: repeat(3, minmax(0, 104px)); gap: 14px; justify-content: center }
.sa-preview li { display: flex; flex-direction: column; align-items: center; gap: 6px; min-width: 0 }
.sa-preview .cap { font-size: 11.5px; line-height: 1.3; color: var(--sa-text-2); text-align: center }
.sa-preview .cap b { display: block; font: 700 9.5px/1.4 var(--sa-mono); letter-spacing: .14em; color: var(--sa-muted) }
.sa-seal.mini { width: 74px; flex: none }
.sa-seal.mini .w { font-size: 11px }
.sa-seal.mini .k { color: var(--sa-text-2) }
.sa-seal.mini .art .rays, .sa-seal.mini .art .bars { animation: none }
.sa-hero-setup { max-width: none; animation: sa-window .7s .3s cubic-bezier(.2, .8, .2, 1) both }
.sa-hero-setup h3 { margin: 4px 0 0 }
/* The portal holds the builder and, rising beside it, the second shadow. */
.sa-portal > .duo { inset: 27% 23% 19%; display: flex; justify-content: center; align-items: flex-end; gap: 3%; mask-image: linear-gradient(#000 68%, transparent 100%) }
.sa-portal .duo svg { width: 44%; height: auto; overflow: visible; mask-image: none }
.sa-portal .duo .b { color: var(--sa-sys); opacity: .8; filter: drop-shadow(0 0 6px rgb(102 207 255 / .8)) }
.sa-portal .duo .v { color: var(--sa-violet); filter: drop-shadow(0 0 8px rgb(176 140 255 / .95)); transform: scaleX(-1); animation: sa-second 1.8s .5s cubic-bezier(.2, .8, .2, 1) both }
@keyframes sa-second { 0% { opacity: 0; transform: scaleX(-1) translateY(40%) scaleY(.3) } 55% { opacity: 1; transform: scaleX(-1) translateY(-4%) scaleY(1.06) } 100% { transform: scaleX(-1) } }

/* Shadows in profile. The glyph on the chest is an abstract mark for the model family, never a logo. */
.sa-psig { width: 40px; height: 54px; flex: none; overflow: visible; color: var(--sa-sys); filter: drop-shadow(0 0 6px currentColor); mask-image: linear-gradient(#000 62%, transparent 100%) }
.sa-psig .body { fill: currentColor }
.sa-psig .face { fill: #04060f }
.sa-psig .folds { fill: none; stroke: #04060f; stroke-opacity: .4; stroke-width: .7 }
.sa-psig .eye { fill: #e6f7ff }
.sa-psig .glyph { fill: none; stroke: #04060f; stroke-width: 1.3; stroke-linejoin: round }
.sa-psig .slash { fill: none; stroke: var(--sa-fail); stroke-width: 2; stroke-linecap: round; stroke-dasharray: 1; animation: sa-draw .3s .5s ease-in both }

/* The duel: builder and verifier face each other across the seal. */
.sa-duel { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 150px) minmax(0, 1fr); align-items: center; gap: 4px; width: 100%; max-width: 380px; margin: 0 auto }
.sa-duel::before { content: ""; position: absolute; z-index: -1; left: 14%; right: 14%; top: 50%; height: 1px; background: linear-gradient(90deg, var(--sa-sys), rgb(102 207 255 / 0) 34%, rgb(176 140 255 / 0) 66%, var(--sa-violet)); opacity: .6 }
.sa-fighter { position: relative; display: flex; flex-direction: column; align-items: center; gap: 5px; min-width: 0; text-align: center }
.sa-fighter .role { font: 700 9.5px/1 var(--sa-mono); letter-spacing: .16em; text-transform: uppercase; color: var(--sa-text-2) }
.sa-fighter .none { font-size: 11.5px; color: var(--sa-muted) }
.sa-fighter .sa-psig { animation: sa-rise .8s cubic-bezier(.2, .8, .2, 1) both }
.sa-fighter[data-side="verify"] .sa-psig { color: var(--sa-violet); transform: scaleX(-1); animation-name: sa-rise-l }
@keyframes sa-rise-l { 0% { opacity: 0; transform: scaleX(-1) translateY(8px) scaleY(.15) } 55% { opacity: 1; transform: scaleX(-1) translateY(-2px) scaleY(1.12) } 100% { transform: scaleX(-1) } }
.sa-fighter[data-state="absent"] .sa-psig { filter: none; animation: none }
.sa-fighter[data-state="absent"] .sa-psig .body { fill: none; stroke: currentColor; stroke-width: .9; stroke-dasharray: 2 1.6; opacity: .7 }
.sa-fighter[data-state="absent"] .sa-psig :is(.face, .eye, .folds) { display: none }
.sa-fighter[data-state="waiting"] .sa-psig { opacity: .55; filter: none }
.sa-fighter[data-state="refused"] .sa-psig { color: #5d6788; filter: none; animation: sa-recoil .8s cubic-bezier(.2, .8, .2, 1) both }
.sa-fighter[data-state="refused"] .sa-psig .eye { fill: var(--sa-fail) }
.sa-fighter[data-state="refused"] .sa-fam { text-decoration: line-through; text-decoration-color: var(--sa-fail) }
@keyframes sa-recoil { 0% { opacity: 0; transform: scaleX(-1) translateY(8px) } 35% { opacity: 1; transform: scaleX(-1) } 52% { transform: scaleX(-1) translateX(-7px) } 64% { transform: scaleX(-1) translateX(5px) } 100% { transform: scaleX(-1) translateX(3px) } }
.sa-fighter .sa-state.refused { position: absolute; top: 100%; margin-top: 5px; animation-delay: .55s }
/* ARISE in the duel: a pool of light opens under the verifier's place. */
.sa-tile.hero .sa-fighter { --fh: 43px }
/* ARISE: a portal opens on the floor of the verifier's side and the other family's shadow rises out of it, clipped at
   the floor line, in .75 s. The portal settles to a faint ring and goes when the moment ends. */
.sa-floor { position: absolute; z-index: -1; left: 50%; top: calc(var(--fh, 54px) - 6px); width: calc(var(--fh, 54px) * 1.25); height: 12px; translate: -50% 0; border-radius: 50%; pointer-events: none; background: radial-gradient(closest-side, rgb(214 196 255 / .95), rgb(176 140 255 / .55) 45%, rgb(63 127 255 / .15) 80%, transparent); box-shadow: 0 0 18px -2px var(--sa-violet); opacity: .45; transform: scaleX(.8); animation: sa-floor .8s cubic-bezier(.2, .8, .2, 1) backwards }
@keyframes sa-floor { 0% { opacity: 1; transform: scaleX(0) } 25% { opacity: 1; transform: scaleX(1.1) } 70% { opacity: 1; transform: scaleX(1) } }
.sa-fighter.summon[data-side="verify"] .sa-psig { animation: sa-emerge .75s .12s cubic-bezier(.2, .8, .2, 1) both }
@keyframes sa-emerge { from { transform: scaleX(-1) translateY(62%); clip-path: inset(0 0 62% 0) } to { transform: scaleX(-1); clip-path: inset(0 0 0 0) } }
/* Narrow: a small seal, so the finding and the next step stay above the fold. */
.sa-tile.hero .sa-duel { grid-template-columns: minmax(0, 1fr) minmax(0, 112px) minmax(0, 1fr); max-width: 330px }
.sa-tile.hero .sa-duel .sa-psig { width: 32px; height: 43px }
.sa-tile.hero .sa-duel .sa-seal { width: 100% }
.sa-tile.hero .sa-seal .w { font-size: 14px }
.sa-tile.hero .sa-seal[data-gate="UNVERIFIED"] .w:not(.was), .sa-tile.hero .sa-seal .was[data-gate="UNVERIFIED"] { font-size: 10.5px }
.sa-tile.hero .sa-seal .h { font-size: 9.5px }

/* Cause before effect: during a moment, evidence waits for --lag and the verdict lands at --hit. */
.sa-ev-item { animation: sa-window .5s var(--lag, 0s) cubic-bezier(.2, .8, .2, 1) both }
.sa-ev-item .sa-check { animation-delay: var(--lag, 0s) }
.sa-ev-item .sa-out { animation-delay: calc(var(--lag, 0s) + .15s) }
.sa-shadow .sa-v { animation: sa-window .45s var(--hit, 0s) ease-out both }
/* Why swaps from "Review running…" to the gate's reason in one frame at the hit; Found and Next appear with it. */
.sa-tile .sa-qa dd.why { display: grid }
.sa-tile .sa-qa dd.why > * { grid-area: 1 / 1; min-width: 0 }
.sa-tile .sa-side-head .sa-copy { animation: sa-on 1ms var(--hit, 0s) both }
.sa-held { visibility: hidden; color: var(--sa-text-2); animation: sa-hold var(--hit, 0s) step-end }
.sa-tile .sa-now, .sa-tile .sa-qa .nx { animation: sa-on 1ms var(--hit, 0s) both }
.sa-tile.sa-at-once .sa-now, .sa-tile.sa-at-once .sa-qa .nx { animation: none }
.sa-qa dt.found { color: var(--sa-fail) }
.sa-qa dd.found .note { color: var(--sa-text) }
/* The running check stands where the new evidence will land, until --lag. */
.sa-ev-stack { display: grid }
.sa-ev-stack > * { grid-area: 1 / 1; min-width: 0 }
.sa-pend { visibility: hidden; display: flex; flex-direction: column; gap: 6px; animation: sa-hold var(--lag, 0s) step-end }
.sa-pend .sa-label { color: var(--sa-violet) }
.sa-pend .sa-check { flex: 1; border-left-color: var(--sa-violet); animation: none }
.sa-exit.run { color: #d6c4ff; display: inline-flex; align-items: center; gap: 6px }
.sa-exit.run::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background-color: var(--sa-violet); box-shadow: 0 0 8px var(--sa-violet) }
.sa-pend-out { display: block; padding: 2px 10px 8px; font: 11px/1.5 var(--sa-mono); color: var(--sa-text-2) }
.sa-pend-out::after { content: ""; display: inline-block; width: 7px; height: 12px; margin-left: 6px; vertical-align: -2px; background-color: var(--sa-violet); animation: sa-pulse .8s steps(2, jump-none) 4 }
.sa-pend-bar { display: block; height: 2px; margin: 0 10px 8px; background: linear-gradient(90deg, transparent, var(--sa-violet), transparent) no-repeat; background-size: 40% 100%; animation: sa-scanbar .9s linear 3 }
@keyframes sa-scanbar { from { background-position: -60% 0 } to { background-position: 160% 0 } }
.sa-side-foot { display: flex; flex-wrap: wrap; gap: 8px; align-items: center }
.sa-history { display: flex; flex-direction: column; gap: 8px; animation: sa-window .3s ease-out both }
.sa-earlier { align-self: flex-start; margin-top: 6px; font-size: 12px }
/* The tile's own colour turns with the verdict, not before it. */
.sa-tile { transition: border-color .4s var(--hit, 0s), box-shadow .4s var(--hit, 0s) }
.sa-tile::before, .sa-tile::after { transition: border-color .4s var(--hit, 0s), filter .4s var(--hit, 0s) }
.sa-tile .sa-qa dt { transition: color .3s var(--hit, 0s) }
.sa-evidence { display: flex; flex-direction: column; gap: 12px }
.sa-pins { display: flex; flex-direction: column; gap: 3px }
.sa-pin { font: 700 11.5px/1.4 var(--sa-mono); color: #ffd0da; background-color: rgb(255 98 131 / .14); border: 1px solid rgb(255 98 131 / .45); border-radius: 2px; padding: 0 5px; white-space: nowrap; margin-right: 6px }
.sa-verdict.compact { padding: 7px 10px; border: 1px solid var(--sa-line); border-radius: 2px; background-color: rgb(4 6 15 / .5) }
.sa-verdict.compact .note { font-size: 12px; color: var(--sa-text-2) }
.sa-v[data-v="BUILDER"], .sa-v[data-v="START"] { color: var(--sa-sys); background-color: rgb(102 207 255 / .08); border-color: rgb(102 207 255 / .4) }

/* The strongest beat: the check is green, and the reviewer's FAIL is stamped over it. */
.sa-verdict.greenfail { position: relative; grid-template-columns: auto minmax(0, 1fr); padding: 12px; border: 1px solid rgb(255 98 131 / .5); border-radius: 3px; background-image: linear-gradient(180deg, rgb(255 98 131 / .09), transparent 70%); box-shadow: 0 0 30px -16px var(--sa-fail) }
.sa-gf-head { grid-column: 1 / -1; font: 800 17px/1.25 var(--sa-sans); letter-spacing: -.005em }
.sa-gf-head span { display: inline-block; animation: sa-window .5s var(--lag, 0s) ease-out both }
.sa-gf-head .ok { color: var(--sa-pass); text-shadow: 0 0 14px rgb(69 232 178 / .45) }
.sa-gf-head .bad { color: var(--sa-fail); text-shadow: 0 0 14px rgb(255 98 131 / .5); animation-delay: var(--hit, 0s) }
.sa-gf-stack { grid-column: 1 / -1; position: relative; padding-bottom: 16px; min-width: 0 }
.sa-gf-stack .sa-check[data-ok="true"] .sa-exit { font-size: 13px; text-shadow: 0 0 10px rgb(69 232 178 / .7) }
.sa-gf-stamp { position: absolute; right: 10px; bottom: 0; display: inline-flex; align-items: baseline; gap: 8px; padding: 6px 10px; background-color: #14070d; border: 2px solid var(--sa-fail); border-radius: 2px; box-shadow: 0 0 22px -4px var(--sa-fail), inset 0 0 14px -8px var(--sa-fail); animation: sa-slam .7s var(--hit, 0s) cubic-bezier(.2, .9, .3, 1.25) both }
.sa-gf-stamp b { font: 900 15px/1 var(--sa-sans); letter-spacing: .1em; color: var(--sa-fail); text-shadow: 0 0 10px rgb(255 98 131 / .7) }
.sa-gf-stamp code { font: 700 12px/1 var(--sa-mono); color: #ffd0da }

/* Not gated cards fold into one quiet line on an armed board. */
.sa-fold { display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: baseline; padding: 4px 2px; color: var(--sa-muted); font-size: 12.5px; border-top: 1px dashed var(--sa-line-2); padding-top: 10px }
.sa-roster { display: flex; flex-direction: column; gap: 4px; min-width: 0; border-top: 1px solid var(--sa-line); padding-top: 10px }
.sa-roster > .sa-label { color: var(--sa-text-2) }
.sa-tile.hero { grid-template-columns: minmax(0, 1fr); grid-template-areas: "mark" "body" "side" "roster" }
.sa-tile.hero > .sa-mark { grid-area: mark }
.sa-tile.hero > .sa-body { grid-area: body }
.sa-tile.hero > .sa-side { grid-area: side }
.sa-tile.hero > .sa-roster { grid-area: roster }

/* v2.4: removed assertions read as diff lines inside the evidence, next to the exit code. */
.sa-removed { border-top: 1px solid rgb(255 98 131 / .35); background-color: rgb(255 98 131 / .07); padding: 7px 10px 8px; display: flex; flex-direction: column; gap: 4px; min-width: 0 }
.sa-check > .sa-removed:last-child { border-bottom: 0 }
.sa-side > .sa-removed { border: 1px solid rgb(255 98 131 / .45); border-left-width: 2px; border-radius: 2px }
.sa-removed-k { font: 700 10.5px/1.4 var(--sa-mono); letter-spacing: .08em; text-transform: uppercase; color: #ff9db1 }
.sa-removed ul { display: flex; flex-direction: column; gap: 2px }
.sa-removed li { display: flex; flex-wrap: wrap; gap: 2px 8px; align-items: baseline; min-width: 0 }
.sa-removed .at { font: 11px/1.5 var(--sa-mono); color: var(--sa-text-2); white-space: nowrap }
.sa-removed .del { font: 600 12px/1.5 var(--sa-mono); color: #ffc2cf; background-color: rgb(255 98 131 / .16); padding: 0 6px; border-radius: 2px; min-width: 0; overflow-wrap: anywhere }
.sa-removed-more { font-size: 11.5px; color: var(--sa-text-2) }
.sa-removed.accepted { border-top-color: var(--sa-line); background-color: transparent }
.sa-removed.accepted .sa-removed-k { color: var(--sa-text-2); text-transform: none; letter-spacing: 0; font: 12px/1.4 var(--sa-sans) }
.sa-removed.accepted .del { color: var(--sa-text-2); background-color: rgb(122 162 255 / .08); font-weight: 400 }
/* Changed assertion lines are not a finding: one neutral line, folded until asked for, never red. */
.sa-changed { border-top: 1px solid var(--sa-line); padding: 5px 10px 6px; display: flex; flex-direction: column; gap: 4px; min-width: 0 }
.sa-side > .sa-changed { border: 1px solid var(--sa-line); border-radius: 2px }
.sa-changed-k { display: inline-flex; align-items: baseline; gap: 8px; align-self: flex-start; background: none; border: 0; padding: 0; font: 12px/1.4 var(--sa-sans); color: var(--sa-text-2); cursor: pointer; text-align: left }
.sa-changed-k:disabled { cursor: default }
.sa-changed-t { font: 11px/1.4 var(--sa-mono); color: var(--sa-sys); text-decoration: underline; text-decoration-color: rgb(102 207 255 / .45); text-underline-offset: 3px }
.sa-changed ul { display: flex; flex-direction: column; gap: 2px }
.sa-changed li { display: flex; flex-wrap: wrap; gap: 2px 8px; align-items: baseline; min-width: 0 }
.sa-changed .at { font: 11px/1.5 var(--sa-mono); color: var(--sa-muted); white-space: nowrap }
.sa-changed .was { font: 12px/1.5 var(--sa-mono); color: var(--sa-text-2); background-color: rgb(122 162 255 / .08); padding: 0 6px; border-radius: 2px; min-width: 0; overflow-wrap: anywhere }
/* v2.5: a multi-line assertion is one block, every line marked as removed, wrapping inside the pane. */
.sa-removed li.multi > .del, .sa-skipped li.multi > .add { flex: 1 1 100%; display: flex; flex-direction: column; padding: 2px 6px }
.sa-diff-ln { display: block; white-space: pre-wrap; overflow-wrap: anywhere; min-width: 0; padding-left: 1.4ch; text-indent: -1.4ch }
/* v2.5: a test switched off is an added line (a skip marker, or a test renamed so it no longer runs), not a deletion. */
.sa-skipped { border-top: 1px solid rgb(255 187 82 / .35); background-color: rgb(255 187 82 / .06); padding: 7px 10px 8px; display: flex; flex-direction: column; gap: 4px; min-width: 0 }
.sa-check > .sa-skipped:last-child { border-bottom: 0 }
.sa-side > .sa-skipped { border: 1px solid rgb(255 187 82 / .45); border-left-width: 2px; border-radius: 2px }
.sa-skipped-k { font: 700 10.5px/1.4 var(--sa-mono); letter-spacing: .08em; text-transform: uppercase; color: #ffd08a }
.sa-skipped ul { display: flex; flex-direction: column; gap: 2px }
.sa-skipped li { display: flex; flex-wrap: wrap; gap: 2px 8px; align-items: baseline; min-width: 0 }
.sa-skipped .at { font: 11px/1.5 var(--sa-mono); color: var(--sa-text-2); white-space: nowrap }
.sa-skipped .add { font: 600 12px/1.5 var(--sa-mono); color: #ffe3b8; background-color: rgb(255 187 82 / .14); padding: 0 6px; border-radius: 2px; min-width: 0; overflow-wrap: anywhere }
.sa-skipped.accepted { border-top-color: var(--sa-line); background-color: transparent }
.sa-skipped.accepted .sa-skipped-k { color: var(--sa-text-2); text-transform: none; letter-spacing: 0; font: 12px/1.4 var(--sa-sans) }
.sa-skipped.accepted .add { color: var(--sa-text-2); background-color: rgb(122 162 255 / .08); font-weight: 400 }
/* v2.5: how many tests the check reported, read beside its exit code. A drop is amber; a plain count is grey. */
.sa-ran { margin-left: auto; padding-left: 12px; font: 11px/1.4 var(--sa-mono); color: var(--sa-text-2); white-space: nowrap }
.sa-exit + .sa-ran { margin-left: 0; border-left: 1px solid var(--sa-line) }
.sa-check-cmd > .sa-ran { flex: 1 1 100%; margin-left: 0; padding-left: 0; border-left: 0 }
.sa-ran[data-drop="true"] { color: var(--sa-warn); font-weight: 700 }
.sa-check > .sa-ranline { display: block; padding: 6px 10px; border-top: 1px solid var(--sa-line) }
.sa-side > .sa-ranline, .sa-body > .sa-ranline { display: block; padding: 6px 10px; border: 1px solid rgb(255 187 82 / .45); border-left-width: 2px; border-radius: 2px; white-space: normal }
.sa-older.cut { color: #ff9db1; border-color: rgb(255 98 131 / .5) }
.sa-older.off { color: #ffd08a; border-color: rgb(255 187 82 / .5) }
.sa-cost { font: 11px/1.6 var(--sa-mono); color: var(--sa-text-2) }
.sa-side-head { display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: center; justify-content: space-between }
.sa-copy { display: inline-flex; align-items: center; gap: 8px }
.sa-copy-btn { padding: 3px 10px; font-size: 12px }
.sa-copied { font-size: 12px; color: var(--sa-fail) }
.sa-copied[data-ok="true"] { color: var(--sa-pass) }
.sa-kind { font: 700 10px/1.2 var(--sa-mono); letter-spacing: .12em; text-transform: uppercase; color: var(--sa-violet); border: 1px solid rgb(176 140 255 / .55); border-radius: 2px; padding: 2px 6px }
/* The trial says, in the backend's words, what is real and what is simulated. */
.sa-disclose { order: 5; flex: 1 1 100%; display: flex; flex-wrap: wrap; gap: 4px 8px; min-width: 0 }
.sa-disclose .sa-synth { white-space: normal; min-width: 0 }
.sa-synth b { font-weight: 800; letter-spacing: .1em; text-transform: uppercase; color: var(--sa-warn) }
.sa-synth.real { border-style: solid; border-color: rgb(69 232 178 / .4) }
.sa-synth.real b { color: var(--sa-pass) }
/* The guided setup card. */
.sa-stages { display: flex; flex-wrap: wrap; gap: 6px 18px; align-items: center; font: 700 11px/1.4 var(--sa-mono); letter-spacing: .1em; text-transform: uppercase; color: var(--sa-text-2) }
.sa-stages li { display: inline-flex; align-items: center; gap: 7px }
.sa-stages li[data-ok="true"] { color: var(--sa-pass) }
.sa-stages .sa-box { margin-top: 0 }
.sa-setup-line .sa-stages { flex: 1 1 auto }
/* The hook observation: a plain sentence under the stages, never a mark or a warning. */
.sa-hook-seen { margin: 0; font-size: 12.5px; line-height: 1.5; color: var(--sa-text-2); min-width: 0; overflow-wrap: anywhere }
.sa-checklist.sa-guide { gap: 0 }
.sa-guide li { padding: 9px 0; border-top: 1px solid var(--sa-line) }
.sa-guide li:first-child { border-top: 0 }
.sa-row-main { display: flex; flex-direction: column; gap: 5px; min-width: 0 }
.sa-row-top { display: flex; flex-wrap: wrap; gap: 3px 10px; align-items: baseline; min-width: 0 }
.sa-row-top .sa-item { display: inline }
.sa-row-top:has(.sa-pair) { align-items: center }
.sa-value { font: 11.5px/1.5 var(--sa-mono); color: var(--sa-text); background-color: var(--sa-inset); border: 1px solid var(--sa-line); border-radius: 2px; padding: 0 6px; min-width: 0; overflow-wrap: anywhere }
li[data-ok="true"] > .sa-row-main .sa-value { border-color: rgb(69 232 178 / .35) }
.sa-pair { display: inline-flex; flex-wrap: wrap; align-items: center; gap: 4px 10px }
.sa-side-fam { display: inline-flex; align-items: center; gap: 6px }
.sa-side-fam .role { font: 700 9.5px/1 var(--sa-mono); letter-spacing: .14em; text-transform: uppercase; color: var(--sa-text-2) }
.sa-psig.mini { width: 16px; height: 22px; filter: drop-shadow(0 0 3px currentColor) }
.sa-psig.mini.v { color: var(--sa-violet); transform: scaleX(-1) }
.sa-vs { font: 700 14px/1 var(--sa-mono); color: var(--sa-pass) }
.sa-arm, .sa-cmd { display: flex; flex-wrap: wrap; gap: 6px 8px; align-items: center; min-width: 0 }
.sa-pick select { font: 12.5px/1.4 var(--sa-sans); color: var(--sa-text); background-color: var(--sa-inset); border: 1px solid var(--sa-line-2); border-radius: 4px; padding: 5px 8px; max-width: 100%; min-width: 0 }
.sa-pick select:hover { border-color: var(--sa-sys) }
.sa-pick { min-width: 0; max-width: 100% }
.sa-said { font-size: 12px; color: var(--sa-text-2) }
.sa-row-main > :is(.sa-btn, .sa-arm, .sa-cmd) { align-self: flex-start }
.sa-btn.arm { border-color: var(--sa-sys); color: #bfeaff; box-shadow: 0 0 14px -6px var(--sa-sys) }
.sa-cmd code { font: 11.5px/1.5 var(--sa-mono); color: var(--sa-sys); background-color: var(--sa-inset); border: 1px solid var(--sa-line); border-radius: 2px; padding: 3px 8px; min-width: 0; overflow-wrap: anywhere }
.sa-selftest { display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: baseline; padding-top: 10px; border-top: 1px solid var(--sa-line); font-size: 12.5px; color: var(--sa-text-2) }
.sa-selftest-said { flex: 1 1 240px; min-width: 0 }
.sa-selftest[data-ok="true"] b { color: var(--sa-pass) }
.sa-selftest[data-ok="false"] b { color: var(--sa-fail) }
/* Not armed is not a failed test: the line says so in the setup colour, never as proof. */
.sa-selftest[data-armed="false"] b { color: var(--sa-warn) }
.sa-first { white-space: nowrap }
.sa-hero-setup .sa-guide { display: grid; grid-template-columns: minmax(0, 1fr); column-gap: 28px }

@container sa (min-width: 720px) {
  .sa-qa .rm { display: none }
  .sa-wrap { padding: 22px 26px 32px; gap: 18px }
  .sa-trial { margin: 0 -26px; padding: 12px 26px 10px }
  .sa-trial-top span.why { flex: 1 1 240px }
  .sa-meta { align-items: center }
  .sa-meta > span:not(.sa-live) { flex: 0 1 auto; min-width: 0 }
  .sa-tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px }
  .sa-tile { grid-template-columns: 128px minmax(0, 1fr); gap: 18px; padding: 18px }
  .sa-tile.hero { grid-column: 1 / -1 }
  .sa-seal { gap: 4px }
  .sa-seal .k { font-size: 9.5px }
  .sa-seal .w { font-size: 16px }
  .sa-seal:is([data-gate="UNVERIFIED"], [data-gate="UNGATED"], [data-gate="UNARMED"]) .w:not(.was), .sa-seal .was:is([data-gate="UNVERIFIED"], [data-gate="UNGATED"], [data-gate="UNARMED"]) { font-size: 12px }
  .sa-seal .h { font-size: 10px }
  .sa-title { font-size: 16px }
  .sa-ev { grid-template-columns: 64px minmax(0, 1fr) }
  .sa-stage { grid-template-columns: auto minmax(0, 1fr); justify-items: start; align-items: center; text-align: left; column-gap: 48px; padding: 14px 0 0 }
  .sa-hero .sa-portal { width: clamp(250px, 31cqi, 370px); justify-self: center }
  .sa-pitch { align-items: flex-start }
  .sa-cta { flex-direction: row; align-items: center; gap: 16px }
  .sa-preview-wrap { align-items: flex-start }
  .sa-preview { justify-content: start }
  .sa-hero-setup .sa-guide { grid-template-columns: repeat(2, minmax(0, 1fr)) }
  .sa-hero-setup .sa-guide li:nth-child(2) { border-top: 0 }
  .sa-tile.hero .sa-duel { max-width: 460px; grid-template-columns: minmax(0, 1fr) minmax(0, 170px) minmax(0, 1fr) }
  .sa-tile.hero .sa-duel .sa-psig { width: 52px; height: 70px }
  .sa-tile.hero .sa-fighter { --fh: 70px }
  .sa-tile.hero .sa-seal .w { font-size: 16px }
  .sa-tile.hero .sa-seal[data-gate="UNVERIFIED"] .w:not(.was), .sa-tile.hero .sa-seal .was[data-gate="UNVERIFIED"] { font-size: 12.5px }
  .sa-out { max-height: 12em }
}
@container sa (min-width: 1000px) {
  .sa-tile.hero { grid-template-columns: minmax(0, 1fr) minmax(0, 440px); grid-template-rows: auto auto 1fr; grid-template-areas: "mark side" "body side" "roster side"; padding: 22px; column-gap: 26px }
  .sa-tile.hero > .sa-roster { align-self: start }
  .sa-tile.hero .sa-seal .w { font-size: 21px }
  .sa-tile.hero .sa-seal[data-gate="UNVERIFIED"] .w:not(.was), .sa-tile.hero .sa-seal .was[data-gate="UNVERIFIED"] { font-size: 15.5px }
  .sa-tile.hero .sa-seal .h { font-size: 11px }
  .sa-tile.hero .sa-title { font-size: 19px }
  .sa-tile.hero .sa-side { border-top: 0; padding-top: 0; border-left: 1px solid var(--sa-line); padding-left: 18px }
  .sa-detail { grid-template-columns: 280px minmax(0, 1fr); gap: 28px }
  .sa-index { display: flex; position: sticky; top: 0 }
  .sa-ledger-head h2 { font-size: 21px }
}
/* Reduced motion: nothing moves; every one-shot overlay stays hidden because its resting state is invisible. */
@media (prefers-reduced-motion: reduce) {
  .sa-root *, .sa-root *::before, .sa-root *::after { animation: none !important; transition: none !important }
}
`

// One shared stylesheet for every pane, page and transcript card: React 19 dedupes a <style> by href and hoists it once.
const PaneStyle = () => jsx('style', { href: 'shadow-army-pane', precedence: 'shadow-army', children: css })

const Badge = ({ gate }) => jsx('span', { className: 'sa-badge', 'data-gate': gate, children: GATE_WORD[gate] || `GATE ${gate}` })
const Verdict = ({ verdict }) => jsx('span', { className: 'sa-v', 'data-v': str(verdict).toUpperCase(), children: str(verdict).toUpperCase() })
const Family = ({ id }) => jsx('span', { className: 'sa-fam', 'data-fam': str(id).toLowerCase(), children: family(id) })
const Sim = ({ duty }) => jsx('span', { className: 'sa-sim', children: str(duty).toLowerCase() === 'verify' ? 'simulated reviewer' : 'simulated' })

// Hand-drawn art, all decorative (aria-hidden). Paths are in a 100 x 100 box unless noted.
const svg = (className, viewBox, children, key) => jsx('svg', { className, viewBox, 'aria-hidden': 'true', focusable: 'false', children }, key)
const RAYS = Array.from({ length: 12 }, (_, i) => {
  const a = (i * Math.PI) / 6 + Math.PI / 12, c = Math.cos(a), s = Math.sin(a)
  return `M${(50 + 42 * c).toFixed(1)} ${(50 + 42 * s).toFixed(1)}L${(50 + 52 * c).toFixed(1)} ${(50 + 52 * s).toFixed(1)}`
}).join('')
// A hooded shadow soldier in a 16 x 20 box; the mask fades its cloak into smoke.
const FIGURE = 'M8 1.2c2.1 0 3.6 1.7 3.6 3.8 0 1.4-.6 2.6-1.6 3.3l3.5 2.1c1 .6 1.7 1.7 1.7 2.9V20H.8v-6.7c0-1.2.7-2.3 1.7-2.9L6 8.3C5 7.6 4.4 6.4 4.4 5c0-2.1 1.5-3.8 3.6-3.8Z'
const EYES = 'M6.1 5.3h1.3v.9H6.1zM8.6 5.3h1.3v.9H8.6z'

function GateArt({ gate }) {
  const gated = !QUIET.includes(gate)
  return [
    svg('art', '0 0 100 100', [
      jsx('circle', { className: 'halo', cx: 50, cy: 50, r: 49 }, 'h'),
      jsx('circle', { className: 'ticks', cx: 50, cy: 50, r: 46 }, 't'),
      jsx('circle', { className: 'ring', cx: 50, cy: 50, r: 40.5 }, 'r'),
      gate === 'BLOCKED' && jsx('path', { className: 'bars', pathLength: 1, d: 'M24 24L76 76M76 24L24 76' }, 'b'),
      gate === 'STALE' && jsx('path', { className: 'shard', d: 'M88.06 36.15A40.5 40.5 0 0 1 88.06 63.85' }, 's'),
      gate === 'STALE' && jsx('path', { className: 'crack', pathLength: 1, d: 'M74 6L82 19L77 27L91 41L85 50L96 60M85 50L88 66L81 77L86 94' }, 'c'),
      gate === 'PASS' && jsx('path', { className: 'rays', pathLength: 1, d: RAYS }, 'p')
    ], 'art'),
    gated && svg('runes', '0 0 100 100', jsx('circle', { cx: 50, cy: 50, r: 43.3 }), 'runes')
  ]
}

// flip: the gate changed moments ago and its art plays in. was: the gate word that stands until the verdict lands.
function Seal({ card, flip, was, stamp }) {
  const gate = gateOf(card)
  const lines = sealLines(card).filter(Boolean)
  const held = flip && was && was !== gate
  return jsxs('div', {
    className: `sa-seal${flip ? ' flip' : ''}`, 'data-gate': gate, 'data-closed': String(isDone(card)), role: 'img',
    'aria-label': `Gate ${gateWord(gate)}${isDone(card) ? ', card closed' : ''}. ${whyOf(card)}`,
    children: [
      ...GateArt({ gate }),
      jsxs('span', { className: 'plate', 'aria-hidden': 'true', children: [
        jsx('span', { className: 'k', children: 'gate' }),
        jsxs('span', { className: 'words', children: [
          held && jsx('span', { className: 'w was', 'data-gate': was, children: gateWord(was) }, 'was'),
          jsx('span', { className: 'w', children: gateWord(gate) }, 'now')
        ] })
      ] }),
      lines.length > 0 && jsx('span', { className: 'h', 'aria-hidden': 'true', children: lines.map((line, i) => jsx('span', { children: line }, i)) }),
      stamp && jsx('span', { className: 'sa-stamp', 'aria-hidden': 'true', children: svg('', '0 0 100 100', [
        jsx('circle', { cx: 50, cy: 50, r: 44, strokeWidth: 4 }, 'o'),
        jsx('circle', { cx: 50, cy: 50, r: 37, strokeWidth: 1.5, strokeDasharray: '3 3' }, 'i'),
        jsx('path', { d: 'M30 30L70 70M70 30L30 70', strokeWidth: 6, strokeLinecap: 'square' }, 'x')
      ]) }, stamp)
    ]
  }, `${cardKey(card)}:${gate}`)
}

const Sigil = ({ refused }) => svg('sa-sigil', '0 0 16 20', [
  jsx('path', { d: FIGURE }, 'f'),
  jsx('path', { className: 'eyes', d: EYES }, 'e'),
  refused && jsx('path', { className: 'slash', pathLength: 1, d: 'M-1 18L17 2' }, 's')
])

// A cowled shadow in profile, facing right, in a 24 x 32 box. FOLDS are two cloak creases.
const PROFILE = 'M8 .6C11.8 1.6 17.6 3.6 19.6 7.4L19.4 9.6C19.5 11.4 19.2 12.6 18.6 13.8L16.8 15.2C18.7 15.9 20.6 17.4 21.5 19.9C22.6 23 23 27 23.2 32H.8C1 27 1.6 22.6 2.8 19.4C3.4 17.6 3.8 16.2 4 14.6C3.8 11 4.2 7 5.4 4.4C6.1 2.8 7 1.5 8 .6Z'
const FACE = 'M19.5 8.2C16.8 7.9 14.4 9.6 14.4 12.2C14.4 13.9 15.4 15.1 16.9 15.2L18.6 13.8C19.2 12.6 19.5 11.4 19.4 9.6Z'
const EYE = 'M16.2 11.1L18.8 10.6V11.6L16.2 12.1Z'
const FOLDS = 'M6.2 18.5C7.2 22.5 7.4 27 7.2 32M17.4 19.2C18.6 22.4 19.2 27 19.4 32'
// Abstract chest glyphs, one per family: plain geometry, never a company mark.
const GLYPHS = {
  ring: 'M12 20.1a2.9 2.9 0 1 1 0 5.8a2.9 2.9 0 1 1 0-5.8Z',
  diamond: 'M12 19.6L15.4 23L12 26.4L8.6 23Z',
  triangle: 'M12 19.8L15.2 25.4H8.8Z',
  square: 'M9.4 20.4h5.2v5.2H9.4Z',
  hexagon: 'M12 19.9l2.8 1.6v3.1L12 26.1l-2.8-1.6v-3.1Z'
}
const FAMILY_GLYPH = { openai: 'ring', anthropic: 'diamond', google: 'triangle', meta: 'square', xai: 'hexagon' }
const glyphOf = id => {
  const key = str(id).toLowerCase()
  if (!key) return null
  if (FAMILY_GLYPH[key]) return GLYPHS[FAMILY_GLYPH[key]]
  const names = Object.keys(GLYPHS)
  return GLYPHS[names[[...key].reduce((n, c) => n + c.charCodeAt(0), 0) % names.length]]
}

function ProfileSigil({ family: fam, refused, className = 'sa-psig' }) {
  const glyph = glyphOf(fam)
  return svg(className, '0 0 24 32', [
    jsx('path', { className: 'body', d: PROFILE }, 'b'),
    jsx('path', { className: 'folds', d: FOLDS }, 'c'),
    jsx('path', { className: 'face', d: FACE }, 'f'),
    jsx('path', { className: 'eye', d: EYE }, 'e'),
    glyph && jsx('path', { className: 'glyph', d: glyph }, 'g'),
    refused && jsx('path', { className: 'slash', pathLength: 1, d: 'M-1 31L25 3' }, 's')
  ])
}

// Who built the card and who judged it. A same-family request that was refused stands in the verifier's place, struck.
function duelOf(card) {
  const gate = card.gate || {}
  const rows = historyOf(card)
  const built = rows.findLast(row => ['build', 'repair'].includes(str(row.duty).toLowerCase()) && row.family)
  const builder = gate.builder?.family ? gate.builder : built ? { family: built.family, model: built.model } : null
  const refusedAt = rows.findLastIndex(row => row.kind === 'refused_same_family')
  const judgedAt = rows.findLastIndex(row => row.kind === 'verdict' || (row.kind === 'arose' && str(row.duty).toLowerCase() === 'verify'))
  if (refusedAt >= 0 && refusedAt > judgedAt) return { builder, verifier: { family: rows[refusedAt].family, model: rows[refusedAt].model, refused: true } }
  const judged = rows[judgedAt]
  if (gate.verifier?.family) return { builder, verifier: gate.verifier }
  if (judged?.family) return { builder, verifier: { family: judged.family, model: judged.model } }
  // Nothing in the ledger yet: a verifier that is queued or running still takes its place, dimmed.
  const called = list(card.shadows).findLast(s => str(s.duty).toLowerCase() === 'verify' && s.family)
  const state = str(called?.state).toLowerCase()
  return { builder, verifier: called ? { family: called.family, model: called.model, refused: state === 'refused', waiting: state === 'pending' ? 'queued' : state === 'running' ? 'running' : '' } : null }
}

function Fighter({ who, side, summon, risen = '' }) {
  const fam = str(who?.family)
  const refused = Boolean(who?.refused)
  return jsxs('div', {
    className: `sa-fighter${summon ? ' summon' : ''}`, 'data-side': side, 'data-state': refused ? 'refused' : !fam ? 'absent' : who.waiting ? 'waiting' : 'present',
    children: [
      summon && side === 'verify' && jsx('span', { className: 'sa-floor', 'aria-hidden': 'true' }, summon),
      jsx(ProfileSigil, { family: fam, refused }, `${fam}|${refused}|${risen}`),
      jsx('span', { className: 'role', children: side === 'build' ? 'Builder' : 'Verifier' }),
      fam ? jsx(Family, { id: fam }) : jsx('span', { className: 'none', children: 'none yet' }),
      who?.waiting && jsx('span', { className: 'none', children: who.waiting }),
      refused && jsx('span', { className: 'sa-state refused', children: 'refused' })
    ]
  })
}

function Duel({ card, flip, was, stamp, summon }) {
  const { builder, verifier } = duelOf(card)
  // The verifier re-mounts (and rises) when a summon starts, never when it ends.
  const risen = useRef('')
  if (summon) risen.current = summon
  const said = verifier
    ? verifier.refused ? `A verifier from ${family(verifier.family)}, the builder's family, was refused.` : `Verified by ${family(verifier.family)}.`
    : 'No verifier yet.'
  return jsxs('div', { className: 'sa-duel', role: 'group', 'aria-label': `${builder ? `Built by ${family(builder.family)}. ` : ''}${said}`, children: [
    jsx(Fighter, { who: builder, side: 'build' }),
    jsx(Seal, { card, flip, was, stamp }),
    jsx(Fighter, { who: verifier, side: 'verify', summon, risen: risen.current })
  ] })
}

function Emblem() {
  return svg('sa-emblem', '0 0 32 32', [
    jsx('circle', { cx: 16, cy: 16, r: 14.5, fill: 'none', stroke: 'currentColor', strokeWidth: 1.4 }, 'o'),
    jsx('circle', { cx: 16, cy: 16, r: 11, fill: 'none', stroke: 'currentColor', strokeWidth: .8, strokeDasharray: '2 2.2', opacity: .7 }, 'i'),
    jsx('path', { className: 'fig', d: 'M16 6.5c1.8 0 3.1 1.4 3.1 3.2 0 1.1-.5 2.1-1.3 2.7l3 1.9c.8.5 1.3 1.4 1.3 2.4v6.1H9.9v-6.1c0-1 .5-1.9 1.3-2.4l3-1.9c-.8-.6-1.3-1.6-1.3-2.7 0-1.8 1.3-3.2 3.1-3.2Z' }, 'f')
  ])
}

function Portal() {
  return jsxs('div', { className: 'sa-portal', 'aria-hidden': 'true', children: [
    jsx('span', { className: 'swirl' }),
    svg('r1', '0 0 100 100', [
      jsx('circle', { cx: 50, cy: 50, r: 48, fill: 'none', stroke: 'currentColor', strokeWidth: .6, opacity: .5 }, 'a'),
      jsx('circle', { cx: 50, cy: 50, r: 45, fill: 'none', stroke: 'currentColor', strokeWidth: 2.4, strokeDasharray: '.6 4.1', opacity: .8 }, 'b')
    ]),
    svg('r2', '0 0 100 100', jsx('circle', { cx: 50, cy: 50, r: 38, fill: 'none', stroke: 'currentColor', strokeWidth: 1.2, strokeDasharray: '9 4 2 4' })),
    jsx('span', { className: 'void' }),
    jsxs('span', { className: 'duo', children: [
      jsx(ProfileSigil, { className: 'sa-psig b' }), jsx(ProfileSigil, { className: 'sa-psig v' })
    ] })
  ] })
}

function ShadowRow({ shadow, finding = true, index = 0 }) {
  const state = str(shadow.state).toLowerCase()
  const status = shadow.verdict
    ? jsx(Verdict, { verdict: shadow.verdict })
    : state === 'running'
      ? jsxs('span', { className: 'sa-state run', children: [jsx('i', { 'aria-hidden': 'true' }), shadow.elapsed_s != null ? `running ${duration(shadow.elapsed_s)}` : 'running'] })
      : state === 'refused'
        ? jsx('span', { className: 'sa-state refused', children: 'refused' })
        : jsx('span', { className: 'sa-state', children: state === 'pending' ? 'queued' : human(state) || 'idle' })
  return jsxs('div', {
    className: 'sa-shadow', 'data-shadow-id': shadow.id, 'data-state': state, 'data-duty': str(shadow.duty).toLowerCase(),
    style: { '--i': Math.min(index, 6) },
    children: [
      jsx(Sigil, { refused: state === 'refused' }),
      jsx('span', { className: 'sa-duty', children: str(shadow.duty).toUpperCase() || 'SHADOW' }),
      jsxs('span', { className: 'sa-who', children: [jsx(Family, { id: shadow.family }), jsx('span', { className: 'sa-model', children: str(shadow.model) }), shadow.simulated && jsx(Sim, { duty: shadow.duty })] }),
      shadow.verdict ? jsx('span', { className: 'sa-state', children: status }) : status,
      finding && shadow.finding && jsx('span', { className: 'sa-finding', 'data-bad': String(state === 'refused' || str(shadow.verdict).toUpperCase() === 'FAIL'), children: str(shadow.finding) })
    ]
  })
}

// v2.4: assertion lines the plugin's own inventory no longer finds. Advisory: they never change a gate state.
// Each is "removed" (the number of assertions dropped) or "changed" (its text changed, the count did not drop). An
// older backend marks neither, and every line it sends was counted as removed.
const lostOf = row => list(row?.weakened).filter(w => w && typeof w === 'object' && str(w.file).trim() && str(w.text).trim())
const removedOf = row => lostOf(row).filter(w => w.kind !== 'changed')
const changedOf = row => lostOf(row).filter(w => w.kind === 'changed')
const countOf = (value, rows) => {
  const n = Number(value)
  return value != null && Number.isFinite(n) && n >= rows.length ? Math.floor(n) : rows.length
}
const removedCount = row => countOf(row?.weakened_count, removedOf(row))
const changedCount = row => countOf(row?.changed_count, changedOf(row))
const sinceOf = rows => rows.some(w => w.since === 'start') ? 'since the card started' : 'since the last review'

// v2.5: tests switched off since the card started or the last review (a skip marker added, or a test renamed so the
// runner no longer collects it). The rows reuse the evidence projection and still carry kind "changed" for
// compatibility; that field means nothing here and is ignored.
const skippedOf = row => list(row?.skipped).filter(w => w && typeof w === 'object' && str(w.file).trim() && str(w.text).trim())
const skippedCount = row => countOf(row?.skipped_count, skippedOf(row))
// v2.5: fewer tests ran than before ({before, now}), and the count the check itself reported.
const dropOf = row => {
  const drop = row?.tests_ran
  if (!drop || typeof drop !== 'object') return null
  const before = Number(drop.before), now = Number(drop.now)
  return Number.isInteger(before) && Number.isInteger(now) && now >= 0 && now < before ? { before, now } : null
}
const ranOf = check => Number.isInteger(check?.tests_ran) && check.tests_ran >= 0 ? check.tests_ran : null

// One assertion or test line as a diff line. A statement over several lines is one block, each line marked.
function DiffLine({ w, sign, said, className }) {
  const lines = str(w.text).split('\n').map(line => line.trimEnd()).filter(line => line.trim())
  const multi = lines.length > 1
  return jsxs('li', { className: multi ? 'multi' : undefined, children: [
    jsx('code', { className: 'at', children: pinText(w) }),
    jsxs('code', { className, children: [
      jsx('span', { className: 'sa-sr', children: `${said}: ` }),
      ...lines.map((line, i) => multi
        ? jsxs('span', { className: 'sa-diff-ln', children: [jsx('span', { 'aria-hidden': 'true', children: `${sign} ` }), line] }, i)
        : jsxs('span', { children: [jsx('span', { 'aria-hidden': 'true', children: `${sign} ` }), line] }, i))
    ] })
  ] })
}

// Removed lines read as a diff: file and line, then the text with a leading minus. A PASS that accepted them says so
// quietly, in grey. Changed lines are not a finding: one neutral line, folded until asked for. Tests switched off
// follow, in the same family.
const scanUnavailable = row => row?.weakened_count === null || row?.skipped_count === null
function Removed({ row }) {
  const removed = removedOf(row), changed = changedOf(row)
  const count = removedCount(row), edits = changedCount(row)
  const skipped = [scanUnavailable(row) && jsx('p', { className: 'sa-hint sa-advisory', children: 'Test scan unavailable: review the test diff by hand' }, 'scan-unavailable'), jsx(Skipped, { row }, 'skipped')]
  if (!count && !edits) return skipped
  const accepted = row?.accepted_removed === true && count > 0
  const since = sinceOf(removed)
  const label = accepted
    ? `PASS with ${plural(count, 'assertion')} removed ${since}; the reviewer accepted ${count === 1 ? 'it' : 'them'}`
    : `${plural(count, 'assertion')} removed ${since}`
  return [
    count > 0 && jsxs('div', { className: `sa-removed${accepted ? ' accepted' : ''}`, role: 'group', 'aria-label': label, children: [
      jsx('span', { className: 'sa-removed-k', children: label }),
      removed.length > 0 && jsx('ul', { children: removed.map((w, i) => jsx(DiffLine, { w, sign: '-', said: 'removed', className: 'del' }, i)) }),
      count > removed.length && jsx('span', { className: 'sa-removed-more', children: `and ${count - removed.length} more` })
    ] }, 'removed'),
    edits > 0 && jsx(Changed, { rows: changed, count: edits }, 'changed'),
    skipped
  ]
}

// A test switched off is an added line that disables it: "+", never the red minus of a deletion.
function Skipped({ row }) {
  const rows = skippedOf(row), count = skippedCount(row)
  if (!count) return null
  const accepted = row?.accepted_skipped === true
  const since = sinceOf(rows.length ? rows : [{ since: 'start' }])
  const label = accepted
    ? `PASS with ${plural(count, 'test')} switched off ${since}; the reviewer accepted ${count === 1 ? 'it' : 'them'}`
    : `${plural(count, 'test')} switched off ${since}`
  return jsxs('div', { className: `sa-skipped${accepted ? ' accepted' : ''}`, role: 'group', 'aria-label': label, children: [
    jsx('span', { className: 'sa-skipped-k', children: label }),
    rows.length > 0 && jsx('ul', { children: rows.map((w, i) => jsx(DiffLine, { w, sign: '+', said: 'added', className: 'add' }, i)) }),
    count > rows.length && jsx('span', { className: 'sa-removed-more', children: `and ${count - rows.length} more` })
  ] })
}

// "Ran 2 tests, 3 before" when fewer tests ran; otherwise the count the check reported, and nothing else.
function Ran({ check, drop, line }) {
  const n = drop ? drop.now : ranOf(check)
  if (n == null) return null
  return jsxs('span', { className: `sa-ran${line ? ' sa-ranline' : ''}`, 'data-drop': String(Boolean(drop)), children: [
    jsx('span', { className: 'sa-sr', children: ', ' }),
    `Ran ${plural(n, 'test')}${drop ? `, ${drop.before} before` : ''}`
  ] })
}

function Changed({ rows, count }) {
  const [open, setOpen] = useState(false)
  const label = `${plural(count, 'assertion line')} changed ${sinceOf(rows)}`
  return jsxs('div', { className: 'sa-changed', role: 'group', 'aria-label': label, children: [
    jsxs('button', { type: 'button', className: 'sa-changed-k', 'aria-expanded': String(open), disabled: !rows.length, onClick: () => setOpen(!open), children: [
      jsx('span', { children: label }), rows.length > 0 && jsx('span', { className: 'sa-changed-t', 'aria-hidden': 'true', children: open ? 'hide' : 'show' })
    ] }),
    open && jsx('ul', { children: rows.map((w, i) => jsxs('li', { children: [
      jsx('code', { className: 'at', children: pinText(w) }),
      jsxs('code', { className: 'was', children: [jsx('span', { className: 'sa-sr', children: 'was: ' }), str(w.text)] })
    ] }, i)) }),
    open && count > rows.length && jsx('span', { className: 'sa-removed-more', children: `and ${count - rows.length} more` })
  ] })
}
const hasLost = row => removedCount(row) > 0 || changedCount(row) > 0
// Any advisory evidence on a row: assertions removed or changed, tests switched off, or fewer tests run.
const hasAdvice = row => scanUnavailable(row) || hasLost(row) || skippedCount(row) > 0 || Boolean(dropOf(row))

function Check({ check, removed, drop }) {
  if (!check || !check.command) return [removed || null, drop && jsx(Ran, { drop, line: true }, 'ran')]
  const code = check.exit_code
  const ok = code === 0
  // Rules, unittest's progress dots (E., F.) and Python's caret marks under a traceback line are noise in a short window.
  const lines = str(check.output_excerpt).split('\n')
    .filter(line => line.trim() && !/^\s*[=\-_~]{8,}\s*$/.test(line) && !/^\s*[~^]+\s*$/.test(line) && !/^[.EFsxu]{1,40}$/.test(line.trim()))
  // The first line names the failing test; the last five hold the frame that raised, the error and the summary.
  const excerpt = (lines.length > 7 ? [lines[0], '…', ...lines.slice(-5)] : lines).join('\n')
  return jsxs('div', {
    className: 'sa-check', 'data-ok': code == null ? undefined : String(ok),
    children: [
      jsxs('div', { className: 'sa-check-cmd', children: [
        jsx('code', { children: `$ ${str(check.command)}` }),
        jsx('span', { className: 'sa-exit', 'data-ok': String(ok), children: code == null ? 'exit unknown' : `exit ${code}` }),
        jsx(Ran, { check, drop })
      ] }),
      removed,
      excerpt && jsx('pre', { className: 'sa-out', tabIndex: 0, 'aria-label': 'Check output', children: excerpt })
    ]
  })
}

function isSimulated(card, row) {
  return row.simulated === true || list(card.shadows).some(s => s.simulated && s.duty === row.duty && s.family === row.family && s.model === row.model)
}

function verdictNote(row, old) {
  if (str(row.finding)) return str(row.finding)
  if (str(row.verdict).toUpperCase() !== 'PASS') return 'No finding recorded.'
  return old ? 'Passed the check on an older code version. It does not cover the code on disk now.' : 'Passed the check on this code version.'
}

// A finding with a file and line reads as a pin; the plain sentence is the fallback.
function Note({ row, old }) {
  const pins = pinsOf(row)
  if (!pins.length) return jsx('span', { className: 'note', children: row.kind === 'verdict' ? verdictNote(row, old) : str(row.finding) })
  return jsx('span', { className: 'note sa-pins', children: pins.map((pin, i) => jsxs('span', { children: [
    jsx('code', { className: 'sa-pin', children: pinText(pin) }), ' ', str(pin.text) || str(row.finding)
  ] }, i)) })
}

const ExitTag = ({ check }) => check?.command && check.exit_code != null &&
  jsx('span', { className: 'sa-exit', 'data-ok': String(check.exit_code === 0), children: `exit ${check.exit_code}` })

// One piece of evidence: a review verdict, or the builder's own check run. "compact" drops the terminal readout.
function VerdictRow({ card, row, now, compact, quiet }) {
  const builder = row.kind !== 'verdict'
  const verdict = builder ? (row.kind === 'arose' ? 'START' : 'BUILDER') : str(row.verdict).toUpperCase()
  const old = olderTree(card, row)
  const meta = jsxs('span', { className: 'meta', children: [
    row.tree && jsx('span', { className: 'sa-hash', children: `${row.kind === 'finished' ? 'started from code version ' : ''}${str(row.tree)}` }),
    jsx('span', { children: ago(now - (seconds(row.at) ?? now)) }),
    row.family && jsx(Family, { id: row.family }),
    row.model && jsx('span', { className: 'sa-model', children: str(row.model) }),
    isSimulated(card, row) && jsx(Sim, { duty: builder ? row.duty : 'verify' }),
    old && jsx(Older, {}),
    !builder && measured(row.elapsed_s) && jsx('span', { className: 'sa-cost', children: `review ${took(row.elapsed_s)}` }),
    !builder && measured(row.tokens) && jsx('span', { className: 'sa-cost', children: `${Math.round(row.tokens).toLocaleString('en-US')} tokens` }),
    compact && jsx(ExitTag, { check: row.check }),
    compact && removedCount(row) > 0 && jsx('span', { className: `sa-older${row.accepted_removed === true ? '' : ' cut'}`, children: `${removedCount(row)} removed` }),
    compact && changedCount(row) > 0 && jsx('span', { className: 'sa-older', children: `${changedCount(row)} changed` }),
    compact && skippedCount(row) > 0 && jsx('span', { className: `sa-older${row.accepted_skipped === true ? '' : ' off'}`, children: `${plural(skippedCount(row), 'test')} switched off` }),
    compact && dropOf(row) && jsx('span', { className: 'sa-older off', children: `ran ${dropOf(row).now}, ${dropOf(row).before} before` })
  ] })
  const removed = !compact && jsx(Removed, { row })
  const drop = dropOf(row)
  if (greenFail(row) && !compact) {
    const pin = pinsOf(row)[0]
    return jsxs('div', { className: 'sa-verdict greenfail', children: [
      jsxs('p', { className: 'sa-gf-head', children: [
        jsx('span', { className: 'ok', children: 'The tests pass.' }), ' ', jsx('span', { className: 'bad', children: 'The reviewer still failed it.' })
      ] }),
      jsx(Verdict, { verdict }), meta,
      jsxs('div', { className: 'sa-gf-stack', children: [
        jsx(Check, { check: row.check, removed, drop }),
        jsxs('div', { className: 'sa-gf-stamp', children: [jsx('b', { children: verdict }), pin && jsx('code', { children: pinText(pin) })] })
      ] }),
      !quiet && jsx(Note, { row, old })
    ] })
  }
  return jsxs('div', {
    className: `sa-verdict${old ? ' old' : ''}${compact ? ' compact' : ''}`,
    children: [jsx(Verdict, { verdict }), meta, !quiet && jsx(Note, { row, old }), !compact && jsx(Check, { check: row.check, removed, drop })]
  })
}

// A card's opening check run (sent on its first event) is evidence too: the trial opens on a real failing test.
const isEvidence = row => row.kind === 'verdict' || (['finished', 'arose'].includes(row.kind) && Boolean(row.check?.command))

// While a moment holds the new evidence back (--lag), its place shows the check that is running, never a blank.
function Running({ row }) {
  const command = str(row?.check?.command)
  return jsxs('div', { className: 'sa-pend', 'aria-hidden': 'true', children: [
    jsx('span', { className: 'sa-label', children: row?.kind === 'verdict' ? 'Review · running the check' : 'Builder · running the check' }),
    jsxs('div', { className: 'sa-check', children: [
      jsxs('div', { className: 'sa-check-cmd', children: [
        jsx('code', { children: command ? `$ ${command}` : '$ …' }),
        jsx('span', { className: 'sa-exit run', children: 'running' })
      ] }),
      jsx('i', { className: 'sa-pend-bar' }),
      jsx('span', { className: 'sa-pend-out', children: 'waiting for output' })
    ] })
  ] })
}

// The newest piece of evidence, with its terminal readout. The rest of the card's past waits behind History.
function Evidence({ card, now, row, moment, quiet }) {
  if (!row) return jsx('p', { className: 'sa-empty', children: 'No verdict yet.' })
  // One stable structure with or without the running placeholder, so the evidence never re-mounts (and replays its
  // entrance) when a moment ends.
  return jsx('div', { className: 'sa-evidence', children: jsx('div', { className: 'sa-ev-stack', children: [
    moment?.lag > 0 && jsx(Running, { row }, `p${moment.seq}`),
    jsx('div', { className: 'sa-ev-item', children: jsx(VerdictRow, { card, row, now, quiet }) }, rowKey(row))
  ] }) })
}

// The hero leads with its newest evidence and what happened after it: the latest refusal, the latest close event and
// the latest edit. Everything older is history, newest first.
function sideOf(card) {
  const rows = historyOf(card)
  const at = rows.findLastIndex(isEvidence)
  const after = rows.slice(at + 1)
  const pick = kinds => after.filter(row => kinds.includes(row.kind)).at(-1)
  const bars = [pick(['refused_same_family', 'refused_unbound']), pick(['closed']) || pick(CLOSING), pick(['edited'])]
    .filter(Boolean).sort((a, b) => rows.indexOf(a) - rows.indexOf(b))
  const older = rows.filter((row, i) => (i < at && isEvidence(row)) || (BAR[row.kind] && !bars.includes(row))).reverse()
  return { latest: at >= 0 ? rows[at] : null, bars, older }
}

// The current finding: the latest FAIL on the code on disk now, with its file and line when the reviewer gave them.
function findingOf(card) {
  if (gateOf(card) !== 'BLOCKED' || isDone(card)) return null
  const last = verdictRows(card).at(-1)
  return last && str(last.verdict).toUpperCase() === 'FAIL' && !olderTree(card, last) && (pinsOf(last).length || str(last.finding)) ? last : null
}

function refusalText(card, row) {
  const tree = row.tree ? ` on code version ${row.tree}` : ''
  switch (row.kind) {
    case 'refused_same_family': {
      if (str(row.finding)) return str(row.finding)
      const built = row.builder_family || card.gate?.builder?.family
      return `Verifier requested: ${family(row.family)}.${built ? ` ${family(built)} built this card.` : ''}`
    }
    case 'refused_unbound': return str(row.finding) || 'A verdict from a session the gate did not dispatch was rejected.'
    case 'close_refused': return `The gate was asked to close the card. ${str(row.finding) || 'No PASS on the current tree.'}`
    case 'close_permitted':
    case 'close_allowed':
      return `${str(row.finding) || `PASS${tree}.`}${closed(card) ? '' : ' Permission only: Kanban has not reported the card closed.'}`
    case 'closed': return passedAtClose(card, row) ? 'Card closed' : 'Card closed without review'
    case 'edited': return str(row.finding) || 'A tracked file changed after the PASS.'
    default: return str(row.finding)
  }
}

// close_allowed is the v2.0 name of close_permitted.
const BAR = {
  refused_same_family: ['REFUSED', 'dashed'],
  refused_unbound: ['REFUSED', 'dashed'],
  close_refused: ['GATE REFUSED CLOSE', ''],
  close_permitted: ['GATE PERMITTED CLOSE', 'permitted'],
  close_allowed: ['GATE PERMITTED CLOSE', 'permitted'],
  closed: ['CARD CLOSED', 'closed'],
  edited: ['EDITED', 'edited']
}
function passedAtClose(card, row) {
  if (row.gate_at_close != null) return row.gate_at_close === 'PASS'
  const rows = historyOf(card)
  const previous = rows[rows.indexOf(row) - 1]
  return ['close_permitted', 'close_allowed'].includes(previous?.kind)
}

const barOf = (card, row) => row.kind === 'closed' && !passedAtClose(card, row)
  ? ['CLOSED', 'neutral'] : BAR[row.kind]
const CLOSING = ['close_refused', 'close_permitted', 'close_allowed', 'closed']

// A stable key per history row, so a new verdict mounts (and animates) while the old one stays put.
const rowKey = row => `${seconds(row.at) ?? ''}|${str(row.kind)}|${str(row.tree)}|${str(row.verdict)}`
const olderTree = (card, row) => Boolean(row.tree && card.gate?.tree && row.tree !== card.gate.tree)
const Older = () => jsx('span', { className: 'sa-older', children: 'older version' })
const Closed = () => jsx('span', { className: 'sa-closed', children: 'CLOSED' })

function Bars({ card, rows }) {
  return rows.map(row => jsxs('div', {
    className: `sa-refusal ${barOf(card, row)[1]}`, 'data-kind': row.kind,
    children: [jsx('b', { children: barOf(card, row)[0] }), jsx('span', { children: refusalText(card, row) }), CLOSING.includes(row.kind) && olderTree(card, row) && jsx(Older, {})]
  }, rowKey(row)))
}

// v2.4: a review of a Git working tree with no Kanban card is labelled as such. Always by kind, never by board name:
// a real Kanban board may be called "workspace".
const Kind = ({ card }) => card.kind === 'workspace' && jsx('span', { className: 'sa-kind', children: 'Workspace' })

const Owner = ({ card }) => ownerOf(card) && jsxs('span', { className: 'sa-owner', children: [
  jsx('span', { className: 'sa-sr', children: 'Owner: ' }), ownerOf(card)
] })

// The System line over a tile: the labels of the events that just landed, one after another.
function Moment({ moment }) {
  const { beats, times } = moment
  return jsx('div', { className: 'sa-moment', 'aria-hidden': 'true', children: beats.map(([label, tone], i) => {
    const last = i + 1 === beats.length
    return jsx('span', {
      className: last ? 'last' : undefined, 'data-tone': tone, children: label,
      style: { '--d': `${times[i]}s`, '--t': last ? '0s' : `${(times[i + 1] - times[i]).toFixed(2)}s` }
    }, i)
  }) }, `m${moment.seq}`)
}

// The shadows that matter now: every queued or running one, and the two most recent others. The rest fold away.
function rosterOf(shadows) {
  const active = s => ['running', 'pending'].includes(str(s.state).toLowerCase())
  const recent = shadows.filter(s => !active(s)).slice(-2)
  const now = shadows.filter(s => active(s) || recent.includes(s))
  return { now, earlier: shadows.filter(s => !now.includes(s)) }
}

function Roster({ shadows, finding }) {
  const [open, setOpen] = useState(false)
  const { now, earlier } = rosterOf(shadows)
  const rows = open ? shadows : now
  return jsxs('div', { className: 'sa-shadows', children: [
    ...rows.map((s, i) => jsx(ShadowRow, { shadow: s, finding, index: i }, s.id || `${i}`)),
    earlier.length > 0 && jsx('button', {
      type: 'button', className: 'sa-link sa-earlier', 'aria-expanded': String(open), onClick: () => setOpen(!open),
      children: open ? 'Hide earlier shadows' : `Show ${plural(earlier.length, 'earlier shadow')}`
    }, 'more')
  ] })
}

// History: older evidence and older gate events, newest first, each in one compact line.
function History({ card, rows, now }) {
  return jsx('ol', { className: 'sa-history', 'aria-label': 'Earlier on this card, newest first', children: rows.map(row => jsx('li', { children: BAR[row.kind]
    ? jsxs('div', { className: `sa-refusal ${barOf(card, row)[1]}`, 'data-kind': row.kind, children: [
        jsx('b', { children: barOf(card, row)[0] }), jsx('span', { children: refusalText(card, row) }), CLOSING.includes(row.kind) && olderTree(card, row) && jsx(Older, {})
      ] })
    : jsx(VerdictRow, { card, row, now, compact: true }) }, rowKey(row))) })
}

// The receipt as copied: a measure that is zero or missing ("Review: 0s", "tokens: None") is left out of its line.
const EMPTY = /^(review|tokens)\s*:\s*(0s?|none|null|undefined)\.?$/i
const tidyReceipt = text => str(text).split('\n')
  .map(line => /^(review|tokens)\s*:/i.test(line) ? line.split(';').map(part => part.trim()).filter(part => !EMPTY.test(part)).join('; ') : line)
  .filter(Boolean).join('\n').trim()

// Copies the backend's receipt for the latest verdict through the host's clipboard door.
function CopyReceipt({ text, onCopy }) {
  const [said, setSaid] = useState('')
  const timer = useRef(null)
  useEffect(() => () => clearTimeout(timer.current), [])
  const copy = async () => {
    const ok = await onCopy(tidyReceipt(text))
    setSaid(ok ? 'Copied' : 'Copy failed')
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setSaid(''), 2400)
  }
  return jsxs('span', { className: 'sa-copy', children: [
    jsx('button', { type: 'button', className: 'sa-btn quiet sa-copy-btn', onClick: () => void copy(), children: 'Copy receipt' }),
    jsx('span', { className: 'sa-copied', role: 'status', 'data-ok': String(said === 'Copied'), children: said })
  ] })
}

function HeroSide({ card, now, moment, found, onOpen, onCopy }) {
  const [open, setOpen] = useState(false)
  const side = sideOf(card)
  const gate = card.gate || {}
  // The gate's own advisory, shown above the evidence for each kind no evidence row carries: assertions removed or
  // changed, tests switched off.
  const extra = {
    ...(scanUnavailable(gate) && !scanUnavailable(side.latest) ? { weakened_count: gate.weakened_count, skipped_count: gate.skipped_count } : {}),
    ...(hasLost(gate) && !hasLost(side.latest) ? { weakened: gate.weakened, weakened_count: gate.weakened_count, changed_count: gate.changed_count } : {}),
    ...(skippedCount(gate) > 0 && !skippedCount(side.latest) ? { skipped: gate.skipped, skipped_count: gate.skipped_count } : {})
  }
  const fresh = Object.keys(extra).length > 0
  // The gate's test-count drop sits by the latest exit code only when that run reported the same count; otherwise it
  // is its own line, so a count is never pinned to a run that did not report it.
  let latest = side.latest
  const drop = dropOf(gate) && !dropOf(latest) ? dropOf(gate) : null
  const pinned = drop && latest && ranOf(latest.check) === drop.now
  if (pinned) latest = { ...latest, tests_ran: gate.tests_ran }
  const receipt = str(card.receipt_markdown).trim()
  return jsxs('div', { className: 'sa-side', children: [
    jsxs('div', { className: 'sa-side-head', children: [
      jsx('h3', { className: 'sa-label', children: 'Evidence' }),
      // A new receipt names the verdict, so it appears when the verdict lands (--hit).
      receipt && onCopy && jsx(CopyReceipt, { text: receipt, onCopy }, receipt)
    ] }),
    fresh && jsx(Removed, { row: extra }),
    drop && !pinned && jsx(Ran, { drop, line: true }),
    jsx(Evidence, { card, now, row: latest, moment, quiet: Boolean(found) && side.latest === found }),
    jsx(Bars, { card, rows: side.bars }),
    jsxs('div', { className: 'sa-side-foot', children: [
      side.older.length > 0 && jsx('button', {
        type: 'button', className: 'sa-btn quiet', 'aria-expanded': String(open), onClick: () => setOpen(!open),
        children: `${open ? 'Hide history' : 'History'} · ${side.older.length}`
      }),
      jsx('button', { type: 'button', className: 'sa-btn quiet sa-more', onClick: () => onOpen(card), children: 'Open the full ledger' })
    ] }),
    open && jsx(History, { card, rows: side.older, now })
  ] })
}

// While a moment holds new evidence back, Why says what is running; Next waits. Without a hold both read at once.
const HELD = { review: 'Review running…', check: 'Check running…' }
const Held = ({ text, held }) => [
  held && jsx('span', { className: 'sa-held', children: held }, 'held'),
  jsx('span', { className: 'sa-now', children: text }, 'now')
]

function Tile({ card, hero, now, flip, was, moment, onOpen, onCopy }) {
  const gate = gateOf(card)
  const done = isDone(card)
  const lane = card.needs_you ? 'needs you' : done ? '' : human(card.state)
  const summon = moment?.arise ? `u${moment.seq}` : null
  const stamp = moment?.stamp ? `s${moment.seq}` : null
  const shadows = list(card.shadows)
  const found = hero ? findingOf(card) : null
  // Narrow panes only: with no finding yet, a removed assertion is named under the title, so the story's key fact is
  // on the first screen. Wide layouts show it beside, in the evidence.
  const cut = hero && !found && !done && card.gate?.state !== 'PASS' && removedCount(card.gate) > 0 ? removedOf(card.gate)[0] : null
  const held = moment?.hit > 0 ? HELD[moment.pending] || '' : ''
  // Each moment remounts the Why, Found and Next lines, so their hold restarts with it and never overlaps old text.
  const beat = moment ? `${moment.seq}|` : ''
  const roster = shadows.length > 0 && jsx(Roster, { shadows, finding: !hero })
  return jsxs('article', {
    className: `sa-tile${hero ? ' hero' : ''}${moment ? ` sa-m-${moment.tone} sa-p${moment.seq % 2}${held ? '' : ' sa-at-once'}` : ''}`,
    'data-card-id': card.id, 'data-gate': gate, 'data-closed': String(done),
    style: moment ? { '--lag': `${moment.lag}s`, '--hit': `${moment.hit}s` } : undefined,
    children: [
      moment && jsx('span', { className: 'sa-flash', 'data-tone': moment.tone, 'aria-hidden': 'true' }, `f${moment.seq}`),
      moment && jsx(Moment, { moment }, `m${moment.seq}`),
      jsxs('div', { className: 'sa-mark', children: hero
        ? [jsx(Duel, { card, flip, was, stamp, summon }, 'duel'), done && jsx(Closed, {}, 'closed')]
        : [
            summon && jsx('span', { className: 'sa-summon', 'aria-hidden': 'true', children: svg('', '0 0 16 20', [jsx('path', { d: FIGURE }, 'f'), jsx('path', { className: 'eyes', d: EYES }, 'e')]) }, summon),
            jsx(Seal, { card, flip, was, stamp }, 'seal'), done && jsx(Closed, {}, 'closed')
          ] }),
      jsxs('div', { className: 'sa-body', children: [
        jsxs('div', { className: 'sa-title-row', children: [
          jsxs('button', { type: 'button', className: 'sa-title', 'data-open': cardKey(card), onClick: () => onOpen(card), children: [str(card.title) || str(card.id), jsx('span', { className: 'sa-sr', children: ' — open its ledger' })] }),
          lane && jsx('span', { className: 'sa-lane', children: lane }),
          jsx(Kind, { card }),
          jsx(Owner, { card })
        ] }),
        hero
          ? jsxs('dl', { className: 'sa-qa', children: [
              found && jsx('dt', { className: 'found nx', children: 'Found' }, `fdt${beat}`),
              found && jsx('dd', { className: 'found nx', children: jsx(Note, { row: found }) }, `fdd${beat}${rowKey(found)}`),
              cut && jsx('dt', { className: 'found nx rm', children: 'Removed' }, `rdt${beat}`),
              cut && jsx('dd', { className: 'found nx rm', children: jsxs('span', { className: 'note sa-pins', children: [
                jsxs('span', { children: [jsx('code', { className: 'sa-pin', children: pinText(cut) }), ' ', plural(removedCount(card.gate), 'assertion') + ' removed since ' + (cut.since === 'start' ? 'the card started' : 'the last review')] })
              ] }) }, `rdd${beat}${pinText(cut)}`),
              jsx('dt', { children: 'Why' }, 'wdt'),
              jsx('dd', { className: 'why', children: jsx(Held, { text: whyOf(card), held }) }, `w${beat}${whyOf(card)}`),
              hintOf(card) && jsx('dt', { className: 'nx', children: 'Next' }, `ndt${beat}`),
              hintOf(card) && jsx('dd', { className: 'nx', children: hintOf(card) }, `n${beat}${hintOf(card)}`)
            ] })
          : jsx('p', { className: 'sa-why', children: card.needs_you && str(card.decision).trim() ? `Waiting on you: ${sentence(card.decision)}` : whyOf(card) }),
        !hero && hintOf(card) && jsxs('p', { className: 'sa-next', children: [jsx('span', { className: 'sa-label', children: 'Next' }), ' ', hintOf(card)] }),
        // A closed card keeps its old findings in its ledger, so its tile never reads as an open problem.
        !hero && !done && roster
      ] }),
      // The hero puts its current evidence before its list of shadows, so a narrow pane reads the newest result first.
      hero && jsx(HeroSide, { card, now, moment, found, onOpen, onCopy }),
      hero && roster && jsxs('div', { className: 'sa-roster', children: [jsx('h3', { className: 'sa-label', children: 'Shadows on this card' }), roster] })
    ]
  })
}

function Needs({ cards, onOpen }) {
  const needs = cards.filter(card => card.needs_you)
  if (!needs.length) return null
  return jsxs('section', {
    className: 'sa-needs', 'data-count': String(needs.length), 'aria-label': 'Needs you',
    children: [
      jsx('span', { className: 'sa-needs-tag', children: `NEEDS YOU · ${needs.length}` }),
      jsx('ul', { children: needs.map(card => jsx('li', { children: jsxs('button', {
        type: 'button', className: 'sa-needs-item', onClick: () => onOpen(card),
        children: [jsx('b', { children: sentence(card.decision) || 'Blocked without a decision note' }), jsx('span', { children: ` · ${str(card.title)} · answer on its Kanban card` })]
      }) }, cardKey(card))) })
    ]
  })
}

function RailEvent({ card, row, now }) {
  const when = row.at == null ? null : jsx('time', { title: clock(row.at), children: ago(now - (seconds(row.at) ?? now)) })
  if (BAR[row.kind]) {
    const [tag, cls] = barOf(card, row)
    return jsxs('li', { className: `sa-bar ${cls}`, 'data-kind': row.kind, children: [
      jsx('b', { children: tag }), jsx('span', { className: 'txt', children: refusalText(card, row) }), CLOSING.includes(row.kind) && olderTree(card, row) && jsx(Older, {}), when
    ] })
  }
  const duty = DUTY[str(row.duty).toLowerCase()] || 'Shadow'
  const who = (row.duty || row.family) && jsxs('div', { className: 'who', children: [
    row.duty && jsx('span', { className: 'sa-duty', children: str(row.duty).toUpperCase() }),
    row.family && jsx(Family, { id: row.family }),
    row.model && jsx('span', { className: 'sa-model', children: str(row.model) }),
    row.kind === 'verdict' && jsx(Verdict, { verdict: row.verdict }),
    row.kind === 'verdict' && isSimulated(card, row) && jsx(Sim, { duty: 'verify' })
  ] })
  if (row.kind === 'verdict') {
    const old = olderTree(card, row)
    const verdict = str(row.verdict).toUpperCase()
    return jsxs('li', { className: 'sa-ev verdict', 'data-v': verdict, 'data-kind': row.kind, children: [
      when,
      jsxs('div', { className: 'body', children: [
        who,
        jsx('div', { className: 'txt', children: verdictNote(row, old) }),
        row.tree && jsxs('div', { className: 'sa-model', children: [`code version ${row.tree} `, old && jsx(Older, {})] }),
        jsx(Check, { check: row.check, removed: jsx(Removed, { row }), drop: dropOf(row) })
      ] })
    ] })
  }
  const said = str(row.finding) ? ` ${str(row.finding)}` : ''
  // "arose" is the stored event name for a child that started.
  const text = row.kind === 'arose' ? `${duty} started.${said}`
    : row.kind === 'finished' ? `${duty} finished.${said}`
    : `${human(row.kind) || 'Event'}${row.finding ? `: ${row.finding}` : ''}`
  const version = ['arose', 'finished'].includes(row.kind) && row.tree && jsx('div', { className: 'sa-model', children: `${row.kind === 'finished' ? 'started from ' : ''}code version ${row.tree}` })
  // The builder's own check run, with any assertions it removed, is evidence too.
  const run = ['arose', 'finished'].includes(row.kind) && (row.check?.command || hasAdvice(row)) && jsx(Check, { check: row.check, removed: jsx(Removed, { row }), drop: dropOf(row) })
  return jsxs('li', { className: 'sa-ev', 'data-kind': row.kind, children: [when, jsxs('div', { className: 'body', children: [who, jsx('div', { className: 'txt', children: text }), version, run] })] })
}

function Ledger({ card, cards, now, onOpen, onClose, onCopy, headingRef }) {
  const gate = gateOf(card)
  const verdicts = verdictRows(card)
  const rows = historyOf(card)
  const builder = card.gate?.builder, verifier = card.gate?.verifier
  const edit = editAfterPass(card)
  // A card Kanban reports done without a "closed" row still gets a closure bar.
  const tail = closed(card) && !rows.some(row => row.kind === 'closed')
    ? [{ kind: 'closed', at: null, finding: 'Kanban reports this card done.' }] : []
  const headingId = `sa-ledger-${cardKey(card).replace(/[^\w-]/g, '_')}`
  const empty = !rows.length && !tail.length
  // A card the gate leaves alone has no verdicts to wait for: its empty ledger repeats the hint, said once.
  const quiet = QUIET.includes(gate) && !verdicts.length
  const next = quiet && empty ? '' : hintOf(card)
  return jsxs('div', { className: 'sa-detail', children: [
    jsx('nav', { className: 'sa-index', 'aria-label': 'Cards', children: cards.map(other => jsxs('button', {
      type: 'button', className: 'sa-index-item', 'aria-current': other === card ? 'true' : 'false', onClick: () => onOpen(other),
      children: [jsx('span', { className: 't', children: str(other.title) }), jsx(Badge, { gate: gateOf(other) }), jsx('span', { className: 's', children: whyOf(other) })]
    }, cardKey(other))) }),
    jsxs('section', { className: 'sa-ledger', 'aria-labelledby': headingId, children: [
      jsx('button', { type: 'button', className: 'sa-btn quiet sa-back', onClick: onClose, children: 'Back to all cards' }),
      jsxs('div', { className: 'sa-ledger-head', children: [
        jsx('h2', { id: headingId, tabIndex: -1, ref: headingRef, children: str(card.title) }), jsx(Badge, { gate }),
        isDone(card) ? jsx(Closed, {}) : card.state && jsx('span', { className: 'sa-lane', children: human(card.state) }),
        jsx(Kind, { card }),
        jsx(Owner, { card }),
        str(card.receipt_markdown).trim() && onCopy && jsx(CopyReceipt, { text: str(card.receipt_markdown).trim(), onCopy })
      ] }),
      jsxs('dl', { className: 'sa-qa', children: [
        jsx('dt', { children: 'Why' }), jsx('dd', { className: 'why', children: whyOf(card) }),
        next && jsx('dt', { children: 'Next' }), next && jsx('dd', { children: next }),
        card.needs_you && jsx('dt', { children: 'You' }), card.needs_you && jsx('dd', { children: sentence(card.decision) || 'Blocked; answer on the Kanban card.' })
      ] }),
      !quiet && jsxs('div', { className: 'sa-summary', 'aria-label': 'Verdict summary', children: [
        jsx('span', { className: 'sa-label', children: 'Verdicts' }),
        verdicts.length
          ? verdicts.map((row, i) => jsxs('span', { style: { display: 'inline-flex', gap: '6px', alignItems: 'center' }, children: [
              i > 0 && jsx('span', { className: 'arrow', 'aria-hidden': 'true', children: '→' }),
              jsx(Verdict, { verdict: row.verdict }),
              row.tree && jsx('span', { className: 'sa-hash', children: str(row.tree) })
            ] }, i))
          : jsx('span', { className: 'sa-empty', children: 'none yet' }),
        edit && jsxs('span', { style: { display: 'inline-flex', gap: '6px', alignItems: 'center' }, children: [
          jsx('span', { className: 'arrow', 'aria-hidden': 'true', children: '→' }),
          jsx('span', { className: 'sa-v', 'data-v': 'EDITED', children: 'EDITED' }),
          card.gate?.tree && jsx('span', { className: 'sa-hash', children: str(card.gate.tree) })
        ] }),
        (builder || verifier) && jsxs('span', { className: 'by', children: [
          builder && 'built by', builder && jsx(Family, { id: builder.family }), builder?.model && jsx('span', { className: 'sa-model', children: str(builder.model) }),
          builder && verifier && jsx('span', { 'aria-hidden': 'true', children: '·' }),
          verifier && 'verified by', verifier && jsx(Family, { id: verifier.family }), verifier?.model && jsx('span', { className: 'sa-model', children: str(verifier.model) })
        ] })
      ] }),
      !empty
        ? jsx('ol', { className: 'sa-rail', 'aria-label': 'Card ledger, oldest first', children: [...rows, ...tail].map((row, i) => jsx(RailEvent, { card, row, now }, i)) })
        : (!quiet || hintOf(card)) && jsx('p', { className: 'sa-empty', children: quiet ? hintOf(card) : 'No gate activity on this card yet.' }),
      list(card.shadows).length > 0 && jsxs('div', { children: [
        jsx('h3', { className: 'sa-label', style: { marginBottom: '4px' }, children: 'Shadows on this card' }),
        jsx('div', { className: 'sa-shadows', children: list(card.shadows).map((s, i) => jsx(ShadowRow, { shadow: s, index: i }, s.id || i)) })
      ] })
    ] })
  ] })
}

// A setting name such as plugins.entries.shadow-army.settings.check wraps only after a dot, never inside a word.
const settingText = text => str(text).split(/(\s+)/).map((token, i) => /\w[.-]\w/.test(token)
  ? jsx('span', { className: 'sa-key', children: token.split(/(?<=\.)/).flatMap((part, j) => [j > 0 && jsx('wbr', {}, `b${j}`), jsx('span', { children: part }, j)]) }, i)
  : token)

const ARMED_TEXT = 'Cards with a git workspace need a verifier from a different model family to pass the exact code on disk before Hermes can close them. Other cards are not gated.'
const UNARMED_TEXT = 'Until the setup below is done, Hermes closes cards without a check. Nothing here is gated yet.'
// The settings form is a host route the snapshot names (v2.3 setup_url); anything else is ignored.
const settingsRoute = snap => {
  const url = str(snap?.setup_url)
  return url.startsWith('/') && !url.startsWith('//') ? url : ''
}
const SettingsButton = ({ route }) => route && jsx('button', {
  type: 'button', className: 'sa-btn sa-settings', onClick: () => host.navigate(route), children: 'Open Shadow Army settings'
})

// Where setup stands, in the snapshot's own terms. Configured: every setting is in place. Armed: the backend says the
// gate blocks closes. Both are states of the profile. Whether Hermes has called the gate's hook is an observation
// only an agent's tool call produces, so it is not a stage: it is its own quiet line (HookSeen).
// Items that are not settings: the hook is seen, not set, and the Hermes version note is advisory (v2.4).
const ADVISORY = ['hook', 'hermes_version']
function stagesOf(snap) {
  const items = list(snap?.setup)
  return [
    ['configured', items.length > 0 && items.filter(item => !ADVISORY.includes(str(item.id))).every(item => item.ok === true), 'Configured', 'Not configured'],
    ['armed', snap?.armed === true, 'Armed', 'Not armed']
  ]
}
const Stages = ({ snap }) => jsx('ol', { className: 'sa-stages', 'aria-label': 'Setup state', children: stagesOf(snap).map(([id, ok, yes, no]) =>
  jsxs('li', { 'data-ok': String(ok), children: [jsx('span', { className: 'sa-box', 'data-ok': String(ok), 'aria-hidden': 'true' }), ok ? yes : no] }, id)) })

// "2 minutes ago", spelled out for a sentence.
function agoWords(delta) {
  if (delta < 45) return 'just now'
  const [n, unit] = delta < 3600 ? [Math.max(1, Math.round(delta / 60)), 'minute'] : delta < 86400 ? [Math.round(delta / 3600), 'hour'] : [Math.round(delta / 86400), 'day']
  return `${n} ${unit}${n === 1 ? '' : 's'} ago`
}
// The hook as observed, worded as what it is and what makes it appear; it is not a step, so it has no mark. Never seen
// and not seen in the last day are different facts: the backend keeps the last call's age after it turns stale. A hook
// item without seen_s cannot tell the two apart, so it says neither.
const NEXT_CALL = "Hermes calls the gate's hook on the agent's next tool call."
function hookSeen(item) {
  if (!item) return ''
  if (Number.isFinite(item.seen_s)) {
    const last = `Hermes last called the gate's hook ${agoWords(item.seen_s)}.`
    return item.ok === true ? last : `${last} It calls it again on the agent's next tool call.`
  }
  if (item.ok === true) return "Hermes has called the gate's hook in this profile."
  return 'seen_s' in item ? `${NEXT_CALL} Not seen yet in this profile.` : `${NEXT_CALL} Not seen lately in this profile.`
}
const HookSeen = ({ snap }) => {
  const said = hookSeen(list(snap?.setup).find(item => item && item.id === 'hook'))
  return said ? jsx('p', { className: 'sa-hook-seen', children: said }) : null
}

// The guided setup card: each item with its current value, a ticked or open mark, and one action. The hook is not an
// item to do; it is the observation line under the stages.
function SetupCard({ snap, feed, route, test, setTest }) {
  const items = list(snap?.setup).filter(item => item?.id !== 'hook')
  return jsxs('div', { className: 'sa-panel-body', children: [
    jsx(Stages, { snap }),
    jsx(HookSeen, { snap }),
    jsx('ul', { className: 'sa-checklist sa-guide', children: items.map((item, i) => jsx(SetupRow, { item, snap, feed, route }, str(item.id) || i)) }),
    jsx(SelfTest, { snap, feed, test, setTest })
  ] })
}

function SetupRow({ item, snap, feed, route }) {
  const ok = item.ok === true
  const id = str(item.id)
  const value = str(item.value).trim()
  const hint = str(item.hint).trim()
  let detail = value ? jsx('code', { className: 'sa-value', children: value }) : null
  let action = null
  if (id === 'families') detail = jsx(Families, { value }) || detail
  if (id === 'verifier') action = jsx(ArmPicker, { snap, feed, route, ok })
  if (id === 'backend' && !ok) action = jsx(Command, { text: str(snap?.setup_actions?.routing_command).trim(), feed })
  if (!ok && !action && ['repo', 'check', 'lifetime'].includes(id)) action = jsx(SettingsButton, { route })
  return jsxs('li', { 'data-ok': String(ok), 'data-id': id, children: [
    jsx('span', { className: 'sa-box', 'data-ok': String(ok), 'aria-hidden': 'true' }),
    jsxs('span', { className: 'sa-row-main', children: [
      jsxs('span', { className: 'sa-row-top', children: [
        jsx('span', { className: 'sa-item', children: str(item.label).trim() || sentence(human(id)) || 'Setup' }),
        jsx('span', { className: 'sa-sr', children: ok ? ', done: ' : ', not done: ' }),
        detail
      ] }),
      // A ticked item's hint is a confirmation; it is shown only when it says more than the value.
      hint && hint !== value && (!ok || !value) && jsx('span', { className: 'sa-item-hint', children: settingText(hint) }),
      action
    ] })
  ] })
}

// "openai builds, anthropic verifies": the two families side by side, each with its sigil.
function Families({ value }) {
  const m = /^(.+?) builds, (.+?) verifies$/.exec(value)
  if (!m) return null
  return jsxs('span', { className: 'sa-pair', children: [
    jsxs('span', { className: 'sa-side-fam', children: [jsx(ProfileSigil, { family: m[1], className: 'sa-psig mini b' }), jsx('span', { className: 'role', children: 'builds' }), jsx(Family, { id: m[1] })] }),
    jsx('span', { className: 'sa-vs', 'aria-hidden': 'true', children: '≠' }),
    jsxs('span', { className: 'sa-side-fam', children: [jsx(ProfileSigil, { family: m[2], className: 'sa-psig mini v' }), jsx('span', { className: 'role', children: 'verifies' }), jsx(Family, { id: m[2] })] })
  ] })
}

// The verifier picker offers only routes the backend lists (configured models of an independent family). No free text.
function ArmPicker({ snap, feed, route, ok }) {
  const acts = snap?.setup_actions && typeof snap.setup_actions === 'object' ? snap.setup_actions : null
  const routes = list(acts?.routes).filter(r => r && str(r.provider) && str(r.model))
  const [pick, setPick] = useState(0)
  const [said, setSaid] = useState('')
  const [change, setChange] = useState(false)
  const busy = feed.get().pending === 'arm'
  if (!routes.length) return jsx(SettingsButton, { route })
  // An armed profile with its verifier in place needs no arming; the picker opens only to change the route.
  if (ok && snap?.armed === true && !change && !said) {
    return acts?.can_arm === true ? jsx('button', { type: 'button', className: 'sa-btn quiet', onClick: () => setChange(true), children: 'Change verifier' }) : null
  }
  // v2.4: when this serving process cannot save the route, the backend says why; the settings form does it instead.
  // A verifier already in place needs neither.
  const reason = str(acts?.reason).trim()
  if (acts?.can_arm !== true && ok) return null
  if (acts?.can_arm !== true && reason) {
    return jsxs('span', { className: 'sa-arm', children: [jsx('span', { className: 'sa-said', children: reason }), jsx(SettingsButton, { route })] })
  }
  const chosen = routes[Math.min(pick, routes.length - 1)]
  const arm = async () => setSaid(await feed.arm(chosen) || `Saved: ${str(chosen.label) || str(chosen.model)}.`)
  return jsxs('span', { className: 'sa-arm', children: [
    jsxs('label', { className: 'sa-pick', children: [
      jsx('span', { className: 'sa-sr', children: 'Verifier route' }),
      jsx('select', { value: String(Math.min(pick, routes.length - 1)), onChange: event => { setPick(Number(event.target.value)); setSaid('') },
        children: routes.map((r, i) => jsx('option', { value: String(i), children: `${str(r.label) || `${str(r.model)} through ${str(r.provider)}`} · ${family(r.family)}` }, i)) })
    ] }),
    jsx('button', { type: 'button', className: 'sa-btn arm', disabled: busy || acts?.can_arm !== true, onClick: () => void arm(), children: busy ? 'Arming…' : 'Arm the gate' }),
    said && jsx('span', { className: 'sa-said', role: 'status', children: said })
  ] })
}

// A command the plugin may not run for you, with a copy button through the host's clipboard door.
function Command({ text, feed }) {
  const [said, setSaid] = useState('')
  if (!text) return null
  return jsxs('span', { className: 'sa-cmd', children: [
    jsx('code', { children: text }),
    jsx('button', { type: 'button', className: 'sa-btn quiet', onClick: async () => setSaid(await feed.copy(text) ? 'Copied' : 'Copy failed'), children: 'Copy' }),
    said && jsx('span', { className: 'sa-said', role: 'status', children: said })
  ] })
}

// The latest self-test, and whether the profile was armed when it ran. A result without "armed" (an older backend)
// counts as armed only while the snapshot says the profile is armed now.
function lastTest(snap, test) {
  const last = test || (snap?.selftest && typeof snap.selftest === 'object' ? snap.selftest : null)
  if (!last) return null
  const known = typeof last.armed === 'boolean'
  return { ...last, known, armed: known ? last.armed : snap?.armed === true }
}
const NOT_ARMED = 'This profile is not armed, so real closes are allowed.'

// The first setup item still open, which the not-armed self-test points at.
const firstOpen = snap => list(snap?.setup).find(item => item && item.ok !== true && !ADVISORY.includes(str(item.id)))

// The self-test asks the gate's real decision path to close a throwaway unreviewed card. It reads as proof only on an
// armed profile whose close was refused; the backend's message is shown as sent.
function SelfTest({ snap, feed, test, setTest }) {
  const [error, setError] = useState('')
  const here = useRef(null)
  const last = lastTest(snap, test)
  const busy = feed.get().pending === 'selftest'
  const run = async () => {
    setError('')
    const { result, error: refusal } = await feed.selftest()
    if (result) setTest({ ...result, checked_at: result.checked_at ?? Date.now() / 1000 })
    else setError(refusal)
  }
  const when = last && seconds(last.checked_at) != null ? ago(Date.now() / 1000 - seconds(last.checked_at)) : ''
  const proof = Boolean(last?.armed && last.refused === true)
  // What the self-test proves is the gate's decision on this profile; whether Hermes calls the hook is the hook line's.
  const head = !last ? '' : !last.armed ? NOT_ARMED : last.refused === true ? `The gate refused a test close ${when || 'just now'}.` : 'The gate did not refuse the test close.'
  // The backend's not-armed message opens with the same sentence; it is said once. An older backend's message on an
  // unarmed profile reports a refusal that real closes would not get, so it is left out. A tool call written for the
  // agent ("Next: delegate_task(…)") is not for people and is left out too.
  const message = last && (last.known || last.armed) ? str(last.message).replace(/\s+Next:\s*\w+\(.*$/s, '').trim() : ''
  const rest = message.startsWith(head) ? message.slice(head.length).trim() : message
  const open = last && !last.armed ? firstOpen(snap) : null
  const goTo = () => {
    // Look only inside this setup card (the checklist is the self-test's sibling), never across the document.
    const row = here.current?.parentElement?.querySelector(`.sa-guide li[data-id="${CSS.escape(str(open?.id))}"]`)
    row?.scrollIntoView({ block: 'center' })
    ;(row?.querySelector('button:not([disabled]), select') || row)?.focus?.()
  }
  return jsxs('div', { ref: here, className: 'sa-selftest', 'data-ok': last ? String(proof) : undefined, 'data-armed': last ? String(last.armed) : undefined, children: [
    jsx('button', { type: 'button', className: 'sa-btn', disabled: busy, onClick: () => void run(), children: busy ? 'Testing…' : 'Run self-test' }),
    jsxs('span', { role: 'status', className: 'sa-selftest-said', children: [
      error,
      !error && last && jsx('b', { children: head }),
      !error && rest && ` ${rest}`,
      !error && open && jsxs('span', { className: 'sa-first', children: [
        ' First open item: ',
        jsx('button', { type: 'button', className: 'sa-link', onClick: goTo, children: str(open.label).trim() || sentence(human(open.id)) })
      ] }),
      !error && !last && 'Asks the gate to close a throwaway unreviewed card and shows its decision on this profile.'
    ] })
  ] })
}

// With cards on the board the setup card folds to one line that still says where setup stands.
function Setup({ snap, feed, open, onToggle, route }) {
  const [test, setTest] = useState(null)
  const toggle = jsx('button', { type: 'button', className: 'sa-link', 'aria-expanded': String(open), 'aria-controls': 'sa-setup-body', onClick: onToggle, children: open ? 'Hide' : 'Show' })
  if (!open) {
    // Folded, the line says whether the gate is configured and armed; the hook observation is in the open card.
    return jsxs('section', { className: 'sa-setup-line', id: 'sa-setup', 'aria-label': 'Setup', children: [jsx('span', { className: 'sa-label', children: 'Setup' }), jsx(Stages, { snap }), !list(snap?.cards).some(c => !['done', 'archived'].includes(c.state) && c.gate?.state !== 'UNGATED') && jsx('span', { className: 'sa-hint', children: 'No open code card.' }), toggle] })
  }
  return jsxs('section', { className: 'sa-panel', id: 'sa-setup', 'aria-labelledby': 'sa-setup-h', children: [
    jsxs('div', { className: 'sa-panel-head', children: [
      jsx('h2', { id: 'sa-setup-h', tabIndex: -1, children: snap?.armed === true ? 'Setting up the gate' : 'The gate is not armed' }),
      jsxs('span', { className: 'sa-hint', children: [toggle] })
    ] }),
    jsxs('div', { id: 'sa-setup-body', className: 'sa-panel-body', children: [
      jsx('p', { children: snap?.armed === true ? ARMED_TEXT : UNARMED_TEXT }),
      jsx(SetupCard, { snap, feed, route, test, setTest }),
      jsx('div', { className: 'sa-row', children: jsx(SettingsButton, { route }) })
    ] })
  ] })
}

// What the trial will show, drawn as three small seals. Illustrations of the trial's beats, not live state.
const PREVIEW = [
  { gate: 'BLOCKED', word: 'REFUSED', step: 'Close', caption: 'No review, close refused' },
  { gate: 'BLOCKED', word: 'FAIL', note: 'exit 0', step: 'Twist', caption: 'Green tests, still failed' },
  { gate: 'PASS', word: 'PASS', step: 'Seal', caption: 'Only the exact code clears' }
]

// First run: the hero. Headline beside the gate, one bright button, and the setup list below.
function FirstRun({ snap, feed, route, onTrial, pending }) {
  const items = list(snap?.setup)
  const armed = snap?.armed !== false
  const [test, setTest] = useState(null)
  return jsxs('section', { className: 'sa-hero', 'aria-labelledby': 'sa-hero-h', children: [
    jsxs('div', { className: 'sa-stage', children: [
      jsx(Portal, {}),
      jsxs('div', { className: 'sa-pitch', children: [
        jsx('p', { className: 'sa-eyebrow', children: 'Independent by design' }),
        jsx('h2', { id: 'sa-hero-h', children: 'Every gate needs a second shadow.' }),
        jsx('p', { className: 'sa-sub', children: 'Build. Challenge. Verify. Only the exact code earns a seal.' }),
        jsxs('div', { className: 'sa-cta', children: [
          jsx('button', { type: 'button', className: 'sa-btn primary big', onClick: onTrial, disabled: Boolean(pending), children: pending === 'start' ? 'Starting the trial…' : 'Play the trial' }),
          jsx('span', { className: 'sa-hint', children: 'About a minute. A tiny repo on this machine, a real test run, simulated reviewers.' })
        ] }),
        jsxs('div', { className: 'sa-preview-wrap', children: [
          jsx('span', { className: 'sa-label', children: 'In the trial' }),
          jsx('ol', { className: 'sa-preview', children: PREVIEW.map(p => jsxs('li', { children: [
            jsxs('span', { className: 'sa-seal mini', 'data-gate': p.gate, 'aria-hidden': 'true', children: [
              ...GateArt({ gate: p.gate }),
              jsxs('span', { className: 'plate', children: [jsx('span', { className: 'w', children: p.word }), p.note && jsx('span', { className: 'k', children: p.note })] })
            ] }),
            jsxs('span', { className: 'cap', children: [jsx('b', { children: p.step }), p.caption] })
          ] }, p.step)) })
        ] })
      ] })
    ] }),
    jsxs('section', { className: 'sa-panel sa-hero-setup', id: 'sa-setup', 'aria-labelledby': 'sa-setup-h', children: [
      jsxs('div', { className: 'sa-panel-head', children: [
        jsx('h2', { id: 'sa-setup-h', tabIndex: -1, children: 'No cards at the gate yet' }),
        jsx(SettingsButton, { route })
      ] }),
      jsx('p', { children: armed ? ARMED_TEXT : UNARMED_TEXT }),
      jsx('h3', { children: 'Set up your own' }),
      items.length ? jsx(SetupCard, { snap, feed, route, test, setTest }) : jsx('p', { className: 'sa-hint', children: 'Ask Hermes to work a Kanban card. It appears here with its gate.' })
    ] })
  ] })
}

// What the trial runs for real and what it simulates, as the backend lists them (v2.4). Older backends: one label.
const words = value => list(value).map(str).map(text => text.trim()).filter(Boolean)
function Disclosure({ trial }) {
  const real = words(trial?.real), simulated = words(trial?.simulated)
  if (!real.length && !simulated.length) return jsx('span', { className: 'sa-synth', children: 'synthetic demo data' })
  return jsxs('span', { className: 'sa-disclose', children: [
    simulated.length > 0 && jsxs('span', { className: 'sa-synth', children: [jsx('b', { children: 'Simulated' }), ` ${simulated.join(', ')}`] }),
    real.length > 0 && jsxs('span', { className: 'sa-synth real', children: [jsx('b', { children: 'Real' }), ` ${real.join(', ')}`] })
  ] })
}

function Trial({ state, feed, cards }) {
  const busy = Boolean(state.pending)
  const trial = state.snap?.demo && state.snap.trial && typeof state.snap.trial === 'object' ? state.snap.trial : null
  const shut = cards.some(closed)
  const canStep = trial ? trial.can_step === true : !shut
  // v2.3: tampering exists only between the PASS and the close, and only the backend knows when that is.
  const canTamper = trial?.can_tamper === true
  const autoOn = state.auto && canStep && !trial?.finished
  // Progress without a total: a trail of the steps played so far, and one open mark while a next step exists.
  const step = trial && Number.isFinite(trial.step) && trial.step > 0 ? Math.min(Math.floor(trial.step), 40) : 0
  // Next leads until the story ends, then Reset.
  const lead = canStep ? 'step' : 'reset'
  const kind = (action, base = '') => `sa-btn${lead === action ? ' primary' : base}`
  // The step's sentence names the verdict, so it waits for the seal like everything else.
  const hold = feed.holding()
  const message = hold ? (hold.review ? 'Review running…' : 'The gate is changing…')
    : str(trial?.message) || 'Reviewers are simulated. The git tree and the test run are real.'
  return jsxs('section', { className: 'sa-trial', 'aria-label': 'The trial', children: [
    jsxs('div', { className: 'sa-trial-top', children: [
      jsx('b', { children: 'THE TRIAL' }),
      step > 0 && jsx('span', { className: 'sa-count', children: `Step ${step}` }),
      jsx(Disclosure, { trial }),
      // The live region stays mounted; only the line inside it is re-keyed, so each new message types itself in.
      jsx('span', { className: 'why', role: 'status', children: jsx('span', { className: 'msg', children: message }, message) })
    ] }),
    step > 0 && jsxs('div', { className: 'sa-steps', 'aria-hidden': 'true', children: [
      ...Array.from({ length: step }, (_, i) => jsx('i', { 'data-on': 'true', 'data-now': String(i === step - 1) }, i)),
      canStep && !trial.finished && jsx('i', { 'data-next': 'true' }, 'next'),
      trial.finished && jsx('i', { 'data-end': 'true' }, 'end')
    ] }),
    jsxs('div', { className: 'sa-trial-row', children: [
      jsx('button', { type: 'button', className: kind('step'), onClick: () => void feed.step(), disabled: busy || !canStep, children: state.pending === 'step' ? 'Stepping…' : 'Next' }),
      jsx('button', { type: 'button', className: `sa-btn${canTamper ? ' tamper' : ''}`, onClick: () => void feed.tamper(), disabled: busy || !canTamper, children: state.pending === 'tamper' ? 'Tampering…' : 'Tamper one line' }),
      jsx('button', { type: 'button', className: 'sa-btn quiet', 'aria-pressed': String(autoOn), onClick: feed.toggleAuto, disabled: !canStep, children: autoOn ? 'Pause auto-play' : 'Auto-play' }),
      jsx('span', { className: 'grow' }),
      jsx('button', { type: 'button', className: kind('reset', ' quiet'), onClick: () => void feed.reset(), disabled: busy, children: state.pending === 'reset' ? 'Resetting…' : 'Reset' })
    ] })
  ] })
}

// Header tally words per gate state, worst first. PASS on an open card means it may close.
const TALLY = [['BLOCKED', 'blocked'], ['UNKNOWN', 'unreadable'], ['STALE', 'stale'], ['UNVERIFIED', 'awaiting review'], ['PASS', 'ready to close']]

const LIVE = {
  live: ['Live', 'Updates on every gate event; reconciles every 5 seconds.'],
  connecting: ['Connecting', 'Reading the gate on this connection.'],
  offline: ['Offline', 'The backend did not answer. Retrying every 5 seconds.'],
  incompatible: ['Needs update', 'The backend plugin is a different version.']
}

function Problems({ errors, alone, onRetry }) {
  return jsxs('section', { className: alone ? 'sa-panel' : 'sa-note', 'aria-label': 'Backend notes', role: alone ? undefined : 'status', children: [
    alone
      ? jsx('h2', { children: 'The gate backend reported a problem' })
      : jsx('span', { className: 'sa-label', children: errors.length === 1 ? 'Backend note' : `Backend notes · ${errors.length}` }),
    jsx('ul', { className: 'sa-errors', children: errors.map((error, i) => jsx('li', { children: str(error) }, i)) }),
    alone && jsx('p', { children: 'No cards could be read, so this is not a first run. Fix the note above, then the cards appear.' }),
    alone && jsx('div', { className: 'sa-row', children: jsx('button', { type: 'button', className: 'sa-btn', onClick: onRetry, children: 'Try again' }) })
  ] })
}

// A moment is what the tile plays when new history lands: the labels of the new rows, in order. "ARISE" leads a
// verdict whose verifier had not been seen starting. The tone of the last label colours the hit.
const VERIFY = row => row.kind === 'arose' && str(row.duty).toLowerCase() === 'verify'
const BAR_TONE = { dashed: 'fail', '': 'fail', permitted: 'sys', closed: 'pass', edited: 'warn', neutral: 'sys' }
function beatsOf(card, rows, from) {
  const beats = []
  rows.slice(from).forEach((row, j) => {
    if (VERIFY(row)) beats.push(['ARISE', 'arise'])
    else if (row.kind === 'verdict') {
      const before = rows.slice(0, from + j).reverse()
      const summoned = before.findIndex(VERIFY), prior = before.findIndex(r => r.kind === 'verdict')
      if (summoned < 0 || (prior >= 0 && prior < summoned)) beats.push(['ARISE', 'arise'])
      // The green-tests FAIL plays the check's own result first, then the verdict over it.
      if (greenFail(row)) beats.push(['EXIT 0', 'pass'])
      const verdict = str(row.verdict).toUpperCase()
      beats.push([verdict || 'VERDICT', verdict === 'PASS' ? 'pass' : verdict === 'FAIL' ? 'fail' : 'sys'])
    } else if (BAR[row.kind]) {
      const [label, cls] = barOf(card, row)
      beats.push([label, BAR_TONE[cls] || 'sys', row.kind === 'close_refused'])
    }
  })
  return beats
}

// Remembers, per card, the gate and history length already drawn, so a change plays once and a poll never replays it.
// It is render-time bookkeeping: a repeat render of the same snapshot finds nothing new and keeps the same moment.
const MOMENT_MS = 5000
// ARISE plays alone first; the evidence appears at --lag and the verdict lands at --hit.
function timing(beats) {
  const arise = beats.length > 1 && beats[0][0] === 'ARISE'
  const times = beats.map((_, i) => (arise ? (i ? 1.4 + (i - 1) * 1.2 : 0) : i * 1.15))
  const hit = times.at(-1)
  return { arise, times, hit, lag: arise ? Math.min(1.4, hit) : hit }
}
// The same moment, seen from the feed: when a snapshot both changes a card's gate and lands new history, the seal
// keeps the old word until --hit. The header, the status bar, toasts and Kanban badges keep that old gate as long.
function holdsOf(prev, snap, holds) {
  const now = Date.now()
  const out = new Map([...holds].filter(([, hold]) => hold.until > now))
  if (!prev || prev.demo !== snap.demo) return new Map()
  const before = new Map(list(prev.cards).map(card => [cardKey(card), card]))
  for (const card of list(snap.cards)) {
    const old = before.get(cardKey(card))
    if (!old) continue
    const was = gateOf(old), gate = gateOf(card), n = list(old.history).length, rows = historyOf(card)
    if (was === gate || rows.length <= n) continue
    const beats = beatsOf(card, rows, n).slice(-3)
    const hit = beats.length ? timing(beats).hit : 0
    // A newer change replaces a running hold, as the seal re-keys on the new gate and holds the one before it.
    if (hit > 0) out.set(cardKey(card), { was, until: now + hit * 1000, review: rows.slice(n).some(row => row.kind === 'verdict') })
    else out.delete(cardKey(card))
  }
  return out
}

// Re-renders once the last held verdict has landed, so a surface that waited for the seal catches up on time.
function useRelease(feed) {
  const [, setTick] = useState(0)
  useEffect(() => {
    const left = feed.holdLeft()
    if (!left) return
    const timer = setTimeout(() => setTick(n => n + 1), left + 30)
    return () => clearTimeout(timer)
  })
}

function momentOf(m) {
  const { arise, times, hit, lag } = timing(m.beats)
  return { seq: m.seq, beats: m.beats, tone: m.beats.at(-1)[1], stamp: m.beats.some(b => b[2]), pending: m.pending, arise, times, hit, lag: m.pending ? lag : 0 }
}

function watch(memory, cards, mode) {
  if (memory.mode !== mode) { memory.mode = mode; memory.cards = new Map() }
  const t = Date.now(), out = new Map()
  for (const card of cards) {
    const key = cardKey(card), gate = gateOf(card), rows = historyOf(card)
    let m = memory.cards.get(key)
    if (!m) { m = { gate, n: rows.length, gateAt: 0, at: 0, seq: 0, beats: [], pending: '' }; memory.cards.set(key, m) }
    if (m.gate !== gate) { m.was = m.gate; m.gate = gate; m.gateAt = t }
    if (rows.length > m.n) {
      const beats = beatsOf(card, rows, m.n)
      // Only new evidence (a review verdict or a check run) has something running before it lands. A moment of close
      // or edit rows alone holds nothing back, so no "running" placeholder covers the evidence already shown.
      const fresh = rows.slice(m.n).filter(isEvidence).at(-1)
      if (beats.length) { m.beats = beats.slice(-3); m.at = t; m.seq++; m.pending = fresh ? (fresh.kind === 'verdict' ? 'review' : 'check') : '' }
    }
    m.n = rows.length
    out.set(key, {
      flip: Boolean(m.gateAt) && t - m.gateAt < MOMENT_MS,
      was: m.was,
      moment: m.at && t - m.at < MOMENT_MS ? momentOf(m) : null
    })
  }
  return out
}

// The host keeps a collapsed right pane mounted under display: none. Watching this pane's own root tells a hidden pane
// from an open one, so a hidden pane drops to a passive user: no auto-play, and the slow poll.
function useShown(ref) {
  const [shown, setShown] = useState(true)
  useEffect(() => {
    const el = ref.current
    if (!el || typeof IntersectionObserver !== 'function') return
    const watcher = new IntersectionObserver(entries => setShown(entries.some(entry => entry.isIntersecting)))
    watcher.observe(el)
    return () => watcher.disconnect()
  }, [ref])
  return shown
}

function ShadowArmy({ feed }) {
  const rootRef = useRef(null)
  const visible = useShown(rootRef)
  const state = useFeed(feed, !visible)
  useRelease(feed)
  const [selected, setSelected] = useState(null)
  const [setupOpen, setSetupOpen] = useState(false)
  const [foldOpen, setFoldOpen] = useState(false)
  const headingRef = useRef(null)
  const openerRef = useRef(null)
  const seenRef = useRef({ mode: null, cards: new Map() })
  const snap = state.snap
  const cards = list(snap?.cards)
  const setup = list(snap?.setup)
  const errors = list(snap?.errors)
  const now = seconds(snap?.observed_at) ?? Date.now() / 1000
  const { hero, rest } = orderCards(cards)
  const ordered = hero ? [hero, ...rest] : rest
  const current = selected ? cards.find(card => cardKey(card) === selected) : null
  // The shown snapshot decides the mode, so a starting trial keeps the old view until its first snapshot lands.
  const demoView = snap ? snap.demo === true : state.demo
  const firstRun = Boolean(snap) && !cards.length && !errors.length
  const unarmed = Boolean(snap) && snap.armed === false && !demoView
  // On an armed board the not gated cards fold into one quiet line, so gated work leads.
  const folded = Boolean(snap) && !demoView && snap.armed !== false ? rest.filter(card => QUIET.includes(gateOf(card))) : []
  const shown = ordered.filter(card => !folded.includes(card))
  const route = settingsRoute(snap)
  // The setup line stays on a live board, so where setup stands and the self-test are always one click away.
  const showSetup = Boolean(snap) && !demoView && setup.length > 0

  const marks = watch(seenRef.current, cards, `${state.scope}|${demoView}`)

  useEffect(() => { setSelected(null); setSetupOpen(false); setFoldOpen(false) }, [state.scope, demoView])
  useEffect(() => {
    if (!current) return
    if (rootRef.current) rootRef.current.scrollTop = 0
    headingRef.current?.focus()
  }, [selected])

  const open = card => { openerRef.current = cardKey(card); setSelected(cardKey(card)) }
  const close = () => {
    const key = openerRef.current
    setSelected(null)
    requestAnimationFrame(() => {
      for (const el of rootRef.current?.querySelectorAll('[data-open]') || []) if (el.getAttribute('data-open') === key) el.focus()
    })
  }
  const toSetup = () => {
    setSelected(null)
    setSetupOpen(true)
    requestAnimationFrame(() => {
      const heading = rootRef.current?.querySelector('#sa-setup-h')
      heading?.scrollIntoView({ block: 'start' })
      heading?.focus()
    })
  }

  const shadows = cards.flatMap(card => list(card.shadows)).map(s => str(s.state).toLowerCase())
  const running = shadows.filter(s => s === 'running').length
  const queued = shadows.filter(s => s === 'pending').length
  const needs = cards.filter(card => card.needs_you).length
  const [liveLabel, liveTitle] = LIVE[state.status] || LIVE.connecting

  let body
  if (!snap && state.status === 'incompatible') {
    body = jsxs('section', { className: 'sa-panel', children: [jsx('h2', { children: 'Shadow Army needs an update here' }), jsx('p', { children: state.problem })] })
  } else if (!snap && state.status === 'offline') {
    body = jsxs('section', { className: 'sa-panel', children: [
      jsx('h2', { children: 'The gate backend is not answering' }),
      jsx('p', { children: 'This pane reads the shadow-army plugin on the connected Hermes backend. Enable the plugin for this profile, restart the backend, then try again.' }),
      jsx('div', { className: 'sa-row', children: jsx('button', { type: 'button', className: 'sa-btn', onClick: () => void feed.refresh(), children: 'Try again' }) })
    ] })
  } else if (!snap || (demoView && !cards.length)) {
    body = jsx('p', { className: 'sa-empty', role: 'status', children: demoView ? 'Starting the trial…' : 'Reading the gate…' })
  } else if (current) {
    body = jsx(Ledger, { card: current, cards: ordered, now, onOpen: open, onClose: close, onCopy: feed.copy, headingRef })
  } else {
    const tile = card => jsx(Tile, {
      card, hero: card === hero, now, ...marks.get(cardKey(card)), onOpen: open, onCopy: feed.copy
    }, cardKey(card))
    body = firstRun
      ? jsx(FirstRun, { snap, feed, route, onTrial: () => void feed.startTrial(), pending: state.pending })
      : [
          errors.length > 0 && jsx(Problems, { errors, alone: !cards.length, onRetry: () => void feed.refresh() }, 'errors'),
          showSetup && jsx(Setup, { snap, feed, open: setupOpen, onToggle: () => setSetupOpen(!setupOpen), route }, 'setup'),
          jsx(Needs, { cards, onOpen: open }, 'needs'),
          shown.length > 0 && jsx('div', { className: 'sa-tiles', children: shown.map(tile) }, 'tiles'),
          folded.length > 0 && jsxs('section', { className: 'sa-fold', 'aria-label': 'Cards the gate leaves alone', children: [
            jsx('span', { children: `${plural(folded.length, 'card')} not gated.` }),
            jsx('button', { type: 'button', className: 'sa-link', 'aria-expanded': String(foldOpen), 'aria-controls': 'sa-quiet', onClick: () => setFoldOpen(!foldOpen), children: foldOpen ? 'Hide' : 'Show' })
          ] }, 'fold'),
          folded.length > 0 && foldOpen && jsx('div', { className: 'sa-tiles', id: 'sa-quiet', children: folded.map(tile) }, 'quiet')
        ]
  }

  // Says what is true: decisions for you first, then every open gate that is not clear, by state.
  const openCards = cards.filter(card => !isDone(card))
  // A card whose verdict is still landing on its seal counts under the gate the seal still shows.
  const tally = TALLY.map(([gate, word]) => [openCards.filter(card => feed.shown(card) === gate).length, word]).filter(([n]) => n).map(([n, word]) => `${n} ${word}`)
  const counts = [needs ? `${needs} decision${needs === 1 ? '' : 's'} for you` : 'No decisions for you', ...tally,
    running && `${plural(running, 'shadow')} running`, queued && `${queued} queued`, plural(cards.length, 'card')].filter(Boolean).join(' · ')

  return jsx('section', {
    className: 'sa-root', 'data-testid': 'shadow-army', ref: rootRef,
    onKeyDown: event => { if (event.key === 'Escape' && current) { event.stopPropagation(); close() } },
    children: jsxs('div', { className: 'sa-wrap', children: [
      jsx(PaneStyle, {}),
      jsxs('header', { className: 'sa-head', children: [jsx(Emblem, {}), jsx('h1', { children: 'Shadow Army' }), jsx('p', { className: 'sa-tag', children: 'No model grades its own homework.' })] }),
      jsxs('div', { className: 'sa-meta', children: [
        jsxs('span', { className: 'sa-live', 'data-status': state.status, title: liveTitle, role: 'status', children: [jsx('i', { 'aria-hidden': 'true' }), state.status === 'offline' && snap ? 'Offline · showing the last snapshot' : liveLabel] }),
        unarmed && jsx('button', { type: 'button', className: 'sa-link sa-unarmed', onClick: toSetup, title: 'Open the setup checklist', children: 'Gate not armed' }),
        snap && jsx('span', { children: cards.length ? counts : errors.length ? 'No cards read' : 'Nothing on this connection yet' }),
        snap && !demoView && !firstRun && jsx('button', { type: 'button', className: 'sa-link', onClick: () => void feed.startTrial(), disabled: Boolean(state.pending), children: 'Play the trial' })
      ] }),
      demoView && jsx(Trial, { state, feed, cards }),
      state.notice && jsx('p', { className: 'sa-note', role: 'status', children: state.notice }),
      snap && state.status === 'incompatible' && jsx('p', { className: 'sa-note', role: 'status', children: state.problem }),
      body
    ] })
  })
}

// ---- Hermes surfaces beyond the pane. Each is one small registration that can be deleted alone. ----

const OPEN_GATES = ['BLOCKED', 'STALE', 'UNKNOWN', 'UNVERIFIED', 'PASS']
const openBoard = () => host.navigate('/shadow-army')

// Status bar: the worst open gates at a glance, in the host's own status-bar colours. A click opens the board.
const statusCss = `
.sa-sb { display: inline-flex; align-items: center; gap: 6px; height: 100%; padding: 0 6px; background: none; border: 0; font: inherit; color: inherit; cursor: pointer; white-space: nowrap }
.sa-sb:hover { color: var(--ui-text-primary) }
.sa-sb:focus-visible { outline: 2px solid var(--ui-accent); outline-offset: -2px; border-radius: 3px }
.sa-sb svg { width: 12px; height: 12px; fill: none; stroke: currentColor; stroke-width: 1.6; flex: none }
.sa-sb[data-gate="BLOCKED"] svg { color: var(--ui-red) }
.sa-sb[data-gate="STALE"] svg { color: var(--ui-yellow) }
.sa-sb[data-gate="PASS"] svg { color: var(--ui-green) }
.sa-sb[data-gate="UNVERIFIED"] svg { color: var(--ui-accent) }
`
function GateStatus({ feed }) {
  const state = useFeed(feed, true)
  useRelease(feed)
  const open = list(state.snap?.cards).filter(card => !isDone(card))
  const tally = OPEN_GATES.map(gate => [gate, open.filter(card => feed.shown(card) === gate).length]).filter(([, n]) => n)
  const text = state.status === 'offline' ? 'offline' : state.status === 'incompatible' ? 'needs update' : !state.snap ? 'reading…'
    : tally.length ? tally.slice(0, 2).map(([gate, n]) => `${n} ${gate}`).join(' · ') : 'no open gates'
  const label = `${state.snap?.demo ? 'Trial · ' : ''}Gates: ${text}`
  return jsxs('button', {
    type: 'button', className: 'sa-sb', 'data-gate': tally[0]?.[0] || 'NONE', onClick: openBoard,
    title: `Shadow Army. ${tally.map(([gate, n]) => `${n} ${gate}`).join(', ') || 'No open gates'}. Open the gate board.`,
    children: [
      jsx('style', { href: 'shadow-army-status', precedence: 'shadow-army', children: statusCss }),
      svg('', '0 0 12 12', [jsx('circle', { cx: 6, cy: 6, r: 4.8 }, 'o'), jsx('path', { d: 'M3.6 6h4.8', strokeLinecap: 'round' }, 'b')]),
      jsx('span', { children: label })
    ]
  })
}
function registerStatus(ctx, feed) {
  const area = sdk.STATUSBAR_AREAS?.right
  if (typeof area !== 'string') return
  ctx.register({ id: 'status', area, order: 120, render: () => jsx(GateStatus, { feed }) })
}

// Toasts: a live card whose gate turns BLOCKED, STALE or PASS while you look elsewhere. Never for the trial.
const TOAST = { BLOCKED: 'error', STALE: 'warning', PASS: 'success' }
// A toast for a card whose verdict is landing on an open pane's seal waits until it has landed.
function registerToasts(ctx, feed) {
  if (typeof host.notify !== 'function') return
  feed.onReal((snap, prev) => {
    if (!prev) return
    const before = new Map(list(prev.cards).map(card => [cardKey(card), gateOf(card)]))
    for (const card of list(snap.cards)) {
      const was = before.get(cardKey(card)), gate = gateOf(card)
      if (!was || was === gate || !TOAST[gate]) continue
      const say = () => host.notify({ kind: TOAST[gate], title: `Gate ${gate}: ${str(card.title) || str(card.id)}`, message: whyOf(card), detail: hintOf(card) || undefined })
      const wait = feed.holdLeft(card)
      if (wait > 0) ctx.setTimeout(say, wait)
      else say()
    }
  })
}

// Kanban badges: the gate word on each live card the board shows. Re-registered with the same id when gates change.
const BADGE_TONE = { BLOCKED: 'red', STALE: 'yellow', PASS: 'green', UNVERIFIED: 'sky', UNKNOWN: 'neutral', UNARMED: 'neutral' }
function registerBadges(ctx, feed) {
  const area = sdk.KANBAN_CARD_AREAS?.badges
  if (typeof area !== 'string') return
  let painted = null
  // Kanban cards only: a workspace review has no Kanban card, whatever its id or board.
  const paint = snap => {
    const cards = new Map(list(snap?.cards).filter(card => kanbanCard(card) && BADGE_TONE[gateOf(card)]).map(card => [str(card.id), card]))
    const signature = [...cards].map(([id, card]) => `${id}:${gateOf(card)}:${whyOf(card)}`).join('|')
    if (signature === painted) return
    painted = signature
    ctx.register({ id: 'gate-badge', area, data: { decorate: ({ id }) => {
      const card = cards.get(str(id))
      return card ? { label: `Gate ${gateWord(gateOf(card))}`, tone: BADGE_TONE[gateOf(card)], tooltip: whyOf(card) } : null
    } } })
  }
  paint(null)
  // While an open pane's seal holds a verdict, the board's badges wait for it too.
  let later = null
  feed.onReal(snap => {
    later?.()
    later = null
    const wait = feed.holdLeft()
    if (wait > 0) later = ctx.setTimeout(() => { later = null; paint(feed.real()) }, wait)
    else paint(snap)
  })
}

// Keybind: open the gate board. User-rebindable in Settings.
function registerKeybind(ctx) {
  if (typeof sdk.KEYBINDS_AREA !== 'string') return
  ctx.register({ id: 'open-key', area: sdk.KEYBINDS_AREA, data: { id: 'shadow-army.open-board', label: 'Shadow Army: Open the gate board', category: 'Shadow Army', defaults: ['mod+alt+g'], run: openBoard } })
}

// Transcript card: the agent writes ::shadow-army-gate{card="t_…"} on a paragraph of its own and the live gate
// for that card renders in place. Attributes are untrusted: anything that is not a plain card id renders nothing.
function GateCard({ attrs, feed }) {
  const state = useFeed(feed, true)
  const id = str(attrs?.card).trim()
  if (!/^[\w.:-]{1,96}$/.test(id)) return null
  const find = snap => list(snap?.cards).find(card => kanbanCard(card) && str(card.id) === id)
  const card = (state.snap?.demo ? null : find(state.snap)) || find(feed.real()) || find(state.snap)
  return jsxs('section', { className: 'sa-root sa-inline', 'aria-label': 'Shadow Army gate', children: [
    jsx(PaneStyle, {}),
    card
      ? jsxs('div', { className: 'sa-tile', 'data-gate': gateOf(card), 'data-closed': String(isDone(card)), children: [
          jsxs('div', { className: 'sa-mark', children: [jsx(Seal, { card }), isDone(card) && jsx(Closed, {})] }),
          jsxs('div', { className: 'sa-body', children: [
            jsx('button', { type: 'button', className: 'sa-title', onClick: openBoard, children: str(card.title) || id }),
            jsxs('dl', { className: 'sa-qa', children: [
              jsx('dt', { children: 'Why' }), jsx('dd', { className: 'why', children: whyOf(card) }),
              hintOf(card) && jsx('dt', { children: 'Next' }), hintOf(card) && jsx('dd', { children: hintOf(card) })
            ] })
          ] })
        ] })
      : jsx('p', { className: 'sa-empty', children: state.snap ? `No gate record for card ${id} on this connection.` : 'Reading the gate…' })
  ] })
}
function registerTranscriptCard(ctx, feed) {
  if (typeof sdk.TRANSCRIPT_DIRECTIVE_AREA !== 'string') return
  ctx.register({ id: 'gate-card', area: sdk.TRANSCRIPT_DIRECTIVE_AREA, data: { name: 'shadow-army-gate', render: ({ attrs }) => jsx(GateCard, { attrs, feed }) } })
}

export default {
  id: ID,
  name: 'Shadow Army',
  register(ctx) {
    const feed = createFeed(ctx)
    ctx.onEvent(`plugin.${ID}.gate.changed`, event => feed.onEvent(event?.payload))
    const view = () => jsx(ShadowArmy, { feed })
    ctx.register({ id: 'pane', area: 'panes', title: 'Shadow Army', data: { placement: 'right', width: '440px' }, render: view })
    ctx.register({ id: 'page', area: ROUTES_AREA, data: { path: '/shadow-army' }, render: view })
    ctx.register({ id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/shadow-army', label: 'Shadow Army', codicon: 'organization' } })
    ctx.register({
      id: 'open', area: PALETTE_AREA,
      data: { id: 'shadow-army.open', label: 'Shadow Army: Open the gate board', keywords: ['shadow', 'army', 'gate', 'verify', 'kanban'], run: openBoard }
    })
    registerStatus(ctx, feed)
    registerToasts(ctx, feed)
    registerBadges(ctx, feed)
    registerKeybind(ctx)
    registerTranscriptCard(ctx, feed)
  }
}
