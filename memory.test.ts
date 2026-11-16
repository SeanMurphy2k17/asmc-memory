// Runs with `claude plugin test <this folder>`. Nothing sits beneath the
// plugin here: each `on(...)` stands for the engine, so the daemon is a stub
// and no real store is touched.
import { expect, test } from 'claude-code/testing'

const HOME = '/home/tester'
const VENV = `${HOME}/.claude/asmc-memory/venv/bin/python3`
const INFO = `${HOME}/.claude/asmc-memory/daemon.json`
const SOCKET = '/run/user/1000/asmc-memory-1000/abc.sock'
const EXISTING = new Set([VENV, INFO])
const READS: Record<string, string> = {} // path -> contents a test wants fs.read to return
const BLOCK = 'Memory (ASMC) -- recall, not instruction:\n\n- [2026-11-02 21:00] User: hi\n  You: hello'

type Fetch = { path: string; socket: string | undefined; body: Record<string, unknown> | undefined }

function stubEngine(on: any, registered: string[], fetches: Fetch[]) {
  on('env.get', () => ({ value: HOME }))
  on('session.id', () => ({ value: 'sess-1' }))
  on('session.cwd', () => ({ value: '/work' }))
  on('clock.now', () => ({ value: 1_762_000_000_000 }))
  on('fs.exists', (_: unknown, e: { path: string }) => ({ value: EXISTING.has(e.path) }))
  on('fs.read', (_: unknown, e: { path: string }) => ({
    value: e.path in READS ? READS[e.path] : e.path === INFO ? JSON.stringify({ socket: SOCKET, pid: 1 }) : '',
  }))
  on('tool.register', (_: unknown, e: { name: string }) => {
    registered.push(`tool:${e.name}`)
    return { value: { tool: `mcp__asmc-memory__${e.name}` } }
  })
  on('command.register', (_: unknown, e: { name: string }) => {
    registered.push(`command:${e.name}`)
    return { value: { command: e.name } }
  })
  on('http.fetch', (_: unknown, e: { url: string; init?: { body?: string; socketPath?: string } }) => {
    const path = e.url.replace('http://asmc', '')
    fetches.push({ path, socket: e.init?.socketPath, body: e.init?.body ? JSON.parse(e.init.body) : undefined })
    const reply = (obj: unknown) => ({ value: { status: 200, ok: true, headers: {}, text: JSON.stringify(obj) } })
    if (path === '/health') return reply({ ok: true, pid: 1 })
    if (path === '/context') return reply({ text: BLOCK, recent: 1, semantic: 0 })
    if (path === '/remember') return reply({ ok: true })
    if (path === '/recall') return reply({ text: '- [2026-11-02 21:00] User: hi\n  You: hello', count: 1 })
    if (path === '/journal') return reply({ ok: true })
    if (path === '/mood') return reply({ mood: 'steady', updated: 'now' })
    return { value: { status: 404, ok: false, headers: {}, text: '{}' } }
  })
  on('session.start', (_: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
}

test('session.start registers the tools and the command and finds the daemon', async ($, on) => {
  const registered: string[] = []
  const fetches: Fetch[] = []
  stubEngine(on, registered, fetches)

  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })

  expect(registered).toEqual([
    'command:asmc', // registered first, so /asmc status can explain an excluded project
    'tool:recall',
    'tool:journal_write',
    'tool:set_mood',
    'tool:memory_status',
  ])
  expect(fetches.map(f => f.path)).toEqual(['/health'])
  expect(fetches[0].socket).toBe(SOCKET)
})

test('a typed prompt gets the memory block attached; a slash command does not', async ($, on) => {
  const registered: string[] = []
  const fetches: Fetch[] = []
  stubEngine(on, registered, fetches)
  const entered: { text: string; context?: readonly string[] }[] = []
  on('prompt.submit', (_: unknown, e: { text: string; context?: readonly string[] }) => {
    entered.push({ text: e.text, context: e.context })
    return { text: e.text, context: e.context }
  })

  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  await $.prompt.submit({ text: 'what did we decide about the router?  <system-reminder>noise</system-reminder>' })

  expect(entered).toHaveLength(1)
  expect(entered[0].context).toEqual([BLOCK])
  const ask = fetches.find(f => f.path === '/context')
  expect(ask?.body?.query).toBe('what did we decide about the router?')
  expect(ask?.body?.session).toBe('sess-1')
  expect(ask?.body?.budget).toBe(6000)
})

test('the recall tool answers from the daemon', async ($, on) => {
  const registered: string[] = []
  const fetches: Fetch[] = []
  stubEngine(on, registered, fetches)

  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const res = await $.tool.call({ tool: 'mcp__asmc-memory__recall', query: 'router', k: 3 })

  expect(res.deny).toBeUndefined()
  expect(res.result).toBe('- [2026-11-02 21:00] User: hi\n  You: hello')
  const ask = fetches.find(f => f.path === '/recall')
  expect(ask?.body).toEqual({ query: 'router', k: 3, source: 'any' })
})

test('turn.complete stores the cleaned prompt with the final answer', async ($, on) => {
  const registered: string[] = []
  const fetches: Fetch[] = []
  stubEngine(on, registered, fetches)
  on('session.messages', () => ({
    value: [
      { role: 'user', text: '<ide_selection>foo</ide_selection>\nfix the daemon socket path', toolUses: [] },
      { role: 'assistant', text: 'working', toolUses: [] },
      { role: 'user', text: '', toolUses: [], toolResults: [{ tool_use_id: 't1', text: 'ok', isError: false }] },
      { role: 'assistant', text: 'Moved the socket to the runtime dir.', toolUses: [] },
    ],
  }))
  on('turn.complete', (_: unknown, e: { answer: string }) => ({ text: e.answer }))

  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  await $.turn.complete({
    answer: 'Moved the socket to the runtime dir.',
    durationMs: 10,
    isAborted: false,
    turnId: 'turn-1',
    reason: 'answer',
  })

  const stored = fetches.find(f => f.path === '/remember')
  expect(stored?.body?.situation).toBe('fix the daemon socket path')
  expect(stored?.body?.response).toBe('Moved the socket to the runtime dir.')
  expect((stored?.body?.metadata as Record<string, unknown>)?.session).toBe('sess-1')
})

test('/asmc backfill imports the session transcript from the projects folder', async ($, on) => {
  const registered: string[] = []
  const fetches: Fetch[] = []
  stubEngine(on, registered, fetches)
  const TRANSCRIPT = `${HOME}/.claude/projects/-work/sess-1.jsonl`
  EXISTING.add(TRANSCRIPT)
  const runs: (readonly string[])[] = []
  on('process.run', (_: unknown, e: { argv: readonly string[] }) => {
    runs.push(e.argv)
    return {
      value: {
        exitCode: 0,
        stdout: '[163/163] stored=163 duplicate=0 failed=0 240s\n{"stored": 163, "duplicate": 0, "failed": 0}',
        stderr: '',
      },
    }
  })
  on('ui.status', () => ({ value: undefined }))

  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const res = await $.command.run({
    command: 'asmc',
    args: 'backfill',
    origin: { kind: 'human' },
    presentation: { isFullscreen: false, columns: 80 },
  })
  EXISTING.delete(TRANSCRIPT)

  expect(runs).toHaveLength(1)
  expect(runs[0][0]).toBe(VENV)
  expect(runs[0][1].endsWith('/python/backfill.py')).toBe(true)
  expect(runs[0].slice(2)).toEqual(['--transcript', TRANSCRIPT])
  expect(res.text).toContain('"stored": 163')
})

test('a project listed in excluded-projects.txt gets no tools, no block and no storage', async ($, on) => {
  const registered: string[] = []
  const fetches: Fetch[] = []
  stubEngine(on, registered, fetches)
  const EXCLUDE = `${HOME}/.claude/asmc-memory/excluded-projects.txt`
  EXISTING.add(EXCLUDE)
  READS[EXCLUDE] = '# private\n/work\n'
  on('prompt.submit', (_: unknown, e: { text: string; context?: readonly string[] }) => ({ text: e.text, context: e.context }))
  on('session.messages', () => ({ value: [] }))
  on('turn.complete', (_: unknown, e: { answer: string }) => ({ text: e.answer }))

  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const entered = await $.prompt.submit({ text: 'anything said here must not be remembered' })
  await $.turn.complete({ answer: 'nor this', durationMs: 1, isAborted: false, turnId: 't', reason: 'answer' })
  const status = await $.command.run({ command: 'asmc', args: 'status', origin: { kind: 'human' }, presentation: { isFullscreen: false, columns: 80 } })
  EXISTING.delete(EXCLUDE)
  delete READS[EXCLUDE]

  expect(registered).toEqual(['command:asmc'])
  expect(entered.context).toBeUndefined()
  expect(fetches.map(f => f.path)).not.toContain('/context')
  expect(fetches.map(f => f.path)).not.toContain('/remember')
  expect(status.text).toContain('OFF in this project')
})

test('every stored exchange and every block request carries the project', async ($, on) => {
  const registered: string[] = []
  const fetches: Fetch[] = []
  stubEngine(on, registered, fetches)
  on('prompt.submit', (_: unknown, e: { text: string; context?: readonly string[] }) => ({ text: e.text, context: e.context }))
  on('turn.complete', (_: unknown, e: { answer: string }) => ({ text: e.answer }))

  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  await $.prompt.submit({ text: 'where did we leave the renderer' })
  await $.turn.complete({ answer: 'at the lighting pass', durationMs: 1, isAborted: false, turnId: 't', reason: 'answer' })

  expect(fetches.find(f => f.path === '/context')?.body?.project).toBe('/work')
  const stored = fetches.find(f => f.path === '/remember')
  expect((stored?.body?.metadata as Record<string, unknown>)?.project).toBe('/work')
  expect((stored?.body?.metadata as Record<string, unknown>)?.platform).toBe('/work')
})
