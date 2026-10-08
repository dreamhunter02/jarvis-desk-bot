"""Hermes agent LLM provider for xiaozhi-esp32-server that talks while the agent works.

xiaozhi's generic OpenAI provider yields only the final answer, so a voice robot sits
silent for the whole agent run. Hermes's streamed chat completions also carry
``event: hermes.tool.progress`` frames; this provider turns them into short spoken
updates and fills long silences:

    "Let me check."             nothing back after ``ack_after_s``
    "Checking your tasks." ...  when Hermes starts a kind of tool (once per kind; TOOL_LINES)
    "Still working on it."      after ``still_after_s`` without any speech (up to MAX_STILL times)

It also:
  * drops CJK text, so an English voice never reads Chinese from a drifting model;
  * removes its own progress lines from the history sent back to Hermes;
  * appends a short status of background-task jobs (running, latest step, newly
    finished) to the outgoing copy of the latest user message;
  * relays Hermes's command approvals: on ``event: approval.request`` it asks the user
    aloud, keeps the Hermes stream open, and sends the spoken yes or no to
    ``POST /v1/runs/{run_id}/approval`` before resuming the same turn.

Updates are yielded as ordinary text, so xiaozhi speaks them through its normal TTS
path ahead of the answer. Config (data/.config.yaml, under the LLM entry for Hermes):

    type: hermes
    progress: true        # false = no spoken updates
    ack_after_s: 3
    still_after_s: 20
    read_timeout: 300
"""

import json
import os
import queue
import re
import threading
import time

from pathlib import Path

import httpx

from config.logger import setup_logging
from core.providers.llm.openai.openai import LLMProvider as OpenAIProvider

TAG = __name__
logger = setup_logging()

ACK = "Let me check."
STILL = ("Still working on it.", "Bear with me, almost there.", "Still on it.")
MAX_STILL = 6  # enough for a ~2 minute subagent run
# (pattern over the request, tool name and preview, spoken line); first match wins.
# The request matters: tasks and notes often go through a generic skill script whose
# preview never names them.
TOOL_LINES = (
    # Loading the background-task skill says nothing (its name would match "tasks");
    # the start/list/cancel commands below get their own lines.
    (r"skill_view\s+\S*background-task", None),
    # Helpers before "tasks": the script path and goal text often contain "task".
    (r"bg_task\.py (?:list|show|cancel|tell)", "Checking on your helpers."),
    (r"bg_task\.py start", "Starting a helper."),
    (r"bg_task\.py goal", "Starting a goal agent."),
    (r"bg_task|background-task/", "Checking on your helpers."),
    (r"delegate_task|subagent", "Handing part of this to a helper."),
    (r"\btasks?\b|\bto-?dos?\b|\bto do\b", "Checking your tasks."),
    (r"obsidian|vault|\bnotes?\b", "Looking through your notes."),
    (r"remind|cron|schedule|calendar", "Checking your schedule."),
    (r"perplexity|web_search|web_extract|search the web|browser|\bnews\b", "Searching the web."),
    (r"weather", "Checking the weather."),
    (r"github|gitlab|\bgit\b", "Checking the repository."),
    (r"(?:^|\s)memory(?:\s|$)|session_search", "Checking what I remember."),  # the tool, not "memory.total"
    (r"read_file|search_files|write_file|patch", "Going through the files."),
)
PROGRESS_LINES = {ACK, *STILL, *(line for _, line in TOOL_LINES if line)}
NOT_CAUGHT = "Sorry, I didn't catch that."
# The voice is English-only; GLM sometimes drifts into Chinese on garbled input.
CJK = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]+")


def spoken_line(request: str, tool: str, label: str) -> str | None:
    if re.search(TOOL_LINES[0][0], f"{tool} {label}", re.I):
        return None
    text = f"{request} {tool} {label}".casefold()
    for pattern, line in TOOL_LINES:
        if re.search(pattern, text):
            return line
    return None


BACKGROUND_DIR = Path(os.environ.get("BG_TASK_DIR", Path.home() / ".hermes/background"))


def background_note() -> str:
    """Status of background-task jobs for the agent: what is running (helpers and goal agents,
    with their latest step), what is waiting for the user, and what ended since the last turn
    (each outcome is reported once, then marked mentioned)."""
    if not BACKGROUND_DIR.is_dir():
        return ""
    running, waiting, ended = [], [], []
    for path in sorted(BACKGROUND_DIR.glob("*.json")):
        try:
            job = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        status = job.get("status")
        kind = "goal agent" if job.get("kind") == "goal" else "helper"
        name = f"{job.get('title')} ({kind}, {job.get('id')}"
        if status == "running":
            minutes = max(1, round((time.time() - job.get("started_ts", time.time())) / 60))
            detail = f", round {job['round']}" if job.get("kind") == "goal" and job.get("round") else ""
            if job.get("last_step"):
                ago = round(time.time() - job.get("last_step_ts", time.time()))
                detail += f"; {job.get('steps', 0)} steps, latest {ago} s ago: {job['last_step'][:160]}"
            running.append(f"{name}, {minutes} min so far{detail})")
        elif status == "waiting":
            waiting.append(f"{name}) asks: {job.get('question') or job.get('spoken') or ''}")
        elif status in ("done", "failed", "stopped", "paused") and not job.get("mentioned"):
            ended.append(f"{name}, {status}): {job.get('spoken') or job.get('error') or ''}")
            job["mentioned"] = True
            try:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(job, indent=1))
                tmp.replace(path)
            except OSError:
                pass
    if not (running or waiting or ended):
        return ""
    parts = []
    if running:
        parts.append("running (you know only the latest step shown; for more, run `show JOB_ID`): "
                     + " | ".join(running))
    if waiting:
        parts.append("waiting for the user's answer (pass it on with `tell JOB_ID`): " + " | ".join(waiting))
    if ended:
        parts.append("ended since last turn (already announced aloud): " + " | ".join(ended))
    return ("\n\n[Background jobs, for your awareness; mention only if relevant or asked. "
            "Commands: background-task show / tell / cancel JOB_ID. " + " || ".join(parts) + "]")


def with_background_note(dialogue: list) -> list:
    """Attach the background-job status to the outgoing copy of the latest user message."""
    note = background_note()
    if not note:
        return dialogue
    for i in range(len(dialogue) - 1, -1, -1):
        if dialogue[i].get("role") == "user" and isinstance(dialogue[i].get("content"), str):
            return dialogue[:i] + [{**dialogue[i], "content": dialogue[i]["content"] + note}] + dialogue[i + 1:]
    return dialogue


def strip_progress(dialogue: list) -> list:
    """Drop our spoken updates from earlier assistant turns before they go back to Hermes."""
    cleaned = []
    for message in dialogue:
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, str):
            for line in PROGRESS_LINES:
                content = content.replace(line, "")
            message = {**message, "content": re.sub(r"\s+", " ", content).strip()}
        cleaned.append(message)
    return cleaned


# --- command approvals ---------------------------------------------------------------
# Hermes holds a flagged command until it hears back (approvals.timeout, 300 s by default,
# then it denies). The run waits here between the spoken question and the user's answer.
PENDING_APPROVALS = {}  # xiaozhi session id -> parked run
APPROVAL_WAIT_S = 290
_YES = re.compile(r"\b(?:yes|yeah|yep|yup|sure|ok(?:ay)?|go ahead|do it|approve[ds]?|allow(?:ed)?|fine|"
                  r"run it|proceed|go for it|affirmative)\b", re.I)
_NO = re.compile(r"\b(?:no|nope|nah|don'?t|do not|stop|deny|denied|cancel|skip|never|wait|hold on|not now)\b", re.I)
_FOR_SESSION = re.compile(r"\b(?:always|from now on|for (?:this|the) (?:session|conversation)|for the rest)\b", re.I)


def approval_question(event: dict) -> str:
    """One short spoken question for an approval.request event.

    Descriptions from Hermes's security scan are long ("Security scan — [HIGH] Pipe to
    interpreter: curl | sh: Command pipes output ... Safer: run ..."); keep the severity and
    the rule's name.
    """
    description = re.sub(r"\s+", " ", str(event.get("description") or "")).strip()
    scan = re.search(r"\[(\w+)\]\s*([^:]+)", description)
    if scan:
        severity, rule = scan.group(1).lower(), scan.group(2).strip().lower()
        what = f"a {severity}-risk command: {rule}"
    elif description:
        first = re.split(r"(?<=[.!?:])\s", description, maxsplit=1)[0].rstrip(".:")
        first = re.sub(r"[`*_#|]", "", first)
        what = f"a command: {first if len(first) <= 90 else first[:87].rsplit(' ', 1)[0] + '...'}"
    else:
        command = str(event.get("command") or "").strip()
        words = [w for w in command.split() if "=" not in w.split("/")[0]]  # skip VAR=value prefixes
        what = f"a command with {Path(words[0]).name}" if words else "a command"
    return f"JARVIS needs your OK to run {what}. Should it go ahead?"


def approval_choice(answer: str, choices: list) -> str | None:
    """Map a spoken answer to an approval choice, or None if it is unclear."""
    yes, no = bool(_YES.search(answer or "")), bool(_NO.search(answer or ""))
    if yes == no:
        return None
    if no:
        return "deny"
    if _FOR_SESSION.search(answer) and "session" in choices:
        return "session"
    return "once"


class LLMProvider(OpenAIProvider):
    def __init__(self, config):
        super().__init__(config)
        self.progress = bool(config.get("progress", True))
        self.ack_after_s = float(config.get("ack_after_s", 3))
        self.still_after_s = float(config.get("still_after_s", 20))
        self.read_timeout = float(config.get("read_timeout", 300))

    def _stream_events(self, dialogue, out: queue.Queue) -> None:
        """Reader thread: put ("text", str) / ("tool", (name, label)) / ("done", err) on out."""
        body = {"model": self.model_name, "messages": dialogue, "stream": True}
        body.update(self.extra_body_cfg)
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        error = None
        try:
            timeout = httpx.Timeout(connect=5, read=self.read_timeout, write=10, pool=5)
            with httpx.stream("POST", url, json=body, headers=headers, timeout=timeout) as response:
                response.raise_for_status()
                event = None
                for line in response.iter_lines():
                    if not line:
                        event = None
                        continue
                    if line.startswith("event:"):
                        event = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        payload = json.loads(data)
                    except ValueError:
                        continue
                    if event == "hermes.tool.progress":
                        if payload.get("status") == "running":
                            out.put(("tool", (payload.get("tool", ""), payload.get("label", ""))))
                    elif event == "approval.request":
                        out.put(("approval", payload))
                    elif event is None:
                        choices = payload.get("choices") or []
                        delta = (choices[0].get("delta") or {}) if choices else {}
                        if delta.get("content"):
                            out.put(("text", delta["content"]))
        except Exception as exc:  # surfaced by the generator
            error = exc
        out.put(("done", error))

    def _respond(self, session_id, dialogue):
        dialogue = strip_progress(self.normalize_dialogue(dialogue))
        request = next((m.get("content") for m in reversed(dialogue) if m.get("role") == "user"), "")
        request = request if isinstance(request, str) else ""
        parked = PENDING_APPROVALS.pop(session_id, None)
        if parked and time.monotonic() - parked["asked_at"] < APPROVAL_WAIT_S:
            yield from self._resume_after_approval(session_id, parked, request)
            return
        dialogue = with_background_note(dialogue)
        now = time.monotonic()
        # Spoken-update state for this request: ack, per-kind tool lines, "still working" lines.
        state = {"started": now, "last_spoken": now, "acked": False, "said": set(), "stills": 0}
        result = {"answer": "", "tools": 0}
        yield from self._run(dialogue, request, state, result)
        yield from self._park_if_approval(session_id, request, state, result)

    def _park_if_approval(self, session_id, request, state, result):
        """The turn stopped at an approval request: ask the user and keep the run open."""
        event = result.get("approval")
        if not event:
            return
        PENDING_APPROVALS[session_id] = {
            "out": result["out"], "event": event, "request": request, "state": state,
            "asked_at": time.monotonic(),
        }
        logger.bind(tag=TAG).info(f"Hermes approval requested: {event.get('description')!r} {event.get('command')!r}")
        yield " " + approval_question(event)

    def _resume_after_approval(self, session_id, parked, answer):
        event = parked["event"]
        choice = approval_choice(answer, event.get("choices") or ["once", "deny"])
        if choice is None:
            parked["asked_at"] = time.monotonic()
            PENDING_APPROVALS[session_id] = parked
            yield "Sorry, should JARVIS run that command? Please say yes or no."
            return
        body = {"choice": choice}
        if event.get("request_id"):
            body["request_id"] = event["request_id"]
        url = f"{self.base_url.rstrip('/')}/runs/{event.get('run_id')}/approval"
        try:
            reply = httpx.post(url, json=body, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=10)
            reply.raise_for_status()
        except Exception as exc:
            logger.bind(tag=TAG).error(f"Hermes approval reply failed: {exc}")
            yield "Sorry, I couldn't pass your answer on; that request has expired."
            return
        logger.bind(tag=TAG).info(f"Hermes approval answered: {choice}")
        yield ("Okay, going ahead. " if choice != "deny" else "Okay, I've told JARVIS not to run it. ")
        state = parked["state"]
        state["last_spoken"] = time.monotonic()
        result = {"answer": "", "tools": 0}
        yield from self._run(None, parked["request"], state, result, out=parked["out"])
        yield from self._park_if_approval(session_id, parked["request"], state, result)

    def _run(self, dialogue, request, state, result, out=None):
        """Stream one Hermes turn (or continue a parked one); record its answer text and tool
        count in ``result``, and the approval event if the turn stops to ask for one."""
        if out is None:
            out = queue.Queue()
            threading.Thread(target=self._stream_events, args=(dialogue, out), daemon=True).start()
        result["out"] = out
        answering = False
        thinking = False  # inside <think>...</think>
        dropped_cjk = False
        prefix = ""
        while True:
            try:
                kind, value = out.get(timeout=0.25)
            except queue.Empty:
                if not self.progress or answering:
                    continue
                now = time.monotonic()
                if not state["acked"] and now - state["started"] >= self.ack_after_s:
                    state["acked"], state["last_spoken"] = True, now
                    yield ACK + " "
                elif state["acked"] and state["stills"] < MAX_STILL and now - state["last_spoken"] >= self.still_after_s:
                    state["last_spoken"] = now
                    yield STILL[state["stills"] % len(STILL)] + " "
                    state["stills"] += 1
                continue
            if kind == "done":
                if value is not None:
                    logger.bind(tag=TAG).error(f"Hermes request failed: {value}")
                    if not answering:
                        yield "Sorry, I could not reach JARVIS just now."
                elif not answering and dropped_cjk:
                    yield NOT_CAUGHT
                return
            if kind == "approval":
                result["approval"] = value
                return  # the stream stays open; _park_if_approval asks the user
            if kind == "tool":
                result["tools"] += 1
                # Text before a tool call was a preamble ("I'll delegate that."), not the
                # answer: keep the progress lines going until the real answer streams.
                answering = False
                if not self.progress:
                    continue
                line = spoken_line(request, *value)
                logger.bind(tag=TAG).info(f"Hermes tool: {value[0]} {value[1]!r} -> {line}")
                if line and line not in state["said"]:
                    state["said"].add(line)
                    state["acked"], state["last_spoken"] = True, time.monotonic()
                    yield line + " "
                continue
            text = value
            if "<think>" in text:
                thinking = True
                text = text.split("<think>")[0]
            if "</think>" in text:
                thinking = False
                text = text.split("</think>")[-1]
            if text and not thinking:
                if CJK.search(text):
                    if not dropped_cjk:
                        logger.bind(tag=TAG).warning("Hermes answered in a CJK language; dropping it")
                    dropped_cjk = True
                    text = CJK.sub("", text)
                if not answering and not re.search(r"[A-Za-z0-9]", text):
                    prefix += text  # hold the face emoji until there is something to say
                    continue
                if not answering:
                    text, prefix = prefix + text, ""
                answering = True
                state["last_spoken"] = time.monotonic()
                result["answer"] += text
                yield text

    def response(self, session_id, dialogue, **kwargs):
        yield from self._respond(session_id, dialogue)

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        # Hermes runs its own tools; xiaozhi's local functions are not offered to it.
        for text in self._respond(session_id, dialogue):
            yield text, None
