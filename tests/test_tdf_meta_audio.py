"""Offline media and private-STT contracts; not recording or delivery evidence."""
from dataclasses import replace
import base64
import hashlib
import json
from types import SimpleNamespace

import pytest
from services import tdf_meta_audio as audio
from services import audio_transcription_service as stt
from services.meta_whatsapp_cloud import MetaSenderSnapshot, VerifiedMetaBinding, VerifiedMetaAuthority
from services.meta_whatsapp_credentials import MetaCredential
from services.meta_whatsapp_webhook import MetaWebhookEvent

NOW = 1791637200
URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=555555&ext=123&hash=synthetic"
# Only a format-validation fixture, not a decodable recording.
BYTES = b"OggS\x00" + b"\x00" * 21 + b"\x01\x13" + b"OpusHead" + b"\x00" * 11


def voice_case():
    sender = MetaSenderSnapshot(46, 1, 1, "1719510329487224", "2192778428137676", "1137550632773388", "sandbox")
    credential = MetaCredential(1, NOW + 3600, "synthetic-meta-test-secret-only")
    binding = VerifiedMetaBinding(sender, VerifiedMetaAuthority(sender, 1, "v25.0", NOW, NOW + 60, True, True), credential)
    event = MetaWebhookEvent(sender, "message", "synthetic-key", NOW, "wamid.voice", "synthetic-contact",
                            content_type="audio", media_id="555555", media_mime_type="audio/ogg; codecs=opus")
    metadata = {"id": event.media_id, "mime_type": "audio/ogg", "file_size": len(BYTES),
                "sha256": hashlib.sha256(BYTES).hexdigest(), "url": URL}
    return event, binding, metadata


def invoke(event, binding, metadata, **overrides):
    params = dict(cfg={"GRAPH_VERSION": "v25.0"}, binding_loader=lambda: binding, now=lambda: NOW,
                  metadata_request=lambda *args, **kwargs: metadata, metadata_parser=lambda value: value,
                  download=lambda *args, **kwargs: BYTES, transcriber=lambda *args, **kwargs: "MENU")
    params.update(overrides)
    return audio.transcribe_meta_voice_note(event, **params)


@pytest.mark.parametrize("checksum_encoding", ["hex", "base64"])
def test_bound_media_scope_checksum_and_private_transcription(checksum_encoding):
    event, binding, metadata = voice_case()
    if checksum_encoding == "base64":
        metadata["sha256"] = base64.b64encode(hashlib.sha256(BYTES).digest()).decode("ascii")
        event = replace(event, media_sha256=metadata["sha256"])
    calls = []
    def lookup(method, url, **kwargs):
        calls.append("lookup")
        assert method == "GET" and url == "https://graph.facebook.com/v25.0/555555"
        assert kwargs["params"] == {"phone_number_id": event.sender.phone_number_id}
        assert kwargs["verify"] is True and kwargs["allow_redirects"] is False
        return metadata
    def download(url, token, **kwargs):
        calls.append("download")
        assert url == URL and token == binding.credential.access_token
        return BYTES
    def transcribe(data, mime, **kwargs):
        calls.append("transcribe")
        assert data == BYTES and mime == "audio/ogg"
        assert kwargs["max_bytes"] == 2 * 1024 * 1024 and 0 < kwargs["timeout_seconds"] <= 20
        return " MENU "
    assert invoke(event, binding, metadata, metadata_request=lookup, download=download, transcriber=transcribe) == "MENU"
    assert calls == ["lookup", "download", "transcribe"]
    assert event.media_id not in repr(event) and binding.credential.access_token not in repr(binding)


@pytest.mark.parametrize("field,value", [
    ("id", "999999"), ("mime_type", "audio/mpeg"), ("file_size", True), ("file_size", 0),
    ("file_size", 2097153), ("sha256", "bad"), ("url", "http://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=x"),
    ("url", "https://lookaside.fbsbx.com.evil.invalid/whatsapp_business/attachments/?mid=x"),
    ("url", "https://user:pass@lookaside.fbsbx.com/whatsapp_business/attachments/?mid=x"),
    ("url", "https://lookaside.fbsbx.com:443/whatsapp_business/attachments/?mid=x"),
    ("url", "https://127.0.0.1/whatsapp_business/attachments/?mid=x"),
    ("url", "https://lookaside.fbsbx.com/another-path/?mid=x"),
])
def test_invalid_meta_metadata_never_downloads_or_transcribes(field, value):
    event, binding, metadata = voice_case(); metadata[field] = value
    with pytest.raises(audio.TdfAudioError):
        invoke(event, binding, metadata, download=lambda *a, **k: pytest.fail("invalid media must not download"))


@pytest.mark.parametrize("data", [b"wrong bytes", b"OggS\x00" + b"x" * 64, BYTES + b"wrong"])
def test_unverified_media_never_reaches_openai(data):
    event, binding, metadata = voice_case()
    with pytest.raises(audio.TdfAudioError):
        invoke(event, binding, metadata, download=lambda *a, **k: data,
               transcriber=lambda *a, **k: pytest.fail("bad hash/format must not reach STT"))


def test_matching_checksum_does_not_allow_a_non_opus_container():
    event, binding, metadata = voice_case()
    data = b"OggS\x00" + b"x" * 100
    metadata.update(sha256=hashlib.sha256(data).hexdigest(), file_size=len(data))
    with pytest.raises(audio.TdfAudioError):
        invoke(event, binding, metadata, download=lambda *a, **k: data,
               transcriber=lambda *a, **k: pytest.fail("format still needs validation"))


def test_webhook_hash_must_match_meta_and_total_deadline_precedes_openai(monkeypatch):
    event, binding, metadata = voice_case()
    with pytest.raises(audio.TdfAudioError):
        invoke(replace(event, media_sha256="a" * 64), binding, metadata,
               download=lambda *a, **k: pytest.fail("webhook/Meta hash mismatch blocks download"))
    clock = [100.0]
    monkeypatch.setattr(audio.time, "monotonic", lambda: clock[0])
    def slow_download(*args, **kwargs):
        clock[0] += 36
        return BYTES
    with pytest.raises(audio.TdfAudioError):
        invoke(event, binding, metadata, download=slow_download,
               transcriber=lambda *a, **k: pytest.fail("deadline must prevent OpenAI call"))


@pytest.mark.parametrize("status,headers,chunks", [
    (302, {"Location": "https://evil.invalid"}, []),
    (200, {"Content-Type": "audio/ogg", "Content-Length": "2097153"}, []),
    (200, {"Content-Type": "text/html"}, []),
    (200, {"Content-Type": "audio/ogg", "Content-Encoding": "gzip"}, []),
    (200, {"Content-Type": "audio/ogg"}, [b"x" * 2097153]),
])
def test_attachment_transport_rejects_redirect_encoding_mime_and_unbounded_bytes(monkeypatch, status, headers, chunks):
    connections = []
    remaining_chunks = iter(chunks)
    response = SimpleNamespace(status=status, getheader=lambda name, default=None: headers.get(name, default),
                               read1=lambda size: next(remaining_chunks, b""))
    class Connection:
        def __init__(self, host, *, timeout, context):
            import ssl
            assert host == "lookaside.fbsbx.com" and timeout <= 3
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            self.sock = SimpleNamespace(settimeout=lambda value: None)
            self.closed = False; connections.append(self)
        def set_debuglevel(self, value): assert value == 0
        def connect(self): pass
        def request(self, method, path, *, headers):
            assert method == "GET" and path.startswith("/whatsapp_business/attachments/?")
            assert headers["Authorization"] == "Bearer synthetic-test-token"
        def getresponse(self): return response
        def close(self): self.closed = True
    monkeypatch.setattr(audio.http.client, "HTTPSConnection", Connection)
    with pytest.raises(audio.TdfAudioError):
        audio._download(URL, "synthetic-test-token", deadline=audio.time.monotonic() + 10)
    assert len(connections) == 1 and connections[0].closed


def test_rotated_or_foreign_binding_and_expired_window_do_not_download():
    event, binding, metadata = voice_case()
    calls = []
    def changed():
        calls.append(1)
        return binding if len(calls) < 3 else replace(binding, credential=replace(binding.credential, revision=2))
    with pytest.raises(audio.TdfAudioError, match="binding_invalid"):
        invoke(event, binding, metadata, binding_loader=changed,
               download=lambda *a, **k: pytest.fail("rotation blocks download"))
    with pytest.raises(audio.TdfAudioError, match="binding_invalid"):
        invoke(replace(event, sender=replace(event.sender, tenant_id=47)), binding, metadata)
    with pytest.raises(audio.TdfAudioError, match="binding_invalid"):
        invoke(event, binding, metadata, now=lambda: NOW + 86400,
               metadata_request=lambda *a, **k: pytest.fail("expired window blocks lookup"))


def test_provider_exception_is_fixed_and_does_not_contain_media_or_credentials(capsys, caplog):
    event, binding, metadata = voice_case()
    def fail(*args, **kwargs):
        raise RuntimeError("secret " + URL + binding.credential.access_token)
    with pytest.raises(audio.TdfAudioError) as error:
        invoke(event, binding, metadata, metadata_request=fail)
    assert str(error.value) == "tdf_audio_unavailable"
    output = capsys.readouterr()
    assert URL not in output.out + output.err + caplog.text + str(error.value)


class Context:
    def __init__(self, value): self.value = value
    def __enter__(self): return self.value
    def __exit__(self, *args): return False


def fake_stt(monkeypatch, chunks):
    calls = []
    response = SimpleNamespace(iter_bytes=lambda **kwargs: iter(chunks))
    def create(**kwargs):
        calls.append(kwargs)
        assert kwargs["file"][0] == "audio.ogg" and kwargs["file"][2] == "audio/ogg"
        assert kwargs["file"][1].read() == BYTES
        return Context(response)
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(
        with_streaming_response=SimpleNamespace(create=create))))
    def openai(**kwargs):
        assert kwargs["base_url"] == "https://api.openai.com/v1" and kwargs["max_retries"] == 0
        return Context(client)
    def http_client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["proxy"] is None and kwargs["follow_redirects"] is False
        assert kwargs["timeout"].write <= 3 and kwargs["timeout"].pool <= 1
        assert kwargs["headers"] == {"Accept-Encoding": "identity"}
        assert len(kwargs["event_hooks"]["response"]) == 1
        return Context(SimpleNamespace())
    monkeypatch.setattr(stt, "_openai_api_key", lambda: "synthetic-test-api-key")
    monkeypatch.setattr(stt, "llm_provider_network_allowed", lambda *a: True)
    monkeypatch.setattr(stt, "OpenAI", openai)
    monkeypatch.setattr(stt.httpx, "Client", http_client)
    return calls


def test_private_stt_pins_official_endpoint_never_uses_cache(monkeypatch):
    calls = fake_stt(monkeypatch, [json.dumps({"text": "MENÚ"}).encode()])
    monkeypatch.setattr(stt, "_stt_cache_get", lambda *a: pytest.fail("private audio must not read cache"))
    monkeypatch.setattr(stt, "_stt_cache_set", lambda *a: pytest.fail("private audio must not write cache"))
    assert stt.transcribe_private_audio_bytes(BYTES, "audio/ogg", max_bytes=2097152, timeout_seconds=20) == "MENÚ"
    assert len(calls) == 1 and calls[0]["model"] == "gpt-4o-transcribe"


@pytest.mark.parametrize("chunks", [[b"x" * 32769], [b'{}'], [b'{"text":""}'], [json.dumps({"text": "x" * 2001}).encode()]])
def test_private_stt_rejects_unbounded_or_unintelligible_response(monkeypatch, chunks):
    calls = fake_stt(monkeypatch, chunks)
    with pytest.raises(stt.PrivateAudioTranscriptionError):
        stt.transcribe_private_audio_bytes(BYTES, "audio/ogg", max_bytes=2097152, timeout_seconds=20)
    assert len(calls) == 1


def test_private_stt_test_policy_does_not_start_sdk(monkeypatch):
    monkeypatch.setattr(stt, "_openai_api_key", lambda: "synthetic-test-api-key")
    monkeypatch.setattr(stt, "llm_provider_network_allowed", lambda *a: False)
    monkeypatch.setattr(stt, "OpenAI", lambda *a, **k: pytest.fail("offline policy must run before SDK"))
    with pytest.raises(stt.PrivateAudioTranscriptionError, match="unavailable"):
        stt.transcribe_private_audio_bytes(BYTES, "audio/ogg", max_bytes=2097152, timeout_seconds=20)


def test_private_stt_response_deadline_is_enforced(monkeypatch):
    fake_stt(monkeypatch, [b'{"text":"MENU"}'])
    values = iter([100.0, 121.0])
    monkeypatch.setattr(stt.time, "monotonic", lambda: next(values))
    with pytest.raises(stt.PrivateAudioTranscriptionError, match="failed"):
        stt.transcribe_private_audio_bytes(BYTES, "audio/ogg", max_bytes=2097152, timeout_seconds=20)


@pytest.mark.parametrize("status,headers", [
    (400, {}), (500, {"Content-Length": "99999999"}),
    (302, {"Location": "https://another.invalid/"}),
    (200, {"Content-Length": "32769"}), (200, {"Content-Encoding": "gzip"}),
])
def test_private_stt_real_sdk_rejects_http_errors_before_reading_body(monkeypatch, status, headers):
    import httpx
    # The actual SDK normally reads complete error bodies, even with its streaming
    # response API. Mock only the HTTP transport; no provider/DNS/socket is used.
    reads, closed, requests = [], [], []
    class UnreadableBody(httpx.SyncByteStream):
        def __iter__(self):
            reads.append(1)
            raise AssertionError("rejected provider body must never be read")
            yield b""  # pragma: no cover
        def close(self): closed.append(1)
    def respond(request):
        requests.append(request)
        assert request.url == "https://api.openai.com/v1/audio/transcriptions"
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(status, headers=headers, stream=UnreadableBody())
    mock_transport = httpx.MockTransport(respond)
    def http_client(**kwargs):
        return httpx.Client(transport=mock_transport, **kwargs)
    monkeypatch.setattr(stt, "httpx", SimpleNamespace(Client=http_client, Timeout=httpx.Timeout))
    monkeypatch.setattr(stt, "_openai_api_key", lambda: "synthetic-test-api-key")
    monkeypatch.setattr(stt, "llm_provider_network_allowed", lambda *a: True)
    with pytest.raises(stt.PrivateAudioTranscriptionError, match="failed"):
        stt.transcribe_private_audio_bytes(BYTES, "audio/ogg", max_bytes=2097152, timeout_seconds=20)
    assert len(requests) == 1 and closed and not reads


def test_private_stt_real_sdk_success_remains_bounded_and_streamed(monkeypatch):
    import httpx
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": "application/json"},
                              content=b'{"text":"MENU"}')
    mock_transport = httpx.MockTransport(respond)
    monkeypatch.setattr(stt, "httpx", SimpleNamespace(
        Client=lambda **kwargs: httpx.Client(transport=mock_transport, **kwargs), Timeout=httpx.Timeout))
    monkeypatch.setattr(stt, "_openai_api_key", lambda: "synthetic-test-api-key")
    monkeypatch.setattr(stt, "llm_provider_network_allowed", lambda *a: True)
    assert stt.transcribe_private_audio_bytes(BYTES, "audio/ogg", max_bytes=2097152, timeout_seconds=20) == "MENU"
    assert len(requests) == 1
