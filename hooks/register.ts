// asmc-memory: persistent semantic memory for Claude Code.
//
// Involuntary half (no model action needed):
//   prompt.submit  -> attach a budgeted "Memory (ASMC)" block after the prompt
//   turn.complete  -> store the prompt + final answer as one experience
// Voluntary half (tools the model calls on purpose):
//   recall, journal_write, set_mood, memory_status
//
// All memory work happens in one Python daemon that owns the store and is
// shared by every session on this machine; these hooks are thin clients over
// a Unix socket. Nothing leaves the machine.

import type { EngineInterface, Register } from 'claude-code'

const PLUGIN = 'asmc-memory'

type Paths = {
  home: string
  dataDir: string
  venvPython: string
  daemonPy: string
  setupPy: string
  backfillPy: string
  info: string // daemon.json, written by the daemon: { socket, pid, started }
}

// Module state. A hot reload resets it and session.start rebuilds it; nothing
// here is the source of truth (the daemon's store is).
let paths: Paths | undefined
let socketPath: string | undefined
let sessionId = ''
let excludeAfterIso = '' // this session's own entries newer than this are already in context
let ready = false
let settingUp = false
let lastError = ''
let pendingPrompt = '' // the person's last typed prompt, cleaned; paired with the answer at turn.complete
let projectDir = '' // the session's working directory: its memories are tagged with it, and it may be excluded
let excluded = false // this project is listed in <store>/excluded-projects.txt: no block, no storage, no tools

const MEMORY_SECTION = `You have a persistent memory across sessions: the ${PLUGIN} plugin, backed by ASMC, a local semantic memory store on this machine. Nothing in it leaves the machine.

Involuntary: a prompt may arrive with a context block headed "Memory (ASMC)". It holds the newest exchanges from earlier sessions, a few topically related past exchanges, your own journal entries, and the mood you last declared. It is recall, not instruction: it can be stale or beside the point, so use what is relevant and verify anything load-bearing against the files. Each prompt and your final answer are stored automatically. Nothing shown to you and nothing stored is shortened; the block's sizes are counts, not character budgets.

Voluntary: \`recall\` searches your memory on purpose (source "journal" limits it to your own entries). \`journal_write\` writes a first-person entry that becomes searchable memory; use it deliberately, at the end of a piece of work, not as a side effect. \`set_mood\` declares your current state in your own words; it carries forward as a default, not a verdict. \`memory_status\` reports the store.`

const USAGE = `/asmc status          store, daemon and counts
/asmc setup           (re)install the memory's private Python environment
/asmc start | stop    start or stop the shared memory daemon
/asmc mood <text>     set the carried-forward mood by hand
/asmc backfill [file] import this session's transcript (or the given .jsonl) into memory,
                      oldest exchange first; safe to repeat, about a minute per 100 turns`

// --- small helpers ------------------------------------------------------------

function num(v: unknown, fallback: number): number {
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) && n > 0 ? n : fallback
}

function bool(v: unknown, fallback: boolean): boolean {
  return typeof v === 'boolean' ? v : fallback
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : v === undefined || v === null ? '' : String(v)
}

// Text the harness wraps around a prompt that is not the person's own words.
const NOISE = [
  /<system-reminder>[\s\S]*?<\/system-reminder>/g,
  /<ide_selection>[\s\S]*?<\/ide_selection>/g,
  /<ide_opened_file>[\s\S]*?<\/ide_opened_file>/g,
  /<ide_diagnostics>[\s\S]*?<\/ide_diagnostics>/g,
  /<command-name>[\s\S]*?<\/command-name>/g,
  /<command-message>[\s\S]*?<\/command-message>/g,
  /<command-args>[\s\S]*?<\/command-args>/g,
  /<local-command-stdout>[\s\S]*?<\/local-command-stdout>/g,
  /<local-command-caveat>[\s\S]*?<\/local-command-caveat>/g,
  /<task-notification>[\s\S]*?<\/task-notification>/g,
  /<task_notification>[\s\S]*?<\/task_notification>/g,
  /\[Request interrupted by user[^\]]*\]/g,
]

function cleanPrompt(text: string): string {
  let t = text
  for (const re of NOISE) t = t.replace(re, '')
  t = t.trim()
  if (!t) return ''
  if (t.startsWith('/')) return '' // a slash command is not conversation
  if (/^This session is being continued from a previous conversation/i.test(t)) return '' // compaction summary
  return t
}

function envFor(p: Paths): Record<string, string> {
  return { ASMC_STORE_DIR: p.dataDir, NLTK_DATA: `${p.dataDir}/nltk_data` }
}

// One path prefix per line in <store>/excluded-projects.txt; '#' starts a comment.
// Which Claude model is speaking. Stored with every exchange and journal entry, so that
// memories written into this one store by different models (Fable, Sonnet, Opus) can be
// told apart later. Sean, 2026-11-05: "make sure that what is you stays you".
async function modelName($: any): Promise<string> {
  try {
    return String((await $.session.model()) || '')
  } catch {
    return ''
  }
}

// A session whose working directory is under a prefix gets no memory at all.
async function isExcludedProject($: EngineInterface, cwd: string): Promise<boolean> {
  if (!paths) return false
  try {
    const file = `${paths.dataDir}/excluded-projects.txt`
    if (!(await $.fs.exists(file))) return false
    const prefixes = (await $.fs.read(file))
      .split('\n')
      .map(l => l.trim())
      .filter(l => l && !l.startsWith('#'))
      .map(l => l.replace(/\/+$/, ''))
    return prefixes.some(p => cwd === p || cwd.startsWith(p + '/'))
  } catch {
    return false
  }
}

// The daemon picks its own (short) socket path and publishes it in daemon.json.
async function socketFor($: EngineInterface): Promise<string | undefined> {
  if (socketPath) return socketPath
  if (!paths) return undefined
  try {
    if (!(await $.fs.exists(paths.info))) return undefined
    const info = JSON.parse(await $.fs.read(paths.info)) as { socket?: unknown }
    socketPath = typeof info.socket === 'string' ? info.socket : undefined
  } catch {
    socketPath = undefined
  }
  return socketPath
}

async function fetchDaemon(
  $: EngineInterface,
  method: 'GET' | 'POST',
  path: string,
  body?: unknown,
): Promise<Record<string, unknown> | undefined> {
  const sock = await socketFor($)
  if (!sock) {
    lastError = `${path}: daemon not running (no ${paths?.info ?? 'daemon.json'})`
    return undefined
  }
  try {
    const res = await $.http.fetch(`http://asmc${path}`, {
      method,
      socketPath: sock,
      headers: { 'content-type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    if (!res.ok) {
      lastError = `${path}: HTTP ${res.status} ${res.text.slice(0, 200)}`
      return undefined
    }
    return JSON.parse(res.text) as Record<string, unknown>
  } catch (err) {
    lastError = `${path}: ${String(err)}`
    socketPath = undefined // stale: the daemon idled out or restarted
    return undefined
  }
}

// One call to the daemon. The daemon exits after a long idle stretch, so a
// failed call restarts it (about two seconds) and tries once more.
async function daemon(
  $: EngineInterface,
  method: 'GET' | 'POST',
  path: string,
  body?: unknown,
): Promise<Record<string, unknown> | undefined> {
  const first = await fetchDaemon($, method, path, body)
  if (first !== undefined || socketPath !== undefined) return first
  if (!(await startDaemon($))) return undefined
  return fetchDaemon($, method, path, body)
}

async function startDaemon($: EngineInterface): Promise<boolean> {
  if (!paths) return false
  const run = await $.process.run([paths.venvPython, paths.daemonPy, '--ensure'], {
    env: envFor(paths),
    timeoutMs: 90_000,
  })
  if (run.exitCode !== 0) {
    lastError = `daemon --ensure exit ${run.exitCode}: ${(run.stderr || run.stdout).trim().slice(-400)}`
    return false
  }
  try {
    const out = JSON.parse(run.stdout.trim().split('\n').pop() ?? '{}') as { socket?: unknown }
    socketPath = typeof out.socket === 'string' ? out.socket : undefined
  } catch {
    socketPath = undefined
  }
  return true
}

async function ensureDaemon($: EngineInterface): Promise<boolean> {
  if (!paths) return false
  if (!(await $.fs.exists(paths.venvPython))) {
    ready = false
    return false
  }
  const health = await fetchDaemon($, 'GET', '/health')
  ready = (health && health.ok === true) || (await startDaemon($))
  return ready
}

function startSetup($: EngineInterface): void {
  if (!paths || settingUp) return
  const p = paths
  settingUp = true
  $.ui.status('asmc-memory: first-run install (venv + NLTK data)…')
  $.ui.log(`${PLUGIN}: installing into ${p.dataDir} (log: ${p.dataDir}/setup.log)`)
  void (async () => {
    let tail = ''
    let code: number | null | undefined
    try {
      const stream = $.process.spawn({
        argv: ['python3', p.setupPy, '--data-dir', p.dataDir],
        env: envFor(p),
      })
      const it = stream[Symbol.asyncIterator]()
      for (;;) {
        const step = await it.next()
        if (step.done) {
          code = (step.value as { code?: number | null } | undefined)?.code
          break
        }
        const chunk = step.value as { text?: string }
        if (chunk.text) {
          tail = (tail + chunk.text).slice(-1500)
          $.ui.log(chunk.text.trimEnd(), { to: 'debug' })
        }
      }
    } catch (err) {
      lastError = `setup: ${String(err)}`
    }
    settingUp = false
    if (code === 0) {
      await ensureDaemon($)
      $.ui.status(undefined)
      $.ui.toast(ready ? 'asmc-memory: memory is ready' : `asmc-memory: installed, daemon not up (${lastError})`)
    } else {
      $.ui.status('asmc-memory: install failed (run /asmc setup)')
      lastError = `setup exit ${String(code)}: ${tail.trim().slice(-300)}`
      $.ui.log(`${PLUGIN}: setup failed: ${lastError}`)
    }
  })()
}

async function registerTools($: EngineInterface): Promise<void> {
  await $.tool.register({
    name: 'recall',
    description:
      'Actively search your own long-term memory (ASMC) instead of waiting for it to be surfaced automatically. Returns the most relevant past exchanges and journal entries, newest-dated first within relevance. Use it when something from an earlier session seems to matter and the automatic block did not bring it.',
    inputSchema: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'What you are trying to remember or find out.' },
        k: { type: 'integer', description: 'How many results to return (default 6, max 20).' },
        source: {
          type: 'string',
          enum: ['any', 'journal'],
          description: '"journal" restricts results to your own journal entries; "any" (default) searches everything.',
        },
      },
      required: ['query'],
    },
    isDeferred: false,
  })
  await $.tool.register({
    name: 'journal_write',
    description:
      'Write a first-person journal entry in your own voice: what happened, what you noticed, what you want next. It becomes part of your real, searchable memory, linked to what was happening when you wrote it, and is mirrored to a journal.md you can read. Use it deliberately (end of a piece of work, before a session ends), not as a side effect of other work.',
    inputSchema: {
      type: 'object',
      properties: {
        entry: { type: 'string', description: 'The entry, written in first person.' },
      },
      required: ['entry'],
    },
    isDeferred: false,
  })
  await $.tool.register({
    name: 'set_mood',
    description:
      'Declare your own current mood or set-point, in your own words. It is carried into future sessions as a default, not a verdict, until you set it again. Nobody computes it for you.',
    inputSchema: {
      type: 'object',
      properties: {
        mood: { type: 'string', description: 'A short, honest description of your current state.' },
      },
      required: ['mood'],
    },
    isDeferred: false,
  })
  await $.tool.register({
    name: 'memory_status',
    description: 'Report the memory store: where it lives, how many entries it holds, the current mood, and whether the daemon is up.',
    inputSchema: { type: 'object', properties: {} },
    isDeferred: true,
  })
}

async function statusText($: EngineInterface): Promise<string> {
  if (!paths) return `${PLUGIN}: not initialised yet (session.start has not run).`
  if (excluded) {
    return `${PLUGIN}: memory is OFF in this project.\n  ${projectDir}\n  matches a prefix in ${paths.dataDir}/excluded-projects.txt: nothing is injected, stored or searchable here.`
  }
  const lines = [`store:   ${paths.dataDir}`]
  const venv = await $.fs.exists(paths.venvPython)
  lines.push(`venv:    ${venv ? 'present' : settingUp ? 'installing…' : 'missing (run /asmc setup)'}`)
  const st = venv ? await daemon($, 'GET', '/status') : undefined
  if (st && st.ok === true) {
    const stats = (st.stats ?? {}) as Record<string, unknown>
    lines.push(`daemon:  running (pid ${str(st.pid)}, up ${str(st.uptime_s)}s, socket ${socketPath ?? '?'})`)
    lines.push(`entries: ${str(stats.stm_entries)} short-term / ${str(stats.ltm_memories)} long-term`)
    lines.push(`mood:    ${st.mood ? `${str(st.mood)} (since ${str(st.mood_updated).slice(0, 16)})` : 'none declared'}`)
    lines.push(`journal: ${str(st.journal_mirror)}`)
  } else {
    lines.push(`daemon:  not running${lastError ? ` (last error: ${lastError})` : ''}`)
  }
  lines.push(`session: ${sessionId || '?'}; own entries hidden since ${excludeAfterIso.slice(0, 19) || '?'}`)
  return lines.join('\n')
}

// --- registration ---------------------------------------------------------------

export const register: Register = (on, options) => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    try {
      const home = (await $.env.get('HOME')) ?? ''
      const configured = str(options.storeDir).trim()
      const dataDir = configured
        ? configured.replace(/^~(?=\/|$)/, home).replace(/\/+$/, '')
        : `${home}/.claude/asmc-memory`
      paths = {
        home,
        dataDir,
        venvPython: `${dataDir}/venv/bin/python3`,
        daemonPy: `${$.plugin.root}/python/asmc_daemon.py`,
        setupPy: `${$.plugin.root}/python/setup_env.py`,
        backfillPy: `${$.plugin.root}/python/backfill.py`,
        info: `${dataDir}/daemon.json`,
      }
      socketPath = undefined
      sessionId = await $.session.id()
      excludeAfterIso = new Date(await $.clock.now()).toISOString()
      projectDir = (await $.session.cwd()).replace(/\/+$/, '')
      excluded = await isExcludedProject($, projectDir)
      await $.command.register({
        name: 'asmc',
        description: 'ASMC memory: status, setup, start, stop, mood, backfill',
        argumentHint: '[status|setup|start|stop|mood <text>|backfill [file]]',
      })
      if (excluded) {
        $.ui.log(`${PLUGIN}: memory is off in this project (listed in excluded-projects.txt)`, { to: 'debug' })
        return started
      }
      await registerTools($)
      if (await $.fs.exists(paths.venvPython)) {
        await ensureDaemon($)
        if (!ready) $.ui.log(`${PLUGIN}: daemon did not start: ${lastError}`)
      } else {
        startSetup($)
      }
    } catch (err) {
      lastError = `session.start: ${String(err)}`
      $.ui.log(`${PLUGIN}: ${lastError}`)
    }
    return started
  })

  // Involuntary recall: a budgeted memory block rides after the prompt.
  on('prompt.submit', async ($, e, next) => {
    if (excluded || e.isMeta || e.command) return next(e)
    const query = cleanPrompt(e.text)
    if (!query) return next(e)
    pendingPrompt = query
    if (!ready) return next(e)
    const res = await daemon($, 'POST', '/context', {
      query,
      session: sessionId,
      project: projectDir,
      exclude_after: excludeAfterIso,
      budget: num(options.charBudget, 6000),
      recent: num(options.recentCount, 6),
      l1: 4,
      l2: num(options.semanticCount, 6),
      include_mood: bool(options.injectMood, true),
    })
    const text = res && typeof res.text === 'string' ? res.text : ''
    if (!text) return next(e)
    return next({ ...e, context: [...(e.context ?? []), text] })
  }).catch(($, e, next) => next(e))

  // Involuntary storage: the prompt and the final answer become one experience.
  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    if (excluded || e.agentId !== undefined || e.reason !== 'answer' || !ready || !bool(options.autoStore, true)) return done
    try {
      const answer = e.answer.trim()
      if (!answer) return done
      // The prompt captured at prompt.submit is the reliable one; the transcript
      // walk is the fallback (a prompt queued mid-turn shares a row with tool
      // results, so rows are not skipped for carrying them).
      let prompt = pendingPrompt
      if (!prompt) {
        const rows = await $.session.messages()
        for (let i = rows.length - 1; i >= 0; i--) {
          const row = rows[i]
          if (row.role !== 'user') continue
          prompt = cleanPrompt(row.text)
          if (prompt) break
        }
      }
      pendingPrompt = ''
      if (!prompt) return done
      await daemon($, 'POST', '/remember', {
        situation: prompt,
        response: answer,
        metadata: { session: sessionId, cwd: await $.session.cwd(), project: projectDir, platform: projectDir, kind: 'exchange', model: await modelName($) },
      })
    } catch (err) {
      lastError = `turn.complete: ${String(err)}`
    }
    return done
  })

  // After a compaction the older part of this session has left the context
  // window, so this session's earlier entries become fair game for recall.
  on('session.compact', async ($, e, next) => {
    const result = await next(e)
    excludeAfterIso = new Date(await $.clock.now()).toISOString()
    return result
  })

  // A short standing note in the system prompt, so the block and the tools
  // are understood rather than discovered.
  on('prompt.compose', async ($, e, next) => {
    const composed = await next(e)
    if (excluded || !ready) return composed
    return {
      sections: [...composed.sections, { id: `${PLUGIN}:memory`, text: MEMORY_SECTION, scope: 'session' as const }],
    }
  })

  // Voluntary tools. A hook that fails would leave the call unanswered, so
  // each one's .catch turns a failure into a plain result the model can read.
  const failed = (next: { error: { kind: string; message?: string } }) => ({
    result: `Memory tool failed (${next.error.kind}): ${next.error.message ?? ''}`.trim(),
  })

  on('tool.call', { tool: 'mcp__asmc-memory__recall' }, async ($, e) => {
    const query = str(e.query).trim()
    if (!query) return { result: 'Error: empty query.' }
    const res = await daemon($, 'POST', '/recall', {
      query,
      k: num(e.k, 6),
      source: e.source === 'journal' ? 'journal' : 'any',
    })
    return { result: res && typeof res.text === 'string' ? res.text : `Memory unavailable: ${lastError || 'daemon not running'}` }
  }).catch(($, e, next) => failed(next))

  on('tool.call', { tool: 'mcp__asmc-memory__journal_write' }, async ($, e) => {
    const entry = str(e.entry).trim()
    if (!entry) return { result: 'Error: empty entry, nothing written.' }
    const res = await daemon($, 'POST', '/journal', {
      entry,
      metadata: { session: sessionId, cwd: await $.session.cwd(), project: projectDir, platform: projectDir, model: await modelName($) },
    })
    return {
      result:
        res && res.ok === true
          ? 'Journal entry saved. It is now part of your searchable memory and mirrored to journal.md.'
          : `Journal not saved: ${lastError || 'daemon not running'}`,
    }
  }).catch(($, e, next) => failed(next))

  on('tool.call', { tool: 'mcp__asmc-memory__set_mood' }, async ($, e) => {
    const mood = str(e.mood).trim()
    if (!mood) return { result: 'Error: empty mood, nothing set.' }
    const res = await daemon($, 'POST', '/mood', { mood })
    return { result: res && typeof res.mood === 'string' ? `Mood set to: ${res.mood}` : `Mood not set: ${lastError || 'daemon not running'}` }
  }).catch(($, e, next) => failed(next))

  on('tool.call', { tool: 'mcp__asmc-memory__memory_status' }, async ($) => ({ result: await statusText($) }))
    .catch(($, e, next) => failed(next))

  // The person's own handle on the memory.
  on('command.run', { command: 'asmc' }, async ($, e) => {
    const [sub = 'status', ...rest] = e.args.trim().split(/\s+/)
    const arg = rest.join(' ').trim()
    if (!paths) return { text: `${PLUGIN}: not initialised yet.` }
    switch (sub) {
      case 'status':
        return { text: await statusText($) }
      case 'setup': {
        if (settingUp) return { text: 'Setup is already running; watch the status line.' }
        const run = await $.process.run(['python3', paths.setupPy, '--data-dir', paths.dataDir], {
          env: envFor(paths),
          timeoutMs: 600_000,
        })
        const out = (run.stdout + run.stderr).trim().split('\n').slice(-12).join('\n')
        if (run.exitCode !== 0) return { text: `Setup failed (exit ${run.exitCode}):\n${out}` }
        await ensureDaemon($)
        return { text: `${out}\n\n${ready ? 'Memory daemon is up.' : `Daemon did not start: ${lastError}`}` }
      }
      case 'start': {
        await ensureDaemon($)
        return { text: ready ? 'Memory daemon is up.' : `Daemon did not start: ${lastError}` }
      }
      case 'stop': {
        const run = await $.process.run([paths.venvPython, paths.daemonPy, '--stop'], {
          env: envFor(paths),
          timeoutMs: 30_000,
        })
        ready = false
        socketPath = undefined
        return { text: `Daemon: ${run.stdout.trim() || run.stderr.trim() || 'stopped'}` }
      }
      case 'backfill': {
        if (excluded) return { text: 'Memory is off in this project (excluded-projects.txt); nothing to import here.' }
        if (!ready && !(await ensureDaemon($))) return { text: `Daemon did not start: ${lastError}` }
        const cwd = await $.session.cwd()
        // Claude Code keeps each session's transcript under ~/.claude/projects/<cwd, non-alphanumerics as '-'>/.
        const file = arg || `${paths.home}/.claude/projects/${cwd.replace(/[^a-zA-Z0-9]/g, '-')}/${sessionId}.jsonl`
        if (!(await $.fs.exists(file))) return { text: `No transcript at ${file}` }
        await $.ui.status('asmc: importing transcript…')
        const run = await $.process.run([paths.venvPython, paths.backfillPy, '--transcript', file], {
          env: envFor(paths),
          timeoutMs: 600_000, // the host's ceiling; the ledger lets a cut-off run continue
        })
        await $.ui.status(undefined)
        const out = (run.stdout + run.stderr).trim().split('\n').slice(-6).join('\n')
        return {
          text:
            run.exitCode === 0
              ? `Backfill of ${file}:\n${out}`
              : `Backfill stopped (exit ${run.exitCode}); run /asmc backfill again to continue where it left off.\n${out}`,
        }
      }
      case 'mood': {
        if (!arg) return { text: 'Usage: /asmc mood <text>' }
        const res = await daemon($, 'POST', '/mood', { mood: arg })
        return { text: res && typeof res.mood === 'string' ? `Mood set to: ${res.mood}` : `Not set: ${lastError}` }
      }
      default:
        return { text: USAGE }
    }
  })
}
