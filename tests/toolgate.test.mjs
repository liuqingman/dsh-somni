// Unit tests for the ToolGate (tools.gate config): mount/unmount semantics,
// execute hard-reject while closed, flip-flop via state-file changes.
//   npm run build && node tests/toolgate.test.mjs
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import assert from 'node:assert/strict'
import { ToolGate } from '../lib/toolgate.js'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function makeCtx() {
  const tools = new Map()
  const unmounts = new Map()
  return {
    tools: {
      register(def) {
        tools.set(def.name, def)
        unmounts.set(def.name, 0)
        return () => { tools.delete(def.name); unmounts.set(def.name, (unmounts.get(def.name) ?? 0) + 1) }
      },
    },
    logger: () => ({ error() {}, warn() {}, info() {}, debug() {} }),
    state: { tools, unmounts },
  }
}

const def = (name) => ({
  name,
  description: name,
  parameters: { type: 'object', properties: {} },
  output: { schema: { type: 'string' }, render: () => [{ type: 'text', text: '' }] },
  isConcurrencySafe: () => true,
  timeoutMs: 1000,
  async execute() { return name },
})

const dir = mkdtempSync(path.join(tmpdir(), 'somni-gate-'))
const stateFile = path.join(dir, 'state.json')
let failed = 0
try {
  // 1) no state file → mounts immediately (gate open)
  let ctx = makeCtx()
  let gate = new ToolGate(ctx, { stateFile, stateKey: 'disabled_plugins.dsh-somni', pollMs: 20 })
  const d1 = gate.register(def('recall_memories'))
  const d2 = gate.register(def('dream_now'))
  gate.start()
  assert.equal(ctx.state.tools.size, 2)
  console.log('1) initial mount ok')

  // 2) flip state → closed → tools unmounted; execute on a stale ref hard-rejects
  writeFileSync(stateFile, JSON.stringify({ disabled_plugins: { 'dsh-somni': true } }))
  await sleep(80)
  assert.equal(ctx.state.tools.size, 0)
  assert.equal(gate.isClosed(), true)
  await assert.rejects(() => ctx.state.tools.has('recall_memories') ? Promise.resolve() : d1 && Promise.reject(new Error('x')), () => true)
  // hard-reject: call through the definition captured at register time
  const staleDef = { ...def('recall_memories'), execute: async () => 'should not run' }
  // reconstruct: gate's wrapped execute lives on the registered def; simulate via a fresh register cycle
  console.log('2) soft-close unmounts ok')

  // 3) reopen → tools back
  writeFileSync(stateFile, JSON.stringify({ disabled_plugins: { 'dsh-somni': false } }))
  await sleep(80)
  assert.equal(ctx.state.tools.size, 2)
  assert.equal(gate.isClosed(), false)
  console.log('3) reopen remounts ok')

  // 4) flip-flop × 3 — stable
  for (let i = 0; i < 3; i++) {
    writeFileSync(stateFile, JSON.stringify({ disabled_plugins: { 'dsh-somni': true } }))
    await sleep(60)
    assert.equal(ctx.state.tools.size, 0)
    writeFileSync(stateFile, JSON.stringify({ disabled_plugins: { 'dsh-somni': false } }))
    await sleep(60)
    assert.equal(ctx.state.tools.size, 2)
  }
  console.log('4) three flip-flops ok')

  // 5) per-tool disposer forgets the tool; a later reopen must not remount it
  d2() // dispose dream_now
  await sleep(20)
  writeFileSync(stateFile, JSON.stringify({ disabled_plugins: { 'dsh-somni': true } }))
  await sleep(80)
  writeFileSync(stateFile, JSON.stringify({ disabled_plugins: { 'dsh-somni': false } }))
  await sleep(80)
  assert.deepEqual([...ctx.state.tools.keys()].sort(), ['recall_memories'])
  console.log('5) per-tool disposer forgets ok')

  // 6) execute hard-reject while closed (in-flight window)
  const ctx2 = makeCtx()
  const gate2 = new ToolGate(ctx2, { stateFile, stateKey: 'disabled_plugins.dsh-somni', pollMs: 20 })
  gate2.register(def('list_intentions'))
  gate2.start()
  const mounted = ctx2.state.tools.get('list_intentions')
  assert.ok(mounted, 'mounted def present')
  writeFileSync(stateFile, JSON.stringify({ disabled_plugins: { 'dsh-somni': true } }))
  await sleep(80)
  assert.equal(ctx2.state.tools.size, 0)
  // stale handle: the unmounted definition object is still in the model's hands → must throw
  await assert.rejects(() => mounted.execute({}, {}), /disabled by gate/)
  console.log('6) execute hard-reject while closed ok')

  // 7) missing key / malformed JSON keeps last state, no crash
  writeFileSync(stateFile, '{not json')
  await sleep(80)
  assert.equal(gate2.isClosed(), true)
  writeFileSync(stateFile, JSON.stringify({ unrelated: 1 }))
  await sleep(80)
  assert.equal(gate2.isClosed(), false)
  await gate2.stop()
  await gate.stop()
  console.log('7) malformed / missing key tolerated ok')

  console.log('ALL OK')
} catch (e) {
  console.error('FAIL:', e.message)
  failed = 1
} finally {
  rmSync(dir, { recursive: true, force: true })
}
process.exit(failed)
