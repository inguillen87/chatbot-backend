from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from flask import current_app
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import and_, or_

from models import TenantProfile, User
from models_memory import Contact
from services.contact_intake import is_placeholder_email, normalize_email


CONTRACT_VERSION = "crm.people.directory.v2"
CURSOR_VERSION = 1
CURSOR_SALT = "chatboc.crm.people.directory.v2"
PII_PERMISSION = "crm_contacts_read"


class PeopleDirectoryError(ValueError):
    def __init__(self, reason_code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.status_code = status_code


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return _aware(value).isoformat().replace("+00:00", "Z")


def _normalize_phone(value: Any) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


def _flatten_permissions(value: Any) -> set[str]:
    if value in (None, ""):
        return set()
    if isinstance(value, str):
        return {item.strip().lower() for item in value.split(",") if item.strip()}
    if isinstance(value, Mapping):
        return {str(key).strip().lower() for key, enabled in value.items() if enabled and str(key).strip()}
    if isinstance(value, (list, tuple, set, frozenset)):
        result: set[str] = set()
        for item in value:
            result.update(_flatten_permissions(item))
        return result
    return set()


def actor_has_explicit_pii_permission(actor: User) -> bool:
    metadata = actor.accesibilidad if isinstance(actor.accesibilidad, Mapping) else {}
    employee_scope = metadata.get("employee_scope") if isinstance(metadata.get("employee_scope"), Mapping) else {}
    legacy_scope = getattr(actor, "scope", None)
    legacy_scope = legacy_scope if isinstance(legacy_scope, Mapping) else {}
    permissions: set[str] = set()
    for container in (metadata, employee_scope, legacy_scope):
        for key in ("permissions", "permisos", "capabilities", "scopes"):
            permissions.update(_flatten_permissions(container.get(key)))
    return PII_PERMISSION in permissions or "*" in permissions


def _serializer() -> URLSafeSerializer:
    secret = current_app.config.get("CRM_PEOPLE_CURSOR_SECRET") or current_app.secret_key
    if not secret:
        raise PeopleDirectoryError("people_cursor_configuration_unavailable", "La firma del cursor no esta configurada", status_code=503)
    return URLSafeSerializer(secret_key=secret, salt=CURSOR_SALT)


def _filters_hash(*, q: str, marketing: str, channel: str, sort: str) -> str:
    raw = json.dumps(
        {"q": q, "marketing": marketing, "channel": channel, "sort": sort},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _encode_cursor(*, tenant_id: int, actor_id: int, filters_hash: str, occurred_at: datetime, stable_key: str) -> str:
    return _serializer().dumps({
        "v": CURSOR_VERSION,
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "filters_hash": filters_hash,
        "sort": "recent_desc",
        "occurred_at": _iso(occurred_at),
        "stable_key": stable_key,
    })


def _decode_cursor(token: str, *, tenant_id: int, actor_id: int, filters_hash: str) -> tuple[datetime, str]:
    try:
        payload = _serializer().loads(token)
    except BadSignature as exc:
        raise PeopleDirectoryError("invalid_people_cursor", "Cursor invalido") from exc
    if not isinstance(payload, Mapping) or payload.get("v") != CURSOR_VERSION:
        raise PeopleDirectoryError("invalid_people_cursor", "Cursor invalido")
    if payload.get("tenant_id") != tenant_id or payload.get("actor_id") != actor_id:
        raise PeopleDirectoryError("invalid_people_cursor", "Cursor invalido")
    if payload.get("filters_hash") != filters_hash or payload.get("sort") != "recent_desc":
        raise PeopleDirectoryError("people_cursor_filter_mismatch", "El cursor no corresponde a estos filtros")
    try:
        occurred_at = datetime.fromisoformat(str(payload.get("occurred_at") or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise PeopleDirectoryError("invalid_people_cursor", "Cursor invalido") from exc
    stable_key = payload.get("stable_key")
    if not isinstance(stable_key, str) or not stable_key:
        raise PeopleDirectoryError("invalid_people_cursor", "Cursor invalido")
    return _aware(occurred_at), stable_key


@dataclass
class _Person:
    user: User | None
    contact: Contact | None
    occurred_at: datetime
    stable_key: str

    @property
    def phone(self) -> str:
        return str((self.contact.phone if self.contact else None) or (self.user.telefono if self.user else None) or "").strip()

    @property
    def email(self) -> str:
        raw = (self.contact.email if self.contact else None) or (self.user.email if self.user else None)
        return normalize_email(raw) or ""

    @property
    def name(self) -> str:
        return str((self.contact.name if self.contact else None) or (self.user.name if self.user else None) or "Contacto sin identificar").strip()

    @property
    def marketing(self) -> bool:
        prefs = self.contact.preferences if self.contact and isinstance(self.contact.preferences, Mapping) else {}
        return bool(prefs.get("marketing_opt_in") or (self.user and self.user.acepta_marketing))

    @property
    def channel(self) -> str:
        prefs = self.contact.preferences if self.contact and isinstance(self.contact.preferences, Mapping) else {}
        raw = str(prefs.get("channel") or prefs.get("canal") or prefs.get("last_channel") or "").strip().lower()
        if raw:
            return "whatsapp" if raw in {"wa", "twilio", "whatsapp_business"} else raw
        if self.phone or (self.user and is_placeholder_email(self.user.email)):
            return "whatsapp"
        return "email" if self.email else "unknown"


def _mask_name(value: str) -> str:
    parts = [part for part in value.split() if part]
    return " ".join(f"{part[0]}***" for part in parts[:3]) or "Contacto"


def _mask_email(value: str) -> str:
    if not value or "@" not in value:
        return ""
    local, domain = value.split("@", 1)
    return f"{local[:1]}***@{domain}"


def _mask_phone(value: str) -> str:
    digits = _normalize_phone(value)
    return f"***{digits[-4:]}" if digits else ""


def _identity_keys(*, phone: Any, email: Any) -> list[str]:
    keys: list[str] = []
    phone_key = _normalize_phone(phone)
    email_key = normalize_email(email)
    if phone_key:
        keys.append(f"phone:{phone_key}")
    if email_key:
        keys.append(f"email:{email_key.lower()}")
    return keys


def _people_for_tenant(tenant: TenantProfile) -> list[_Person]:
    owner_ids = [value for value in (tenant.pyme_id, tenant.municipio_id) if value]
    tenant_contact_roles = ("usuario", "cliente", "vecino", "customer", "lead")
    user_filter = and_(
        User.tenant_id == tenant.id,
        User.es_empleado.is_(False),
        User.rol.in_(tenant_contact_roles),
    )
    if owner_ids:
        user_filter = or_(user_filter, User.empresa_id.in_(owner_ids))
    users = User.query.filter(user_filter).all()
    contacts = Contact.query.filter_by(tenant_id=tenant.id).all()

    contact_by_legacy_id: dict[int, Contact] = {}
    contact_by_identity: dict[str, Contact] = {}
    for contact in sorted(contacts, key=lambda item: (_aware(item.last_interaction_at or item.created_at), str(item.id)), reverse=True):
        prefs = contact.preferences if isinstance(contact.preferences, Mapping) else {}
        legacy_id = prefs.get("legacy_user_id")
        if isinstance(legacy_id, int):
            contact_by_legacy_id.setdefault(legacy_id, contact)
        for key in _identity_keys(phone=contact.phone, email=contact.email):
            contact_by_identity.setdefault(key, contact)

    people: list[_Person] = []
    used_contacts: set[str] = set()
    seen_identity: set[str] = set()
    for user in users:
        contact = contact_by_legacy_id.get(user.id)
        identity_keys = _identity_keys(phone=user.telefono, email=user.email)
        if contact is None:
            contact = next((contact_by_identity[key] for key in identity_keys if key in contact_by_identity), None)
        combined_keys = set(identity_keys)
        if contact is not None:
            combined_keys.update(_identity_keys(phone=contact.phone, email=contact.email))
            used_contacts.add(contact.id)
        if combined_keys and combined_keys.intersection(seen_identity):
            continue
        seen_identity.update(combined_keys)
        occurred_at = _aware((contact.last_interaction_at or contact.updated_at or contact.created_at) if contact else user.fecha_creacion)
        stable_key = f"contact:{contact.id}" if contact else f"user:{user.id:020d}"
        people.append(_Person(user=user, contact=contact, occurred_at=occurred_at, stable_key=stable_key))

    for contact in sorted(contacts, key=lambda item: (_aware(item.last_interaction_at or item.created_at), str(item.id)), reverse=True):
        if contact.id in used_contacts:
            continue
        keys = set(_identity_keys(phone=contact.phone, email=contact.email))
        if keys and keys.intersection(seen_identity):
            continue
        seen_identity.update(keys)
        people.append(_Person(
            user=None,
            contact=contact,
            occurred_at=_aware(contact.last_interaction_at or contact.updated_at or contact.created_at),
            stable_key=f"contact:{contact.id}",
        ))
    return people


def build_people_directory(
    *, tenant: TenantProfile, actor: User, limit: int, cursor: str | None,
    q: str, marketing: str, channel: str, pii_requested: bool,
) -> dict[str, Any]:
    sort = "recent_desc"
    filter_digest = _filters_hash(q=q, marketing=marketing, channel=channel, sort=sort)
    position = _decode_cursor(cursor, tenant_id=tenant.id, actor_id=actor.id, filters_hash=filter_digest) if cursor else None
    people = _people_for_tenant(tenant)
    needle = q.casefold()
    filtered = [
        person for person in people
        if (not needle or needle in " ".join((person.name, person.email, person.phone)).casefold())
        and (marketing == "all" or person.marketing == (marketing == "true"))
        and (channel == "all" or person.channel == channel)
    ]
    filtered.sort(key=lambda person: (person.occurred_at, person.stable_key), reverse=True)
    total = len(filtered)
    if position:
        occurred_at, stable_key = position
        filtered = [person for person in filtered if (person.occurred_at, person.stable_key) < (occurred_at, stable_key)]
    page_people = filtered[: limit + 1]
    has_more = len(page_people) > limit
    page_people = page_people[:limit]

    pii_granted = pii_requested and actor_has_explicit_pii_permission(actor)
    items = []
    for person in page_people:
        name = person.name if pii_granted else _mask_name(person.name)
        email = person.email if pii_granted else _mask_email(person.email)
        phone = person.phone if pii_granted else _mask_phone(person.phone)
        items.append({
            "id": person.stable_key,
            "user_id": person.user.id if person.user else None,
            "contact_id": person.contact.id if person.contact else None,
            "name": name,
            "email": email,
            "phone": phone,
            "channel": person.channel,
            "marketing": person.marketing,
            "tags": list(person.contact.tags or []) if person.contact and isinstance(person.contact.tags, list) else [],
            "last_seen": _iso(person.occurred_at),
            "source": "user_contact" if person.user and person.contact else ("contact" if person.contact else "user"),
            "pii_masked": not pii_granted,
        })

    next_cursor = None
    if has_more and page_people:
        last = page_people[-1]
        next_cursor = _encode_cursor(
            tenant_id=tenant.id, actor_id=actor.id, filters_hash=filter_digest,
            occurred_at=last.occurred_at, stable_key=last.stable_key,
        )
    return {
        "contract_version": CONTRACT_VERSION,
        "items": items,
        "page": {"limit": limit, "total": total, "has_more": has_more, "next_cursor": next_cursor},
        "filters": {"q": q, "marketing": marketing, "channel": channel, "sort": sort},
        "pii": {
            "requested": pii_requested,
            "masked": not pii_granted,
            "permission": PII_PERMISSION,
            "granted": pii_granted,
            "reason_code": None if pii_granted else ("pii_permission_required" if pii_requested else "pii_masked_by_default"),
        },
    }
