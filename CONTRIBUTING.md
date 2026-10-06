# Contributing

Thanks for helping improve JARVIS Desk Bot.

## Reporting a problem

Open an issue with:

- what you said to the robot and what it did;
- the matching lines from the xiaozhi-server log (`tmp/server.log`) and the Hermes agent log
  (`~/.hermes/logs/agent.log`);
- your versions of xiaozhi-esp32-server, Hermes and the robot firmware, and the models you use.

Remove personal details (names, tasks, keys, addresses) from logs before posting.

## Changing the code

- **xiaozhi changes** go through `xiaozhi/apply.py`. Keep it idempotent: anchor each edit on
  upstream code, check for its own marker before editing, back the file up once as
  `*.before-jarvis-desk-bot`, and skip with a message when an anchor is missing.
- **Hermes skills** must stay free of personal data. Use a placeholder and an
  `ADD YOUR PERSONAL INFO HERE` comment instead.
- **Firmware changes** are patches against ESP-Brookesia `release/v0.7`; check that
  `git apply --check` passes on a clean checkout.
- Keep spoken text short and plain: everything the robot says is read aloud verbatim.
- Before opening a pull request, run `python -m py_compile` on changed Python files, and test
  the change on a robot or with a simulated device.

Explain the problem a change solves in the pull request. If it fixes behaviour you measured,
add the numbers to `docs/design-notes.md`.

## License

By contributing you agree that your contributions are licensed under the MIT License.
