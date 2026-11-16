#!/usr/bin/env python3
"""Import Claude Code transcripts (.jsonl) into the ASMC memory store.

Each real prompt the person typed is paired with the final answer of its turn
and stored as one exchange, oldest first, carrying the time it happened and
the project it came from. Rows the harness injects (task notifications,
scheduled checks, compaction summaries, slash-command echoes, image
placeholders) are skipped, and the same noise the live hook strips from
prompts is stripped here.

Projects listed in <store>/excluded-projects.txt (one path prefix per line)
are never read. Sessions under a temporary directory are skipped.

A ledger at <store>/backfill.json remembers how far each session has been
imported, so running this twice adds nothing.

usage: backfill.py --transcript FILE [--skip-open]            one session
       backfill.py --all-projects [--projects-root DIR]       every session on this machine
       ... [--store DIR] [--dry-run] [--all] [--limit N]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

NOISE = [re.compile(p, re.S) for p in (
    r"<system-reminder>.*?</system-reminder>",
    r"<ide_selection>.*?</ide_selection>",
    r"<ide_opened_file>.*?</ide_opened_file>",
    r"<ide_diagnostics>.*?</ide_diagnostics>",
    r"<command-name>.*?</command-name>",
    r"<command-message>.*?</command-message>",
    r"<command-args>.*?</command-args>",
    r"<local-command-stdout>.*?</local-command-stdout>",
    r"<local-command-caveat>.*?</local-command-caveat>",
    r"<task-notification>.*?</task-notification>",
    r"<task_notification>.*?</task_notification>",
    r"\[Request interrupted by user[^\]]*\]",
)]
SKIP_ORIGINS = {"task-notification"}
SKIP_TURN_ORIGINS = {"scheduled", "task_notification"}
TEMP_ROOTS = ("/tmp/", "/var/tmp/")


def clean_prompt(text: str) -> str:
    for rx in NOISE:
        text = rx.sub("", text)
    text = text.strip()
    if not text or text.startswith("/"):
        return ""
    if text.lower().startswith("this session is being continued from a previous conversation"):
        return ""
    return text


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def is_prompt_row(r: dict) -> bool:
    if r.get("type") != "user" or r.get("isMeta") or r.get("isCompactSummary") or r.get("isSidechain"):
        return False
    if (r.get("origin") or {}).get("kind") in SKIP_ORIGINS:
        return False
    if r.get("turnOrigin") in SKIP_TURN_ORIGINS:
        return False
    return True


def pairs_from(path: str):
    """Yield {ts, session, cwd, prompt, answer, uuid} for each completed exchange."""
    current = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = r.get("type")
            if t == "user" and is_prompt_row(r):
                prompt = clean_prompt(text_of((r.get("message") or {}).get("content")))
                if not prompt:
                    continue
                if current and current["prompt"] == prompt and current["ts"][:16] == (r.get("timestamp") or "")[:16]:
                    continue  # the same prompt row logged again (a retried turn): keep pairing with its latest answer
                if current and current["answer"]:
                    yield current
                current = {
                    "ts": r.get("timestamp") or "",
                    "session": r.get("sessionId") or "",
                    "cwd": r.get("cwd") or "",
                    "uuid": r.get("uuid") or "",
                    "prompt": prompt,
                    "answer": "",
                }
            elif t == "assistant" and current and not r.get("isApiErrorMessage") and not r.get("isSidechain"):
                for b in (r.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "text":
                        txt = (b.get("text") or "").strip()
                        if txt:
                            current["answer"] = txt
    if current:
        yield current  # the last one may still be open; the caller decides


def load_excluded(store: str) -> list:
    path = os.path.join(store, "excluded-projects.txt")
    prefixes = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    prefixes.append(line.rstrip("/"))
    except OSError:
        pass
    return prefixes


def is_excluded(path: str, prefixes: list) -> bool:
    path = (path or "").rstrip("/")
    return any(path == p or path.startswith(p + "/") for p in prefixes if p)


def load_ledger(store: str) -> dict:
    try:
        with open(os.path.join(store, "backfill.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_ledger(store: str, ledger: dict) -> None:
    tmp = os.path.join(store, "backfill.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(ledger, fh, indent=2)
    os.replace(tmp, os.path.join(store, "backfill.json"))


def collect(files: list, prefixes: list, skip_open: bool):
    """Exchanges from every file, each tagged with its project (the session's
    starting directory), with a reason for every file or exchange left out."""
    items, report = [], collections.OrderedDict()
    for f in files:
        pairs = list(pairs_from(f))
        if pairs and not pairs[-1]["answer"]:
            pairs = pairs[:-1]
        project = pairs[0]["cwd"] if pairs else ""
        entry = report.setdefault(project or f, {"files": 0, "exchanges": 0, "first": None, "last": None, "status": "to import"})
        entry["files"] += 1
        if not pairs:
            entry["status"] = "no exchanges"
            continue
        if is_excluded(project, prefixes):
            entry["status"] = "EXCLUDED"
            entry["exchanges"] += len(pairs)
            continue
        if project.startswith(TEMP_ROOTS):
            entry["status"] = "skipped: temporary directory"
            entry["exchanges"] += len(pairs)
            continue
        for p in pairs:
            if not p["answer"] or is_excluded(p["cwd"], prefixes):
                continue
            p["project"] = project
            p["file"] = f
            items.append(p)
            entry["exchanges"] += 1
            entry["first"] = min(entry["first"], p["ts"]) if entry["first"] else p["ts"]
            entry["last"] = max(entry["last"], p["ts"]) if entry["last"] else p["ts"]
    items.sort(key=lambda p: p["ts"])
    if skip_open and items:
        items = items[:-1]  # the newest exchange belongs to a turn that is still running
    return items, report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transcript", help="one session's .jsonl file")
    ap.add_argument("--all-projects", action="store_true", help="every session under the projects root")
    ap.add_argument("--projects-root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--store", default=os.environ.get("ASMC_STORE_DIR"), help="memory store directory")
    ap.add_argument("--dry-run", action="store_true", help="report what would be stored, store nothing")
    ap.add_argument("--skip-open", action="store_true",
                    help="skip the newest exchange (its turn is still running)")
    ap.add_argument("--all", action="store_true", help="ignore the ledger and consider every exchange")
    ap.add_argument("--limit", type=int, default=0, help="store at most N exchanges")
    args = ap.parse_args()
    if not args.transcript and not args.all_projects:
        ap.error("give --transcript FILE or --all-projects")

    if args.store:
        os.environ["ASMC_STORE_DIR"] = args.store
    import asmc_daemon  # noqa: E402  (reads ASMC_STORE_DIR at import)
    store = asmc_daemon.STORE
    prefixes = load_excluded(store)

    files = [args.transcript] if args.transcript else sorted(glob.glob(os.path.join(args.projects_root, "*", "*.jsonl")))
    items, report = collect(files, prefixes, args.skip_open)

    ledger = load_ledger(store)
    todo = [] if args.all else None
    for p in items:
        done_ts = (ledger.get(p["session"]) or {}).get("last_ts", "")
        if args.all or p["ts"] > done_ts:
            (todo if todo is not None else items).append(p) if todo is not None else None
    if todo is None:
        todo = [p for p in items if p["ts"] > (ledger.get(p["session"]) or {}).get("last_ts", "")]
    if args.limit:
        todo = todo[: args.limit]
    chars = sum(len(p["prompt"]) + len(p["answer"]) for p in todo)

    print(f"{'project':58} {'files':>5} {'exch':>5} {'first':10} {'last':10}  status")
    for proj, e in report.items():
        print(f"{proj[:58]:58} {e['files']:5d} {e['exchanges']:5d} {(e['first'] or '')[:10]:10} {(e['last'] or '')[:10]:10}  {e['status']}")
    print(json.dumps({"store": store, "excluded_prefixes": prefixes, "exchanges_found": len(items),
                      "already_imported": len(items) - len(todo), "to_store": len(todo), "chars": chars,
                      "first": todo[0]["ts"] if todo else None, "last": todo[-1]["ts"] if todo else None}))
    if args.dry_run or not todo:
        return 0

    if not asmc_daemon.healthy():
        print("daemon is not running; start it first (python asmc_daemon.py --ensure)", file=sys.stderr)
        return 2

    started = time.time()
    stored = direct = dupes = failed = 0
    last_ok = {}

    def checkpoint() -> None:
        for sess, ts in last_ok.items():
            prev = ledger.get(sess) or {}
            ledger[sess] = {"last_ts": max(ts, prev.get("last_ts", "")), "count": prev.get("count", 0),
                            "updated": datetime.now(timezone.utc).isoformat()}
        save_ledger(store, ledger)

    counts = collections.Counter()
    for i, p in enumerate(todo, 1):
        body = {"situation": p["prompt"], "response": p["answer"], "when": p["ts"],
                "metadata": {"session": p["session"], "cwd": p["cwd"], "project": p["project"], "platform": p["project"],
                             "kind": "exchange", "source": "backfill", "prompt_uuid": p["uuid"]}}
        status, out = asmc_daemon.call("POST", "/remember", body, timeout=300)
        if status is None or not isinstance(out, dict):
            failed += 1
            print(f"[{i}/{len(todo)}] failed: daemon unreachable or bad reply ({status})", file=sys.stderr)
            continue
        if out.get("ok"):
            if out.get("duplicate"):
                dupes += 1
            else:
                stored += 1
                counts[p["session"]] += 1
                direct += 1 if out.get("direct") else 0
            last_ok[p["session"]] = p["ts"]
        else:
            failed += 1
            if out.get("reason") == "excluded project":
                print(f"[{i}/{len(todo)}] refused by the daemon: excluded project {p['project']}", file=sys.stderr)
        if i % 10 == 0 or i == len(todo):
            print(f"[{i}/{len(todo)}] stored={stored} (direct to long-term: {direct}) duplicate={dupes} failed={failed} "
                  f"{time.time() - started:.0f}s", flush=True)
            for sess, n in counts.items():
                entry = ledger.setdefault(sess, {})
                entry["count"] = entry.get("count", 0) + n
            counts.clear()
            checkpoint()
        if failed and failed >= 5 and stored == 0:
            print("too many failures, stopping", file=sys.stderr)
            break

    checkpoint()
    print(json.dumps({"stored": stored, "direct_to_long_term": direct, "duplicate": dupes, "failed": failed,
                      "seconds": round(time.time() - started, 1)}))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
