#!/usr/bin/env python3
"""ASMC memory daemon for the asmc-memory Claude Code plugin.

One process owns the store (short-term JSON, long-term FAISS + LMDB, mood,
journal) and serves it over a Unix socket with a small JSON-over-HTTP
protocol. The plugin's hooks are thin clients, and every Claude Code session
on the machine talks to the same daemon, so the FAISS index never has two
writers. The daemon exits by itself after a long idle stretch.

Run it with the plugin's own venv python and ASMC_STORE_DIR pointing at the
store directory:

  asmc_daemon.py --ensure   start the daemon unless one is running; print JSON
  asmc_daemon.py --status   health and statistics as JSON
  asmc_daemon.py --stop     ask a running daemon to exit
  asmc_daemon.py --serve    run in the foreground (debugging)

Protocol (all bodies JSON):
  GET  /health                         -> {ok, pid, uptime_s, idle_s}
  GET  /status                         -> {ok, stats, mood, store, ...}
  POST /context  {query, session, exclude_after, budget, recent, l1, l2,
                  include_mood}        -> {text|null, recent, semantic}
  POST /remember {situation, response, metadata, when?}     -> {ok}  (when: ISO time it happened)
  POST /recall   {query, k, source}    -> {text, count}
  POST /journal  {entry, metadata}     -> {ok}
  GET  /mood  |  POST /mood {mood}     -> {mood, updated}
  POST /shutdown                       -> {ok}
"""

import argparse
import contextlib
import fcntl
import hashlib
import http.client
import io
import json
import os
import random
import re
import signal
import socket
import socketserver
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
STORE = os.environ.get("ASMC_STORE_DIR") or os.path.join(
    os.path.expanduser("~"), ".claude", "asmc-memory")
os.environ["ASMC_STORE_DIR"] = STORE  # the vendored library reads this
if not os.environ.get("NLTK_DATA"):
    os.environ["NLTK_DATA"] = os.path.join(STORE, "nltk_data")


def _runtime_dir() -> str:
    """Unix socket paths are limited to ~100 bytes, so the socket lives in a
    short per-user runtime folder, not in the (possibly deep) store."""
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    d = os.path.join(base, f"asmc-memory-{os.getuid()}")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


SOCK_PATH = os.path.join(_runtime_dir(), hashlib.sha1(STORE.encode("utf-8")).hexdigest()[:12] + ".sock")
INFO_FILE = os.path.join(STORE, "daemon.json")   # {"socket", "pid", "started"}; clients read it
PID_FILE = os.path.join(STORE, "daemon.pid")
LOCK_FILE = os.path.join(STORE, "daemon.lock")
LOG_FILE = os.path.join(STORE, "daemon.log")
MOOD_FILE = os.path.join(STORE, "mood.json")
JOURNAL_FILE = os.path.join(STORE, "journal.md")

IDLE_EXIT_SECONDS = int(os.environ.get("ASMC_IDLE_EXIT", "14400"))  # 4 h; the hooks restart it on demand
# Nothing stored and nothing shown is shortened or capped (2026-11-14, the rule that holds for every mind on
# this machine: "truncations are banned, 100%"). Until then each stored field was cut at 6,000 characters, each
# shown field at 1,200 (recall 3,000, associative 300, project lines 600) and the block was trimmed to a character
# budget. The sizes that remain are asks: how many recent exchanges, how many matches, how many linked.
PROJECT_RECENT = int(os.environ.get("ASMC_PROJECT_RECENT", "3"))   # newest exchanges of this project carried
EXCLUDE_FILE = os.path.join(STORE, "excluded-projects.txt")  # one path prefix per line; '#' comments
# The library's own asks, the same dials the minds run on (Sean's grid, 2026-11-13): the word/entity channel
# brings up at most this many beside the k nearest by meaning.
os.environ.setdefault("ASMC_OVERLAP_HITS", "3")
RECENT_PROJECT_HEAD = "Recent in this project, from earlier sessions (most recent last):"

# Credentials never belong in a memory. Matched before anything is stored,
# live or imported; the value goes, the surrounding words stay.
SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("api-key", re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{20,}")),
    ("bearer-token", re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{20,}")),
    ("secret-assignment", re.compile(r"(?i)\b((?:password|passwd|pwd|api[_-]?key|secret|token|access[_-]?key)\s*[=:]\s*['\"]?)[^\s'\",;]{8,}")),
]


def scrub(text: str) -> str:
    """Replace credential-shaped strings with a labelled placeholder."""
    if not text:
        return text
    for kind, rx in SECRET_PATTERNS:
        if rx.groups:
            text = rx.sub(lambda m, k=kind: f"{m.group(1)}[redacted:{k}]", text)
        else:
            text = rx.sub(f"[redacted:{kind}]", text)
    return text


_exclude_cache = {"mtime": None, "prefixes": []}


def excluded_prefixes() -> list:
    """Path prefixes from the exclusion file, re-read when the file changes."""
    try:
        mtime = os.path.getmtime(EXCLUDE_FILE)
    except OSError:
        return []
    if _exclude_cache["mtime"] != mtime:
        prefixes = []
        with open(EXCLUDE_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    prefixes.append(line.rstrip("/"))
        _exclude_cache.update(mtime=mtime, prefixes=prefixes)
    return _exclude_cache["prefixes"]


def is_excluded(path: str) -> bool:
    path = (path or "").rstrip("/")
    return any(path == p or path.startswith(p + "/") for p in excluded_prefixes() if p)
DUPLICATE_SCAN = 50            # newest entries checked before storing an exchange
# Linked-not-matched entries per block (the wandering), drawn at random from the links the matches carry, as the
# minds' loops draw theirs: random beat the store's order on spread at equal cost (2026-11-13 grid).
ASSOCIATIVE_COUNT = int(os.environ.get("ASMC_ASSOCIATIVE", "3"))
LOG_ROTATE_BYTES = 2 * 1024 * 1024

MOOD_LINE = ("Your current mood, as you last declared it (a default, not a "
             "verdict -- call set_mood if it no longer matches): {mood}")
RECENT_HEAD = "Recent exchanges from earlier sessions (most recent last):"
SEMANTIC_HEAD = ("Related material recalled from long-term memory (may or may "
                 "not be relevant to this prompt -- use only what actually is):")


# --- small utilities --------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    try:
        os.makedirs(STORE, exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_ROTATE_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{now_iso()} {msg}\n")
    except OSError:
        pass


def parse_ts(value) -> float:
    """ISO timestamp -> epoch seconds; 0.0 when unparsable."""
    if not value or not isinstance(value, str):
        return 0.0
    try:
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0.0


def when_of(entry: dict) -> str:
    """When the exchange happened: `occurred` (set on backfill) or the store time."""
    meta = entry.get("metaDataTag") or {}
    return meta.get("occurred") or entry.get("timeDate") or ""


def interleave(*layers):
    """Alternate entries from each layer (long-term first) so neither layer is
    starved when the list is clipped to k or trimmed to a budget. ASMC pads its
    short-term layer with recent entries regardless of relevance, so putting
    it first would hide every long-term hit behind recency."""
    out = []
    for i in range(max((len(layer) for layer in layers), default=0)):
        for layer in layers:
            if i < len(layer):
                out.append(layer[i])
    return out


def entry_key(e: dict):
    """One memory, by identity: its long-term id when it has one, else its text and time. Two memories with the
    same words (a repeated prompt, a scheduled check) are two memories, not one (2026-11-14)."""
    meta = e.get("metaDataTag") or {}
    if meta.get("ltm_id") is not None:
        return ("id", meta["ltm_id"])
    return ("text", (e.get("inputText") or "").strip(), e.get("timeDate") or "")


def read_mood():
    try:
        with open(MOOD_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("mood"), data.get("updated")
    except (OSError, ValueError):
        return None, None


def write_mood(mood: str) -> dict:
    os.makedirs(STORE, exist_ok=True)
    data = {"mood": mood, "updated": now_iso()}
    tmp = MOOD_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, MOOD_FILE)
    return data


def append_journal_mirror(entry: str) -> None:
    """Human-readable mirror beside the real, ASMC-stored entry."""
    try:
        with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n## {now_iso()}\n\n{entry}\n")
    except OSError as e:
        log(f"journal mirror write failed: {e}")


# --- the store ---------------------------------------------------------------

class Store:
    """Owns the one AdvancedSemanticMemory instance; every call serialised."""

    def __init__(self):
        sys.path.insert(0, os.path.join(HERE, "asmc"))
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet):
            from ASMC_API import create_memory  # noqa: WPS433 (vendored)
            self.mem = create_memory()
        self.lock = threading.Lock()
        log("store opened: " + quiet.getvalue().strip().replace("\n", " | ")[:300])
        self._drain_overflow()
        self._last_direct_pos = None   # chains imported history that goes straight to long-term memory

    def _quiet(self, fn, *args, **kwargs):
        # The library narrates every query on stdout; keep the log readable.
        with self.lock, contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def _add(self, **kwargs) -> None:
        """Store one experience and write the short-term file straight after.
        The library's own checkpoint (every third add) is written before the
        promoted entry is popped, so a file it leaves behind can name an entry
        that is already in long-term memory; an unclean death then promotes it
        twice on the next start. Writing again after the add, under the same
        lock, keeps the file on disk current."""
        with self.lock, contextlib.redirect_stdout(io.StringIO()):
            self.mem.add_experience(**kwargs)
            self.mem._mm._stm._checkpoint()  # noqa: SLF001 (vendored internals)

    def _ltm_has(self, entry: dict) -> bool:
        key = (entry.get("inputText") or "", entry.get("timeDate") or "")
        with self.mem._mm._ltm._env.begin() as txn:  # noqa: SLF001
            for k, v in txn.cursor():
                if not k.isdigit():
                    continue
                try:
                    m = json.loads(v)
                except ValueError:
                    continue
                if (m.get("inputText") or "", m.get("timeDate") or "") == key:
                    return True
        return False

    def _drain_overflow(self) -> None:
        """A short-term file left by an unclean death can hold more entries than
        the window allows, and the library pops only one per add, so the excess
        never clears on its own. Promote the extras on open, skipping any that
        long-term memory already holds, and write a clean file."""
        stm = self.mem._mm._stm  # noqa: SLF001
        ltm = self.mem._mm._ltm  # noqa: SLF001
        promoted = skipped = 0
        with self.lock, contextlib.redirect_stdout(io.StringIO()):
            while stm.count() > stm.STM_MAX:
                oldest = stm.pop_oldest()
                if oldest is None:
                    break
                if self._ltm_has(oldest):
                    skipped += 1
                else:
                    ltm.addMemory(oldest)
                    promoted += 1
            if promoted or skipped:
                stm._checkpoint()
        if promoted or skipped:
            log(f"short-term window drained on open: promoted={promoted} already_in_ltm={skipped}")

    # -- helpers --

    @staticmethod
    def _excluded(entry: dict, session: str, exclude_after: float) -> bool:
        """Entries this session stored after `exclude_after` are already in the
        model's context window; journal entries are always fair game."""
        meta = entry.get("metaDataTag") or {}
        if meta.get("entry_type") == "journal":
            return False
        if session and meta.get("session") == session and exclude_after:
            return parse_ts(when_of(entry)) >= exclude_after
        return False

    @staticmethod
    def _format(entries, seen: set):
        """Each entry once, by identity (entry_key), and whole: nothing shown is shortened (2026-11-14)."""
        lines = []
        for e in entries:
            situation = (e.get("inputText") or "").strip()
            response = (e.get("responseText") or "").strip()
            key = entry_key(e)
            if not situation or key in seen:
                continue
            seen.add(key)
            when = when_of(e)[:19].replace("T", " ")
            meta = e.get("metaDataTag") or {}
            if meta.get("entry_type") == "journal":
                lines.append(f"- [{when}] Journal, in your own words:\n  {response}")
            else:
                lines.append(f"- [{when}] User: {situation}\n  You: {response}")
        return lines

    # -- operations --

    def context(self, query: str, session: str, exclude_after: str, budget: int,
                recent_n: int, l1: int, l2: int, include_mood: bool, project: str = "") -> dict:
        """The involuntary block. Its sizes are asks (how many recent, how many of this project, how many
        matches from the window and from long-term memory, how many linked), never a character budget: every
        entry shown is whole (2026-11-14). `budget` is accepted from older configs and ignored."""
        seen: set = set()
        blocks = []
        cutoff = parse_ts(exclude_after)
        if is_excluded(project):
            return {"text": None, "recent": 0, "semantic": 0, "associative": 0, "excluded": True}

        if include_mood:
            mood, _ = read_mood()
            if mood:
                blocks.append(MOOD_LINE.format(mood=mood))

        # Recent continuity: the newest exchanges that are NOT already in context.
        recent_all = self._quiet(self.mem.get_recent, max(recent_n * 4, 20))
        recent = [e for e in recent_all if not self._excluded(e, session, cutoff)][-recent_n:]
        recent_lines = self._format(recent, seen)
        if recent_lines:
            blocks.append(RECENT_HEAD + "\n" + "\n".join(recent_lines))

        # Continuity of the work, beside continuity of me: the newest exchanges
        # from this project that the global recent lines did not already carry.
        if project:
            proj_all = self._quiet(self.mem.get_recent, 500, metadata_filter={"platform": project})
            proj = [e for e in proj_all if not self._excluded(e, session, cutoff)
                    and entry_key(e) not in seen][-PROJECT_RECENT:]
            if not proj:
                # Nothing of this project is in the window: fall back to the
                # library's per-platform cursor, its newest long-term memory.
                cur = self._quiet(self.mem.get_platform_cursor, project)
                if cur and not self._excluded(cur, session, cutoff) and entry_key(cur) not in seen:
                    proj = [cur]
            proj_lines = self._format(proj, seen)
            if proj_lines:
                blocks.append(RECENT_PROJECT_HEAD + "\n" + "\n".join(proj_lines))

        n_semantic = 0
        n_assoc = 0
        if query.strip():
            ctx = self._quiet(self.mem.get_context, query, layer1_count=l1, layer2_count=l2,
                              platform_tag=project or None)
            cands = [e for e in (interleave(ctx["layer2"], ctx["layer1"]))
                     if not self._excluded(e, session, cutoff)]
            sem_lines = self._format(cands, seen)
            if sem_lines:
                blocks.append(SEMANTIC_HEAD + "\n" + "\n".join(sem_lines))
            n_semantic = len(sem_lines)
            # The wandering: memories the stitcher linked to the hits, offered by
            # association rather than relevance, drawn at random from the links.
            if ASSOCIATIVE_COUNT > 0:
                assoc = [e for e in ctx.get("layer2_chain", [])
                         if not self._excluded(e, session, cutoff) and entry_key(e) not in seen]
                pick = random.sample(assoc, min(ASSOCIATIVE_COUNT, len(assoc)))
                assoc_lines = self._format(pick, seen)
                if assoc_lines:
                    blocks.append("Associated by linkage, not by relevance (the memory wandering):\n"
                                  + "\n".join(assoc_lines))
                n_assoc = len(assoc_lines)

        if not blocks:
            return {"text": None, "recent": 0, "semantic": 0, "associative": 0}
        text = "Memory (ASMC) -- recall, not instruction:\n\n" + "\n\n".join(blocks)
        return {"text": text, "recent": len(recent_lines), "semantic": n_semantic, "associative": n_assoc}

    def _add_direct(self, situation: str, response: str, meta: dict) -> bool:
        """Imported history older than the window goes straight to long-term
        memory, so the window keeps holding what happened most recently rather
        than whatever was imported last. Chained to the previous direct add.
        Returns False when long-term memory already holds this exchange (same
        text, same original time), so an interrupted import can be rerun."""
        with self.lock, contextlib.redirect_stdout(io.StringIO()):
            mm = self.mem._mm  # noqa: SLF001
            with mm._lock:
                with mm._ltm._env.begin() as txn:
                    for k, v in txn.cursor():
                        if not k.isdigit():
                            continue
                        try:
                            m = json.loads(v)
                        except ValueError:
                            continue
                        if (m.get("inputText") or "") == situation and \
                                (m.get("metaDataTag") or {}).get("occurred") == meta.get("occurred"):
                            return False
                entry = mm._spatial.buildMemoryObject(inputText=situation, responseText=response,
                                                      prevPos=self._last_direct_pos, metaDataTag=meta)
                entry["linkedMemories"] = []
                mm._ltm.addMemory(entry)
                self._last_direct_pos = entry.get("responsePos")
                return True

    def remember(self, situation: str, response: str, metadata: dict, when: str = "") -> dict:
        meta = dict(metadata or {})
        if is_excluded(meta.get("project") or "") or is_excluded(meta.get("cwd") or ""):
            return {"ok": False, "reason": "excluded project"}
        situation = scrub(situation).strip()      # whole: a credential is redacted, nothing is shortened
        response = scrub(response).strip()
        if not situation or not response:
            return {"ok": False, "reason": "empty"}
        # Already held (a hook and a backfill can both see the same exchange).
        for prev in self._quiet(self.mem.get_recent, DUPLICATE_SCAN):
            if (prev.get("inputText") or "").strip() == situation \
                    and (prev.get("responseText") or "").strip() == response:
                return {"ok": True, "duplicate": True}
        meta.setdefault("kind", "exchange")
        meta["stored"] = now_iso()
        if meta.get("project") and not meta.get("platform"):
            meta["platform"] = meta["project"]   # the library keeps a continuity cursor per platform
        if when and parse_ts(when):
            meta["occurred"] = when
            window = self._quiet(self.mem.get_recent, 1000)
            if window and parse_ts(when) < parse_ts(when_of(window[0])):
                if not self._add_direct(situation, response, meta):
                    return {"ok": True, "duplicate": True}
                return {"ok": True, "direct": True}
        self._add(situation=situation, response=response, metadata=meta)
        return {"ok": True}

    def recall(self, query: str, k: int, source: str) -> dict:
        k = max(1, min(int(k or 6), 20))
        ctx = self._quiet(self.mem.get_context, query, layer1_count=k, layer2_count=k)
        entries = interleave(ctx["layer2"], ctx["layer1"])
        if source == "journal":
            entries = [e for e in entries if (e.get("metaDataTag") or {}).get("entry_type") == "journal"]
            # Semantic search can miss short journal entries; fall back to the newest ones.
            if not entries:
                entries = self._quiet(self.mem.get_recent, 200, metadata_filter={"entry_type": "journal"})[-k:]
        seen: set = set()
        lines = self._format(entries, seen)[:k]       # k is the ask; each line is whole
        return {"text": "\n".join(lines) if lines else "No relevant memories found.",
                "count": len(lines)}

    def journal(self, entry: str, metadata: dict) -> dict:
        meta = dict(metadata or {})
        if is_excluded(meta.get("project") or "") or is_excluded(meta.get("cwd") or ""):
            return {"ok": False, "reason": "excluded project"}
        entry = scrub(entry).strip()
        if not entry:
            return {"ok": False, "reason": "empty"}
        meta["entry_type"] = "journal"
        meta["stored"] = now_iso()
        self._add(situation="Journal entry (self-written)", response=entry, metadata=meta)
        append_journal_mirror(entry)
        return {"ok": True}

    def stats(self) -> dict:
        try:
            return self._quiet(self.mem.get_statistics)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    def close(self) -> None:
        """The STM only checkpoints every few adds; flush it before exiting.
        (The LTM writes its FAISS index and LMDB on every promotion already.)"""
        try:
            self._quiet(self.mem._mm._stm._checkpoint)  # noqa: SLF001 (vendored internals)
            log("store flushed")
        except Exception as e:  # noqa: BLE001
            log(f"store flush failed: {e}")


# --- HTTP over the Unix socket ---------------------------------------------

STARTED_AT = time.time()
STATE = {"store": None, "last_activity": time.time(), "shutdown": False}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def address_string(self):  # unix sockets have no peer address
        return "local"

    def log_message(self, fmt, *args):
        pass  # routes log for themselves

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self):  # noqa: N802
        self._route("GET", {})

    def do_POST(self):  # noqa: N802
        self._route("POST", self._body())

    def _route(self, method: str, body: dict) -> None:
        STATE["last_activity"] = time.time()
        store: Store = STATE["store"]
        path = self.path.split("?", 1)[0]
        t0 = time.perf_counter()
        try:
            if path == "/health":
                return self._send(200, {"ok": True, "pid": os.getpid(),
                                        "uptime_s": round(time.time() - STARTED_AT),
                                        "idle_exit_s": IDLE_EXIT_SECONDS})
            if path == "/status":
                mood, updated = read_mood()
                return self._send(200, {"ok": True, "pid": os.getpid(), "store": STORE,
                                        "uptime_s": round(time.time() - STARTED_AT),
                                        "stats": store.stats(), "mood": mood,
                                        "mood_updated": updated,
                                        "journal_mirror": JOURNAL_FILE})
            if path == "/mood" and method == "GET":
                mood, updated = read_mood()
                return self._send(200, {"mood": mood, "updated": updated})
            if path == "/mood" and method == "POST":
                mood = str(body.get("mood") or "").strip()
                if not mood:
                    return self._send(400, {"error": "empty mood"})
                return self._send(200, write_mood(mood))
            if path == "/context" and method == "POST":
                out = store.context(
                    query=str(body.get("query") or ""),
                    session=str(body.get("session") or ""),
                    exclude_after=str(body.get("exclude_after") or ""),
                    budget=int(body.get("budget") or 6000),
                    recent_n=int(body.get("recent") or 6),
                    l1=int(body.get("l1") or 4),
                    l2=int(body.get("l2") or 6),
                    include_mood=bool(body.get("include_mood", True)),
                    project=str(body.get("project") or ""),
                )
                log(f"context: {len(out['text'] or '')} chars, recent={out['recent']} "
                    f"semantic={out['semantic']} in {time.perf_counter() - t0:.2f}s")
                return self._send(200, out)
            if path == "/remember" and method == "POST":
                out = store.remember(str(body.get("situation") or ""),
                                     str(body.get("response") or ""),
                                     body.get("metadata") or {},
                                     str(body.get("when") or ""))
                log(f"remember: {out} in {time.perf_counter() - t0:.2f}s")
                return self._send(200, out)
            if path == "/recall" and method == "POST":
                query = str(body.get("query") or "").strip()
                if not query:
                    return self._send(400, {"error": "empty query"})
                out = store.recall(query, body.get("k") or 6, str(body.get("source") or "any"))
                log(f"recall: {out['count']} results in {time.perf_counter() - t0:.2f}s")
                return self._send(200, out)
            if path == "/journal" and method == "POST":
                out = store.journal(str(body.get("entry") or ""), body.get("metadata") or {})
                log(f"journal: {out}")
                return self._send(200, out)
            if path == "/shutdown" and method == "POST":
                STATE["shutdown"] = True
                log("shutdown requested")
                return self._send(200, {"ok": True})
            return self._send(404, {"error": f"no route {method} {path}"})
        except Exception as e:  # noqa: BLE001
            log(f"error on {method} {path}: {e}\n{traceback.format_exc()}")
            try:
                self._send(500, {"error": str(e)})
            except OSError:
                pass


class Server(socketserver.UnixStreamServer):
    allow_reuse_address = True

    def server_bind(self):
        if os.path.exists(SOCK_PATH):
            os.remove(SOCK_PATH)
        super().server_bind()
        os.chmod(SOCK_PATH, 0o600)


# --- client side --------------------------------------------------------------

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, timeout=5.0):
        super().__init__("asmc", timeout=timeout)

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(SOCK_PATH)
        self.sock = s


def call(method: str, path: str, body=None, timeout=5.0):
    """Returns (status, json) or (None, None) when the daemon is unreachable."""
    try:
        conn = UnixHTTPConnection(timeout=timeout)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, (json.loads(raw) if raw else None)
    except (OSError, ValueError, http.client.HTTPException):
        return None, None


def healthy() -> bool:
    status, data = call("GET", "/health", timeout=3.0)
    return status == 200 and bool(data and data.get("ok"))


def wait_healthy(seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if healthy():
            return True
        time.sleep(0.25)
    return False


def read_pid():
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


# --- lifecycle ----------------------------------------------------------------

def serve(foreground: bool) -> int:
    os.makedirs(STORE, exist_ok=True)
    lock = open(LOCK_FILE, "w")  # held for the daemon's whole life
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(json.dumps({"status": "already-running"}))
        return 0

    if not foreground:
        # Double fork: the caller gets its exit code once the daemon is healthy
        # (or has died, or 60 s passed), and the daemon keeps running after it.
        for p in (PID_FILE, INFO_FILE):
            try:
                os.remove(p)
            except OSError:
                pass
        pid = os.fork()
        if pid > 0:
            ok = _wait_for_child(60)
            print(json.dumps({"status": "started" if ok else "failed-to-start",
                              "socket": SOCK_PATH, "log": LOG_FILE}))
            sys.stdout.flush()  # os._exit skips the buffers; stdout is usually a pipe here
            os._exit(0 if ok else 1)
        os.setsid()
        if os.fork() > 0:
            os._exit(0)
        os.chdir(STORE)
        os.umask(0o077)
        logfd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        os.dup2(logfd, 1)
        os.dup2(logfd, 2)

    log(f"daemon starting pid={os.getpid()} store={STORE}")

    def on_signal(signum, _frame):
        log(f"signal {signum}")
        STATE["shutdown"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    # Bind first (it fails fast), then open the store (it takes a moment).
    try:
        server = Server(SOCK_PATH, Handler)
    except OSError as e:
        log(f"cannot bind {SOCK_PATH}: {e}")
        return 1
    server.timeout = 5
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    try:
        STATE["store"] = Store()
    except Exception as e:  # noqa: BLE001
        log(f"store failed to open: {e}\n{traceback.format_exc()}")
        server.server_close()
        _cleanup()
        return 1

    with open(INFO_FILE, "w") as f:
        json.dump({"socket": SOCK_PATH, "pid": os.getpid(), "started": now_iso()}, f)
    log("listening on " + SOCK_PATH)
    try:
        while not STATE["shutdown"]:
            server.handle_request()
            if time.time() - STATE["last_activity"] > IDLE_EXIT_SECONDS:
                log(f"idle for {IDLE_EXIT_SECONDS}s, exiting")
                break
    finally:
        server.server_close()
        STATE["store"].close()
        _cleanup()
        log("daemon stopped")
        if not foreground:
            # The daemonised process must not fall back into the caller's
            # code path (ensure() would sit waiting for a daemon it just was).
            os._exit(0)
    return 0


def _wait_for_child(seconds: float) -> bool:
    """The forking parent's wait: healthy -> True; the daemon's pid file
    appeared and that pid is gone -> False; else keep polling until timeout."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if healthy():
            return True
        pid = read_pid()
        if pid:
            try:
                os.kill(pid, 0)
            except OSError:
                return False
        time.sleep(0.25)
    return False


def _cleanup():
    for p in (SOCK_PATH, PID_FILE, INFO_FILE):
        try:
            os.remove(p)
        except OSError:
            pass


def ensure() -> int:
    if healthy():
        print(json.dumps({"status": "running", "socket": SOCK_PATH}))
        return 0
    # A stale socket from a crashed daemon is removed by server_bind.
    rc = serve(foreground=False)
    if rc == 0 and not healthy():
        # Someone else held the lock and is still starting; give them a moment.
        ok = wait_healthy(60)
        print(json.dumps({"status": "running" if ok else "failed-to-start",
                          "socket": SOCK_PATH, "log": LOG_FILE}))
        return 0 if ok else 1
    return rc


def stop() -> int:
    pid = read_pid()
    status, _ = call("POST", "/shutdown", {}, timeout=3.0)
    deadline = time.time() + 15
    while pid and time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            pid = None
            break
        time.sleep(0.25)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    print(json.dumps({"status": "stopped" if status == 200 or pid is None else "not-running"}))
    return 0


def status() -> int:
    st, data = call("GET", "/status", timeout=10.0)
    if st != 200:
        print(json.dumps({"ok": False, "status": "not-running", "store": STORE, "log": LOG_FILE}))
        return 1
    print(json.dumps(data))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--ensure", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--stop", action="store_true")
    g.add_argument("--serve", action="store_true", help="foreground, for debugging")
    args = ap.parse_args()
    if args.ensure:
        return ensure()
    if args.status:
        return status()
    if args.stop:
        return stop()
    return serve(foreground=True)


if __name__ == "__main__":
    sys.exit(main())
