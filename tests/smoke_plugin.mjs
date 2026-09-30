// End-to-end smoke for the built plugin (lib/) against a minimal fake harness
// context. Exercises: sidecar spawn via ctx.subprocess, capture → session-log,
// tool registration + execution, prompt sections, pre-step waterfall,
// session close, and a full dream through the host-LLM bridge (fake ctx.llm).
//
//   npm run build && node tests/smoke_plugin.mjs
import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync, readdirSync, readFileSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import assert from 'node:assert/strict'

const PY = process.env.PYTHON || 'python3.10'
const plugin = await import('../lib/index.js')

// ---------- fake harness ----------
const handlers = new Map()
const sections = new Map()
const tools = new Map()
const effects = []
let llmCalls = 0

function makeLogger(name) {
  const out = (lvl) => (fmt, ...args) => {
    if (lvl === 'debug') return
    console.log(`[${lvl}] ${name}:`, fmt, ...args)
  }
  return { error: out('error'), warn: out('warn'), info: out('info'), debug: out('debug') }
}

function text(t) { return { type: 'text', text: t } }
function msgText(m) { return (m.content ?? []).filter((b) => b.type === 'text').map((b) => b.text).join('\n') }

async function* fakeStream(opts) {
  llmCalls += 1
  const toolNames = (opts.tools ?? []).map((t) => t.name)
  assert.ok(toolNames.includes('mark_log_processed') && toolNames.includes('write_dream_log'), `dream tools missing: ${toolNames}`)
  assert.equal(opts.provider, 'fake')
  let pendingPath
  for (const m of opts.messages) {
    if (m.role !== 'user') continue
    for (const tok of msgText(m).split(/\s+/)) {
      if (tok.endsWith('.md') && tok.includes('/session-log/')) pendingPath = tok.replace(/[`,;]/g, '')
    }
  }
  const call = (name, args) => ({ type: 'tool-call', id: `call_${llmCalls}`, name, arguments: JSON.stringify(args) })
  let block
  if (llmCalls === 1) block = call('read_session_log', { path: pendingPath })
  else if (llmCalls === 2) block = call('mark_log_processed', { path: pendingPath })
  else if (llmCalls === 3) block = call('write_dream_log', { content: '---\ndream_id: smoke-ts\n---\n\n## 提取结果\n（TS 冒烟）' })
  else block = text('## 🌙 睡梦总结\n\n**处理日志**: 1 个 session-log（TS 冒烟）')
  yield { type: 'block-start', index: 0, blockType: block.type }
  if (block.type === 'text') yield { type: 'text-delta', index: 0, text: block.text }
  yield { type: 'block-end', index: 0, block }
  yield { type: 'usage', usage: { inputTokens: 11, outputTokens: 7 } }
  yield { type: 'finish', reason: block.type === 'text' ? 'stop' : 'tool-calls' }
}

const ctx = {
  logger: makeLogger,
  on(event, fn) {
    if (!handlers.has(event)) handlers.set(event, [])
    handlers.get(event).push(fn)
    return () => true
  },
  effect(fn) {
    const dispose = fn()
    effects.push(dispose)
    return dispose
  },
  subprocess: {
    spawn(spec) {
      const child = spawn(spec.argv[0], spec.argv.slice(1), {
        cwd: spec.cwd, env: { ...process.env, ...spec.env }, stdio: ['pipe', 'pipe', 'pipe'],
      })
      const done = new Promise((resolve) => child.on('exit', (exitCode, signal) => resolve({ exitCode, signal })))
      return {
        pid: child.pid, stdin: child.stdin, stdout: child.stdout, stderr: child.stderr, collected: {}, done,
        terminate() { child.kill('SIGTERM') },
        async waitForExit() { await done; return true },
      }
    },
  },
  systemPrompt: { section(s) { sections.set(s.name, s); return () => true }, context() { return () => true } },
  tools: { register(def) { tools.set(def.name, def); return () => true } },
  agents: { list: () => [] },
  llm: { stream: fakeStream },
  agentDefaultModel: { currentSelection: () => ({ provider: 'fake', model: 'fake-1' }) },
}

async function emit(event, ...args) {
  for (const fn of handlers.get(event) ?? []) await fn(...args)
}
async function waterfall(event, payload, final) {
  const fns = handlers.get(event) ?? []
  const run = (i) => (i >= fns.length ? final() : fns[i](payload, () => run(i + 1)))
  return run(0)
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
async function waitFor(pred, ms, what) {
  const t0 = Date.now()
  while (!pred()) {
    if (Date.now() - t0 > ms) throw new Error(`timeout waiting for ${what}`)
    await sleep(100)
  }
}

// ---------- run ----------
const dataDir = mkdtempSync(path.join(tmpdir(), 'somni-ts-smoke-'))
let exitCode = 1
try {
  const cfg = plugin.Config({
    dataDir, python: PY, logLevel: 'warning',
    embed: { provider: 'off' },
    dream: { idleMs: 60_000, minPendingLogs: 1 },
  })
  assert.equal(plugin.name, 'dsh-somni')
  plugin.apply(ctx, cfg)

  await waitFor(() => tools.has('recall_memories') && tools.has('dream_now'), 30_000, 'tool registration')
  console.log('tools registered:', [...tools.keys()].sort().join(', '))
  assert.equal(tools.get('recall_memories').parameters.properties.limit.type, 'integer')

  // 1) capture a short conversation
  const session = { id: 'sess-ts-1', header: {} }
  const exec = { agent: { id: session.id } }
  let seq = 0
  const ev = (type, data) => ({ type, seq: ++seq, time: Date.now(), data })
  await emit('session/event', session, ev('user/message', { id: 'm1', role: 'user', content: [text('帮我看下 nginx 502')], source: { kind: 'user' } }))
  await emit('session/event', session, ev('assistant/message', { turn: 1, step: 1, message: { id: 'm2', role: 'assistant', content: [text('先看 upstream 日志')], source: { kind: 'model', provider: 'fake', model: 'fake-1' } }, stream: [] }))
  await emit('session/event', session, ev('tool/call', { turn: 1, step: 1, callId: 'c1', name: 'exec-shell', arguments: '{"command":"tail error.log"}' }))
  await emit('session/event', session, ev('tool/result', { turn: 1, step: 1, message: { id: 'm3', role: 'tool', toolCallId: 'c1', content: [text('upstream timed out')], source: { kind: 'tool', callId: 'c1' } } }))
  await emit('session/event', session, ev('turn/end', { turn: 1, reason: 'stop' }))
  await emit('session/flush', session)
  console.log('capture ok')

  // 2) tools: add intention, recall recent conversation
  const r1 = await tools.get('add_intention').execute({ what: '复查 nginx 超时', cue: 'nginx,502' }, exec)
  assert.match(r1, /pi-/)
  const r2 = await tools.get('search_recent_conversation').execute({ query: 'nginx' }, exec)
  assert.match(r2, /502/)
  console.log('tools ok')

  // 3) pre-step waterfall refreshes prompt sections (intention cue fires) and may append a hint
  const userMsg = { id: 'm4', role: 'user', content: [text('nginx 又 502 了')], source: { kind: 'user' } }
  const decision = await waterfall('agent/pre-step',
    { agent: { id: session.id }, messages: [userMsg], turn: 2, step: 1, signal: new AbortController().signal },
    async () => ({ kind: 'enter', messages: [userMsg] }))
  assert.equal(decision.kind, 'enter')
  assert.ok(decision.messages.length >= 1)
  const intentions = sections.get('somni:intentions').text({})
  const discipline = sections.get('somni:discipline').text({})
  assert.match(intentions, /nginx/)
  assert.match(discipline, /回忆纪律/)
  console.log('prompt sections ok; assoc hint appended:', decision.messages.length > 1)

  // 4) dispose the session → log closed on disk
  await emit('session/disposed', session)
  await waitFor(() => existsSync(path.join(dataDir, 'memory', 'session-log')) &&
    readdirSync(path.join(dataDir, 'memory', 'session-log')).some((f) => f.endsWith('.md')), 10_000, 'session-log file')
  const logFile = readdirSync(path.join(dataDir, 'memory', 'session-log')).find((f) => f.endsWith('.md'))
  const body = readFileSync(path.join(dataDir, 'memory', 'session-log', logFile), 'utf8')
  assert.match(body, /status: pending/)
  assert.match(body, /exec-shell/)
  console.log('session-log ok:', logFile)

  // 5) dream through the host-LLM bridge
  const summary = await tools.get('dream_now').execute({}, exec)
  console.log('dream_now →', summary.split('\n')[0])
  assert.match(summary, /睡梦总结/)
  assert.ok(llmCalls >= 4, `llmCalls=${llmCalls}`)
  assert.ok(readdirSync(path.join(dataDir, 'memory', 'dream-log')).some((f) => f.endsWith('.md')), 'dream-log missing')
  assert.ok(!readdirSync(path.join(dataDir, 'memory', 'session-log')).some((f) => f.endsWith('.md')), 'session-log not archived')
  console.log('dream ok: llmCalls =', llmCalls)

  // 6) teardown
  for (const d of effects) await d()
  console.log('ALL OK')
  exitCode = 0
} finally {
  rmSync(dataDir, { recursive: true, force: true })
}
process.exit(exitCode)
