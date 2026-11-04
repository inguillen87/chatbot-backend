import logging
import re
from typing import Any, Dict, Optional

from services.common_utils import formatear_telefono_e164
from models import TenantProfile, User
from models_memory import Contact
from extensions import db
from sqlalchemy import and_, or_, func

logger = logging.getLogger(__name__)


def _sanitize_profile_name(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = str(value).strip()
    if len(cleaned) < 2:
        return None
    lowered = cleaned.lower()
    banned = {"hola", "buenas", "eh", "mmm", "vecino", "vecino/a", "usuario", "anonimo"}
    if lowered in banned:
        return None
    words = re.findall(r"[a-záéíóúñ]+", lowered)
    greetings = {"hola", "buenas", "buenos", "buen"}
    if words and all(word in greetings for word in words):
        return None
    return cleaned


def sanitize_profile_name(value: Optional[str]) -> Optional[str]:
    return _sanitize_profile_name(value)


PROJECTION_SCHEMA = "tenant_contact_projection.v1"
_PROJECTION_FIELDS = ("nombre", "email", "dni", "telefono", "user_id", "is_known_contact")


def _normalized_phone(phone):
    if not phone:
        return None
    try:
        return formatear_telefono_e164(phone)
    except Exception:
        return str(phone).strip() or None


def _positive_id(value):
    return value if type(value) is int and value > 0 else None


def current_contact_phone(*server_candidates):
    """One current channel/actor phone; never take candidates from this cache.

    Opaque anonymous session IDs contain no phone. Multiple valid but different
    current phones are ambiguous and therefore cannot authorize cache reuse.
    """
    phones = set()
    for value in server_candidates:
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            return None
        value = value.strip()
        if value.lower().startswith("whatsapp:"):
            value = value.split(":", 1)[1].strip()
        if not re.fullmatch(r"[+0-9()\s.\-]+", value):
            continue
        normalized = _normalized_phone(value)
        if normalized and re.fullmatch(r"\+[1-9][0-9]{7,14}", normalized):
            phones.add(normalized)
    return next(iter(phones)) if len(phones) == 1 else None


def _parse_contact_projection(value, *, tenant_id, phone):
    """A convenience projection, never auth, verified ownership or consent.

    Unmarked legacy dictionaries are not accepted as automatic lookup data.
    Callers supply scope from the server's current channel/session binding.
    """
    if _positive_id(tenant_id) is None or not isinstance(value, dict):
        return {}
    scope = value.get("_scope")
    if (not isinstance(scope, dict) or scope.get("schema") != PROJECTION_SCHEMA
            or type(scope.get("tenant_id")) is not int
            or scope["tenant_id"] != tenant_id
            or scope.get("source") not in {"Contact", "User", "channel_profile"}):
        return {}
    if set(scope) != {"schema", "tenant_id", "phone", "source", "source_id"}:
        return {}
    if any(value.get(key) is not None and not isinstance(value.get(key), str)
           for key in ("nombre", "email", "telefono")):
        return {}
    # DNI is not a field in either lookup model; never reintroduce it from cache.
    if value.get("dni") is not None or type(value.get("is_known_contact")) is not bool:
        return {}
    source, source_id, user_id = scope["source"], scope["source_id"], value.get("user_id")
    if source == "User" and (_positive_id(source_id) is None
            or type(user_id) is not int or user_id != source_id):
        return {}
    if source == "Contact" and (not isinstance(source_id, str) or not source_id or user_id is not None):
        return {}
    if source == "channel_profile" and (source_id is not None or user_id is not None
            or value.get("email") is not None or value["is_known_contact"]):
        return {}
    normalized = _normalized_phone(value.get("telefono"))
    if not normalized or scope.get("phone") != normalized:
        return {}
    expected_phone = current_contact_phone(phone)
    if expected_phone is None or expected_phone != normalized:
        return {}
    return {**{key: value.get(key) for key in _PROJECTION_FIELDS}, "_scope": dict(scope)}


def read_contact_projection(value, *, tenant_id, phone):
    """Revalidate cached data using current server tenant/phone, without writes.

    Marker IDs never select a User or Contact. Any change of source or fields,
    inactive scope, ambiguity or revocation invalidates this old projection.
    This is record freshness/partition, not authentication, ownership or consent.
    """
    parsed = _parse_contact_projection(value, tenant_id=tenant_id, phone=phone)
    if not parsed:
        return {}
    fresh = _resolve_scoped_contact(current_contact_phone(phone), None, tenant_id=tenant_id)
    if (fresh.get("is_known_contact") is not True
            or parsed["_scope"] != fresh.get("_scope")
            or any(parsed.get(key) != fresh.get(key) for key in _PROJECTION_FIELDS)):
        return {}
    return {**fresh, "_scope": dict(fresh["_scope"])}


def store_contact_projection(context, value, *, tenant_id, phone):
    """Replace auto lookup/cache only; do not rewrite profile or explicit drafts."""
    if not isinstance(context, dict):
        return {}
    context.pop("resolved_contact", None)
    context.pop("contact_cache", None)
    projection = read_contact_projection(value, tenant_id=tenant_id, phone=phone)
    if projection:
        context["resolved_contact"] = projection
        context["contact_cache"] = {**projection, "_scope": dict(projection["_scope"])}
    return projection


def resolve_contact(phone: Optional[str], profile_name: Optional[str] = None,
                    *, tenant: Optional[TenantProfile] = None) -> Dict[str, Any]:
    """Read only in one organization; no create/merge or global phone User lookup.

    Missing, inactive or ambiguous scope returns only channel phone/profile.
    A UserRole is not permission to copy a personal profile between tenants.
    """
    if tenant is None:
        return _resolve_scoped_contact(phone, profile_name, tenant_id=None)
    # Even reading an expired ORM primary key may load it. Never flush caller WIP.
    with db.session.no_autoflush:
        return _resolve_scoped_contact(phone, profile_name,
                                       tenant_id=getattr(tenant, "id", None))


def _resolve_scoped_contact(phone, profile_name, *, tenant_id):
    """Shared bounded DB read. Never receives cache source_id as a selector."""
    phone = _normalized_phone(phone)
    result = {"nombre": sanitize_profile_name(profile_name), "email": None,
              "dni": None, "telefono": phone, "user_id": None,
              "is_known_contact": False, "_scope": None}
    target = _positive_id(tenant_id)
    if target is None or not phone:
        return result
    with db.session.no_autoflush:
        scope = (TenantProfile.query.with_entities(TenantProfile.id, TenantProfile.slug,
            TenantProfile.municipio_id, TenantProfile.pyme_id, TenantProfile.is_active)
            .filter(TenantProfile.id == target).one_or_none())
        if scope is None or scope.is_active is False:
            return result
        marker = {"schema": PROJECTION_SCHEMA, "tenant_id": target, "phone": phone,
                  "source": "channel_profile", "source_id": None}
        result["_scope"] = marker
        contacts = (Contact.query.with_entities(Contact.id, Contact.name, Contact.email)
            .filter(Contact.tenant_id == target,
                    or_(Contact.phone == phone, Contact.whatsapp_id == phone))
            .order_by(Contact.id).limit(2).all())
        if len(contacts) > 1:
            return result
        if contacts:
            item = contacts[0]
            result.update(nombre=sanitize_profile_name(item.name),
                email=item.email, is_known_contact=True,
                _scope={**marker, "source": "Contact", "source_id": item.id})
            return result
        owners = {item for item in (scope.municipio_id, scope.pyme_id)
                  if _positive_id(item) is not None}
        legacy_owners = set()
        for owner in owners:
            tenant_ids = (TenantProfile.query.with_entities(TenantProfile.id)
                .filter(or_(TenantProfile.municipio_id == owner, TenantProfile.pyme_id == owner))
                .limit(2).all())
            if len(tenant_ids) == 1 and tenant_ids[0].id == target:
                legacy_owners.add(owner)
        membership = [User.tenant_id == target]
        if legacy_owners:
            membership.append(and_(User.tenant_id.is_(None), User.empresa_id.in_(legacy_owners)))
        # Explicit tenant/slug/owner contradictions never use a matching field
        # to override a foreign one. Read columns, not dirty ORM attributes.
        users = (User.query.with_entities(User.id, User.name, User.email)
            .filter(User.telefono == phone, or_(*membership),
                or_(User.tenant_slug.is_(None), func.trim(User.tenant_slug) == "", User.tenant_slug == scope.slug),
                or_(User.empresa_id.is_(None), User.empresa_id.in_(owners)),
                (or_(User.municipio_id.is_(None), User.municipio_id == scope.municipio_id)
                 if scope.municipio_id is not None else User.municipio_id.is_(None)),
                (or_(User.pyme_id.is_(None), User.pyme_id == scope.pyme_id)
                 if scope.pyme_id is not None else User.pyme_id.is_(None)))
            .order_by(User.id).limit(2).all())
        if len(users) != 1:
            return result
        item = users[0]
        result.update(nombre=sanitize_profile_name(item.name),
            email=item.email, user_id=item.id, is_known_contact=True,
            _scope={**marker, "source": "User", "source_id": item.id})
        return result
