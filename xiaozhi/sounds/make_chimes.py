"""Generate the wake and end chimes (16 kHz mono PCM16 WAV, like xiaozhi's ack_chime.wav).

    python make_chimes.py [OUTPUT_DIR]

wake_chime.wav  "ba-ding": two quick rising bell notes, played when the wake word is heard.
end_chime.wav   "tung":    two lower, falling notes, played before the robot goes to sleep.
Both stay near the loudness of the 1400 Hz acknowledgement chime.
"""

import sys
import wave
from pathlib import Path

import numpy as np

RATE = 16_000
PEAK = 6000  # the acknowledgement chime peaks at about 5300


def bell(freq: float, seconds: float, decay: float) -> np.ndarray:
    """A soft bell: fundamental plus a quiet octave, 5 ms attack, exponential decay."""
    t = np.arange(int(RATE * seconds)) / RATE
    tone = np.sin(2 * np.pi * freq * t) + 0.25 * np.sin(2 * np.pi * 2 * freq * t)
    envelope = np.minimum(t / 0.005, 1.0) * np.exp(-t / decay)
    return tone * envelope


def chime(notes) -> np.ndarray:
    """Overlapping notes: each (start_s, freq, length_s, decay_s)."""
    total = max(start + length for start, _, length, _ in notes)
    out = np.zeros(int(RATE * total) + 1)
    for start, freq, length, decay in notes:
        note = bell(freq, length, decay)
        i = int(RATE * start)
        out[i : i + len(note)] += note
    fade = int(RATE * 0.01)
    out[-fade:] *= np.linspace(1, 0, fade)
    return out / np.abs(out).max() * PEAK


def save(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.astype(np.int16).tobytes())
    print(f"{path}: {len(samples) / RATE:.2f} s")


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent)
    out.mkdir(parents=True, exist_ok=True)
    # D6 then G6: bright and rising, distinct from the single 1400 Hz acknowledgement.
    save(out / "wake_chime.wav", chime([(0.0, 1175, 0.12, 0.05), (0.08, 1568, 0.22, 0.08)]))
    # G5 then C5: lower and falling, the "tung" to the wake chime's "ting".
    save(out / "end_chime.wav", chime([(0.0, 784, 0.16, 0.07), (0.12, 523, 0.34, 0.13)]))


if __name__ == "__main__":
    main()
