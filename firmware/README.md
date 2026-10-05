# Robot firmware: stay awake until "bye bye"

The ESP-Brookesia xiaozhi firmware has its own sleep timer, separate from the server:
it sleeps 30 s after the wake word if no speech starts (`afe_wakeup_start_timeout_ms`)
and 10 s after speech ends (`afe_wakeup_end_timeout_ms`). The timer pauses only while
the robot is speaking, so it cut conversations while the agent was still working, and
whenever the on-board voice detector missed speech that the server heard. When it
fires, the robot sends `goodbye` itself.

`stay-awake.patch` sets both to 24 hours (effectively never), so a conversation ends
only on an end phrase from the server. Pair it with `listen_silence_timeout: 0` in
`xiaozhi/config.example.yaml`.

Against [esp-brookesia](https://github.com/espressif/esp-brookesia) `release/v0.7`,
`examples/agent/chatbot`:

```bash
cd esp-brookesia
git apply /path/to/jarvis-desk-bot/firmware/stay-awake.patch
cd examples/agent/chatbot
idf.py build
idf.py -p PORT app-flash    # replaces only the app; Wi-Fi settings and assets stay
```

`app-flash` assumes a single `factory` app partition; with OTA partitions, check which
slot boots.
