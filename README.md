# JARVIS Desk Bot

**A voice front end for the [Hermes](https://github.com/NousResearch/hermes-agent) agent on an ESP32 desk robot.**
Talk to your agent, hear what it is doing while it works, and hand long jobs to background
helpers that report back when they are done.

<p>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"/></a>
  <a href="https://github.com/dreamhunter02/jarvis-desk-bot/stargazers"><img src="https://img.shields.io/github/stars/dreamhunter02/jarvis-desk-bot?style=social" alt="GitHub stars"/></a>
  <a href="https://x.com/vineethkalluru/status/2108388089731436546"><img src="https://img.shields.io/badge/X-launch%20post-black?logo=x&logoColor=white" alt="Launch post on X"/></a>
</p>

**Stack:** ESP32 desk robot → xiaozhi-server (Parakeet ASR + Kokoro TTS, all local) → any Hermes agent.

<p align="center">
  <a href="docs/media/jarvis-demo.mp4"><img src="docs/media/demo-poster.jpg" width="400" alt="Watch the demo (sound on)"/></a>
</p>

## See it in action

Real recordings of the robot on my desk; skipped waits are marked (+20 s). Click a clip to play it with sound, which you need for the chimes.

<table>
  <tr>
    <td width="33%" align="center"><b>Wake chime</b></td>
    <td width="33%" align="center"><b>Talks while it works</b></td>
    <td width="33%" align="center"><b>Background helpers</b></td>
  </tr>
  <tr>
    <td><a href="docs/media/wake-chime.mp4"><img src="docs/media/wake-chime.webp" width="100%" alt="Wake chime demo"/></a></td>
    <td><a href="docs/media/thinking.mp4"><img src="docs/media/thinking.webp" width="100%" alt="Spoken progress demo"/></a></td>
    <td><a href="docs/media/helper.mp4"><img src="docs/media/helper.webp" width="100%" alt="Background helper demo"/></a></td>
  </tr>
  <tr>
    <td align="center"><sub>Say "Jarvis", hear a rising <i>ba-ding</i>, then just talk.</sub></td>
    <td align="center"><sub>No dead air: progress lines and "thinking" eyes while tools run.</sub></td>
    <td align="center"><sub>Long jobs go to a helper. JARVIS answers right away.</sub></td>
  </tr>
  <tr>
    <td width="33%" align="center"><b>Reports back honestly</b></td>
    <td width="33%" align="center"><b>Google Tasks</b></td>
    <td width="33%" align="center"><b>Bye bye + end chime</b></td>
  </tr>
  <tr>
    <td><a href="docs/media/reports-back.mp4"><img src="docs/media/reports-back.webp" width="100%" alt="Helper result demo"/></a></td>
    <td><a href="docs/media/tasks.mp4"><img src="docs/media/tasks.webp" width="100%" alt="Google Tasks demo"/></a></td>
    <td><a href="docs/media/end-chime.mp4"><img src="docs/media/end-chime.webp" width="100%" alt="End chime demo"/></a></td>
  </tr>
  <tr>
    <td align="center"><sub>Helper results, failures included, with what is still safe.</sub></td>
    <td align="center"><sub>Notes a task on the right list, by voice.</sub></td>
    <td align="center"><sub>Stays in the conversation until you say bye, then a falling <i>tung</i>.</sub></td>
  </tr>
</table>

JARVIS Desk Bot is a set of drop-in changes for three open-source projects: the robot's
[ESP-Brookesia](https://github.com/espressif/esp-brookesia) firmware,
[xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) (wake word, speech
recognition, text to speech), and Hermes (the agent, its tools and skills). Together they turn
a chat-style voice gadget into an assistant that can manage your tasks, search the web and run
multi-minute jobs without leaving you in silence.

## Features

- **Talks while it works.** Instead of silence until the final answer, you hear "Let me check.",
  then what the agent is doing ("Checking your tasks.", "Searching the web."), and "Still working
  on it." during long steps.
- **Background helpers and goal agents.** Say "spin off a helper to…" and JARVIS starts the job,
  answers right away, and announces the result when it finishes. Quick jobs go to a *helper*
  (about 10 minutes); long goals such as deployments and benchmarks go to a *goal agent*, a
  Hermes kanban goal card that works for hours in its own session and asks you when it is
  blocked. Ask
  "what's running?" or "how is it going?" at any time, answer its questions, or cancel a job.
- **Asks before risky commands.** When Hermes's safety check holds a command for approval, the
  robot asks ("JARVIS needs your OK to run a high-risk command: pipe to interpreter. Should it go
  ahead?") and your spoken yes or no goes back to Hermes, which carries on with the same task.
- **Stays in the conversation.** No timeouts: the robot keeps listening until you say "bye bye",
  "good bye" or "bye jarvis".
- **Chimes.** A rising "ba-ding" when it hears the wake word, a falling "tung" when it goes to sleep.
- **Google Tasks skill.** Read, add, update and complete tasks, routed to a personal or a work
  list by keyword.
- **Tuned defaults.** Model, reasoning and agent-guard settings that keep a voice agent fast and
  stop it from claiming work it has not done.

## How it works

```
                     wake word, audio                     request (streamed)
  ESP32 robot  ───────────────────────►  xiaozhi-server  ───────────────────►  Hermes agent
  (ESP-Brookesia)                        ASR, TTS,                              tools, skills,
               ◄───────────────────────  Hermes provider ◄───────────────────  Google Tasks, web
                 speech, chimes, faces                     text + tool progress events
                                                ▲
                                                │ job status            background helpers
                                                └───────────────────────  (detached Hermes jobs)
                                                                          announce when done
```

1. The robot detects "Jarvis" on-device and streams your speech to xiaozhi-server, which
   transcribes it.
2. The **Hermes provider** sends the request to the Hermes API server and streams the reply. It
   turns Hermes's tool-progress events into short spoken updates and attaches the status of
   any background jobs.
3. Hermes runs its tools and skills and writes a short, speakable answer (rules in `SOUL.md`).
4. Background work goes to the **background-task** skill: a *helper* is one detached Hermes run;
   a *goal agent* is a Hermes kanban card in goal mode, run by the gateway's kanban dispatcher
   and checked after every turn by Hermes's goal judge. Both log each step and announce a
   one-sentence result through the robot when done.

Read [docs/design-notes.md](docs/design-notes.md) for the problems each piece solves and the
measurements behind the defaults.

## Requirements

| Component | Tested with |
|---|---|
| Robot | ESP32-S3 desk robot running the ESP-Brookesia `chatbot` example with the xiaozhi agent (`release/v0.7`), ESP-IDF 5.5 |
| Voice server | xiaozhi-esp32-server with a local ASR (Parakeet) and an English TTS (Kokoro) |
| Agent | Hermes agent with the API server enabled |
| Models | Any OpenAI-compatible provider with reliable tool calling. Tested: Nemotron 3.5 Super VL (main), DeepSeek V4.1 Flash (fallback, goal agents, goal judge), MiMo V2.6 Flash (helpers) |
| Tasks | A Google account and an OAuth token with the Google Tasks scope |
| Host | Linux with a systemd user session (background helpers run as user scopes) |
| Announcements | A command that speaks text on the robot (`BG_ANNOUNCE_CMD`, see below) |

## Quick start

### 1. Get the code and personalize it

```bash
git clone https://github.com/dreamhunter02/jarvis-desk-bot.git
cd jarvis-desk-bot
grep -rn "ADD YOUR PERSONAL INFO HERE\|{{" hermes xiaozhi
```

Fill in every place the last command lists: your name, time zone, task list names, work
keywords, the skills you use for notes, web search and the robot, and your Hermes API key.

### 2. Set up Hermes

```bash
cp hermes/SOUL.md ~/.hermes/SOUL.md
cp hermes/memories/USER.md ~/.hermes/memories/USER.md
cp -r hermes/skills/task-manager hermes/skills/tasks-reminders hermes/skills/background-task ~/.hermes/skills/
cp hermes/skills/task-manager/task-classification.example.json ~/.hermes/task-classification.json
```

Then:

- merge [`hermes/config.example.yaml`](hermes/config.example.yaml) into `~/.hermes/config.yaml`;
- put your provider key (e.g. `DEEPINFRA_API_KEY`) in `~/.hermes/.env`;
- save a Google OAuth token with the Tasks scope as `~/.hermes/google_token.json`;
- restart the Hermes gateway.

### 3. Set up xiaozhi-server

```bash
python xiaozhi/apply.py ~/xiaozhi-server/main/xiaozhi-server
```

`apply.py` is idempotent and backs up every file it edits as `*.before-jarvis-desk-bot`. It
installs the Hermes provider, the English sentence split, the chimes and their hooks, and points
your Hermes LLM entry at the new provider. Merge
[`xiaozhi/config.example.yaml`](xiaozhi/config.example.yaml) into `data/.config.yaml`, then
restart xiaozhi-server.

### 4. Patch the robot firmware (recommended)

Without this the robot puts itself to sleep 10 s after you stop talking, even mid-task.

```bash
cd esp-brookesia
git apply /path/to/jarvis-desk-bot/firmware/stay-awake.patch
cd examples/agent/chatbot
idf.py build
idf.py -p PORT app-flash
```

See [firmware/README.md](firmware/README.md) for details.

### 5. Try it

Say "Jarvis", wait for the chime, then: "What's on my list today?"

## Usage

| Say | What happens |
|---|---|
| "Jarvis" | Chime; the robot listens until you say an end phrase |
| "What's on my list today?" | Reads your Google Tasks |
| "Add pay the invoice for Friday" | Adds a task to the right list and confirms |
| "Spin off a helper to compare the latest open models" | Starts a quick helper and answers at once |
| "Spin off an agent to deploy the TTS model on the second Spark" | Starts a goal agent for the long job |
| "What's running?" / "How is the deployment going?" | Running jobs, their latest step and round |
| "Use the int8 weights" (after an agent asks) | Passes your answer on; the agent resumes |
| "What did the helper find?" | The saved result of a finished job |
| "Cancel the deployment" | Stops a job |
| "Yes" / "Yes, for this session" / "No" (after an approval question) | Allows the command once, for the session, or blocks it |
| "Bye bye", "good bye", "bye jarvis" | Chime, and the robot goes to sleep |

## Configuration

**Hermes provider** (`data/.config.yaml`, under your Hermes LLM entry):

| Key | Default | Meaning |
|---|---|---|
| `type` | `hermes` | Use this provider |
| `progress` | `true` | Speak progress updates |
| `ack_after_s` | `3` | Say "Let me check." if nothing has happened by then |
| `still_after_s` | `20` | Say "Still working on it." after this long without speech |
| `read_timeout` | `300` | Seconds to wait for Hermes |

Spoken lines per kind of tool live in `TOOL_LINES` in
[`hermes.py`](xiaozhi/hermes_provider/hermes.py); add patterns for your own skills.

**Background helpers** (environment of the Hermes gateway):

| Variable | Default | Meaning |
|---|---|---|
| `BG_ANNOUNCE_CMD` | `python3 ~/.hermes/speech/speech.py enqueue --text` | Command that speaks a result; the text is appended as the last argument |
| `BG_TASK_MAX_RUNNING` | `3` | Jobs that can run at once (both kinds) |
| `BG_HELPER_BUDGET_S` / `BG_HELPER_STEPS` | `600` / `60` | A helper's time and step limits |
| `BG_GOAL_TURNS` / `BG_GOAL_RUNTIME` | `30` / `4h` | A goal card's goal-loop turns and runtime cap |
| `BG_GOAL_MODEL` / `BG_GOAL_PROVIDER` | the kanban profile's model | Model for goal agents |
| `BG_GOAL_ASSIGNEE` | `default` | Kanban profile that runs goal cards |
| `BG_TASK_DIR` | `~/.hermes/background` | Job records and step logs |

These can also be set in `~/.hermes/.env`; the script reads `BG_*` keys (and only those) from it.

Helpers use the subagent model from `delegation.model` in `~/.hermes/config.yaml`. Goal agents
need the kanban dispatcher (embedded in the gateway by default) and a goal judge that returns
reliable JSON: set `auxiliary.goal_judge` (see `hermes/config.example.yaml`). Pick goal and judge
models with reliable tool calling.

**Conversation length** (`data/.config.yaml`): `listen_silence_timeout: 0` keeps a
conversation open until an end phrase. The robot then also hears nearby conversations; say
"bye bye" before a meeting or a video call.

## Project layout

```
firmware/
  stay-awake.patch               keep the robot awake until the server ends the conversation
hermes/
  SOUL.md                        voice persona and rules (template)
  config.example.yaml            models, fallback, reasoning, tool-use guards, subagents
  memories/USER.md               facts about you (template)
  skills/task-manager/           Google Tasks skill and CLI
  skills/tasks-reminders/        reminders tied to tasks
  skills/background-task/        helpers and kanban goal agents: start, goal, tell, list, show, cancel
xiaozhi/
  apply.py                       installer for xiaozhi-server
  config.example.yaml            provider and conversation settings
  hermes_provider/hermes.py      the Hermes provider
  sounds/                        chimes, their hook, and the script that generates them
docs/
  design-notes.md                problems, fixes and measurements
  media/                         demo clips (mp4 with sound, webp previews)
```

## Troubleshooting

| Symptom | Check |
|---|---|
| Silent until the answer | The Hermes LLM entry has `type: hermes`, and `apply.py` reported the TTS splitter as patched |
| The conversation ends while JARVIS is working | `listen_silence_timeout: 0` on the server, and the firmware patch is flashed |
| A helper never reports back | `bg_task.py list`; check `BG_ANNOUNCE_CMD`, and that `systemd-run --user` works for the gateway's user |
| A job stopped early | `bg_task.py show JOB_ID` gives the reason ("used all 60 steps", "hit the 10-minute limit") and the partial result; long jobs belong to a goal agent |
| A goal agent writes `<tool_call>` text instead of acting | Its model's server is not parsing tool calls; set `BG_GOAL_MODEL` to a model with working tool calling |
| A goal card never starts, or pauses at once | `hermes kanban stats` and the gateway log (`kanban dispatcher`); a judge that cannot return JSON pauses the goal, so set `auxiliary.goal_judge` |
| Helpers die when Hermes restarts | They should run as `jarvis-bg-*.scope` units: `systemctl --user list-units 'jarvis-bg-*'` |
| JARVIS says it did something it did not | Keep the tool-use guards on and reasoning at `high` (see `hermes/config.example.yaml`) |
| "I have no subagent tool" | Remove `delegate_task` from `tools.tool_search.defer` |
| JARVIS goes quiet in the middle of a task | It may be waiting on a command approval that never reached you (Hermes denies after `approvals.timeout`, 300 s). The provider relays `approval.request` events; check the xiaozhi log for "Hermes approval requested" |
| Replies in another language | Keep the English rule in `SOUL.md`; the provider already drops CJK text |

## Contributing

Issues and pull requests are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Acknowledgements

Built on [Hermes agent](https://github.com/NousResearch/hermes-agent),
[xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server),
[ESP-Brookesia](https://github.com/espressif/esp-brookesia) and
[ESP-SR](https://github.com/espressif/esp-sr), with [Kokoro](https://github.com/hexgrad/kokoro)
for speech.

## License

[MIT](LICENSE)
