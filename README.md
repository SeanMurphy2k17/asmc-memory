# asmc-memory

Persistent semantic memory for Claude Code. Every exchange is remembered across sessions, relevant past material is recalled into each prompt without being asked, and Claude gets three deliberate tools: `recall`, `journal_write` and `set_mood`. Everything runs locally; nothing is sent anywhere.

It is the Claude Code port of a setup that first ran a local open-weight model against the same memory system (ASMC) through an OpenAI-compatible proxy. The design is the same on purpose: an involuntary half the model never has to think about, and a voluntary half it reaches for on purpose.

## What it does

**Involuntary** (hooks, no model action):

- After each prompt you type, a context block headed *Memory (ASMC)* is attached, invisible to you, visible to Claude. It carries the newest exchanges from earlier sessions, a few past exchanges found by search (exact words and semantic proximity, interleaved), Claude's own journal entries, the mood Claude last declared, and one entry offered by association rather than relevance: a memory the library has linked to one of the hits, which may have nothing to do with the prompt. That last one is deliberate. Unrelated memories are texture; a memory that only ever returns what you asked for is a database. The block is budgeted (default 6,000 characters) and trimmed newest-first for continuity, most-relevant-first for recall.
- When a turn ends with an answer, the prompt and the final answer are stored as one experience.
- Entries from the current session are not re-injected while they are still in the context window. After a compaction, the part of the session that fell out becomes recallable again.

**Voluntary** (tools Claude calls):

| Tool | What it does |
| --- | --- |
| `recall` | Searches memory on purpose. `source: "journal"` limits it to journal entries. |
| `journal_write` | A first-person entry that becomes searchable memory, mirrored to `journal.md`. |
| `set_mood` | Claude declares its own state in its own words. It carries forward as a default, not a verdict. |
| `memory_status` | Where the store is, how much it holds, whether the daemon is up. |

A short section is added to the system prompt so Claude knows the block is recall, not instruction, and what the tools are for.

**For you**: `/asmc` (status), `/asmc setup`, `/asmc start`, `/asmc stop`, `/asmc mood <text>`, `/asmc backfill [file]`.

**One memory, every project.** The store is yours, not a project's: every session on the machine that loads the plugin reads and writes the same memory, in any folder. Each exchange is tagged with the project it came from, the block carries a "recent in this project" line beside the global recent lines, and retrieval seeds from that project's newest memory, so each project stays continuous inside one memory.

**Projects that must stay out.** `<store>/excluded-projects.txt`, one path prefix per line (`#` comments). A session whose working directory is under a prefix gets no memory at all: no block, nothing stored, the tools not even registered, and `/asmc status` says so. The daemon and the importer apply the same list, so a transcript from an excluded project cannot be imported either.

**Credentials.** Before anything is stored, live or imported, credential-shaped strings (API keys, GitHub and Slack tokens, AWS keys, bearer headers, `password=` style assignments, private key blocks) are replaced with a labelled placeholder. The words around them stay.

**Importing what happened before the plugin was installed**: `/asmc backfill` reads the current session's transcript; `python/backfill.py --all-projects --dry-run` lists every Claude Code transcript on the machine with what would be stored, skipped or excluded, and without `--dry-run` imports them all in one timeline, oldest first across projects. History older than the short-term window goes straight to long-term memory, so the window keeps holding what actually happened most recently. It (`~/.claude/projects/<project>/<session>.jsonl`), pairs each prompt you typed with the final answer of its turn, strips the harness noise, and stores the exchanges oldest first with the time they actually happened. Give it a path to import another session. A ledger in the store remembers how far each session has been imported, and an exchange the hooks already stored is recognised, so running it again adds nothing. It takes about a minute per hundred turns.

## Requirements

- Linux or macOS (the memory daemon listens on a Unix socket).
- `python3` 3.10 or newer on your PATH, with the `venv` module (on Debian/Ubuntu: `apt install python3-venv`).
- Network access once, for the first-run install (about 60 MB of downloads; the private environment takes about 180 MB on disk).

## Install

From a Claude Code session (2.1.275 or later):

```
/plugin install asmc-memory --marketplace SeanMurphy2k17/asmc-memory
```

On older versions, add the marketplace first, then install from it:

```
/plugin marketplace add SeanMurphy2k17/asmc-memory
/plugin install asmc-memory@asmc-local
```

Or run it straight from a folder while developing:

```
claude --plugin-dir /path/to/asmc-memory
```

The first session creates a private Python environment under the store directory, installs the memory library's dependencies into it and fetches the NLTK data. The status line shows progress; nothing touches your system Python. If it fails (no network, no `python3-venv`), fix the cause and run `/asmc setup`.

## Where the memory lives

Default: `~/.claude/asmc-memory/` (change it with the plugin's *Memory store directory* option).

```
asmc-memory/
  MemoryStructures/STM/stm.json        short-term memory (newest 50 entries)
  MemoryStructures/LTM/                long-term memory: FAISS index + LMDB
  MemoryStructures/word_cache.lmdb     word coordinate cache (speeds things up over time)
  mood.json                            the last self-declared mood
  journal.md                           human-readable mirror of journal entries
  backfill.json                        how far each imported transcript has been read
  excluded-projects.txt                path prefixes the memory never reads from or writes to
  venv/                                the plugin's private Python environment
  nltk_data/                           NLTK corpora
  daemon.log, setup.log                what happened
  asmc.sock, daemon.pid, daemon.lock   the running daemon
```

Delete the folder to forget everything.

## How it is built

One Python process (`python/asmc_daemon.py`) owns the store and serves it over a Unix socket. Every Claude Code session on the machine talks to the same daemon, so the FAISS index never has two writers. The daemon starts on demand, exits after four hours of silence, and the hooks start it again (about two seconds) the next time memory is needed. The hooks module (`hooks/register.ts`) is a thin client: `prompt.submit` asks for a context block, `turn.complete` stores the exchange, `tool.call` serves the four tools.

Prompts are cleaned before storage: harness reminders, IDE selection wrappers, slash-command output and compaction summaries are stripped, so what is remembered is what was actually said. Each entry carries two times: when it was stored, and (for imported exchanges) when it happened; display and the session-exclusion rule use the latter.

The memory library is [ASMC](python/asmc/VENDORED.md) (Advanced Semantic Memory Clustering), vendored under its MIT license. It maps text into a six-dimensional semantic coordinate space built from WordNet and SentiWordNet, keeps a short-term window, promotes older entries into a FAISS-indexed long-term store, and retrieves by spatial proximity plus entity and content-word overlap. No embedding model, no GPU.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| Memory store directory | `~/.claude/asmc-memory` | Where everything lives |
| Injected memory budget | 6000 | Max characters attached per prompt |
| Recent exchanges to carry forward | 6 | Continuity from earlier sessions |
| Semantic matches to attach | 6 | Topical recall per prompt |
| Remember every exchange | on | The involuntary store step |
| Carry mood forward | on | Attach the last declared mood |

Environment settings, read by the daemon when it starts (set them where Claude Code runs, or in a wrapper that starts it):

| Variable | Default | Meaning |
| --- | --- | --- |
| `ASMC_ASSOCIATIVE` | 1 | Entries offered by association per block; 0 turns the wandering off |
| `ASMC_STITCH_NEIGHBOURS` | 5 | How many nearest memories each new memory is linked to; higher gives the traversal more to wander along |
| `ASMC_FAISS_THREADS` | 1 | Threads FAISS may use; one is right for a flat index of a few hundred small vectors |
| `ASMC_IDLE_EXIT` | 14400 | Seconds of silence before the daemon exits (the hooks restart it on demand) |

The daemon also writes the short-term file after every store and, on start, drains a window that an unclean exit left over its limit, so a crash never duplicates a memory. The memory library itself is vendored with a few measured patches; `python/asmc/VENDORED.md` lists them.

## Troubleshooting

- `/asmc status` says the daemon is not running: `/asmc start`, then read `daemon.log` in the store directory.
- Setup fails: read `setup.log`. The usual causes are no network, or a `python3` without `venv`/`ensurepip`.
- Nothing seems to be remembered: the block is attached only after the daemon is up, and only for prompts that are not slash commands.
- Run `claude --debug` to see the plugin's own log lines.

## License

MIT. ASMC is by Sean Murphy (MIT); see `python/asmc/LICENSE`.
