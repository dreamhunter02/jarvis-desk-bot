"""Install the JARVIS desk-bot changes into a xiaozhi-esp32-server checkout.

    python xiaozhi/apply.py ~/xiaozhi-server/main/xiaozhi-server [--llm HermesLLM]

Idempotent; every file it edits is backed up once as <file>.before-jarvis-desk-bot.

1. Installs the Hermes LLM provider (core/providers/llm/hermes/hermes.py), which
   speaks short progress updates while Hermes works and drops CJK text.
2. Lets the TTS splitter end a sentence at an English full stop followed by
   whitespace, so "Let me check." is spoken immediately (upstream only splits on
   ? ! ; : and CJK punctuation, so English text waits for the whole answer).
3. Optional: if core/handle/turnFeedback.py has on_spoken_sentence (a custom
   thinking-face module), keeps the thinking face up during progress lines.
4. Switches the configured LLM entry in data/.config.yaml to `type: hermes`.
"""

import argparse
import re
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
TAG = ".before-jarvis-desk-bot"


def backup(path: Path) -> None:
    saved = path.with_name(path.name + TAG)
    if not saved.exists():
        shutil.copy2(path, saved)


def install_provider(root: Path) -> None:
    target = root / "core/providers/llm/hermes"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HERE / "hermes_provider/hermes.py", target / "hermes.py")
    print("core/providers/llm/hermes/hermes.py: installed")


def patch_full_stop(root: Path) -> None:
    path = root / "core/providers/tts/base.py"
    text = path.read_text(encoding="utf-8")
    if "jarvis-desk-bot: English sentences" in text:
        print("tts/base.py: already patched")
        return
    anchor = (
        "        if last_punct_pos != -1:\n"
        "            segment_text_raw = current_text[: last_punct_pos + 1]"
    )
    if text.count(anchor) != 1:
        raise SystemExit("tts/base.py: splitter anchor not found; xiaozhi version not supported")
    block = (
        "        # jarvis-desk-bot: English sentences end in \".\", which is not in the sets above.\n"
        "        # Split on a full stop followed by whitespace (not \"3.5\").\n"
        "        for match in re.finditer(r\"\\.(?=\\s)\", current_text):\n"
        "            if match.start() > last_punct_pos:\n"
        "                last_punct_pos = match.start()\n\n"
    )
    backup(path)
    text = text.replace(anchor, block + anchor)
    if not any(line.strip() == "import re" for line in text.splitlines()):
        text = "import re\n" + text
    path.write_text(text, encoding="utf-8")
    print("tts/base.py: patched")


def patch_thinking_face(root: Path) -> None:
    path = root / "core/handle/turnFeedback.py"
    if not path.exists():
        print("turnFeedback.py: not present (optional, skipped)")
        return
    text = path.read_text(encoding="utf-8")
    if "PROGRESS_LINES" in text:
        print("turnFeedback.py: already patched")
        return
    match = re.search(r"async def on_spoken_sentence\(conn, text\):\n(?:    \"\"\".*?\"\"\"\n)?", text, re.S)
    if not match:
        print("turnFeedback.py: no on_spoken_sentence (optional, skipped)")
        return
    block = (
        "    # jarvis-desk-bot: spoken progress updates keep the thinking face up.\n"
        "    from core.providers.llm.hermes.hermes import PROGRESS_LINES\n"
        "    if str(text or '').strip().rstrip('.') in {line.rstrip('.') for line in PROGRESS_LINES}:\n"
        "        return False\n"
    )
    backup(path)
    path.write_text(text[: match.end()] + block + text[match.end():], encoding="utf-8")
    print("turnFeedback.py: patched")


def switch_config(root: Path, llm: str) -> None:
    path = root / "data/.config.yaml"
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^(  {re.escape(llm)}:\n    type: )\w+", re.M)
    if not pattern.search(text):
        print(f"data/.config.yaml: LLM entry {llm!r} not found; set `type: hermes` on it yourself")
        return
    backup(path)
    path.write_text(pattern.sub(r"\1hermes", text, count=1), encoding="utf-8")
    print(f"data/.config.yaml: {llm} -> type: hermes")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("server", type=Path, help="xiaozhi-server directory (contains app.py)")
    parser.add_argument("--llm", default="HermesLLM", help="LLM entry in data/.config.yaml that points at Hermes")
    args = parser.parse_args()
    root = args.server.expanduser()
    install_provider(root)
    patch_full_stop(root)
    patch_thinking_face(root)
    switch_config(root, args.llm)
    print("Restart xiaozhi-server to apply.")


if __name__ == "__main__":
    main()
