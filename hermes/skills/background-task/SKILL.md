---
name: background-task
description: Use when the user asks to spin off a subagent or helper, to run something in the background, or for any multi-step job (research, benchmark analysis, long comparisons) that would take more than about a minute. Starts it detached so you can reply at once; the result is announced on the robot when it is done.
---

# Background tasks

Voice turns must end quickly: the user cannot talk to you while a turn is running.
`delegate_task` keeps the turn open until its subagent finishes, so use it only for a
short subtask whose result you need for the current answer. Everything long, and
every request to "spin off" or "hand off" a helper, goes here instead.

Script: `~/.hermes/skills/background-task/scripts/bg_task.py` (via the `terminal` tool).

```bash
python3 ~/.hermes/skills/background-task/scripts/bg_task.py start --title "TITLE" --goal "GOAL"
python3 ~/.hermes/skills/background-task/scripts/bg_task.py list
python3 ~/.hermes/skills/background-task/scripts/bg_task.py show JOB_ID
python3 ~/.hermes/skills/background-task/scripts/bg_task.py cancel JOB_ID
```

1. Write a self-contained goal: the helper starts with no memory of this
   conversation, so include names, IDs, dates and the expected output. Resolve
   references like "this task" first (for example, read the task from Google Tasks).
2. `--title` is a few words that will be spoken: "the benchmark analysis".
3. Run `start` once. It returns immediately with a job id; the helper runs with the
   subagent model and its own tools.
4. Reply in one sentence that the helper is on it and you will say when it is done,
   e.g. "🙂 I've handed the benchmark analysis to a helper; I'll tell you when it's done."
   Do not wait for it, poll it, or start it twice.
5. When it finishes, a one-sentence summary is announced on the robot automatically.

## Knowing what is running

On the robot, each user message ends with a bracketed `[Background jobs ...]` note:
the jobs still running (with minutes so far) and any that finished since the last
turn, with their one-sentence result. Use it to answer "what's running?" or "how is
the benchmark going?" without a tool call, and to connect a follow-up ("tell me more
about that") to the right job. Do not read the note aloud unprompted; the results were
already announced. For the full result run `show JOB_ID`; to stop a job run
`cancel JOB_ID` and confirm in one sentence. Elsewhere, `list` gives the same status.

Several jobs can run at once (three by default); `start` says "busy" at the limit.
