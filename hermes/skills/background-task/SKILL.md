---
name: background-task
description: Use when the user asks to spin off a helper, subagent or agent, to run something in the background, or for any job that would take more than about a minute. Quick jobs go to a helper (up to about 10 minutes); long goals (deployments, benchmarks, multi-step projects) go to a goal agent that works in rounds for hours. Both start detached so you can reply at once, and both report back when done.
---

# Background work: helpers and goal agents

Voice turns must end quickly: the user cannot talk to you while a turn is running.
`delegate_task` keeps the turn open until its subagent finishes, so use it only for a
short subtask whose result you need for the current answer. Everything else goes here.

| | Helper | Goal agent |
|---|---|---|
| For | One-off lookups, comparisons, quick checks and fixes | Deployments, benchmarks, evaluations, multi-step projects |
| Budget | About 10 minutes and 60 steps, one run | Rounds of up to 30 minutes, several hours in total |
| Memory | None beyond the goal you write | A notes file it rewrites each round, plus your messages |
| Ends | done, or stopped with how far it got | done, waiting for the user (blocked), or paused at its limit |

If unsure, use a helper for anything you would finish in a few minutes yourself, and a
goal agent for anything that installs, deploys, downloads large files or runs a
benchmark.

Script: `~/.hermes/skills/background-task/scripts/bg_task.py` (via the `terminal` tool).

```bash
python3 ~/.hermes/skills/background-task/scripts/bg_task.py start --title "TITLE" --goal "GOAL"   # helper
python3 ~/.hermes/skills/background-task/scripts/bg_task.py goal  --title "TITLE" --goal "GOAL"   # goal agent
python3 ~/.hermes/skills/background-task/scripts/bg_task.py tell JOB_ID "MESSAGE"
python3 ~/.hermes/skills/background-task/scripts/bg_task.py list
python3 ~/.hermes/skills/background-task/scripts/bg_task.py show JOB_ID
python3 ~/.hermes/skills/background-task/scripts/bg_task.py cancel JOB_ID
```

## Starting one

1. Write a self-contained goal: the job starts with no memory of this conversation, so
   include names, hosts, paths, versions, dates and what "done" means. Resolve
   references like "this task" first (for example, read the task from Google Tasks).
2. `--title` is a few words that will be spoken: "the TTS deployment".
3. Run `start` or `goal` once. It returns immediately with a job id.
4. Reply in one sentence that it is on it and you will say when it is done, e.g.
   "🙂 I've handed the TTS deployment to a goal agent; I'll tell you when it's done."
   Do not wait for it, poll it, or start it twice.

## While it runs

Every step (tool call, failed tool, note between tools) is logged automatically. On the
robot, each user message ends with a bracketed `[Background jobs ...]` note listing
running jobs with their latest step (and round, for goal agents), jobs waiting for the
user with their question, and jobs that finished since the last turn with their result.
Use it to answer "what's running?" or "how is it going?" without a tool call: describe
the latest step in plain words, and say if it has been quiet for a long time. For more,
run `show JOB_ID` (recent steps, the goal agent's notes, the result or latest summary).
Do not read the note aloud unprompted; outcomes were already announced.

## Answering and steering

- A goal agent that is **waiting** asked the user something (it was announced). When the
  user answers, pass the answer on with `tell JOB_ID "ANSWER"`; it resumes at once.
- To change direction or add information, `tell` a running goal agent; it reads the
  message at its next round. Helpers do not read messages: cancel and restart instead.
- A **paused** goal agent reached its round or time limit; `tell` it to continue.
- A **stopped** helper ran out of steps or time; its partial result is in `show`. Offer to
  continue the remaining work as a goal agent.
- To stop a job, `cancel JOB_ID` and confirm in one sentence.

At most three jobs run at once; `start` and `goal` say "busy" at the limit.
