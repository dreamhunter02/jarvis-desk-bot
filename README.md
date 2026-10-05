# jarvis-desk-bot

Changes that make an ESP32 desk robot a usable voice front end for a
[Hermes](https://github.com/NousResearch/hermes-agent) agent ("JARVIS"). The robot runs the
xiaozhi firmware and talks to [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)
(wake word, speech recognition, TTS). xiaozhi sends each request to Hermes, which runs the tools
(Google Tasks, notes, web search) and writes the reply.

```
robot ──wake word, audio──► xiaozhi-server ──ASR──► Hermes agent ──tools──► Google Tasks, notes, web
      ◄──speech, faces─────  (Kokoro TTS)   ◄──streamed text + tool progress──┘
```

Before these changes the robot sat silent for 20–60 s on most requests, sometimes answered in
Chinese, and often said it had done things it had not.

## What each change fixes

### xiaozhi-server (`xiaozhi/`)

| Change | Problem it fixes |
|---|---|
| **Hermes progress provider** (`hermes_provider/hermes.py`): reads Hermes's streamed `hermes.tool.progress` events and speaks short updates: "Let me check." after 3 s, a line for the kind of work ("Checking your tasks.", "Searching the web.", "Looking through your notes."), and "Still working on it." after 20 s of silence | The stock OpenAI provider only yields the final answer, so the robot gave no feedback while the agent worked |
| The same provider **drops CJK text** and says "Sorry, I didn't catch that." if nothing English is left | Given garbled input (background speech), the model sometimes answered in Chinese, which an English voice cannot speak |
| Progress lines are **stripped from the history** sent back to Hermes | Otherwise the model sees its own filler lines as past answers |
| **English full stops end a sentence** in the TTS splitter (`tts/base.py`; "3.5" is not split) | Upstream only splits on `? ! ; :` and CJK punctuation, so "Let me check." waited for the whole answer, and English answers were spoken only once complete |
| **Thinking face stays up** during progress lines (optional; needs a `turnFeedback.on_spoken_sentence` hook) | The face switched to "speaking" at the first filler line |

### Robot firmware (`firmware/`)

| Change | Problem it fixes |
|---|---|
| **Stay awake until an end phrase** (`stay-awake.patch`): the firmware's own wake window goes from 30 s / 10 s to 24 h | The robot put itself to sleep 10 s after it stopped hearing speech, cutting conversations while the agent worked or when its on-board voice detector missed speech the server heard |

### Hermes (`hermes/`)

| Change | Problem it fixes |
|---|---|
| **Main model DeepSeek V4.1 Flash, fallback GLM 5.3 Flash** (both on DeepInfra) | GLM 5.3 Flash took 17–49 s per agent step on DeepInfra (17.9 s for a 21-token "hello") and returned 429 "Model busy"; DeepSeek takes 0.7–1.7 s |
| **DeepSeek reasoning `high`** (`agent.reasoning_overrides`) | With reasoning off it answered without a moment to check itself; `high` adds about 0.4 s per step on DeepInfra (Flash still thinks only 0-200 tokens) |
| **Tool-use guards on**: `tool_use_enforcement: auto`, `execution_guidance: auto`, `task_completion_guidance: true` | With them off, DeepSeek ended turns with a promise ("give me a moment to pull them together") and no tool call, and claimed tasks were added without adding them |
| **"Always reply in English"** on its own line in `SOUL.md` | "English only" buried at the end of a long line was ignored on garbled input |
| **Google Tasks is the only task system** (`SOUL.md`, `task-manager` skill, `tasks-reminders` skill) | Every prompt still pointed at a removed task server, so the agent kept trying it and reported it unreachable |
| **Background helpers** (`background-task` skill): "spin off a helper" and long jobs start detached; JARVIS replies at once and the result is announced on the robot when done (`bg_task.py start/list/show/cancel`); the provider also attaches a short status note to each request (what is running, for how long, and what finished since the last turn), so JARVIS can answer "what's running?" and follow-ups without a tool call | `delegate_task` keeps the voice turn open until the subagent finishes (minutes), and Hermes's own async delegation only stores the result for the next API turn, so nothing ever came back by itself |
| Fallback that actually exists | The previous fallback pointed at a local model server that had been stopped, so a 429 from the main model ended the turn |

Measured on "What are my open tasks right now?":

| | before | after |
|---|---|---|
| per agent step | 17–49 s | 0.7–1.7 s |
| first spoken feedback | none until the answer | "Checking your tasks." at 0.8 s |
| full answer | 15–50 s (or never) | 5 s |

## Install

1. **Personalize first.** Every place that needs your details is marked
   `ADD YOUR PERSONAL INFO HERE` or `{{PLACEHOLDER}}`:
   - `hermes/SOUL.md`: your name, and the skills you use for notes, web search and the robot
   - `hermes/memories/USER.md`: short facts about you
   - `hermes/skills/tasks-reminders/SKILL.md`: your time zone and speech delivery command
   - `hermes/skills/task-manager/task-classification.example.json`: list names and work keywords
   - `xiaozhi/config.example.yaml`: your Hermes API server key
   - `background-task`: set `BG_ANNOUNCE_CMD` if your robot announcements are not sent with
     `~/.hermes/speech/speech.py enqueue --text` (this repo does not include that outbox)

2. **xiaozhi-server** (backs up every file it edits as `*.before-jarvis-desk-bot`):

   ```bash
   python xiaozhi/apply.py ~/xiaozhi-server/main/xiaozhi-server
   ```

   Then merge `xiaozhi/config.example.yaml` into `data/.config.yaml` and restart the server.

3. **Hermes:**

   ```bash
   cp hermes/SOUL.md ~/.hermes/SOUL.md
   cp hermes/memories/USER.md ~/.hermes/memories/USER.md
   cp -r hermes/skills/task-manager hermes/skills/tasks-reminders hermes/skills/background-task ~/.hermes/skills/
   cp hermes/skills/task-manager/task-classification.example.json ~/.hermes/task-classification.json
   ```

   Merge `hermes/config.example.yaml` into `~/.hermes/config.yaml`, put `DEEPINFRA_API_KEY` in
   `~/.hermes/.env`, authorize a Google token with the Tasks scope at `~/.hermes/google_token.json`,
   and restart the Hermes gateway.

## Notes

- A fallback model only takes over when the main model errors. A slow but successful reply is
  never handed to the fallback, so pick a fast main model.
- `done`, `update`, `delete` and `search` in `tasks_api.py` act on the default (last used) list.
- The provider's progress lines are matched from the request text and tool name
  (`TOOL_LINES` in `hermes.py`); add patterns for your own skills.

## Licence

MIT
