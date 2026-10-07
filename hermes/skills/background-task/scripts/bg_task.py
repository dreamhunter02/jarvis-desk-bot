#!/usr/bin/env python3
"""Background work for a Hermes voice agent: quick helpers and long-running goal agents.

    bg_task.py start --title "..." --goal "..."    # helper: one run, about 10 min / 60 steps
    bg_task.py goal  --title "..." --goal "..."    # goal agent: rounds, hours, notes between rounds
    bg_task.py tell JOB_ID "message"               # add guidance; resumes a waiting goal agent
    bg_task.py list                                # all jobs, with the latest step of running ones
    bg_task.py show JOB_ID [--steps N]             # recent steps, notes and result of one job
    bg_task.py cancel JOB_ID                       # stop a job

Both kinds return at once and run detached in their own systemd user scope (so restarting
the Hermes gateway does not kill them). Each Hermes run is `hermes chat --oneshot --format
stream-json`; every step (tool call, failed tool, note between tools) is appended to
JOB_ID.progress and the latest one is kept in the record JOB_ID.json.

A helper runs once with the subagent model (delegation.model in ~/.hermes/config.yaml). If
it runs out of steps or time it is reported as stopped, with how far it got.

A goal agent runs in rounds with the main model. Its memory between rounds is a notes file
(JOB_ID.notes.md) it rewrites at the end of each round, plus the previous round's summary
and any messages sent with `tell`. Each round ends with GOAL_STATUS: done, continue or
blocked. It stops when done, waits for the user when blocked, and pauses after its round or
time limit; `tell` resumes a waiting or paused goal agent.

Results are announced through a speech command. A record's `mentioned` flag is set once a
client (the xiaozhi provider) has shown the outcome to the main agent.

Environment:
    BG_TASK_DIR          job records (default ~/.hermes/background)
    BG_ANNOUNCE_CMD      speech command; the text is appended as the last argument
                         (default: python3 ~/.hermes/speech/speech.py enqueue --text)
    BG_TASK_MAX_RUNNING  jobs running at once, both kinds (default 3)
    BG_HELPER_BUDGET_S   helper time limit (default 600)      BG_HELPER_STEPS  (default 60)
    BG_GOAL_ROUND_S      goal round time limit (default 1800) BG_GOAL_STEPS    (default 150)
    BG_GOAL_ROUNDS       rounds before a goal agent pauses (default 8)
    BG_GOAL_BUDGET_S     total time before it pauses (default 14400)
    BG_GOAL_MODEL, BG_GOAL_PROVIDER   goal agent model (default: Hermes's main model)
"""

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HOME = Path.home()
JOBS = Path(os.environ.get("BG_TASK_DIR", HOME / ".hermes/background"))
DEFAULT_ANNOUNCE = f"python3 {HOME / '.hermes/speech/speech.py'} enqueue --text"
MAX_RUNNING = int(os.environ.get("BG_TASK_MAX_RUNNING", "3"))
HELPER_BUDGET_S = int(os.environ.get("BG_HELPER_BUDGET_S", "600"))
HELPER_STEPS = int(os.environ.get("BG_HELPER_STEPS", "60"))
GOAL_ROUND_S = int(os.environ.get("BG_GOAL_ROUND_S", "1800"))
GOAL_STEPS = int(os.environ.get("BG_GOAL_STEPS", "150"))
GOAL_ROUNDS = int(os.environ.get("BG_GOAL_ROUNDS", "8"))
GOAL_BUDGET_S = int(os.environ.get("BG_GOAL_BUDGET_S", "14400"))
SPOKEN_LIMIT = 280
ACTIVE = ("running",)

# Ways of working that stay inside Hermes's command safety checks. Helpers cannot ask for
# approval, so a blocked command only burns steps.
WORK_RULES = (
    "Work rules:\n"
    "- Write files with the write_file tool, never with shell redirects, heredocs or `cat >`; "
    "the shell blocks writes into dot-directories such as ~/.hermes.\n"
    "- Keep each terminal command short and simple. For loops, several chained commands or "
    "nested $( ), write a script with write_file and run it.\n"
    "- Never use nohup, setsid or disown. For a process you only need while you work, use the "
    "terminal tool with background=true. For a service that must keep running after you finish "
    "(for example a model server), start it with `systemd-run --user --unit=NAME --collect "
    "COMMAND`, then check it with `systemctl --user status NAME` and `journalctl --user -u NAME`.\n"
    "- Never sleep more than 60 seconds in one command; poll instead.\n"
    "- If a command is blocked by the security scan, do not retry variations of it; use the "
    "approaches above.\n"
)

HELPER_INSTRUCTIONS = (
    "You are a quick background helper with about {minutes} minutes and {steps} steps. Complete "
    "the job below with your tools and verify the result. Do not ask questions: if something is "
    "ambiguous, make a sensible choice and say so. If the job is too big for that budget, do the "
    "most useful part and say clearly what remains.\n\n" + WORK_RULES + "\n"
    "End your reply with a final line that starts with 'SPOKEN:' followed by one plain sentence "
    "under 30 words summarizing the outcome for a voice assistant (no markdown, links or IDs)."
)

GOAL_INSTRUCTIONS = (
    "You are a goal agent working toward a long-running goal in rounds. This is round {round}. "
    "Each round has up to {steps} steps and {minutes} minutes; you will be called again for the "
    "next round. Your only memory between rounds is the notes file {notes}: open it with "
    "read_file first (write_file refuses to overwrite a file you have not read), and before "
    "this round ends rewrite it with write_file: what is done and verified, the current "
    "state (services, paths, ports, versions), what to do next, and open problems. Work "
    "carefully, verify each step, and do not repeat work the notes say is done. Do not ask "
    "questions unless you are truly blocked.\n\n" + WORK_RULES + "\n"
    "End your reply with two lines:\n"
    "GOAL_STATUS: done | continue | blocked   (done = the goal is achieved and verified; "
    "continue = more work remains; blocked = you need the user, e.g. a decision, credentials "
    "or physical access)\n"
    "SPOKEN: one plain sentence under 30 words for a voice assistant: the result if done, your "
    "question if blocked, progress so far if continuing (no markdown, links or IDs)."
)


# --- records -----------------------------------------------------------------------------

def _record(job_id: str) -> Path:
    return JOBS / f"{job_id}.json"


def _load(job_id: str) -> dict:
    return json.loads(_record(job_id).read_text())


def _save(job: dict) -> None:
    JOBS.mkdir(parents=True, exist_ok=True)
    tmp = _record(job["id"]).with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=1))
    tmp.replace(_record(job["id"]))


def _update(job_id: str, **fields) -> dict:
    """Merge fields into the stored record (other processes write it too)."""
    job = _load(job_id)
    job.update(fields)
    _save(job)
    return job


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
        if job.get("status") in ACTIVE and job.get("pid") and not _alive(job["pid"]):
            job.update(status="failed", error="worker exited unexpectedly")
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


# --- speech ------------------------------------------------------------------------------

def _announce(text: str) -> None:
    command = shlex.split(os.environ.get("BG_ANNOUNCE_CMD", DEFAULT_ANNOUNCE)) + [text[:SPOKEN_LIMIT]]
    try:
        subprocess.run(command, timeout=60, check=False, capture_output=True)
    except Exception as exc:
        print(f"announce failed: {exc}", file=sys.stderr)


def _sentence(answer: str) -> str:
    """The SPOKEN line of an answer, or a clean short last line; "" if neither is speakable."""
    text = re.sub(r"<tool_call>.*?(?:</tool_call>|$)", " ", answer or "", flags=re.S)  # tool calls written as text
    found = re.findall(r"SPOKEN:\s*(.+)$", text, re.M)  # not always at a line start
    if found:
        sentence = found[-1]
    else:
        lines = [l for l in text.strip().splitlines() if l.strip() and not l.strip().startswith("GOAL_STATUS")]
        sentence = lines[-1] if lines else ""
        if len(sentence) > 240 or re.search(r"[<>{}]|arg_key|tool_call", sentence):
            return ""  # not a sentence anyone should hear
    sentence = re.sub(r"<[^>]*>|[*_`#|]+|https?://\S+", "", sentence)
    return re.sub(r"^[^\w\"']+", "", sentence).strip()  # leading emoji


def _goal_status(answer: str) -> str | None:
    found = re.findall(r"GOAL_STATUS:\s*(done|continue|blocked)", answer or "", re.I)
    return found[-1].lower() if found else None


# --- running hermes ----------------------------------------------------------------------

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
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Progress:
    """Append steps to JOB_ID.progress and keep the latest one in the job record."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.path = JOBS / f"{job_id}.progress"
        self.note = ""
        self.saved_at = 0.0
        self.session_id = None
        self.fields = {"steps": _load(job_id).get("steps", 0)}

    def add(self, line: str) -> None:
        with self.path.open("a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        self.fields.update(steps=self.fields["steps"] + 1, last_step=line, last_step_ts=time.time())
        if time.time() - self.saved_at > 5:  # the record is read by other processes; keep writes light
            self.save()

    def save(self) -> None:
        if _load(self.job_id).get("status") == "cancelled":
            return
        _update(self.job_id, **self.fields)
        self.saved_at = time.time()

    def event(self, event: dict) -> str | None:
        """Log one stream-json event; return the final answer on the result event."""
        kind = event.get("type")
        if kind == "system" and event.get("session_id"):
            self.session_id = event["session_id"]
        elif kind == "text":
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


def _turn_end_reason(session_id: str | None) -> str | None:
    """Why Hermes ended the run, from its agent log ("max_iterations_reached", "text_response", ...)."""
    log = HOME / ".hermes/logs/agent.log"
    if not session_id or not log.exists():
        return None
    with log.open("rb") as f:
        f.seek(max(0, log.stat().st_size - 4_000_000))
        tail = f.read().decode(errors="replace")
    found = re.findall(rf"\[{re.escape(session_id)}\][^\n]*Turn ended: reason=([a-z_]+)", tail)
    return found[-1] if found else None


def _run_hermes(job_id: str, prompt: str, model, provider, steps: int, budget_s: int,
                progress: Progress) -> dict:
    """One Hermes run. Returns answer, returncode, seconds and a plain stop reason."""
    command = [_hermes(), "chat", "--oneshot", "--format", "stream-json", "--source", "background",
               "--max-turns", str(steps), "--run-budget", str(budget_s), "-q", prompt]
    if model:
        command += ["-m", model]
    if provider:
        command += ["--provider", provider]
    err_path = JOBS / f"{job_id}.stderr"
    started = time.monotonic()
    with err_path.open("w") as err:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=err, text=True)
        killer = threading.Timer(budget_s + 120, proc.kill)  # hard stop if the budget is ignored
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
    seconds = time.monotonic() - started
    progress.save()
    stderr = [l for l in err_path.read_text(errors="replace").splitlines()
              if l.strip() and not l.startswith("session_id:")]
    text = (answer or "") + "\n" + "\n".join(stderr[-20:])
    ended = _turn_end_reason(progress.session_id) or ""
    if ended.startswith("max_iterations") or re.search(r"maximum iterations|max_iterations", text, re.I):
        reason = f"used all {steps} steps"
    elif seconds >= budget_s - 5 or "budget" in ended:
        reason = f"hit the {round(budget_s / 60)}-minute limit"
    elif proc.returncode == 0:
        reason = ""
    else:
        reason = (stderr[-1] if stderr else f"exited with code {proc.returncode}")[:200]
    return {"answer": answer or "", "rc": proc.returncode, "seconds": seconds, "reason": reason}


# --- launching ---------------------------------------------------------------------------

def _new_job(kind: str, title: str, goal: str) -> dict:
    running = [j for j in _jobs() if j.get("status") in ACTIVE]
    if len(running) >= MAX_RUNNING:
        print(f"busy: {len(running)} background jobs already running; try again when one finishes")
        sys.exit(1)
    job = {
        "id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4],
        "kind": kind,
        "title": title.strip() or "the job",
        "goal": goal.strip(),
        "status": "running",
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "started_ts": time.time(),
    }
    _save(job)
    return job


def _launch(job_id: str) -> None:
    """Start the worker detached, in its own systemd scope when available."""
    log = open(JOBS / f"{job_id}.log", "ab")
    command = [sys.executable, os.path.abspath(__file__), "_run", job_id]
    if shutil.which("systemd-run") and subprocess.run(
            ["systemctl", "--user", "is-system-running"], capture_output=True).returncode in (0, 1):
        # A job started from a service (the Hermes gateway) would otherwise die with it.
        command = ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                   f"--unit=jarvis-bg-{job_id}-{int(time.time())}"] + command
    worker = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                              start_new_session=True)  # also survive the terminal tool that started it
    _update(job_id, pid=worker.pid)


def cmd_start(args) -> None:
    job = _new_job("helper", args.title, args.goal)
    _launch(job["id"])
    print(f"started helper {job['id']}: {job['title']} (about {round(HELPER_BUDGET_S / 60)} min)")


def cmd_goal(args) -> None:
    job = _new_job("goal", args.title, args.goal)
    _update(job["id"], round=0, round_limit=GOAL_ROUNDS, inbox_read=0)
    (JOBS / f"{job['id']}.notes.md").write_text(f"# Notes: {job['title']}\n\n(No rounds yet.)\n")
    _launch(job["id"])
    print(f"started goal agent {job['id']}: {job['title']} (rounds of up to {round(GOAL_ROUND_S / 60)} min)")


def cmd_tell(args) -> None:
    job = _load(args.job_id)
    with (JOBS / f"{job['id']}.inbox").open("a") as f:
        f.write(f"[{time.strftime('%H:%M')}] {args.message.strip()}\n")
    if job.get("kind") == "goal" and job.get("status") in ("waiting", "paused"):
        _update(job["id"], status="running", mentioned=False,
                round_limit=job.get("round", 0) + GOAL_ROUNDS, budget_from=time.time())
        _launch(job["id"])
        print(f"resumed {job['id']} with your message")
    elif job.get("status") in ACTIVE:
        print(f"queued for {job['id']}: a goal agent reads it at its next round; a helper does not read messages")
    else:
        print(f"{job['id']} is {job.get('status')}; start a new job instead")


# --- workers -----------------------------------------------------------------------------

def _cancelled(job_id: str) -> bool:
    return _load(job_id).get("status") == "cancelled"


def _run_helper(job: dict) -> None:
    progress = Progress(job["id"])
    progress.add("started")
    model, provider = _subagent_model()
    prompt = HELPER_INSTRUCTIONS.format(minutes=round(HELPER_BUDGET_S / 60), steps=HELPER_STEPS)
    out = _run_hermes(job["id"], f"{prompt}\n\nJob: {job['goal']}", model, provider,
                      HELPER_STEPS, HELPER_BUDGET_S, progress)
    if _cancelled(job["id"]):
        return
    sentence, title = _sentence(out["answer"]), job["title"]
    if out["answer"] and not out["reason"]:
        spoken = f"Your helper finished {title}: {sentence}" if sentence else f"Your helper finished {title}."
        progress.add("finished")
        _update(job["id"], status="done", answer=out["answer"], spoken=spoken, seconds=round(out["seconds"]))
    elif out["answer"]:
        spoken = f"Your helper stopped before finishing {title} ({out['reason']})" + (f": {sentence}" if sentence else ".")
        progress.add(f"stopped: {out['reason']}")
        _update(job["id"], status="stopped", answer=out["answer"], error=out["reason"], spoken=spoken,
                seconds=round(out["seconds"]))
    else:
        reason = out["reason"] or "no answer"
        spoken = f"Your helper could not finish {title}: it {reason}." if reason.startswith(("used", "hit")) \
            else f"Your helper could not finish {title}."
        progress.add(f"failed: {reason}")
        _update(job["id"], status="failed", error=reason, spoken=spoken, seconds=round(out["seconds"]))
    _announce(spoken)


def _run_goal(job: dict) -> None:
    progress = Progress(job["id"])
    notes = JOBS / f"{job['id']}.notes.md"
    inbox = JOBS / f"{job['id']}.inbox"
    model = os.environ.get("BG_GOAL_MODEL") or None
    provider = os.environ.get("BG_GOAL_PROVIDER") or None
    budget_from = job.get("budget_from", job["started_ts"])
    empty_rounds = 0
    while True:
        job = _load(job["id"])
        n = job.get("round", 0) + 1
        if n > job.get("round_limit", GOAL_ROUNDS) or time.time() - budget_from > GOAL_BUDGET_S:
            limit = f"{n - 1} rounds" if n > job.get("round_limit", GOAL_ROUNDS) else f"{round(GOAL_BUDGET_S / 3600)} hours"
            sentence = _sentence(job.get("last_summary", ""))
            spoken = f"Your goal agent paused {job['title']} after {limit}" + (f": {sentence}" if sentence else ".")
            progress.add(f"paused after {limit}")
            _update(job["id"], status="paused", spoken=spoken, mentioned=False,
                    seconds=round(time.time() - job["started_ts"]))
            _announce(spoken)
            return
        lines = inbox.read_text().splitlines() if inbox.exists() else []
        messages, read = lines[job.get("inbox_read", 0):], len(lines)
        context = f"Goal: {job['goal']}\n\nNotes file ({notes}):\n{notes.read_text() if notes.exists() else '(missing)'}\n"
        if job.get("last_summary"):
            context += f"\nYour summary at the end of round {n - 1}:\n{job['last_summary']}\n"
        if messages:
            context += "\nNew messages from the user (they take priority):\n" + "\n".join(messages) + "\n"
        prompt = GOAL_INSTRUCTIONS.format(round=n, steps=GOAL_STEPS, minutes=round(GOAL_ROUND_S / 60), notes=notes)
        _update(job["id"], round=n, inbox_read=read)
        progress.add(f"round {n} started")
        out = _run_hermes(job["id"], f"{prompt}\n\n{context}", model, provider, GOAL_STEPS, GOAL_ROUND_S, progress)
        if _cancelled(job["id"]):
            return
        status = _goal_status(out["answer"])
        sentence = _sentence(out["answer"])
        progress.add(f"round {n} ended: {status or 'no status'}" + (f" ({out['reason']})" if out["reason"] else ""))
        if out["answer"]:
            _update(job["id"], last_summary=out["answer"][-3000:])
            empty_rounds = 0
        else:
            empty_rounds += 1
        if status == "done" and not out["reason"]:
            spoken = f"Your goal agent finished {job['title']}" + (f": {sentence}" if sentence else ".")
            _update(job["id"], status="done", answer=out["answer"], spoken=spoken, mentioned=False,
                    seconds=round(time.time() - job["started_ts"]))
            _announce(spoken)
            return
        if status == "blocked":
            spoken = f"Your goal agent on {job['title']} needs you" + (f": {sentence}" if sentence else ".")
            _update(job["id"], status="waiting", question=sentence, spoken=spoken, mentioned=False)
            _announce(spoken)
            return
        if empty_rounds >= 2:
            reason = out["reason"] or "no answer"
            spoken = f"Your goal agent could not continue {job['title']}."
            progress.add(f"failed: {reason}")
            _update(job["id"], status="failed", error=reason, spoken=spoken, mentioned=False,
                    seconds=round(time.time() - job["started_ts"]))
            _announce(spoken)
            return
        # continue, no status, or a round that ran out of steps or time: next round


def cmd_run(args) -> None:
    job = _update(args.job_id, pid=os.getpid())
    try:
        (_run_goal if job.get("kind") == "goal" else _run_helper)(job)
    except Exception as exc:  # report instead of dying silently
        if not _cancelled(job["id"]):
            _update(job["id"], status="failed", error=f"worker error: {exc}"[:300],
                    spoken=f"Your background job {job['title']} failed.")
            _announce(f"Your background job {job['title']} failed.")
        raise


# --- reporting ---------------------------------------------------------------------------

def _label(job: dict) -> str:
    return "goal agent" if job.get("kind") == "goal" else "helper"


def cmd_list(_args) -> None:
    jobs = _jobs()
    if not jobs:
        print("no background jobs")
    for job in jobs[-15:]:
        took = _elapsed(job)
        took = f" ({'for ' if job['status'] in ACTIVE else ''}{took})" if took else ""
        extra = ""
        if job.get("kind") == "goal" and job.get("round"):
            extra += f" | round {job['round']}"
        if job["status"] in ACTIVE and job.get("last_step"):
            ago = round(time.time() - job.get("last_step_ts", time.time()))
            extra += f" | {job.get('steps', 0)} steps, latest {ago} s ago: {job['last_step']}"
        if job["status"] == "waiting":
            extra += f" | needs you: {job.get('question', '')}"
        if job["status"] in ("stopped", "failed") and job.get("error"):
            extra += f" | {job['error']}"
        print(f"{job['id']} | {_label(job)} | {job['status']}{took} | {job['title']}{extra}")


def cmd_cancel(args) -> None:
    job = _load(args.job_id)
    if job.get("status") not in ACTIVE + ("waiting", "paused"):
        print(f"{job['id']} is already {job.get('status')}")
        return
    was_running = job.get("status") in ACTIVE
    _update(job["id"], status="cancelled", mentioned=True,
            seconds=round(time.time() - job.get("started_ts", time.time())))
    if was_running:
        try:
            os.killpg(job["pid"], signal.SIGTERM)  # the worker leads its own session: stops hermes too
        except (OSError, KeyError):
            pass
    print(f"cancelled {job['id']}: {job['title']}")


def cmd_show(args) -> None:
    job = _load(args.job_id)
    log = JOBS / f"{job['id']}.progress"
    steps = log.read_text().splitlines() if log.exists() else []
    round_info = f", round {job['round']}" if job.get("kind") == "goal" else ""
    print(f"{job['title']} [{_label(job)}, {job['status']}{round_info}] started {job['started']}, "
          f"{len(steps)} steps logged")
    print(f"goal: {job['goal']}")
    if job.get("question"):
        print(f"needs you: {job['question']}")
    if steps:
        print("recent steps:")
        for step in steps[-args.steps:]:
            print(f"  {step}")
    notes = JOBS / f"{job['id']}.notes.md"
    if notes.exists():
        print("notes:\n" + notes.read_text().strip())
    if job.get("answer"):
        print("result:\n" + job["answer"])
    elif job.get("last_summary"):
        print("latest round summary:\n" + job["last_summary"])
    if job.get("error"):
        print(f"stopped because: {job['error']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("start", "goal"):
        p = sub.add_parser(name)
        p.add_argument("--title", required=True, help="a few words, spoken in the announcement")
        p.add_argument("--goal", required=True, help="complete, self-contained instructions")
    tell = sub.add_parser("tell")
    tell.add_argument("job_id")
    tell.add_argument("message")
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("job_id")
    show.add_argument("--steps", type=int, default=12, help="how many recent steps to print")
    cancel = sub.add_parser("cancel")
    cancel.add_argument("job_id")
    run = sub.add_parser("_run")  # internal: the detached worker
    run.add_argument("job_id")
    args = parser.parse_args()
    {"start": cmd_start, "goal": cmd_goal, "tell": cmd_tell, "_run": cmd_run, "list": cmd_list,
     "show": cmd_show, "cancel": cmd_cancel}[args.cmd](args)


if __name__ == "__main__":
    main()
