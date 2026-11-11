import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import assert from 'node:assert/strict'
const file = new URL('./shadow-army/desktop/plugin.js', import.meta.url)
const source = readFileSync(file, 'utf8').replace(/^import .*$/gm, '').replace(/^export default /m, 'globalThis.__plugin = ')
const element = (type, props) => typeof type === 'function' ? type(props) : { type, props }
const sandbox = { jsx: element, jsxs: element, useState: v => [v, () => {}], useEffect() {}, useRef: () => ({}), useValue: () => null, host: {}, sdk: {}, PALETTE_AREA: 'p', ROUTES_AREA: 'r', SIDEBAR_NAV_AREA: 'n', console }
sandbox.globalThis = sandbox
vm.runInNewContext(source + '\nglobalThis.polish = { Setup, Removed, HeroSide, RailEvent, VerdictRow };', sandbox)
const text = node => node == null || node === false ? '' : Array.isArray(node) ? node.map(text).join(' ') : typeof node === 'object' ? text(node.props?.children) : String(node)
const configured = { armed: true, setup: [{ id: 'repo', ok: true }, { id: 'families', ok: true }], cards: [{ state: 'done', gate: { state: 'PASS' } }] }
const header = text(sandbox.polish.Setup({ snap: configured, open: false }))
assert.match(header, /Configured/)
assert.match(header, /Armed/)
assert.match(header, /No open code card\./)
const broken = { ...configured, setup: [{ id: 'repo', ok: false, hint: 'Missing repository' }] }
assert.match(text(sandbox.polish.Setup({ snap: broken, open: false })), /Not configured/)
console.log('PASS pane setup configuration and no-open-card wording')

const unavailable = 'Test scan unavailable: review the test diff by hand'
for (const row of [{ weakened_count: null, skipped_count: null }, { weakened_count: 0, skipped_count: null }, { weakened_count: null, skipped_count: 0 }]) {
  assert.ok(text(sandbox.polish.Removed({ row })).includes(unavailable))
}
assert.equal(text(sandbox.polish.Removed({ row: { weakened_count: 0, skipped_count: 0 } })).trim(), '')
assert.equal(text(sandbox.polish.Removed({ row: {} })).trim(), '')
const evidence = text(sandbox.polish.HeroSide({ card: { gate: { state: 'UNVERIFIED', weakened_count: null, skipped_count: null }, history: [], shadows: [] }, now: Date.now() }))
assert.ok(evidence.includes(unavailable), 'the evidence panel shows unavailable scans even without an event')
console.log('PASS pane distinguishes unavailable scans from clean zero')

const finished = { kind: 'finished', duty: 'build', tree: 'abc123', weakened_count: 1, weakened: [{ file: 'test_cart.py', line: 1, text: 'assert total == 0', kind: 'removed' }] }
const card = { gate: { state: 'UNVERIFIED', tree: 'def456' }, history: [finished] }
const ledger = text(sandbox.polish.RailEvent({ card, row: finished, now: Date.now() }))
assert.match(ledger, /Builder finished/)
assert.match(ledger, /started from code version abc123/)
assert.match(ledger, /1 assertion removed/)
for (const compact of [false, true]) {
  assert.match(text(sandbox.polish.VerdictRow({ card, row: finished, now: Date.now(), compact })), /started from code version abc123/)
}
const verdict = { kind: 'verdict', verdict: 'PASS', tree: 'def456' }
const verdictText = text(sandbox.polish.RailEvent({ card, row: verdict, now: Date.now() }))
assert.match(verdictText, /code version def456/)
assert.doesNotMatch(verdictText, /started from/)
console.log('PASS finished rows label the starting tree in ledger and evidence')
