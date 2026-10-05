#!/usr/bin/env python3
"""Run a Hermes job in the background and announce its result when it finishes.

    bg_task.py start --title "Benchmark analysis" --goal "..."   # returns at once
    bg_task.py list                                              # running and finished jobs
    bg_task.py show JOB_ID                                       # full result of one job
    bg_task.py cancel JOB_ID                                     # stop a running job

`start` detaches a worker and prints the job id. The worker runs one Hermes query
(`hermes chat --oneshot`) with the subagent model from ~/.hermes/config.yaml
(delegation.model / delegation.provider), saves the full answer, and announces a
one-sentence summary through the speech command (the robot's speech outbox).
While it runs, the worker appends one line per helper step (each tool call, failed
tool, and any note the helper writes between tools) to JOB_ID.progress, and keeps the
latest step in the record. Each record (JOB_ID.json) keeps the status, timings, step
count, latest step, full answer and spoken summary;
`mentioned` is set once the result has been shown to the main agent, so a client can
feed newly finished jobs back into the conversation (see the xiaozhi provider).

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
import signal
import threading
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


def _elapsed(job: dict) -> str:
    seconds = job.get("seconds")
    if seconds is None and job.get("started_ts"):
        seconds = time.time() - job["started_ts"]
    if seconds is None:
        return ""
    return f"{round(seconds)} s" if seconds < 90 else f"{round(seconds / 60)} min"


def _announce(text: str) -> None:
    command = shlex.split(os.environ.get("BG_ANNOUNCE_CMD", DEFAULT_ANNOUNCE)) + [text[:SPOKEN_LIMIT]]
    try:
        subprocess.run(command, timeout=60, check=False, capture_output=True)
    except Exception as exc:
        print(f"announce failed: {exc}", file=sys.stderr)


def _spoken_summary(answer: str, title: str) -> str:
    found = re.findall(r"SPOKEN:\s*(.+)$", answer, re.M)  # the marker is not always at a line start
    sentence = found[-1] if found else answer.strip().splitlines()[-1] if answer.strip() else ""
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
        "started_ts": time.time(),
    }
    _save(job)
    log = open(JOBS / f"{job['id']}.log", "ab")
    command = [sys.executable, os.path.abspath(__file__), "_run", job["id"]]
    if shutil.which("systemd-run"):
        # Run in its own systemd scope: a job started from a service (the Hermes gateway)
        # would otherwise be killed with that service on every restart.
        scoped = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                  f"--unit=jarvis-bg-{job['id']}"] + command
        if subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True).returncode in (0, 1):
            command = scoped
    worker = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True,  # survive the terminal tool that started it
    )
    job["pid"] = worker.pid
    _save(job)
    print(f"started {job['id']}: {job['title']}")


def _preview(value, limit: int = 140) -> str:
    """One line from a tool input: the command, query or first string argument."""
    if isinstance(value, dict):
        for key in ("command", "query", "goal", "url", "path", "name"):
            if isinstance(value.get(key), str):
                value = value[key]
                break
        else:
            value = next((v for v in value.values() if isinstance(v, str)), json.dumps(value))
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


class Progress:
    """Append helper steps to JOB_ID.progress and keep the latest one in the job record."""

    def __init__(self, job: dict):
        self.job = job
        self.path = JOBS / f"{job['id']}.progress"
        self.note = ""
        self.saved_at = 0.0

    def add(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self.path.open("a") as f:
            f.write(f"{stamp} {line}\n")
        self.job["steps"] = self.job.get("steps", 0) + 1
        self.job["last_step"] = line
        self.job["last_step_ts"] = time.time()
        if time.time() - self.saved_at > 5:  # the record is read by other processes; keep writes light
            self.save()

    def save(self) -> None:
        current = _load(self.job["id"])
        if current.get("status") == "cancelled":
            return
        for key in ("steps", "last_step", "last_step_ts"):
            if key in self.job:
                current[key] = self.job[key]
        _save(current)
        self.saved_at = time.time()

    def event(self, event: dict) -> str | None:
        """Log one stream-json event; return the final answer on the result event."""
        kind = event.get("type")
        if kind == "text":
            self.note += event.get("text", "")
        elif kind == "tool_use":
            note = " ".join(self.note.split())
            if note:
                self.add(f"note: {_preview(note, 200)}")
            self.note = ""
            self.add(f"{event.get('name')}: {_preview(event.get('input'))}")
        elif kind == "tool_result" and event.get("is_error"):
            self.add(f"{event.get('name')} failed: {_preview(event.get('output'))}")
        elif kind == "result":
            return event.get("text") or ""
        return None


def cmd_run(args) -> None:
    job = _load(args.job_id)
    job["pid"] = os.getpid()
    _save(job)
    model, provider = _subagent_model()
    command = [_hermes(), "chat", "--oneshot", "--format", "stream-json", "--source", "background",
               "--max-turns", "40", "--run-budget", str(BUDGET_S),
               "-q", f"{INSTRUCTIONS}\n\nJob: {job['goal']}"]
    if model:
        command += ["-m", model]
    if provider:
        command += ["--provider", provider]
    started = time.monotonic()
    progress = Progress(job)
    progress.add("started")
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        killer = threading.Timer(BUDGET_S + 120, proc.kill)  # hard stop if the budget is ignored
        killer.start()
        answer = None
        try:
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                result = progress.event(event)
                if result is not None:
                    answer = result.strip()
            proc.wait()
        finally:
            killer.cancel()
        progress.save()
        if proc.returncode != 0 or not answer:
            err = (proc.stderr.read() or "no answer").strip().splitlines()
            raise RuntimeError((err[-1] if err else "no answer")[:200])
        spoken = _spoken_summary(answer, job["title"])
        if _load(job["id"]).get("status") == "cancelled":
            return
        job = _load(job["id"])
        job.update(status="done", answer=answer, spoken=spoken, seconds=round(time.monotonic() - started))
        _save(job)
        progress.add("finished")
        _announce(spoken)
    except Exception as exc:
        if _load(job["id"]).get("status") == "cancelled":
            return
        progress.add(f"stopped: {exc}"[:200])
        job = _load(job["id"])
        spoken = f"Your helper could not finish {job['title']}."
        job.update(status="failed", error=str(exc)[:300], spoken=spoken, seconds=round(time.monotonic() - started))
        _save(job)
        _announce(spoken)


def cmd_list(_args) -> None:
    jobs = _jobs()
    if not jobs:
        print("no background jobs")
    for job in jobs[-15:]:
        took = _elapsed(job)
        took = f" ({'for ' if job['status'] == 'running' else ''}{took})" if took else ""
        latest = ""
        if job["status"] == "running" and job.get("last_step"):
            ago = round(time.time() - job.get("last_step_ts", time.time()))
            latest = f" | {job.get('steps', 0)} steps, latest {ago} s ago: {job['last_step']}"
        print(f"{job['id']} | {job['status']}{took} | {job['title']}{latest}")


def cmd_cancel(args) -> None:
    job = _load(args.job_id)
    if job.get("status") != "running":
        print(f"{job['id']} is already {job.get('status')}")
        return
    job.update(status="cancelled", seconds=round(time.time() - job.get("started_ts", time.time())), mentioned=True)
    _save(job)
    try:
        os.killpg(job["pid"], signal.SIGTERM)  # the worker leads its own session: stops hermes too
    except (OSError, KeyError):
        pass
    print(f"cancelled {job['id']}: {job['title']}")


def cmd_show(args) -> None:
    job = _load(args.job_id)
    log = JOBS / f"{job['id']}.progress"
    steps = log.read_text().splitlines() if log.exists() else []
    print(f"{job['title']} [{job['status']}] started {job['started']}, {len(steps)} steps logged")
    print(f"goal: {job['goal']}")
    if steps:
        print("recent steps:")
        for step in steps[-args.steps:]:
            print(f"  {step}")
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
    show.add_argument("--steps", type=int, default=12, help="how many recent steps to print")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("job_id")
    run = sub.add_parser("_run")  # internal: the detached worker
    run.add_argument("job_id")
    args = parser.parse_args()
    {"start": cmd_start, "_run": cmd_run, "list": cmd_list, "show": cmd_show, "cancel": cmd_cancel}[args.cmd](args)


if __name__ == "__main__":
    main()
