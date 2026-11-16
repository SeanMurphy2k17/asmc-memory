#!/usr/bin/env python3
"""One-time setup for the asmc-memory plugin. Standard library only, so any
python3 (3.10 or newer) can run it.

Creates a private virtual environment inside the memory store directory,
installs the memory library's dependencies into it, and fetches the NLTK data
ASMC needs, so nothing is installed into the system Python or the plugin
folder. Safe to run again: it only redoes what is missing (use --force to
rebuild the venv from scratch).

  setup_env.py [--data-dir DIR] [--requirements FILE] [--force]
"""

import argparse
import json
import os
import subprocess
import sys
import time
import venv
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA = os.path.join(os.path.expanduser("~"), ".claude", "asmc-memory")
NLTK_PACKAGES = [
    "wordnet", "sentiwordnet", "omw-1.4",
    "punkt_tab", "averaged_perceptron_tagger", "averaged_perceptron_tagger_eng",
    "maxent_ne_chunker_tab", "words",
]
IMPORT_CHECK = "import faiss, lmdb, nltk, numpy, spellchecker; print('imports ok')"


def say(log, msg: str) -> None:
    line = f"[asmc setup] {msg}"
    print(line, flush=True)
    log.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
    log.flush()


def run(log, argv, env=None) -> int:
    say(log, "$ " + " ".join(argv))
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, env=env)
    assert proc.stdout is not None
    for line in proc.stdout:
        log.write(line)
        tail = line.rstrip()
        if tail and not tail.startswith("  "):
            print("    " + tail[:160], flush=True)
    proc.wait()
    log.flush()
    return proc.returncode


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=DEFAULT_DATA)
    ap.add_argument("--requirements", default=os.path.join(HERE, "requirements.txt"))
    ap.add_argument("--force", action="store_true", help="rebuild the venv even if present")
    args = ap.parse_args()

    if sys.version_info < (3, 10):
        print(f"[asmc setup] python 3.10+ required, this is {sys.version.split()[0]}", flush=True)
        return 2

    data = os.path.abspath(os.path.expanduser(args.data_dir))
    os.makedirs(data, exist_ok=True)
    venv_dir = os.path.join(data, "venv")
    bindir = "Scripts" if os.name == "nt" else "bin"
    py = os.path.join(venv_dir, bindir, "python.exe" if os.name == "nt" else "python3")
    nltk_dir = os.path.join(data, "nltk_data")
    env = dict(os.environ, NLTK_DATA=nltk_dir, PIP_DISABLE_PIP_VERSION_CHECK="1")
    t0 = time.time()

    with open(os.path.join(data, "setup.log"), "a", encoding="utf-8") as log:
        say(log, f"store: {data}")
        say(log, f"system python: {sys.executable} ({sys.version.split()[0]})")

        if args.force or not os.path.exists(py):
            say(log, "creating virtual environment" + (" (forced rebuild)" if args.force else ""))
            venv.EnvBuilder(with_pip=True, clear=args.force, symlinks=(os.name != "nt")).create(venv_dir)
        else:
            say(log, "virtual environment present")

        if not os.path.exists(py):
            say(log, f"venv python missing after creation: {py}")
            return 1

        say(log, "installing dependencies (faiss-cpu, lmdb, nltk, numpy, pyspellchecker); this can take a minute")
        rc = run(log, [py, "-m", "pip", "install", "--quiet", "-r", args.requirements], env=env)
        if rc != 0:
            say(log, f"pip install failed (exit {rc}); see {log.name}")
            return 1

        say(log, "fetching NLTK data (wordnet, sentiwordnet, taggers, tokenizers)")
        script = ("import nltk, sys\n"
                  f"pk = {NLTK_PACKAGES!r}\n"
                  "bad = [p for p in pk if not nltk.download(p, quiet=True)]\n"
                  "print('nltk missing: ' + ', '.join(bad)) if bad else print('nltk data ok')\n"
                  "sys.exit(1 if bad else 0)\n")
        rc = run(log, [py, "-c", script], env=env)
        if rc != 0:
            say(log, "NLTK download failed; check the network, then run /asmc setup again")
            return 1

        rc = run(log, [py, "-c", IMPORT_CHECK], env=env)
        if rc != 0:
            say(log, "import check failed")
            return 1

        versions = subprocess.run([py, "-m", "pip", "list", "--format=json"], capture_output=True,
                                  text=True, env=env).stdout
        try:
            wanted = {"faiss-cpu", "lmdb", "nltk", "numpy", "pyspellchecker"}
            versions = {p["name"]: p["version"] for p in json.loads(versions) if p["name"] in wanted}
        except ValueError:
            versions = {}
        with open(os.path.join(data, "setup.json"), "w", encoding="utf-8") as f:
            json.dump({"ok": True, "when": datetime.now(timezone.utc).isoformat(), "python": py,
                       "versions": versions, "nltk_data": nltk_dir}, f, indent=2)
        say(log, f"done in {time.time() - t0:.0f}s: {versions}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
