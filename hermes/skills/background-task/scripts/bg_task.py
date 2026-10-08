#!/usr/bin/env python3
"""Background work for a Hermes voice agent: quick helpers and long-running goal agents.

    bg_task.py start --title "..." --goal "..."    # helper: one run, about 10 min / 60 steps
    bg_task.py goal  --title "..." --goal "..."    # goal agent: a Hermes kanban goal card, hours
    bg_task.py tell JOB_ID "message"               # answer a waiting goal agent, or add guidance
    bg_task.py list                                # all jobs, with the latest step of running ones
    bg_task.py show JOB_ID [--steps N]             # recent steps and result of one job
    bg_task.py cancel JOB_ID                       # stop a job

Both return at once. Workers run detached in their own systemd user scope, so restarting the
Hermes gateway does not kill them. The latest step of each job is kept in its record
(JOB_ID.json) for the main agent's status note.

A helper is one `hermes chat --oneshot --format stream-json` run with the subagent model
(delegation.model in ~/.hermes/config.yaml); every step is appended to JOB_ID.progress. If it
runs out of steps or time it is reported as stopped, with the reason from Hermes's log and how
far it got.

A goal agent is a Hermes kanban card in goal mode (`hermes kanban create --goal`): the kanban
dispatcher in the gateway runs it in its own session, and after each turn Hermes's goal judge
(auxiliary.goal_judge) decides whether it is done. When the worker needs the user it blocks
the card with a question; `tell` unblocks it with the answer. A watcher follows the card,
keeps its latest step from the worker log, and announces when it is done, needs the user, or
fails.

Results are announced through a speech command. A record's `mentioned` flag is set once a
client (the xiaozhi provider) has shown the outcome to the main agent.

Environment:
    BG_TASK_DIR          job records (default ~/.hermes/background)
    BG_ANNOUNCE_CMD      speech command; the text is appended as the last argument
                         (default: python3 ~/.hermes/speech/speech.py enqueue --text)
    BG_TASK_MAX_RUNNING  jobs running at once, both kinds (default 3)
    BG_HELPER_BUDGET_S   helper time limit (default 600)      BG_HELPER_STEPS (default 60)
    BG_GOAL_TURNS        goal-loop turns per card (default 30)
    BG_GOAL_RUNTIME      runtime cap per worker run (default 4h)
    BG_GOAL_MODEL, BG_GOAL_PROVIDER   goal agent model (default: the kanban profile's model)
    BG_GOAL_ASSIGNEE     kanban profile that runs goal cards (default "default")
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
GOAL_TURNS = int(os.environ.get("BG_GOAL_TURNS", "30"))
GOAL_RUNTIME = os.environ.get("BG_GOAL_RUNTIME", "4h")
GOAL_ASSIGNEE = os.environ.get("BG_GOAL_ASSIGNEE", "default")
WATCH_INTERVAL_S = 20
SPOKEN_LIMIT = 280
ACTIVE = ("running", "waiting")  # waiting = a goal card blocked on the user

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

GOAL_CARD_RULES = (
    "\n\n---\nHow to work on this card:\n" + WORK_RULES.replace(
        "Write files with the write_file tool,", "Write files with the write_file tool (find it with tool_search),")
    + "- If you need the user (a decision, credentials, physical access), block the card with "
    "kanban_block and make the reason one plain question.\n"
    "- When the goal is achieved and verified, complete the card. Start the completion summary "
    "with one plain sentence under 30 words that a voice assistant can read aloud (no markdown, "
    "links or IDs), then give the details.\n"
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
            if job.get("card"):  # the kanban card lives on; restart its watcher
                _launch(job["id"])
                job = _load(job["id"])
            else:
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
        reason = f"used all {steps} step{'s' if steps != 1 else ''}"
    elif seconds >= budget_s - 5 or "budget" in ended:
        reason = f"hit the {round(budget_s / 60)}-minute limit"
    elif proc.returncode == 0:
        reason = ""
    else:
        reason = (stderr[-1] if stderr else f"exited with code {proc.returncode}")[:200]
    return {"answer": answer or "", "rc": proc.returncode, "seconds": seconds, "reason": reason}


# --- launching ---------------------------------------------------------------------------

def _new_job(kind: str, title: str, goal: str) -> dict:
    running = [j for j in _jobs() if j.get("status") == "running"]  # waiting cards do not count
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


def _kanban(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([_hermes(), "kanban", *args], capture_output=True, text=True, timeout=timeout)


def _card(card_id: str) -> dict:
    out = _kanban("show", card_id, "--json")
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[-200:] or "kanban show failed")
    return json.loads(out.stdout)


def cmd_goal(args) -> None:
    job = _new_job("goal", args.title, args.goal)
    command = ["create", job["title"], "--body", job["goal"] + GOAL_CARD_RULES, "--goal",
               "--goal-max-turns", str(GOAL_TURNS), "--max-runtime", GOAL_RUNTIME,
               "--assignee", GOAL_ASSIGNEE, "--json"]
    model, provider = os.environ.get("BG_GOAL_MODEL"), os.environ.get("BG_GOAL_PROVIDER")
    if model:
        command += ["--model", model]
    if provider:
        command += ["--provider", provider]
    out = _kanban(*command)
    try:
        card_id = json.loads(out.stdout)["id"]
    except (ValueError, KeyError):
        _update(job["id"], status="failed", error=(out.stderr or out.stdout).strip()[-300:])
        print(f"could not create the goal card: {(out.stderr or out.stdout).strip()[-300:]}")
        sys.exit(1)
    _update(job["id"], card=card_id)
    _launch(job["id"])
    print(f"started goal agent {job['id']} (kanban card {card_id}): {job['title']}")


def cmd_tell(args) -> None:
    job = _load(args.job_id)
    if job.get("kind") != "goal":
        print("helpers do not read messages; cancel it and start a new one with the change")
        return
    if job.get("status") not in ACTIVE:
        print(f"{job['id']} is {job.get('status')}; start a new job instead")
        return
    card = _card(job["card"])["task"]
    if card.get("status") == "blocked":
        out = _kanban("unblock", job["card"], "--reason", args.message.strip())
        _update(job["id"], status="running", question=None)
        print(f"passed your answer to {job['id']}; it resumes within a minute" if out.returncode == 0
              else f"unblock failed: {out.stderr.strip()[-200:]}")
    else:
        out = _kanban("comment", job["card"], args.message.strip())
        print(f"added your message to {job['id']}'s card; the worker reads it on its next turn" if out.returncode == 0
              else f"comment failed: {out.stderr.strip()[-200:]}")


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


def _latest_worker_step(card_id: str) -> str | None:
    """Last tool line ("┊ 💻 $ mkdir -p ...") from the kanban worker's log."""
    out = _kanban("log", card_id, "--tail", "6000")
    lines = [l.strip() for l in out.stdout.splitlines() if l.strip().startswith("┊") and "preparing" not in l]
    if not lines:
        return None
    step = re.sub(r"^┊\s*\S+\s*", "", lines[-1])  # drop the bar and the tool emoji
    return re.sub(r"\s+\d+(\.\d+)?s$", "", step).strip()[:160] or None  # and the duration


def _block_reason(card: dict) -> str:
    for event in reversed(card.get("events") or []):
        if "block" in (event.get("kind") or ""):
            payload = event.get("payload") or {}
            return str(payload.get("reason") or payload.get("message") or "").strip()
    return ""


def _first_sentence(text: str) -> str:
    text = re.sub(r"[*_`#|]+|https?://\S+", "", text or "").strip()
    sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return sentence if len(sentence) <= 220 else sentence[:217].rsplit(" ", 1)[0] + "..."


def _watch_goal(job: dict) -> None:
    """Follow the kanban goal card; announce when it is done, needs the user, or fails."""
    progress = Progress(job["id"])
    if not job.get("steps"):
        progress.add(f"card {job['card']} created")
    last_step, announced_block, errors = None, None, 0
    while True:
        if _cancelled(job["id"]):
            return
        try:
            data = _card(job["card"])
            errors = 0
        except Exception as exc:
            errors += 1
            if errors >= 15:  # about five minutes without an answer from kanban
                _update(job["id"], status="failed", error=f"lost track of the card: {exc}"[:300])
                _announce(f"Your goal agent on {job['title']} stopped reporting.")
                return
            time.sleep(WATCH_INTERVAL_S)
            continue
        card = data["task"]
        status = card.get("status")
        step = _latest_worker_step(job["card"]) if status == "running" else None
        if step and step != last_step:
            progress.add(step)
            last_step = step
        if status in ("done", "review"):
            summary = data.get("latest_summary") or card.get("result") or ""
            sentence = _first_sentence(summary)
            spoken = f"Your goal agent finished {job['title']}" + (f": {sentence}" if sentence else ".")
            progress.add("finished")
            _update(job["id"], status="done", answer=summary, spoken=spoken, mentioned=False,
                    seconds=round(time.time() - job["started_ts"]))
            _announce(spoken)
            return
        if status == "archived":
            _update(job["id"], status="cancelled", mentioned=True)
            return
        if status == "blocked":
            reason = _block_reason(data) or card.get("last_failure_error") or "it is blocked"
            if reason != announced_block:
                failed = bool(card.get("last_failure_error")) and not _block_reason(data)
                spoken = (f"Your goal agent on {job['title']} hit an error and stopped." if failed
                          else f"Your goal agent on {job['title']} needs you: {_first_sentence(reason)}")
                progress.add(f"blocked: {reason}"[:200])
                _update(job["id"], status="waiting", question=reason[:300], spoken=spoken, mentioned=False)
                _announce(spoken)
                announced_block = reason
        elif status in ("ready", "running", "todo", "scheduled", "triage"):
            if _load(job["id"]).get("status") == "waiting":
                _update(job["id"], status="running", question=None)
            announced_block = None
        time.sleep(WATCH_INTERVAL_S)


def cmd_run(args) -> None:
    job = _update(args.job_id, pid=os.getpid())
    try:
        (_watch_goal if job.get("kind") == "goal" else _run_helper)(job)
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
        if job.get("card"):
            extra += f" | card {job['card']}"
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
    if job.get("status") not in ACTIVE:
        print(f"{job['id']} is already {job.get('status')}")
        return
    was_running = job.get("status") in ACTIVE
    if job.get("card"):
        _kanban("archive", job["card"])
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
    round_info = f", kanban card {job['card']}" if job.get("card") else ""
    print(f"{job['title']} [{_label(job)}, {job['status']}{round_info}] started {job['started']}, "
          f"{len(steps)} steps logged")
    print(f"goal: {job['goal']}")
    if job.get("question"):
        print(f"needs you: {job['question']}")
    if steps:
        print("recent steps:")
        for step in steps[-args.steps:]:
            print(f"  {step}")
    if job.get("card"):
        try:
            data = _card(job["card"])
            print(f"card status: {data['task'].get('status')}")
            if data.get("comments"):
                print("card comments:")
                for c in data["comments"][-5:]:
                    print(f"  {c.get('author', '?')}: {str(c.get('body') or c.get('text') or '')[:300]}")
        except Exception as exc:
            print(f"card unavailable: {exc}")
    if job.get("answer"):
        print("result:\n" + job["answer"])
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
