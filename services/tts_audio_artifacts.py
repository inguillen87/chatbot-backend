"""Local TTS artifact checks; these do not validate codecs or public delivery."""

import os
import stat


def is_nonempty_audio_file(path: str) -> bool:
    """Accept only an existing regular file with bytes, without opening media."""
    try:
        metadata = os.stat(path)
    except (OSError, TypeError, ValueError):
        return False
    return stat.S_ISREG(metadata.st_mode) and metadata.st_size > 0
