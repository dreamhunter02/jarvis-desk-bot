"""bujz1: Hermes agent LLM provider that talks while the agent works.

The generic OpenAI provider only yields the final answer, so the robot sits
silent for the whole Hermes run (often 20-60 s). Hermes's streamed chat
completions also carry ``event: hermes.tool.progress`` frames; this provider
turns them into short spoken updates and fills long silences:

    "Let me check."             nothing back after ``ack_after_s``
    "Checking Plane." ...       when Hermes starts a tool (once per kind)
    "Still working on it."      after ``still_after_s`` without any speech

Updates are yielded as ordinary text, so xiaozhi speaks them through its normal
TTS path ahead of the answer; they are removed from the dialogue history sent
back to Hermes. Config (data/.config.yaml, LLM.HermesLLM):

    type: hermes
    progress: true        # false = behave like the plain OpenAI provider
    ack_after_s: 3
    still_after_s: 20
"""

import json
import queue
import re
import threading
import time

import httpx

from config.logger import setup_logging
from core.providers.llm.openai.openai import LLMProvider as OpenAIProvider

TAG = __name__
logger = setup_logging()

ACK = "Let me check."
STILL = ("Still working on it.", "Bear with me, almost there.", "Still on it.")
MAX_STILL = 6  # enough for a ~2 minute subagent run
# (pattern over the request, tool name and preview, spoken line); first match wins.
# The request matters: Plane and notes go through a generic skill script whose
# preview never names them. ASR often hears "Plane" as "plain" or "plan".
TOOL_LINES = (
    # Delegation first: a subagent's goal text often mentions tasks or the web.
    (r"delegate_task|subagent", "Handing part of this to a helper."),
    (r"\bplane\b|\bplain\b|\bplan\b|\btasks?\b|to-?dos?", "Checking your tasks."),
    (r"obsidian|vault|\bnotes?\b", "Looking through your notes."),
    (r"remind|cron|schedule|calendar", "Checking your schedule."),
    (r"perplexity|web_search|web_extract|search the web|browser|\bnews\b", "Searching the web."),
    (r"weather", "Checking the weather."),
    (r"github|gitlab|\bgit\b", "Checking the repository."),
    (r"memory|session_search", "Checking what I remember."),
    (r"read_file|search_files|write_file|patch", "Going through the files."),
)
PROGRESS_LINES = {ACK, *STILL, *(line for _, line in TOOL_LINES)}
NOT_CAUGHT = "Sorry, I didn't catch that."
# Some models (DeepSeek V4.1 Flash) end a turn on an announcement of work they have not
# started. Hermes's own stall guard only matches "let me now" / "I'll now" endings.
MAX_NUDGES = 1
NUDGE = "Do that now: use your tools in this same turn, then tell me the result."
_PROMISE = re.compile(
    r"\b(?:one moment|a moment|a sec(?:ond)?|hold on|bear with me|hang on"
    r"|let me (?:check|verify|pull|look|see|read|get|grab|find|confirm|fetch|add|update|review)"
    r"|i(?:'|\u2019)?ll (?:check|verify|pull|look|read|get|grab|find|confirm|fetch|add|update)"
    r"|i will (?:check|verify|pull|look|read|get|grab|find|confirm|fetch|add|update)"
    r"|(?:checking|pulling|reading|looking|fetching|getting|grabbing|verifying|adding|updating)\b[^.?!]{0,60}\bnow)\b",
    re.IGNORECASE,
)


def is_promise(text: str) -> bool:
    """A short reply that only announces work ("Checking your lists now, one moment")."""
    t = re.sub(r"[^\w\s'\u2019,.!?-]", "", text or "").strip()
    return 0 < len(t) <= 200 and "?" not in t and bool(_PROMISE.search(t))
# The voice is English-only; GLM sometimes drifts into Chinese on garbled input.
CJK = re.compile(r"[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]+")


def spoken_line(request: str, tool: str, label: str) -> str | None:
    text = f"{request} {tool} {label}".casefold()
    for pattern, line in TOOL_LINES:
        if re.search(pattern, text):
            return line
    return None


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
                    elif event is None:
                        choices = payload.get("choices") or []
                        delta = (choices[0].get("delta") or {}) if choices else {}
                        if delta.get("content"):
                            out.put(("text", delta["content"]))
        except Exception as exc:  # surfaced by the generator
            error = exc
        out.put(("done", error))

    def _respond(self, dialogue):
        dialogue = strip_progress(self.normalize_dialogue(dialogue))
        request = next((m.get("content") for m in reversed(dialogue) if m.get("role") == "user"), "")
        request = request if isinstance(request, str) else ""
        now = time.monotonic()
        # Spoken-update state is shared across the (at most two) Hermes turns of one request.
        state = {"started": now, "last_spoken": now, "acked": False, "said": set(), "stills": 0}
        for attempt in range(MAX_NUDGES + 1):
            result = {"answer": "", "tools": 0, "failed": False}
            yield from self._run(dialogue, request, state, result)
            if result["failed"] or result["tools"] or attempt == MAX_NUDGES or not is_promise(result["answer"]):
                return
            # The model announced the work ("one moment, checking your lists") and ended its turn
            # without a tool call. The robot cannot wait for a second message: ask for it now.
            logger.bind(tag=TAG).warning(f"Hermes stopped on a promise ({result['answer']!r}); nudging")
            dialogue = dialogue + [
                {"role": "assistant", "content": result["answer"]},
                {"role": "user", "content": NUDGE},
            ]
            yield " "

    def _run(self, dialogue, request, state, result):
        """Stream one Hermes turn; record its answer text and tool count in ``result``."""
        out: queue.Queue = queue.Queue()
        threading.Thread(target=self._stream_events, args=(dialogue, out), daemon=True).start()
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
                    result["failed"] = True
                    logger.bind(tag=TAG).error(f"Hermes request failed: {value}")
                    if not answering:
                        yield "Sorry, I could not reach JARVIS just now."
                elif not answering and dropped_cjk:
                    result["failed"] = True
                    yield NOT_CAUGHT
                return
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
        yield from self._respond(dialogue)

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        # Hermes runs its own tools; xiaozhi's local functions are not offered to it.
        for text in self._respond(dialogue):
            yield text, None
