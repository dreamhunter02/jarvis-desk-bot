"""jarvis-desk-bot: short chimes for wake and end of conversation (core/handle/chimes.py)."""

import os
import uuid

from core.handle.sendAudioHandle import send_tts_message, sendAudioMessage
from core.providers.tts.dto.dto import SentenceType
from core.utils.util import audio_to_data

SOUNDS = os.path.join(os.path.dirname(__file__), "..", "providers", "tts")


async def play_chime(conn, name: str) -> bool:
    """Play <name>_chime.wav and return once the robot has played it (False if it was not played).

    Sent as its own short speech turn ("start", audio, "stop"); the "stop" waits for the
    audio to finish, so a goodbye sent afterwards does not cut the chime off.
    """
    path = os.path.join(SOUNDS, f"{name}_chime.wav")
    if not os.path.exists(path):
        return False
    try:
        packets = await audio_to_data(path, is_opus=True)
        conn.client_abort = False
        conn.sentence_id = uuid.uuid4().hex
        await send_tts_message(conn, "start")
        conn.client_is_speaking = True
        await sendAudioMessage(conn, SentenceType.FIRST, packets, None)
        await sendAudioMessage(conn, SentenceType.LAST, [], None)
        return True
    except Exception as exc:
        conn.logger.warning(f"jarvis-desk-bot: {name} chime failed ({exc})")
        return False
    finally:
        conn.client_is_speaking = False
