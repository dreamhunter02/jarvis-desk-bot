# Design notes

What went wrong when a desk robot became the voice of a tool-using agent, and what fixed
it. Each change in this repository exists because of one of these problems. The setup was
an ESP32-S3 robot running ESP-Brookesia's xiaozhi firmware, xiaozhi-esp32-server with
Parakeet ASR and Kokoro TTS, and a Hermes agent using models on DeepInfra.

## Results

Measured on "What are my open tasks right now?":

| | before | after |
|---|---|---|
| per agent step | 17–49 s | 0.7–1.7 s |
| first spoken feedback | none until the answer | "Checking your tasks." at 0.8 s |
| full answer | 15–50 s, sometimes never | 5 s |

## Voice pipeline (xiaozhi-server)

| Problem | Fix |
|---|---|
| The robot stayed silent until the agent's final answer, often 20–60 s. xiaozhi's OpenAI provider only yields answer text. | A Hermes-specific provider reads the `hermes.tool.progress` events Hermes already streams and speaks short updates: "Let me check." after 3 s, a line per kind of work, "Still working on it." after 20 s of silence. |
| Even those lines arrived glued to the answer. xiaozhi's TTS splitter ends sentences only at `? ! ; :` and CJK punctuation, so English text was held until the reply ended. | The splitter also ends a sentence at a full stop followed by whitespace ("3.5" is not split). English answers now start speaking after their first sentence too. |
| The thinking face switched to "speaking" at the first filler line. | Progress lines keep the thinking face up. |
| Given garbled input (background speech), the model sometimes answered in Chinese, which an English voice cannot read. | The provider drops CJK text and says "Sorry, I didn't catch that." if nothing is left. `SOUL.md` has an explicit English rule on its own line; "English only" at the end of a long line was ignored. |
| The model saw its own filler lines in the history as past answers. | The provider strips them from the history it sends back. |
| With the greeting off, waking and ending were silent. | A rising chime on wake, and a falling one before the robot sleeps; the goodbye waits until the chime has played. |

## Staying in the conversation

| Problem | Fix |
|---|---|
| The server ended the conversation after 10 s of silence, often while the agent was still working. | `listen_silence_timeout: 0`; only an end phrase ("bye bye") ends a conversation. |
| The firmware has its own wake window (30 s before the first speech, 10 s after the last) that pauses only while the robot speaks. It slept mid-task, and whenever its on-board voice detector missed speech the server heard. The robot then sends `goodbye` itself. | `firmware/stay-awake.patch` raises both to 24 h. |

The trade-off: the robot listens until told to stop, so a nearby meeting or video is sent to
the agent too. Say "bye bye" first.

## Agent behaviour (Hermes)

| Problem | Fix |
|---|---|
| GLM 5.3 Flash took 17–49 s per agent step on DeepInfra (17.9 s for a 21-token "hello") and returned 429 "Model busy". | DeepSeek V4.1 Flash as the main model (0.7–1.7 s per step), GLM 5.3 Flash as the fallback. |
| The fallback pointed at a stopped local model server, so a 429 ended the turn. A fallback only takes over on errors: a slow but successful reply never moves to it. | A fallback that exists; choose a fast main model rather than relying on the fallback. |
| DeepSeek ended turns on a promise ("give me a moment to pull them together") with no tool call, and confirmed changes it never made. Hermes's tool-use guidance had been switched off. | `tool_use_enforcement: auto`, `execution_guidance: auto`, `task_completion_guidance: true`, and reasoning `high` for DeepSeek (about 0.4 s more per step on DeepInfra). |
| The agent said it had no subagent tool. `delegate_task` was on the deferred tool list, and with tool listing off a deferred tool is invisible. | Keep `delegate_task` out of `tools.tool_search.defer`. |
| A removed task server was still named in every prompt, so the agent kept trying it. | Google Tasks as the only task system, in `SOUL.md` and the task skills. |

Phrase-matching guards in the provider (re-asking when a reply sounded like an unverified
claim) were tried and removed: they patched model behaviour with English patterns that do
not generalise. Model choice, reasoning effort and Hermes's own guards are the durable fixes.

## Background helpers

| Problem | Fix |
|---|---|
| `delegate_task` keeps the voice turn open until the subagent finishes, often minutes. Hermes's async delegation on the API server only stores the result for the next request, so nothing comes back by itself. | The `background-task` skill starts a detached one-shot Hermes job with the subagent model, replies at once, and announces a one-sentence result through a speech command when done. |
| The main agent did not know what was running, or what had finished. | The provider attaches a short status note to each request: running jobs with their latest step, and jobs finished since the last turn with their result. |
| "How is it going?" got invented progress. | Every helper step (tool call, failed tool, note between tools) is logged from Hermes's `stream-json` events; the note shows the latest step, and `show` lists recent ones. |
| Restarting the Hermes gateway killed every running helper: they lived in its systemd cgroup. | Each helper runs in its own `systemd-run --user --scope`. |

## What did not work: speech-to-speech

Before this pipeline, NVIDIA's Nemotron VoiceChat (a speech-to-speech model) was tried as the
front end, with the agent behind a single tool ([hermes-voicechat](https://github.com/dreamhunter02/hermes-voicechat)).
With clean recordings it called the tool; with the robot's far-field microphone it mostly did
not, answering "I can do that, would you like me to?" and repeating it after every "yes".
The model alone decides whether to call a tool, and its runtime offers no way to force one.
For an agent whose answers take seconds to minutes anyway, ASR, a tool-calling LLM and TTS
proved more reliable.
