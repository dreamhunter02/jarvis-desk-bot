#!/usr/bin/env python3
"""Run a Hermes job in the background and announce its result when it finishes.

    bg_task.py start --title "Benchmark analysis" --goal "..."   # returns at once
    bg_task.py list                                              # running and finished jobs
    bg_task.py show JOB_ID                                       # full result of one job

`start` detaches a worker and prints the job id. The worker runs one Hermes query
(`hermes chat --oneshot`) with the subagent model from ~/.hermes/config.yaml
(delegation.model / delegation.provider), saves the full answer, and announces a
one-sentence summary through the speech command (the robot's speech outbox).

Environment:
    BG_TASK_DIR          job records (default ~/.hermes/background)
    BG_ANNOUNCE_CMD      speech command; the summary is appended as the last argument
                         (default: python3 ~/.hermes/speech/speech.py enqueue --text)
    BG_TASK_MAX_RUNNING  concurrent jobs (default 3)
    BG_TASK_BUDGET_S     wall-clock budget per job (default 1200)
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

HOME = Path.home()
JOBS = Path(os.environ.get("BG_TASK_DIR", HOME / ".hermes/background"))
DEFAULT_ANNOUNCE = f"python3 {HOME / '.hermes/speech/speech.py'} enqueue --text"
MAX_RUNNING = int(os.environ.get("BG_TASK_MAX_RUNNING", "3"))
BUDGET_S = int(os.environ.get("BG_TASK_BUDGET_S", "1200"))
SPOKEN_LIMIT = 280

INSTRUCTIONS = (
    "You are a background helper. Complete the job below with your tools, verify the result, "
    "and do not ask questions: if something is ambiguous, make a sensible choice and say so. "
    "End your reply with a final line that starts with 'SPOKEN:' followed by one plain sentence "
    "under 30 words summarizing the outcome for a voice assistant (no markdown, links or IDs)."
)


def _hermes() -> str:
    return shutil.which("hermes") or str(HOME / ".local/bin/hermes")


def _subagent_model() -> tuple[str | None, str | None]:
    try:
        import yaml  # Hermes's Python environment usually has it; fall back to defaults if not

        cfg = yaml.safe_load((HOME / ".hermes/config.yaml").read_text()) or {}
        delegation = cfg.get("delegation") or {}
        return delegation.get("model"), delegation.get("provider")
    except Exception:
        return None, None


def _record(job_id: str) -> Path:
    return JOBS / f"{job_id}.json"


def _load(job_id: str) -> dict:
    return json.loads(_record(job_id).read_text())


def _save(job: dict) -> None:
    JOBS.mkdir(parents=True, exist_ok=True)
    tmp = _record(job["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=1))
    tmp.replace(_record(job["id"]))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _jobs() -> list[dict]:
    if not JOBS.exists():
        return []
    jobs = []
    for path in sorted(JOBS.glob("*.json")):
        try:
            job = json.loads(path.read_text())
        except ValueError:
            continue
        if job.get("status") == "running" and job.get("pid") and not _alive(job["pid"]):
            job["status"] = "failed"
            job["error"] = "worker exited unexpectedly"
            _save(job)
        jobs.append(job)
    return jobs


def _announce(text: str) -> None:
    command = shlex.split(os.environ.get("BG_ANNOUNCE_CMD", DEFAULT_ANNOUNCE)) + [text[:SPOKEN_LIMIT]]
    try:
        subprocess.run(command, timeout=60, check=False, capture_output=True)
    except Exception as exc:
        print(f"announce failed: {exc}", file=sys.stderr)


def _spoken_summary(answer: str, title: str) -> str:
    match = re.search(r"^\s*SPOKEN:\s*(.+)$", answer, re.M)
    sentence = match.group(1) if match else answer.strip().splitlines()[-1] if answer.strip() else ""
    sentence = re.sub(r"[*_`#>|]+|https?://\S+", "", sentence)
    sentence = re.sub(r"^[^\w\"']+", "", sentence).strip()  # leading emoji
    return f"Your helper finished {title}: {sentence}" if sentence else f"Your helper finished {title}."


def cmd_start(args) -> None:
    running = [j for j in _jobs() if j.get("status") == "running"]
    if len(running) >= MAX_RUNNING:
        print(f"busy: {len(running)} background jobs already running; try again when one finishes")
        sys.exit(1)
    job = {
        "id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4],
        "title": args.title.strip() or "the job",
        "goal": args.goal.strip(),
        "status": "running",
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    _save(job)
    log = open(JOBS / f"{job['id']}.log", "ab")
    worker = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "_run", job["id"]],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True,  # survive the terminal tool that started it
    )
    job["pid"] = worker.pid
    _save(job)
    print(f"started {job['id']}: {job['title']}")


def cmd_run(args) -> None:
    job = _load(args.job_id)
    job["pid"] = os.getpid()
    _save(job)
    model, provider = _subagent_model()
    command = [_hermes(), "chat", "-Q", "--oneshot", "--source", "background",
               "--max-turns", "40", "--run-budget", str(BUDGET_S),
               "-q", f"{INSTRUCTIONS}\n\nJob: {job['goal']}"]
    if model:
        command += ["-m", model]
    if provider:
        command += ["--provider", provider]
    started = time.monotonic()
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=BUDGET_S + 120)
        answer = done.stdout.strip()
        if done.returncode != 0 or not answer:
            raise RuntimeError((done.stderr or "no output").strip().splitlines()[-1][:200])
        job.update(status="done", answer=answer, seconds=round(time.monotonic() - started))
        _save(job)
        _announce(_spoken_summary(answer, job["title"]))
    except Exception as exc:
        job.update(status="failed", error=str(exc)[:300], seconds=round(time.monotonic() - started))
        _save(job)
        _announce(f"Your helper could not finish {job['title']}.")


def cmd_list(_args) -> None:
    jobs = _jobs()
    if not jobs:
        print("no background jobs")
    for job in jobs[-15:]:
        took = f" ({job['seconds']} s)" if "seconds" in job else ""
        print(f"{job['id']} | {job['status']}{took} | {job['title']}")


def cmd_show(args) -> None:
    job = _load(args.job_id)
    print(f"{job['title']} [{job['status']}] started {job['started']}")
    print(f"goal: {job['goal']}")
    if job.get("answer"):
        print(job["answer"])
    if job.get("error"):
        print(f"error: {job['error']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    start = sub.add_parser("start")
    start.add_argument("--title", required=True, help="a few words, spoken in the announcement")
    start.add_argument("--goal", required=True, help="complete, self-contained instructions")
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("job_id")
    run = sub.add_parser("_run")  # internal: the detached worker
    run.add_argument("job_id")
    args = parser.parse_args()
    {"start": cmd_start, "_run": cmd_run, "list": cmd_list, "show": cmd_show}[args.cmd](args)


if __name__ == "__main__":
    main()
