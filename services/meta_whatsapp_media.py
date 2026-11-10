"""Native media port; no upload/config writer or public URL download is installed.

Only a server-managed per-connection catalog can select a reference. Registration
requires a verified Meta upload receipt; existing branding URLs are insufficient.
The catalog is deliberately absent by default. A DTO never authorizes an asset.
"""
from dataclasses import asdict, dataclass, fields
import hashlib
import io
from pathlib import Path
import re
import warnings
from urllib.parse import urlsplit, urlunsplit

from flask import current_app
from PIL import Image
from sqlalchemy import select

from models import ProviderConnection, ProviderSender, TenantProfile
from services.meta_whatsapp_cloud import MetaSenderSnapshot, meta_id
from services.meta_whatsapp_runtime import MetaRuntimeError
from services.tenant_provider_credentials import ProviderCredentialError
from services.outbox_execution_budget import OutboxExecutionBudgetExceeded, require_outbox_time_remaining
from services.whatsapp_receipt_branding import public_whatsapp_receipt_image_url

CATALOG_KEY = "meta_whatsapp_verified_media_v2"
UPLOAD_PROOF_VERIFIER = "chatboc.meta_media_upload_proof_verifier"
CONTRACT = "meta.server_media_reference.v2"
# Existing routes/archivos.py upload read bound, NOT a certified Meta maximum.
MAX_PROJECT_BYTES = 10 * 1024 * 1024
# Explicit decode resource fence; the verified repository header is 1024x472.
MAX_PROJECT_PIXELS = 4096 * 4096
MAX_PROJECT_FRAMES = 256
ROLES = {"welcome_sticker": "sticker", "receipt_header": "image"}


@dataclass(frozen=True)
class MetaMediaReference:
    contract_version: str
    tenant_id: int
    connection_id: int
    sender_id: int
    app_id: str
    waba_id: str
    phone_number_id: str
    role: str
    revision: int
    media_id: str
    static_path: str
    configured_url: str
    sha256: str
    mime_type: str
    byte_length: int
    width: int
    height: int
    animated: bool
    frames: int
    verified_at: int
    valid_until: int
    verification_receipt_sha256: str


@dataclass(frozen=True)
class VerifiedMediaUpload:
    """Result of a trusted server receipt reader, never a config/JSON flag.

    The production reader is deliberately NOT installed by this module. It must
    prove current server registration URL -> local asset bytes -> native upload
    receipt/sender/media ID and validity. Merely echoing reference.configured_url
    is not a production verifier or evidence that a remote host serves the asset,
    without sending, uploading or granting anything as a side effect of a read.
    """
    sender: MetaSenderSnapshot
    media_id: str
    asset_sha256: str
    receipt_sha256: str
    observed_at: int
    valid_until: int
    configured_url: str


def canonical_configured_media_url(value, *, static_path, public_base_url=""):
    """Canonical URL identity only; shape/host is never authority or fetched.

    Relative legacy static URLs require an explicit HTTPS root from existing
    server APP_BASE_URL. Request host/Origin is never used. Both absolute and
    resolved URLs still need exact catalog and trusted registration-proof match.
    """
    if not isinstance(public_base_url, str):
        raise MetaRuntimeError("meta_configured_media_unverified")
    selected = public_whatsapp_receipt_image_url(value,
        public_base_url=public_base_url, allow_relative_static=bool(public_base_url))
    if selected is None:
        raise MetaRuntimeError("meta_configured_media_unverified")
    try:
        parsed = urlsplit(selected)
        if parsed.path != "/static/" + static_path:
            raise ValueError("static path mismatch")
        host = parsed.hostname.lower().rstrip(".")
        # Existing branding validation admits only HTTPS and default port.
        # Lowercase host/default :443 normalization does not equate origins.
        authority = "[" + host + "]" if ":" in host else host
        return urlunsplit(("https", authority, parsed.path, "", ""))
    except (ValueError, TypeError, AttributeError):
        raise MetaRuntimeError("meta_configured_media_unverified") from None


def parse_media_reference(value, *, now):
    required = {f.name for f in fields(MetaMediaReference)}
    if isinstance(value, MetaMediaReference):
        value = asdict(value)
    if not isinstance(value, dict) or set(value) != required:
        raise MetaRuntimeError("meta_media_reference_invalid")
    integer_fields = ("tenant_id", "connection_id", "sender_id", "revision", "byte_length", "width", "height", "frames")
    if (value.get("contract_version") != CONTRACT or not isinstance(value.get("role"), str) or value["role"] not in ROLES
            or any(type(value[k]) is not int or value[k] <= 0 for k in integer_fields)
            or type(value.get("animated")) is not bool
            or type(now) is not int or now < 0
            or type(value.get("verified_at")) is not int or type(value.get("valid_until")) is not int
            or not 0 <= value["verified_at"] <= now < value["valid_until"]
            or not 0 < value["byte_length"] <= MAX_PROJECT_BYTES
            or value["width"] * value["height"] > MAX_PROJECT_PIXELS
            or value["frames"] > MAX_PROJECT_FRAMES):
        raise MetaRuntimeError("meta_media_reference_invalid")
    for key in ("app_id", "waba_id", "phone_number_id", "media_id"):
        try:
            meta_id(value[key])
        except (ValueError, TypeError, ProviderCredentialError):
            raise MetaRuntimeError("meta_media_reference_invalid") from None
    for key in ("sha256", "verification_receipt_sha256"):
        if not isinstance(value[key], str) or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            raise MetaRuntimeError("meta_media_reference_invalid")
    path = value.get("static_path")
    if (not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,255}", path)
            or path.startswith("/") or any(x in {"", ".", ".."} for x in path.split("/"))):
        raise MetaRuntimeError("meta_media_reference_invalid")
    if canonical_configured_media_url(value["configured_url"], static_path=path) != value["configured_url"]:
        raise MetaRuntimeError("meta_media_reference_invalid")
    if value["role"] == "welcome_sticker":
        limit = 500000 if value["animated"] else 100000
        if (value["mime_type"] != "image/webp" or (value["width"], value["height"]) != (512, 512)
                or value["byte_length"] > limit):
            raise MetaRuntimeError("meta_sticker_format_invalid")
    elif not isinstance(value["mime_type"], str) or value["mime_type"] not in {"image/png", "image/jpeg"} or value["animated"] or value["frames"] != 1:
        raise MetaRuntimeError("meta_image_format_invalid")
    return MetaMediaReference(**value)


def _webp_animation(data):
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP" or int.from_bytes(data[4:8], "little") + 8 != len(data):
        raise MetaRuntimeError("meta_sticker_bitstream_invalid")
    offset, chunks, flags = 12, [], None
    while offset < len(data):
        if offset + 8 > len(data):
            raise MetaRuntimeError("meta_sticker_bitstream_invalid")
        kind, size = data[offset:offset+4], int.from_bytes(data[offset+4:offset+8], "little")
        end = offset + 8 + size
        if end + size % 2 > len(data):
            raise MetaRuntimeError("meta_sticker_bitstream_invalid")
        chunks.append(kind)
        if kind == b"VP8X":
            if flags is not None or size != 10:
                raise MetaRuntimeError("meta_sticker_bitstream_invalid")
            flags = bool(data[offset+8] & 2)
        offset = end + size % 2
    animated = b"ANIM" in chunks or b"ANMF" in chunks
    if animated and (flags is not True or chunks.count(b"ANIM") != 1 or b"ANMF" not in chunks):
        raise MetaRuntimeError("meta_sticker_bitstream_invalid")
    if not animated and flags is True:
        raise MetaRuntimeError("meta_sticker_bitstream_invalid")
    return animated


def _verify_local_asset(reference):
    """Bounded existing static bytes only; never GET an LLM/admin URL."""
    require_outbox_time_remaining()
    if not isinstance(current_app.static_folder, str) or not current_app.static_folder:
        raise MetaRuntimeError("meta_media_asset_unverified")
    raw_root = Path(current_app.static_folder)
    if raw_root.is_symlink() or (hasattr(raw_root, "is_junction") and raw_root.is_junction()):
        raise MetaRuntimeError("meta_media_asset_unverified")
    root = raw_root.resolve()
    path = root.joinpath(*reference.static_path.split("/"))
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise MetaRuntimeError("meta_media_asset_unverified")
        if candidate == root:
            break
    target = path.resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise MetaRuntimeError("meta_media_asset_unverified")
    try:
        with target.open("rb") as stream:
            data = stream.read(MAX_PROJECT_BYTES + 1)
        require_outbox_time_remaining()
        if len(data) != reference.byte_length or hashlib.sha256(data).hexdigest() != reference.sha256:
            raise MetaRuntimeError("meta_media_asset_changed")
        animated = _webp_animation(data) if reference.mime_type == "image/webp" else False
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                mime = {"WEBP": "image/webp", "PNG": "image/png", "JPEG": "image/jpeg"}.get(image.format)
                if (mime != reference.mime_type or image.size != (reference.width, reference.height)
                        or getattr(image, "n_frames", 1) != reference.frames or animated != reference.animated
                        or (reference.role == "receipt_header" and getattr(image, "is_animated", False))):
                    raise MetaRuntimeError("meta_media_bitstream_mismatch")
                for frame in range(reference.frames):
                    require_outbox_time_remaining()
                    image.seek(frame)
                    image.load()
        require_outbox_time_remaining()
    except (MetaRuntimeError, OutboxExecutionBudgetExceeded):
        raise
    except Exception:
        raise MetaRuntimeError("meta_media_asset_unverified") from None


def load_verified_media_reference(identity, role, *, session, now):
    if not isinstance(identity, MetaSenderSnapshot) or not isinstance(role, str) or role not in ROLES:
        raise MetaRuntimeError("meta_media_scope_invalid")
    C, S, T = ProviderConnection, ProviderSender, TenantProfile
    with session.no_autoflush:
        row = session.execute(select(C.config, C.tenant_id, C.external_app_id, C.external_account_id,
            S.tenant_id.label("sender_tenant"), S.waba_id, S.phone_number_id, T.is_active)
            .join(S, S.provider_connection_id == C.id).join(T, T.id == C.tenant_id)
            .where(C.id == identity.connection_id, S.id == identity.sender_id,
                C.provider == "meta", C.channel == "whatsapp")).one_or_none()
    if (row is None or row.is_active is not True or row.tenant_id != identity.tenant_id
            or row.sender_tenant != identity.tenant_id or row.external_app_id != identity.app_id
            or row.external_account_id != identity.waba_id or row.waba_id != identity.waba_id
            or row.phone_number_id != identity.phone_number_id):
        raise MetaRuntimeError("meta_media_scope_invalid")
    catalog = row.config.get(CATALOG_KEY) if isinstance(row.config, dict) else None
    if not isinstance(catalog, dict) or role not in catalog:
        raise MetaRuntimeError("meta_media_reference_unverified")
    reference = parse_media_reference(catalog[role], now=now)
    expected = (identity.tenant_id, identity.connection_id, identity.sender_id,
                identity.app_id, identity.waba_id, identity.phone_number_id, role)
    if (reference.tenant_id, reference.connection_id, reference.sender_id, reference.app_id,
            reference.waba_id, reference.phone_number_id, reference.role) != expected:
        raise MetaRuntimeError("meta_media_scope_invalid")
    _verify_local_asset(reference)
    verifier = current_app.extensions.get(UPLOAD_PROOF_VERIFIER)
    if not callable(verifier):
        raise MetaRuntimeError("meta_media_upload_proof_unavailable")
    require_outbox_time_remaining()
    try:
        proof = verifier(reference, identity, session=session, now=now)
    except OutboxExecutionBudgetExceeded:
        raise
    except Exception:
        raise MetaRuntimeError("meta_media_upload_proof_unavailable") from None
    require_outbox_time_remaining()
    if (not isinstance(proof, VerifiedMediaUpload) or proof.sender != identity
            or (proof.media_id, proof.asset_sha256, proof.receipt_sha256, proof.configured_url)
                != (reference.media_id, reference.sha256, reference.verification_receipt_sha256, reference.configured_url)
            or type(proof.observed_at) is not int or type(proof.valid_until) is not int
            or not 0 <= proof.observed_at <= now < proof.valid_until <= reference.valid_until):
        raise MetaRuntimeError("meta_media_upload_proof_invalid")
    return reference


def revalidate_media_reference(reference, identity, *, session, now):
    reference = parse_media_reference(reference, now=now)
    if load_verified_media_reference(identity, reference.role, session=session, now=now) != reference:
        raise MetaRuntimeError("meta_media_reference_retired")
    return reference
