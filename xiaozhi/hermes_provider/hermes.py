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
MAX_STILL = 3
# (pattern over the request, tool name and preview, spoken line); first match wins.
# The request matters: Plane and notes go through a generic skill script whose
# preview never names them. ASR often hears "Plane" as "plain" or "plan".
TOOL_LINES = (
    (r"\bplane\b|\bplain\b|\bplan\b|\btasks?\b|to-?dos?", "Checking your tasks."),
    (r"obsidian|vault|\bnotes?\b", "Looking through your notes."),
    (r"remind|cron|schedule|calendar", "Checking your schedule."),
    (r"perplexity|web_search|web_extract|search the web|browser|\bnews\b", "Searching the web."),
    (r"weather", "Checking the weather."),
    (r"github|gitlab|\bgit\b", "Checking the repository."),
    (r"memory|session_search", "Checking what I remember."),
    (r"delegate|subagent", "Handing part of this to a helper."),
    (r"read_file|search_files|write_file|patch", "Going through the files."),
)
PROGRESS_LINES = {ACK, *STILL, *(line for _, line in TOOL_LINES)}
NOT_CAUGHT = "Sorry, I didn't catch that."
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
        out: queue.Queue = queue.Queue()
        threading.Thread(target=self._stream_events, args=(dialogue, out), daemon=True).start()
        started = last_spoken = time.monotonic()
        answering = acked = False
        said = set()
        stills = 0
        thinking = False  # inside <think>...</think>
        dropped_cjk = False
        prefix = ""
        request = next((m.get("content") for m in reversed(dialogue) if m.get("role") == "user"), "")
        request = request if isinstance(request, str) else ""
        while True:
            try:
                kind, value = out.get(timeout=0.25)
            except queue.Empty:
                if not self.progress or answering:
                    continue
                now = time.monotonic()
                if not acked and now - started >= self.ack_after_s:
                    acked, last_spoken = True, now
                    yield ACK + " "
                elif acked and stills < MAX_STILL and now - last_spoken >= self.still_after_s:
                    last_spoken = now
                    yield STILL[stills % len(STILL)] + " "
                    stills += 1
                continue
            if kind == "done":
                if value is not None:
                    logger.bind(tag=TAG).error(f"Hermes request failed: {value}")
                    if not answering:
                        yield "Sorry, I could not reach JARVIS just now."
                elif not answering and dropped_cjk:
                    yield NOT_CAUGHT
                return
            if kind == "tool":
                if not self.progress or answering:
                    continue
                line = spoken_line(request, *value)
                logger.bind(tag=TAG).info(f"Hermes tool: {value[0]} {value[1]!r} -> {line}")
                if line and line not in said:
                    said.add(line)
                    acked, last_spoken = True, time.monotonic()
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
                yield text

    def response(self, session_id, dialogue, **kwargs):
        yield from self._respond(dialogue)

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        # Hermes runs its own tools; xiaozhi's local functions are not offered to it.
        for text in self._respond(dialogue):
            yield text, None
