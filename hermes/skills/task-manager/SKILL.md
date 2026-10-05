---
name: task-manager
description: Use for every task read, create, update, complete or delete. Google Tasks is the only task system.
version: 0.2.0
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [tasks, gtasks, google]
---

# Task Manager (Google Tasks)

Manage the user's Google Tasks through the official Google Tasks REST API. Google
Tasks is the sole task database (it is also the phone widget). Two lists, a
personal and a work list; new tasks route between them by keyword (see
Classification).

<!-- ADD YOUR PERSONAL INFO HERE: your list names, if they are not "Life Todo" and
     "Work Todo", and anything JARVIS should know about how you use each list. -->

## Prerequisites

- Token: `~/.hermes/google_token.json` (authorized_user, with the scope
  `https://www.googleapis.com/auth/tasks`). The helper refreshes it automatically.
- Keep the token file at 600 permissions; never print or copy its contents.

## How to run

Helper: `~/.hermes/skills/task-manager/scripts/tasks_api.py` (via the `terminal`
tool). Task commands print one line per task: `TASK_ID | status | due | title`.

```bash
python3 ~/.hermes/skills/task-manager/scripts/tasks_api.py lists
python3 ~/.hermes/skills/task-manager/scripts/tasks_api.py list [LIST_ID]
python3 ~/.hermes/skills/task-manager/scripts/tasks_api.py add "TITLE" [--due RFC3339] [--notes N] [--list LIST_ID]
python3 ~/.hermes/skills/task-manager/scripts/tasks_api.py done TASK_ID
python3 ~/.hermes/skills/task-manager/scripts/tasks_api.py update TASK_ID [--title T] [--due RFC3339] [--clear-due] [--notes N] [--list LIST_ID]
python3 ~/.hermes/skills/task-manager/scripts/tasks_api.py delete TASK_ID
python3 ~/.hermes/skills/task-manager/scripts/tasks_api.py search QUERY
```

`done`, `update`, `delete` and `search` work on the default list (the last one
used, kept in `~/.hermes/tasks-api.json`). For a task on the other list, pass
`--list` where supported, or run `list LIST_ID` first.

## API notes

Base: `https://tasks.googleapis.com/tasks/v1`

| Operation | Method + path | Body |
|---|---|---|
| Task lists | GET `/users/@me/lists` | — |
| List tasks | GET `/lists/{listId}/tasks?showCompleted=true` | — |
| Create task | POST `/lists/{listId}/tasks` | `{title, notes?, due?}` |
| Update task | PATCH `/lists/{listId}/tasks/{taskId}` | `{title?, notes?, due?, status?}` |
| Delete | DELETE `/lists/{listId}/tasks/{taskId}` | — |

- Task collections live at `/tasks/v1/lists/{listId}/tasks`, without `/users/@me`;
  only the list of lists uses `/users/@me/lists`. Getting this wrong returns 404.
- `due` is RFC3339 UTC and date-only in practice, e.g. `2026-10-10T00:00:00.000Z`.
- `status` is `needsAction` or `completed`.
- There is no server-side search: `search` pages through and filters locally.

## Classification

On `add` without `--list`, a task goes to the work list if its title or notes
contain a work keyword, otherwise to the personal list. Keywords and list names
come from `~/.hermes/task-classification.json` (copy
`task-classification.example.json`).

## Procedure

1. `lists` must return at least one list; if 401/403 persists after a refresh,
   re-authorize the Google token.
2. Before adding, `search` for an existing task with the same meaning and update it
   instead of creating a duplicate. An existing task with a different date is not
   the same request: update its due date when the user asks for a new one.
3. After every write, re-run `list` or `search` and confirm the change before
   telling the user it is done.
