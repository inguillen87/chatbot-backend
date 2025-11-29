"""Minimal speech-to-text service stub for test environments.

This module provides the ``SpeechToTextService`` class expected by
``services.input_processor``. The implementation intentionally avoids
external dependencies and simply raises ``NotImplementedError`` to make
it clear that speech-to-text is not configured in the current context.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class SpeechToTextService:
    """Placeholder speech-to-text adapter.

    In production this class should wrap a concrete provider such as
    Google Speech-to-Text. The test stub only logs the request and
    returns ``None`` so the rest of the pipeline can continue without
    network access.
    """

    def transcribe_audio_url(self, url: str, mime_type: Optional[str] = None) -> Optional[str]:
        logger.warning(
            "SpeechToTextService.transcribe_audio_url is not implemented; "
            "received url=%s mime_type=%s",
            url,
            mime_type,
        )
        return None
