---
name: tasks-reminders
description: Use when scheduling, rescheduling, cancelling or checking timed reminders, recurring reminders and scheduled actions; the tasks themselves live only in Google Tasks.
---

# Google Tasks and reminder delivery

Google Tasks is the only task database: for every task create, read, update,
complete or cancel request, load `task-manager` and use its helper. Never keep tasks
in local files, memory or temporary agent todos. Google Tasks due dates are
date-only and do not notify anyone; a spoken reminder needs a delivery record.

<!-- ADD YOUR PERSONAL INFO HERE: your time zone and the command your setup uses to
     queue a spoken announcement on the robot (this repo does not include one). -->

Use {{YOUR_TIMEZONE}} unless the user says otherwise. Resolve relative times from the
current clock.

For a one-time timed reminder, first create or find its task, then queue the
announcement with your speech delivery command. Include the task title in the
delivery record and verify the record exists before confirming.

For a recurring reminder or scheduled action, list existing jobs first, then create
one scheduled job with a self-contained prompt and verify it.

When completing, cancelling or rescheduling, update the task and the linked delivery
records together. A scheduled time is not proof of delivery; check the actual status.
Report partial success explicitly if scheduling fails after the task was created.

Voice-first confirmations: speak only the human result, e.g. "I'll remind you at six
this evening." Never include task URLs, IDs, file paths or API details in spoken
replies; the robot reads replies verbatim.
