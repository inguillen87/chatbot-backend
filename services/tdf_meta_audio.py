"""Ephemeral voice-note processing for the separately authorized TDF pilot.

Meta supplies the media URL after an authenticated media-ID lookup. Never use
a URL from the webhook/user, the shared Twilio path or the generic STT cache.
Admission and chunk checks use a 35-second budget with late results discarded.
I/O timeouts are per operation, not an absolute SDK cancellation deadline.
The recording cap is 2 MiB and no stage retries.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import re
import ssl
import time
from urllib.parse import urlsplit

MAX_AUDIO_BYTES = 2 * 1024 * 1024
TOTAL_SECONDS = 35.0
_ID = re.compile(r"^[1-9][0-9]{0,39}$")
_MIME = "audio/ogg"


class TdfAudioError(RuntimeError):
    """Only fixed codes may leave this boundary."""


def _digest(value):
    if not isinstance(value, str):
        raise TdfAudioError("tdf_audio_media_invalid")
    try:
        result = bytes.fromhex(value) if re.fullmatch(r"[0-9a-fA-F]{64}", value) else base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise TdfAudioError("tdf_audio_media_invalid") from None
    if len(result) != 32:
        raise TdfAudioError("tdf_audio_media_invalid")
    return result


def _media_url(url):
    if not isinstance(url, str) or len(url) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise TdfAudioError("tdf_audio_media_invalid")
    parsed = urlsplit(url)
    # Exact Meta attachment service, no suffix match, userinfo, ports or redirects.
    if (parsed.scheme != "https" or parsed.netloc != "lookaside.fbsbx.com"
            or parsed.path != "/whatsapp_business/attachments/" or not parsed.query or parsed.fragment):
        raise TdfAudioError("tdf_audio_media_invalid")
    return parsed


def _download(url, token, *, deadline):
    parsed = _media_url(url)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TdfAudioError("tdf_audio_timeout")
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=min(3.0, remaining),
                                             context=ssl.create_default_context())
    connection.set_debuglevel(0)
    try:
        connection.connect()
        connection.sock.settimeout(min(5.0, max(0.001, deadline - time.monotonic())))
        connection.request("GET", parsed.path + "?" + parsed.query,
                           headers={"Authorization": "Bearer " + token, "Accept": _MIME})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TdfAudioError("tdf_audio_timeout")
        connection.sock.settimeout(min(5.0, remaining))
        response = connection.getresponse()
        if response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity":
            raise TdfAudioError("tdf_audio_download_failed")
        mime = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        if mime != _MIME:
            raise TdfAudioError("tdf_audio_format_unsupported")
        size = response.getheader("Content-Length")
        if size is not None and (not size.isascii() or not size.isdigit() or not 0 < int(size) <= MAX_AUDIO_BYTES):
            raise TdfAudioError("tdf_audio_too_large")
        body = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TdfAudioError("tdf_audio_timeout")
            if connection.sock is not None:
                connection.sock.settimeout(min(5.0, remaining))
            chunk = response.read1(8192)
            if not chunk:
                break
            if len(body) + len(chunk) > MAX_AUDIO_BYTES:
                raise TdfAudioError("tdf_audio_too_large")
            body.extend(chunk)
        return bytes(body)
    except TdfAudioError:
        raise
    except Exception:
        raise TdfAudioError("tdf_audio_download_failed") from None
    finally:
        connection.close()


def transcribe_meta_voice_note(event, *, cfg, binding_loader, now,
                               metadata_request, metadata_parser,
                               download=None, transcriber=None):
    """After receipt/allowlist/window checks, validate media and transcribe once.

    Every network stage re-reads the original credential revision and sender.
    The successful transcript is returned in memory to the public knowledge
    responder; callers must never persist it or the recording.
    """
    deadline = time.monotonic() + TOTAL_SECONDS
    if (event.content_type != "audio" or not isinstance(event.media_id, str)
            or not _ID.fullmatch(event.media_id)
            or (event.sender.tenant_id, event.sender.app_id, event.sender.waba_id,
                event.sender.phone_number_id, event.sender.environment) !=
               (46, "1719510329487224", "2192778428137676", "1137550632773388", "sandbox")):
        raise TdfAudioError("tdf_audio_binding_invalid")
    if not isinstance(event.media_mime_type, str) or event.media_mime_type.split(";", 1)[0].strip().lower() != _MIME:
        raise TdfAudioError("tdf_audio_format_unsupported")
    initial = binding_loader()

    def live():
        binding = binding_loader()
        if (binding.sender != event.sender or binding.credential.revision != initial.credential.revision
                or not hmac.compare_digest(binding.credential.access_token, initial.credential.access_token)
                or not 0 <= now() - event.timestamp < 86400
                or time.monotonic() >= deadline):
            raise TdfAudioError("tdf_audio_binding_invalid")
        return binding

    try:
        binding = live()
        metadata = metadata_parser(metadata_request("GET",
            "https://graph.facebook.com/" + cfg["GRAPH_VERSION"] + "/" + event.media_id,
            headers={"Authorization": "Bearer " + binding.credential.access_token},
            params={"phone_number_id": event.sender.phone_number_id},
            timeout=(3.0, 10.0), allow_redirects=False, verify=True))
        if type(metadata.get("file_size")) is int and metadata["file_size"] > MAX_AUDIO_BYTES:
            raise TdfAudioError("tdf_audio_too_large")
        if (metadata.get("id") != event.media_id or metadata.get("mime_type", "").split(";", 1)[0].strip().lower() != _MIME
                or type(metadata.get("file_size")) is not int or not 0 < metadata["file_size"] <= MAX_AUDIO_BYTES):
            raise TdfAudioError("tdf_audio_media_invalid")
        expected_digest = _digest(metadata.get("sha256"))
        if event.media_sha256 is not None and not hmac.compare_digest(_digest(event.media_sha256), expected_digest):
            raise TdfAudioError("tdf_audio_media_invalid")
        url = metadata.get("url")
        _media_url(url)
        binding = live()
        data = (download or _download)(url, binding.credential.access_token, deadline=min(deadline, time.monotonic() + 10.0))
        if (not isinstance(data, bytes) or not 0 < len(data) <= MAX_AUDIO_BYTES
                or len(data) != metadata["file_size"]
                or not hmac.compare_digest(hashlib.sha256(data).digest(), expected_digest)
                or not data.startswith(b"OggS\x00") or len(data) < 47
                or data[27 + data[26]:27 + data[26] + 8] != b"OpusHead"):
            raise TdfAudioError("tdf_audio_media_invalid")
        live()
        if transcriber is None:
            from services.audio_transcription_service import transcribe_private_audio_bytes
            transcriber = transcribe_private_audio_bytes
        text = transcriber(data, _MIME, max_bytes=MAX_AUDIO_BYTES,
                           timeout_seconds=min(20.0, deadline - time.monotonic()))
        live()
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise TdfAudioError("tdf_audio_unintelligible")
        return text.strip()
    except TdfAudioError:
        raise
    except Exception:
        raise TdfAudioError("tdf_audio_unavailable") from None


def audio_error_message(code):
    if code == "tdf_audio_format_unsupported":
        reason = "En esta prueba puedo leer las notas de voz grabadas con el micrófono de WhatsApp."
    elif code == "tdf_audio_too_large":
        reason = "La nota supera el límite de 2 MB. Probá con una nota más corta."
    else:
        reason = "No pude leer esta nota de voz. Podés intentar con una nota más corta y clara."
    return ("🎙️ " + reason + "\nTambién podés escribir una palabra o un número del menú, "
            "o pedir ayuda de una persona. Escribí MENÚ para volver.")
