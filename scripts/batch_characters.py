#!/usr/bin/env python3
"""Batch driver: run run_character.py over a name list with workers + retries.

  scripts/batch_characters.py NAMES.txt [--workers 3] [--offset 0] [--limit N]
                              [--state batch-state] [--retry-failed] [--dry-run]

One line per name (blank lines and '#' comments ignored). Each name runs the
full per-character pipeline in its own subprocess; run_character.py's own
resume logic means a retry only redoes the stage that failed.

Done detection: play/<slug>.osb6 + play/ui/<slug>/<slug>.osbui + announcer.wav
all exist -> skipped without spawning anything.

Retry policy (mirrors web-prototype/server/fighter-jobs.js automaticRetryPlan,
plus the torn-tri re-roll that only makes sense offline):
  transient   HTTP 429/5xx, rate limit, timeouts   -> up to 3 retries, 2s*2^n
                                                     backoff (mesh polling
                                                     resumes the paid task via
                                                     tripo_tasks.json, so this
                                                     is safe at every stage)
  moderation  moderation_blocked, stage=output     -> up to 2 re-rolls of that
                                                     image (same prompt; the
                                                     provider is stochastic)
              moderation_blocked, stage=input      -> terminal: the prompt /
                                                     name itself is refused
  torn-tri    "torn-tri gate: N > 80"              -> delete tpose/mesh and
                                                     re-roll, up to
                                                     --max-mesh-rerolls (1);
                                                     ~$0.57 per roll
  tripo       img3d/rig task failed or banned      -> up to --max-tripo-retries
                                                     (1) re-buys of the mesh
  other       anything else                        -> terminal, logged

State lives in <state>/results.jsonl (one line per finished name: ok or a
terminal failure) and <state>/failures.jsonl (every terminal failure with the
stage and log tail). Re-running the same command skips names already ok;
--retry-failed also re-queues the failed ones. Per-attempt output is appended
to play/ui/<slug>/batch.log. Touch <state>/STOP to finish in-flight work and
exit; Ctrl-C does the same (a second Ctrl-C kills the children).

Run long batches under `caffeinate -i` in tmux:
  caffeinate -i python3 scripts/batch_characters.py ../wikipedia-people-1000.txt --workers 3
"""
import argparse
import functools
import collections
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

print = functools.partial(print, flush=True)  # logs are usually redirected to a file

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # pipeline/
RUNNER = os.path.join(HERE, "pipeline", "run_character.py")
PLAY = os.environ.get("BATCH_PLAY_DIR") or os.path.join(HERE, "play")  # override for tests

TRANSIENT_RE = re.compile(
    r"HTTP (?:429|5\d\d)\b|rate[_ -]?limit|server_error|ETIMEDOUT|ECONNRESET|"
    r"TimeoutError|timed out|Connection reset|Remote end closed|"
    r"Temporary failure in name resolution|URLError|tripo empty response|substring not found|"
    r"\b(?:img3d|rig) (?:running|queued|pending|unknown)\b", re.I)  # Tripo task still alive: re-poll, no re-buy
MODERATION_STAGE_RE = re.compile(r"[\"']moderation_stage[\"']\s*:\s*[\"'](\w+)[\"']", re.I)
TORN_RE = re.compile(r"torn-tri gate: (\d+)")
TRIPO_FAIL_RE = re.compile(r"\b(?:img3d|rig) (?:failed|banned|expired|cancelled)\b")
PROGRESS_RE = re.compile(r"^@@opensmash (\{.*\})$", re.M)

# Files a torn-tri re-roll must remove so run_character regenerates the image
# and buys a fresh mesh (bundle.json is derived from rigged.glb).
REROLL_FILES = ("tpose.png", "rigged.glb", "rigged.glb.part", "tripo_tasks.json", "bundle.json")

STOP = threading.Event()
LOCK = threading.Lock()


def slug_of(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())[:16]


def outputs(slug):
    ui = os.path.join(PLAY, "ui", slug)
    return [os.path.join(PLAY, f"{slug}.osb6"),
            os.path.join(ui, f"{slug}.osbui"),
            os.path.join(ui, "announcer.wav")]


def is_done(slug):
    return all(os.path.exists(p) for p in outputs(slug))


NOTES = {}   # name -> per-character expander notes (optional TAB-separated second column)


def read_names(path):
    names = []
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip() if line.lstrip().startswith("#") else line.strip()
        if not line:
            continue
        name, _, notes = line.partition("\t")
        name = name.strip()
        names.append(name)
        if notes.strip():
            NOTES[name] = notes.strip()
    return names


def classify(log):
    """-> (kind, detail). kind in transient|moderation-output|moderation-input|
    torn-tri|tripo|other."""
    tail = log[-4000:]
    m = TORN_RE.search(tail)
    if m:
        return "torn-tri", m.group(1)
    if TRIPO_FAIL_RE.search(tail):
        return "tripo", TRIPO_FAIL_RE.search(tail).group(0)
    if "violation of content policy" in tail:   # Tripo code 2008: refused the t-pose image
        return "moderation-input", "tripo"
    if "moderation_blocked" in tail:
        st = MODERATION_STAGE_RE.search(tail)
        stage = st.group(1).lower() if st else "unknown"
        return ("moderation-output" if stage == "output" else "moderation-input"), stage
    if TRANSIENT_RE.search(tail):
        return "transient", TRANSIENT_RE.search(tail).group(0)
    return "other", ""


def last_stage(log):
    """Stage label from the last @@opensmash progress event (run_character
    emits one per stage), so a failure is attributed to the stage it died in."""
    stage = None
    for m in PROGRESS_RE.finditer(log):
        try:
            stage = json.loads(m.group(1)).get("stage") or stage
        except json.JSONDecodeError:
            pass
    return stage


def cost_of(slug):
    try:
        return json.load(open(os.path.join(PLAY, "ui", slug, "cost.json"))).get("total_usd")
    except (OSError, json.JSONDecodeError):
        return None


def append_jsonl(path, doc):
    with LOCK:
        with open(path, "a") as f:
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")


def load_results(path):
    res = {}
    if os.path.exists(path):
        for line in open(path):
            try:
                d = json.loads(line)
                res[d["name"]] = d
            except (json.JSONDecodeError, KeyError):
                pass
    return res


def run_once(args, name, slug, extra_args, procs):
    """One run_character invocation. Returns (returncode, combined log)."""
    cmd = [sys.executable, args.runner, name] + (["--notes", NOTES[name]] if name in NOTES else []) + extra_args
    ui = os.path.join(PLAY, "ui", slug)
    os.makedirs(ui, exist_ok=True)
    proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    with LOCK:
        procs[slug] = proc
    chunks = []
    try:
        out, _ = proc.communicate(timeout=args.attempt_timeout * 60)
        chunks.append(out or "")
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        chunks.append(out or "")
        chunks.append(f"\nBATCH: attempt timed out after {args.attempt_timeout} min\n")
    finally:
        with LOCK:
            procs.pop(slug, None)
    log = "".join(chunks)
    with open(os.path.join(ui, "batch.log"), "a") as f:
        f.write(f"\n===== {time.strftime('%Y-%m-%dT%H:%M:%S')} {' '.join(cmd)}\n{log}")
    return proc.returncode, log


def process(args, name, procs):
    slug = slug_of(name)
    t0 = time.time()
    counts = collections.Counter()
    attempts = []
    if is_done(slug):
        return {"name": name, "slug": slug, "outcome": "ok", "skipped": True,
                "cost_usd": cost_of(slug), "attempts": 0}
    while True:
        if STOP.is_set() and not attempts:
            return {"name": name, "slug": slug, "outcome": "stopped", "attempts": 0}
        rc, log = run_once(args, name, slug, args.runner_args, procs)
        stage = last_stage(log)
        if rc == 0 and is_done(slug):
            return {"name": name, "slug": slug, "outcome": "ok", "attempts": len(attempts) + 1,
                    "retries": dict(counts), "cost_usd": cost_of(slug),
                    "minutes": round((time.time() - t0) / 60, 1)}
        kind, detail = classify(log) if rc != 0 else ("other", "exited 0 but outputs missing")
        attempts.append({"kind": kind, "stage": stage, "detail": detail})
        say(f"{slug}: {kind} at {stage} ({detail}) attempt {len(attempts)}")

        retry, delay = False, 0
        if kind == "transient":
            if counts["transient"] < args.max_transient:
                # rate limits at high concurrency need real waits: 15s, 30s, 60s, 120s ...
                delay = 15 * (2 ** counts["transient"])
                counts["transient"] += 1
                retry = True
            else:
                kind = "transient-exhausted"
        elif kind == "moderation-output" and counts["moderation"] < args.max_moderation:
            counts["moderation"] += 1
            delay, retry = 1.5, True
        elif (kind == "moderation-input" and stage in ("portrait", "stock", "emblem")
              and counts["moderation"] < args.max_moderation):
            # past the t-pose the prompts carry no name; an input block here is
            # the provider judging the (already accepted) t-pose image, and it
            # is not deterministic — Harry Styles passed portrait then failed
            # stock on the same image. Worth the same re-rolls as output blocks.
            counts["moderation"] += 1
            delay, retry = 5, True
        elif kind == "torn-tri" and counts["reroll"] < args.max_mesh_rerolls:
            counts["reroll"] += 1
            for fn in REROLL_FILES:
                p = os.path.join(PLAY, "ui", slug, fn)
                if os.path.exists(p):
                    os.remove(p)
            say(f"{slug}: re-rolling tpose+mesh ({counts['reroll']}/{args.max_mesh_rerolls})")
            retry = True
        elif kind == "tripo" and counts["tripo"] < args.max_tripo_retries:
            counts["tripo"] += 1
            delay, retry = 30, True
        if not retry or STOP.is_set():
            fail = {"name": name, "slug": slug, "outcome": "failed",
                    "kind": kind if not retry else "stopped", "stage": stage, "detail": detail,
                    "attempts": attempts, "cost_usd": cost_of(slug),
                    "minutes": round((time.time() - t0) / 60, 1),
                    "log_tail": log[-1200:]}
            append_jsonl(os.path.join(args.state, "failures.jsonl"), fail)
            if (kind or "").startswith("moderation"):
                # human-readable ledger of what the image provider refused:
                # name, stage it died in, input/output, provider categories
                cats = re.search(r'"categories":\s*\[([^\]]*)\]', log[-4000:])
                cats = (",".join(c.strip().strip('"') for c in cats.group(1).split(",")) if cats
                        else "tripo-content-policy" if detail == "tripo" else "?")
                with LOCK:
                    with open(os.path.join(args.state, "moderation-blocked.txt"), "a") as f:
                        f.write(f"{name}\t{stage}\t{detail}\t{cats}\t{time.strftime('%Y-%m-%dT%H:%M')}\n")
            return fail
        time.sleep(delay)


def say(msg):
    with LOCK:
        print(time.strftime("%H:%M:%S"), msg, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--state", default=os.path.join(HERE, "batch-state"))
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-queue names recorded as failed in results.jsonl")
    ap.add_argument("--retry-kinds", default=None,
                    help="with --retry-failed: only re-queue these failure kinds, as kind or "
                         "kind@stage (comma list, e.g. other,tripo,moderation-input@stock); default all")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-transient", type=int, default=4)
    ap.add_argument("--max-moderation", type=int, default=2)
    ap.add_argument("--max-mesh-rerolls", type=int, default=1)
    ap.add_argument("--max-tripo-retries", type=int, default=1)
    ap.add_argument("--attempt-timeout", type=int, default=45, help="minutes per run_character attempt")
    ap.add_argument("--max-usd", type=float, default=None,
                    help="stop launching new names once this run's recorded spend reaches this")
    ap.add_argument("--publish", action="store_true",
                    help="add each finished character to the baked roster manifest (done serially "
                         "from the main thread; run_character's own --publish is not worker-safe)")
    ap.add_argument("--runner", default=RUNNER, help=argparse.SUPPRESS)
    ap.add_argument("--runner-args", nargs=argparse.REMAINDER, default=[],
                    help="extra args passed through to run_character.py (e.g. --variants fox,link)")
    args = ap.parse_args()
    if args.runner_args and args.runner_args[0] == "--":
        args.runner_args = args.runner_args[1:]

    names = read_names(args.names)
    names = names[args.offset:(args.offset + args.limit) if args.limit else None]

    by_slug = collections.defaultdict(list)
    for n in names:
        by_slug[slug_of(n)].append(n)
    dups = {k: v for k, v in by_slug.items() if len(v) > 1}
    if dups:
        for k, v in dups.items():
            print(f"slug collision '{k}': {v}", file=sys.stderr)
        sys.exit("refusing to run: fix the list (later names would resume the earlier name's outputs)")
    empty = [n for n in names if not slug_of(n)]
    if empty:
        sys.exit(f"names with an empty slug: {empty}")

    os.makedirs(args.state, exist_ok=True)
    lock_path = os.path.join(args.state, "LOCK")
    if os.path.exists(lock_path) and not args.dry_run:
        try:
            other = int(open(lock_path).read().strip() or 0)
            os.kill(other, 0)
            sys.exit(f"another batch (pid {other}) holds {lock_path}; two drivers would double-run names")
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # stale lock from a dead run
    results_path = os.path.join(args.state, "results.jsonl")
    stop_file = os.path.join(args.state, "STOP")
    if os.path.exists(stop_file):
        os.remove(stop_file)
    prior = load_results(results_path)

    todo, done, failed_prior = [], 0, 0
    for n in names:
        p = prior.get(n)
        if is_done(slug_of(n)) or (p and p.get("outcome") == "ok"):
            done += 1
        elif p and p.get("outcome") == "failed" and not (
                args.retry_failed and (not args.retry_kinds
                                       or p.get("kind") in args.retry_kinds.split(",")
                                       or f"{p.get('kind')}@{p.get('stage')}" in args.retry_kinds.split(","))):
            failed_prior += 1
        else:
            todo.append(n)
    print(f"{len(names)} names: {done} done, {failed_prior} failed earlier (skipped; --retry-failed to redo), "
          f"{len(todo)} to run with {args.workers} workers")
    partial = [n for n in todo if os.path.isdir(os.path.join(PLAY, "ui", slug_of(n)))]
    if partial:
        print(f"{len(partial)} resume partially-built dirs (paid stages already on disk are kept): "
              + ", ".join(slug_of(n) for n in partial[:10]) + (" ..." if len(partial) > 10 else ""))
    if args.dry_run:
        for n in todo[:25]:
            print("  would run:", n, "->", slug_of(n))
        if len(todo) > 25:
            print(f"  ... {len(todo) - 25} more")
        print(f"estimate: ~${0.65 * len(todo):.0f} at $0.65/char, ~{3.5 * len(todo) / max(args.workers, 1) / 60:.1f} h wall")
        return
    if not todo:
        return
    open(lock_path, "w").write(str(os.getpid()))
    import atexit
    atexit.register(lambda: os.path.exists(lock_path) and os.remove(lock_path))

    procs = {}

    def on_sigint(sig, frame):
        if STOP.is_set():
            say("second interrupt: killing children")
            with LOCK:
                for p in procs.values():
                    p.kill()
        else:
            say("interrupt: finishing in-flight characters, launching no more (again to kill)")
            STOP.set()
    signal.signal(signal.SIGINT, on_sigint)

    def watch_stop_file():
        while not STOP.is_set():
            if os.path.exists(stop_file):
                say("STOP file seen: finishing in-flight characters")
                STOP.set()
                return
            time.sleep(5)
    threading.Thread(target=watch_stop_file, daemon=True).start()

    t0 = time.time()
    tally = collections.Counter()
    spent = 0.0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {}
        it = iter(todo)
        # Feed the pool lazily so STOP can drain it instead of racing 1000 queued futures.
        def submit_next():
            if STOP.is_set():
                return False
            n = next(it, None)
            if n is None:
                return False
            futs[ex.submit(process, args, n, procs)] = n
            return True
        for _ in range(args.workers):
            if not submit_next():
                break
        while futs:
            for fut in as_completed(list(futs)):
                n = futs.pop(fut)
                try:
                    r = fut.result()
                except Exception as e:  # driver bug, not a pipeline failure
                    r = {"name": n, "slug": slug_of(n), "outcome": "failed", "kind": "driver-error",
                         "detail": repr(e), "attempts": []}
                    append_jsonl(os.path.join(args.state, "failures.jsonl"), r)
                if r.get("outcome") == "ok" and args.publish:
                    try:
                        sys.path.insert(0, os.path.join(HERE, "pipeline"))
                        from baked_roster import publish_character
                        r["published"] = publish_character(r["slug"])
                    except Exception as e:  # incomplete outputs etc.: never take the batch down
                        r["published"] = f"error: {e}"
                        say(f"{r['slug']}: publish failed: {e}")
                if r.get("outcome") != "stopped":
                    append_jsonl(results_path, {k: v for k, v in r.items() if k != "log_tail"})
                tally[r.get("outcome")] += 1
                if not r.get("skipped"):
                    spent += r.get("cost_usd") or 0
                if args.max_usd and spent >= args.max_usd and not STOP.is_set():
                    say(f"spend cap reached (${spent:.2f} >= ${args.max_usd:.2f}): finishing in-flight, launching no more")
                    STOP.set()
                elapsed = (time.time() - t0) / 60
                label = "ok" if r["outcome"] == "ok" else f"{r['outcome']}:{r.get('kind')}@{r.get('stage')}"
                say(f"[{sum(tally.values())}/{len(todo)}] {r['slug']}: {label}"
                    f"  ${r.get('cost_usd') or 0:.2f}  {r.get('minutes', 0)} min  "
                    f"(batch: ${spent:.2f}, {elapsed:.0f} min)")
                submit_next()
                break
    say(f"finished: {dict(tally)}  ${spent:.2f} on disk for this set  "
        f"{(time.time() - t0) / 60:.0f} min  -> {results_path}")


if __name__ == "__main__":
    main()
