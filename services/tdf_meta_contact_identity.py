"""Owner-only, signed Meta BSUID association for the isolated TDF test number.

Official schema: https://developers.facebook.com/documentation/business-messaging/whatsapp/business-scoped-user-ids
Reviewed 2026-10-10. This does not authorize BSUID-only recipients, normalize
phones, persist contact books, handle groups or merge identities across tenants.
"""
from __future__ import annotations

import re

from services.meta_whatsapp_cloud import MetaContractError, MetaSenderSnapshot
from services.meta_whatsapp_webhook import MAX_EVENTS, MetaContactIdentity

_PHONE = re.compile(r"^[1-9][0-9]{6,14}$")
_BSUID = re.compile(r"^[A-Z]{2}\.[A-Za-z0-9]{1,128}$")
_PARENT = re.compile(r"^[A-Z]{2}\.ENT\.[A-Za-z0-9]{1,128}$")
_FIELDS = {"from_user_id", "from_parent_user_id", "recipient_user_id", "recipient_parent_user_id", "user_id", "parent_user_id"}


def owner_contact_resolver(*, sender_scope: MetaSenderSnapshot, recipients):
    """Factory is trusted server configuration; only the signed callback invokes it."""
    expected = (46, "1719510329487224", "2192778428137676", "1137550632773388", "sandbox")
    if ((sender_scope.tenant_id, sender_scope.app_id, sender_scope.waba_id,
         sender_scope.phone_number_id, sender_scope.environment) != expected
            or not isinstance(recipients, (list, tuple)) or not 1 <= len(recipients) <= 5
            or any(not isinstance(phone, str) or not _PHONE.fullmatch(phone) for phone in recipients)
            or len(set(recipients)) != len(recipients)):
        raise MetaContractError("meta_webhook_contact_schema_invalid")
    allowed = frozenset(recipients)

    def resolve(item, kind, contacts, sender):
        if sender != sender_scope or not isinstance(item, dict) or kind not in {"message", "status"}:
            raise MetaContractError("meta_webhook_contact_schema_invalid")
        phone_key, uid_key, parent_key = (("from", "from_user_id", "from_parent_user_id") if kind == "message"
                                        else ("recipient_id", "recipient_user_id", "recipient_parent_user_id"))
        if "group_id" in item or any(key in item for key in _FIELDS - {uid_key, parent_key}):
            raise MetaContractError("meta_webhook_contact_schema_invalid")
        phone = item.get(phone_key)
        rows = contacts if contacts is not None else []
        if not isinstance(rows, list) or len(rows) > MAX_EVENTS or any(not isinstance(row, dict) for row in rows):
            raise MetaContractError("meta_webhook_contact_schema_invalid")
        if any(any(key in row for key in _FIELDS - {"user_id", "parent_user_id"}) for row in rows):
            raise MetaContractError("meta_webhook_contact_schema_invalid")
        has_new_identity = uid_key in item or parent_key in item or any(
            "user_id" in row or "parent_user_id" in row for row in rows)
        if not has_new_identity:
            # Existing phone-only policy and its exact allowlist remain in the
            # pilot; no BSUID is present to discard on this legacy path.
            if rows and (len(rows) != 1 or rows[0].get("wa_id") != phone):
                raise MetaContractError("meta_webhook_contact_conflict")
            return phone
        uid, parent = item.get(uid_key), item.get(parent_key)
        if (not isinstance(uid, str) or not _BSUID.fullmatch(uid)
                or (parent_key in item and (not isinstance(parent, str) or not _PARENT.fullmatch(parent)
                                           or parent[:2] != uid[:2]))):
            raise MetaContractError("meta_webhook_contact_schema_invalid")
        if not isinstance(phone, str) or not _PHONE.fullmatch(phone):
            raise MetaContractError("meta_webhook_contact_identity_unavailable")
        if phone not in allowed:
            raise MetaContractError("meta_webhook_contact_phone_not_allowed")
        matches = [row for row in rows if row.get("user_id") == uid or row.get("wa_id") == phone]
        if len(matches) != 1:
            raise MetaContractError("meta_webhook_contact_conflict")
        row = matches[0]
        if (row.get("user_id") != uid or row.get("wa_id") != phone
                or row.get("parent_user_id") != parent
                or ("parent_user_id" in row) != (parent_key in item)):
            raise MetaContractError("meta_webhook_contact_conflict")
        return MetaContactIdentity(phone, uid, parent)

    return resolve
