"""Apply the approved Chatboc Neon cutover revisions, fail closed.

Dry-run is the default.  The database URL is read only from the environment
variable explicitly named by ``--environment-variable``; this command never
falls back to ``DATABASE_URL`` or application configuration.  Output is one
redacted JSON document and never includes a DSN or raw Neon identity.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from alembic.config import Config as AlembicConfig
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, URL
from sqlalchemy.pool import NullPool

try:  # Supports both ``python -m scripts...`` and direct script execution.
    from scripts.preflight_neon_cutover import (
        PreflightFailure,
        REVIEWED_MIGRATION_HEAD,
        _single_migration_head,
        _validate_environment_variable_name,
        _validate_neon_direct_url,
    )
except ModuleNotFoundError:  # pragma: no cover - direct-script import mode
    from preflight_neon_cutover import (  # type: ignore[no-redef]
        PreflightFailure,
        REVIEWED_MIGRATION_HEAD,
        _single_migration_head,
        _validate_environment_variable_name,
        _validate_neon_direct_url,
    )


CONTRACT_VERSION = "chatboc.neon_cutover_migrations.v1"
INITIAL_REVISION = "20260825_demo_survey_participation_v1"
REPAIR_REVISION = "20260825_legacy_municipio_ticket_scope_repair_v1"
IDEMPOTENCY_REVISION = "20260825_chat_idempotency_v1"
INBOUND_FIFO_REVISION = "20260829_inbound_fifo_v2"
GLOBAL_WRITER_AUTHORITY_REVISION = "20260829_global_writer_authority_v1"
TERRITORIAL_GEOCODING_REVISION = "20260830_territorial_geocoding_v1"
TERRITORIAL_GEOCODING_REVIEW_REVISION = "20260830_geo_review_v1"
TERRITORIAL_GEOCODING_SYNC_REVISION = "20260830_geo_sync_v1"
INBOX_ARTIFACT_REVISION = "20260831_inbox_artifact_v1"
TENANT_REPLY_DELIVERY_REVISION = "20260904_tenant_reply_delivery_v1"
TERRITORIAL_EXECUTION_REVISION = "20260904_geo_execution_v2"
TENANT_BLUEPRINT_REVISION = "20260905_tenant_blueprint_v1"
GOVERNMENT_LAUNCH_REVISION = "20260905_government_launch_v1"
MUNICIPIO_REPLY_REVISION = "20260905_municipio_reply_v1"
MUNICIPIO_HANDOFF_REVISION = "20260905_municipio_handoff_v1"
FLASK_SESSIONS_REVISION = "20260906_flask_sessions_v1"
MIGRATION_STEPS = (
    REPAIR_REVISION,
    IDEMPOTENCY_REVISION,
    INBOUND_FIFO_REVISION,
    GLOBAL_WRITER_AUTHORITY_REVISION,
    TERRITORIAL_GEOCODING_REVISION,
    TERRITORIAL_GEOCODING_REVIEW_REVISION,
    TERRITORIAL_GEOCODING_SYNC_REVISION,
    INBOX_ARTIFACT_REVISION,
    TENANT_REPLY_DELIVERY_REVISION,
    TERRITORIAL_EXECUTION_REVISION,
    TENANT_BLUEPRINT_REVISION,
    GOVERNMENT_LAUNCH_REVISION,
    MUNICIPIO_REPLY_REVISION,
    MUNICIPIO_HANDOFF_REVISION,
    FLASK_SESSIONS_REVISION,
)
FINAL_MIGRATION_REVISION = REVIEWED_MIGRATION_HEAD
EXPECTED_MIGRATION_SOURCE_SHA256 = {
    REPAIR_REVISION: "956193d0258e4937b662d4b83d6d7f308ee4ea5f426eab41d5418b2bd0d11a7a",
    IDEMPOTENCY_REVISION: (
        "2d1e283b4db884a1b286b4587784cb7bcfbf40d2f2a368695035f0f427437c4d"
    ),
    INBOUND_FIFO_REVISION: (
        "628863bf0bade2b7a61ec49d03ad0ebd175c92073fdceca6afcc60f26020d087"
    ),
    GLOBAL_WRITER_AUTHORITY_REVISION: (
        "e5e1801f1d26cc7e596c8dd33418df2122cce4cd52cfb6e83ef2aabc4369950f"
    ),
    TERRITORIAL_GEOCODING_REVISION: (
        "06a9cbc03fe602e13a7a51644cb755c4d9d21faab20ecc3ac475a227996fa866"
    ),
    TERRITORIAL_GEOCODING_REVIEW_REVISION: (
        "2eb446115a7c0045a65927862348d48fe6a6f31b83805574e808c7e0f8a499e3"
    ),
    TERRITORIAL_GEOCODING_SYNC_REVISION: (
        "36cf3a43b025986bc2e30d16e7d5f6055c42c5699e53fd5b41087a84c593389c"
    ),
    INBOX_ARTIFACT_REVISION: (
        "2eac84cb0250ab97d70f27d466632f470b704359c4a782407593bea455911181"
    ),
    TENANT_REPLY_DELIVERY_REVISION: (
        "764a54e8c91b2a14af5e92a825264947ce799709e1de4df6fbbf6df0e996fbca"
    ),
    TERRITORIAL_EXECUTION_REVISION: (
        "6dd63fc71c42e9fd217a5329ee5ab50db5d1d78defb52f54e62e7d46a38a0b38"
    ),
    TENANT_BLUEPRINT_REVISION: (
        "3c81b358d62947804ce727bd05d169ead98486414ae95b0b235cb3e023c8cdbf"
    ),
    GOVERNMENT_LAUNCH_REVISION: (
        "9f358829e217384e72582a391d78fd3941536250b931b845c302f9fc60a3399b"
    ),
    MUNICIPIO_REPLY_REVISION: (
        "fd0d93da0845794e9c60c2f7728059c4277dfba7f9f3bd28e6d839e6ee22ebff"
    ),
    MUNICIPIO_HANDOFF_REVISION: (
        "beec0006b9bc998b71fec9576b909d2f40f5a96b587ad510220edf60ca09dc74"
    ),
    FLASK_SESSIONS_REVISION: (
        "b8e4e5bcc2b7c75686e344a7d66fad9161e8d41739e106ddd3868199ca1f3ced"
    ),
}

STATEMENT_TIMEOUT_MS = 120_000
LOCK_TIMEOUT_MS = 5_000
IDLE_TRANSACTION_TIMEOUT_MS = 60_000
ADVISORY_LOCK_KEY = int.from_bytes(
    hashlib.sha256(CONTRACT_VERSION.encode("ascii")).digest()[:8],
    byteorder="big",
    signed=True,
)

SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
EVIDENCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
LEGACY_TICKET_IDS = (322, 344, 347)
EXPECTED_DEMO_INDEXES = {
    "ix_demo_survey_participation_slug_option",
    "ix_demo_survey_participation_slug_order",
}
EXPECTED_DEMO_TRIGGER = "trg_demo_survey_participation_immutable"
EXPECTED_IDEMPOTENCY_INDEXES = {
    "ix_municipio_chat_idempotency_tenant_created",
}
EXPECTED_IDEMPOTENCY_CONSTRAINTS = {
    "ck_municipio_chat_idempotency_completion",
    "ck_municipio_chat_idempotency_hashes",
    "ck_municipio_chat_idempotency_status",
    "uq_municipio_chat_idempotency_scope",
}
EXPECTED_IDEMPOTENCY_COLUMNS = {
    "id",
    "tenant_id",
    "endpoint",
    "actor_scope_hash",
    "idempotency_key_hash",
    "request_hash",
    "status",
    "response_status",
    "response_json",
    "response_request_id",
    "contract_version",
    "created_at",
    "updated_at",
    "completed_at",
    "expired_at",
}
EXPECTED_INBOUND_FIFO_INDEX = "ix_whatsapp_inbound_turn_stream_fifo"
EXPECTED_INBOUND_FIFO_COLUMNS = (
    "tenant_id",
    "stream_key",
    "received_at",
    "id",
)
EXPECTED_GLOBAL_WRITER_AUTHORITY_COLUMNS = {
    "authority_key",
    "owner_runtime",
    "epoch",
    "render_fenced",
    "vercel_fenced",
    "updated_at",
}
EXPECTED_GLOBAL_WRITER_AUTHORITY_CONSTRAINTS = {
    "ck_cutover_global_writer_authority_epoch",
    "ck_cutover_global_writer_authority_owner",
    "ck_cutover_global_writer_authority_safe_state",
    "ck_cutover_global_writer_authority_singleton",
    "pk_cutover_global_writer_authority",
}
EXPECTED_TERRITORIAL_SCHEMA = {
    "territorial_geocoding_job": {
        "columns": {
            "id",
            "tenant_id",
            "contract_version",
            "source_model",
            "source_id",
            "candidate_fingerprint",
            "address_digest",
            "jurisdiction_digest",
            "status",
            "reason_code",
            "provider",
            "provider_place_id",
            "proposed_lat",
            "proposed_lng",
            "location_type",
            "partial_match",
            "validation_json",
            "result_json",
            "attempt_count",
            "last_attempt_at",
            "applied_at",
            "created_at",
            "updated_at",
        },
        "constraints": {
            "ck_territorial_geocoding_job_status",
            "ck_territorial_geocoding_job_attempt_count",
            "ck_territorial_geocoding_job_fingerprint",
            "ck_territorial_geocoding_job_address_digest",
            "ck_territorial_geocoding_job_jurisdiction_digest",
            "uq_territorial_geocoding_job_candidate",
        },
        "foreign_keys": {
            ("tenant_id", "tenant_profile", "id", "CASCADE"),
        },
        "indexes": {
            "territorial_geocoding_job_pkey": (("id",), True),
            "ix_territorial_geocoding_job_tenant_id": (("tenant_id",), False),
            "ix_territorial_geocoding_job_tenant_status_created": (
                ("tenant_id", "status", "created_at", "id"),
                False,
            ),
            "uq_territorial_geocoding_job_candidate": (
                ("tenant_id", "candidate_fingerprint"),
                True,
            ),
        },
    },
    "territorial_geocoding_attempt": {
        "columns": {
            "id",
            "job_id",
            "tenant_id",
            "attempt_number",
            "request_digest",
            "provider",
            "outcome_status",
            "reason_code",
            "external_call_performed",
            "write_performed",
            "result_digest",
            "result_json",
            "created_at",
        },
        "constraints": {
            "ck_territorial_geocoding_attempt_status",
            "ck_territorial_geocoding_attempt_number",
            "ck_territorial_geocoding_attempt_request_digest",
            "ck_territorial_geocoding_attempt_result_digest",
            "ck_territorial_geocoding_attempt_write_state",
            "uq_territorial_geocoding_attempt_request",
            "uq_territorial_geocoding_attempt_number",
        },
        "foreign_keys": {
            ("job_id", "territorial_geocoding_job", "id", "CASCADE"),
            ("tenant_id", "tenant_profile", "id", "CASCADE"),
        },
        "indexes": {
            "territorial_geocoding_attempt_pkey": (("id",), True),
            "ix_territorial_geocoding_attempt_job_id": (("job_id",), False),
            "ix_territorial_geocoding_attempt_tenant_id": (("tenant_id",), False),
            "ix_territorial_geocoding_attempt_tenant_created": (
                ("tenant_id", "created_at", "id"),
                False,
            ),
            "uq_territorial_geocoding_attempt_request": (
                ("job_id", "request_digest"),
                True,
            ),
            "uq_territorial_geocoding_attempt_number": (
                ("job_id", "attempt_number"),
                True,
            ),
        },
    },
    "territorial_geocoding_review": {
        "columns": {
            "id",
            "job_id",
            "tenant_id",
            "reviewer_user_id",
            "contract_version",
            "decision",
            "reason_code",
            "reviewed_job_status",
            "proposal_digest",
            "idempotency_key_hash",
            "request_digest",
            "coordinate_write_performed",
            "created_at",
        },
        "constraints": {
            "ck_territorial_geocoding_review_decision",
            "ck_territorial_geocoding_review_job_status",
            "ck_territorial_geocoding_review_digests",
            "ck_territorial_geocoding_review_no_coordinate_write",
            "uq_territorial_geocoding_review_idempotency",
        },
        "foreign_keys": {
            ("job_id", "territorial_geocoding_job", "id", "CASCADE"),
            ("tenant_id", "tenant_profile", "id", "CASCADE"),
            ("reviewer_user_id", "user", "id", "RESTRICT"),
        },
        "indexes": {
            "territorial_geocoding_review_pkey": (("id",), True),
            "ix_territorial_geocoding_review_job_id": (("job_id",), False),
            "ix_territorial_geocoding_review_tenant_id": (("tenant_id",), False),
            "ix_territorial_geocoding_review_reviewer_user_id": (
                ("reviewer_user_id",),
                False,
            ),
            "ix_territorial_geocoding_review_tenant_job_created": (
                ("tenant_id", "job_id", "created_at", "id"),
                False,
            ),
            "uq_territorial_geocoding_review_idempotency": (
                ("tenant_id", "job_id", "idempotency_key_hash"),
                True,
            ),
        },
    },
    "territorial_geocoding_sync_receipt": {
        "columns": {
            "id",
            "tenant_id",
            "actor_user_id",
            "contract_version",
            "idempotency_key_hash",
            "request_digest",
            "completed",
            "response_json",
            "created_at",
            "updated_at",
        },
        "constraints": {
            "ck_territorial_geocoding_sync_digests",
            "uq_territorial_geocoding_sync_idempotency",
        },
        "foreign_keys": {
            ("actor_user_id", "user", "id", "RESTRICT"),
            ("tenant_id", "tenant_profile", "id", "CASCADE"),
        },
        "indexes": {
            "territorial_geocoding_sync_receipt_pkey": (("id",), True),
            "ix_territorial_geocoding_sync_receipt_tenant_id": (
                ("tenant_id",),
                False,
            ),
            "ix_territorial_geocoding_sync_receipt_actor_user_id": (
                ("actor_user_id",),
                False,
            ),
            "ix_territorial_geocoding_sync_tenant_created": (
                ("tenant_id", "created_at", "id"),
                False,
            ),
            "uq_territorial_geocoding_sync_idempotency": (
                ("tenant_id", "idempotency_key_hash"),
                True,
            ),
        },
    },
}

# Every post-territorial migration remains explicitly reviewed and fingerprinted.
# The graph head itself is resolved from Alembic; this schema allowlist prevents a
# newly-added migration from becoming cutover-approved merely because it is a head.
POST_SYNC_SCHEMA_REQUIREMENTS: Mapping[str, tuple[Mapping[str, Any], ...]] = {
    INBOX_ARTIFACT_REVISION: (
        {
            "table": "inbox_ticket_artifact",
            "columns": {
                "id", "tenant_id", "source_model", "ticket_id", "action",
                "payload_json", "actor_user_id", "idempotency_key_hash",
                "request_digest", "contract_version", "created_at",
            },
            "exact_columns": True,
            "constraints": {
                "ck_inbox_ticket_artifact_source_model",
                "ck_inbox_ticket_artifact_action",
                "uq_inbox_ticket_artifact_idempotency",
            },
            "foreign_keys": {
                ("tenant_id", "tenant_profile", "id", "CASCADE"),
                ("actor_user_id", "user", "id", "RESTRICT"),
            },
            "indexes": {
                "ix_inbox_ticket_artifact_ticket": (
                    ("tenant_id", "source_model", "ticket_id", "created_at", "id"),
                    False,
                ),
            },
        },
    ),
    TENANT_REPLY_DELIVERY_REVISION: (
        {
            "table": "tenant_ticket_reply_event",
            "columns": {
                "whatsapp_template_registry_id", "whatsapp_template_variables",
                "whatsapp_policy_snapshot", "whatsapp_delivery_status",
                "whatsapp_provider_message_id", "whatsapp_provider_sender_id",
                "whatsapp_provider_status", "whatsapp_error_code",
                "whatsapp_status_event_id", "whatsapp_status_updated_at",
                "whatsapp_provider_accepted_at", "whatsapp_delivered_at",
                "whatsapp_read_at", "whatsapp_failed_at",
            },
            "constraints": {"ck_tenant_ticket_reply_event_wa_delivery_status"},
            "foreign_keys": {
                ("whatsapp_template_registry_id", "message_template_registry", "id", "SET NULL"),
                ("whatsapp_provider_sender_id", "provider_sender", "id", "SET NULL"),
                ("whatsapp_status_event_id", "messaging_event_ledger", "id", "SET NULL"),
            },
            "indexes": {
                "ix_tenant_ticket_reply_event_wa_provider_message": (
                    ("tenant_id", "whatsapp_provider_message_id"), True,
                ),
            },
        },
        {
            "table": "whatsapp_contact_state",
            "columns": {"provider_sender_id"},
            "constraints": {"uq_whatsapp_contact_state_tenant_sender_recipient"},
            "foreign_keys": {
                ("provider_sender_id", "provider_sender", "id", "CASCADE"),
            },
            "indexes": {
                "ix_whatsapp_contact_state_provider_sender_id": (
                    ("provider_sender_id",), False,
                ),
            },
        },
    ),
    TERRITORIAL_EXECUTION_REVISION: (
        {
            "table": "territorial_geocoding_attempt",
            "columns": {"action", "idempotency_key_hash"},
            "constraints": {
                "ck_territorial_geocoding_attempt_idempotency",
                "uq_territorial_geocoding_attempt_idempotency",
            },
        },
        {
            "table": "territorial_geocoding_review",
            "columns": {"proposal_attempt_id", "proposal_attempt_number"},
            "constraints": {"ck_territorial_geocoding_review_proposal_attempt"},
            "foreign_keys": {
                ("proposal_attempt_id", "territorial_geocoding_attempt", "id", "RESTRICT"),
            },
            "indexes": {
                "ix_territorial_geocoding_review_proposal_attempt_id": (
                    ("proposal_attempt_id",), False,
                ),
            },
        },
    ),
    TENANT_BLUEPRINT_REVISION: (
        {
            "table": "tenant_blueprint_application",
            "columns": {
                "id", "tenant_id", "contract_version", "blueprint_id",
                "blueprint_version", "manifest_digest", "request_digest",
                "idempotency_key_hash", "status", "application_snapshot",
                "applied_by_user_id", "created_at",
            },
            "exact_columns": True,
            "constraints": {
                "ck_tenant_blueprint_application_status",
                "ck_tenant_blueprint_application_digests",
                "uq_tenant_blueprint_application_version",
                "uq_tenant_blueprint_application_idempotency",
            },
            "foreign_keys": {
                ("tenant_id", "tenant_profile", "id", "CASCADE"),
                ("applied_by_user_id", "user", "id", "RESTRICT"),
            },
            "indexes": {
                "ix_tenant_blueprint_application_tenant_id": (("tenant_id",), False),
                "ix_tenant_blueprint_application_applied_by_user_id": (("applied_by_user_id",), False),
                "ix_tenant_blueprint_application_tenant_created": (("tenant_id", "created_at", "id"), False),
            },
        },
    ),
    GOVERNMENT_LAUNCH_REVISION: (
        {
            "table": "tenant_blueprint_launch_receipt",
            "columns": {
                "id", "tenant_id", "blueprint_application_id", "contract_version",
                "blueprint_id", "blueprint_version", "launch_id", "manifest_digest",
                "launch_digest", "request_digest", "idempotency_key_hash", "status",
                "application_snapshot", "applied_by_user_id", "created_at",
            },
            "exact_columns": True,
            "constraints": {
                "ck_tenant_blueprint_launch_status",
                "ck_tenant_blueprint_launch_digests",
                "uq_tenant_blueprint_launch_module_version",
                "uq_tenant_blueprint_launch_idempotency",
            },
            "foreign_keys": {
                ("tenant_id", "tenant_profile", "id", "CASCADE"),
                ("blueprint_application_id", "tenant_blueprint_application", "id", "RESTRICT"),
                ("applied_by_user_id", "user", "id", "RESTRICT"),
            },
            "indexes": {
                "ix_tenant_blueprint_launch_tenant_id": (("tenant_id",), False),
                "ix_tenant_blueprint_launch_blueprint_application_id": (("blueprint_application_id",), False),
                "ix_tenant_blueprint_launch_applied_by_user_id": (("applied_by_user_id",), False),
                "ix_tenant_blueprint_launch_tenant_created": (("tenant_id", "created_at", "id"), False),
            },
            "triggers": {"trg_tenant_blueprint_launch_receipt_immutable"},
        },
    ),
    MUNICIPIO_REPLY_REVISION: (
        {
            "table": "municipio_ticket_reply_event",
            "columns": {
                "id", "tenant_id", "source_model", "ticket_id", "comment_id",
                "event_id", "body", "visibility", "actor_user_id", "actor_name",
                "actor_role", "recipient_phone", "whatsapp_template_registry_id",
                "whatsapp_template_variables", "whatsapp_policy_snapshot",
                "whatsapp_delivery_status", "whatsapp_provider_message_id",
                "whatsapp_provider_sender_id", "whatsapp_provider_status",
                "whatsapp_error_code", "whatsapp_status_event_id",
                "whatsapp_status_updated_at", "whatsapp_provider_accepted_at",
                "whatsapp_delivered_at", "whatsapp_read_at", "whatsapp_failed_at",
                "contract_version", "created_at",
            },
            "exact_columns": True,
            "constraints": {
                "ck_municipio_reply_source_model", "ck_municipio_reply_public_visibility",
                "ck_municipio_reply_body_nonempty", "ck_municipio_reply_event_id_nonempty",
                "ck_municipio_reply_recipient_nonempty", "ck_municipio_reply_wa_delivery_status",
                "uq_municipio_reply_tenant_event", "uq_municipio_reply_tenant_comment",
            },
            "foreign_keys": {
                ("tenant_id", "tenant_profile", "id", "CASCADE"),
                ("ticket_id", "municipio_ticket", "id", "CASCADE"),
                ("comment_id", "ticket_comentario", "id", "CASCADE"),
                ("actor_user_id", "user", "id", "RESTRICT"),
                ("whatsapp_template_registry_id", "message_template_registry", "id", "SET NULL"),
                ("whatsapp_provider_sender_id", "provider_sender", "id", "RESTRICT"),
                ("whatsapp_status_event_id", "messaging_event_ledger", "id", "SET NULL"),
            },
            "indexes": {
                "ix_municipio_reply_ticket": (("tenant_id", "ticket_id", "created_at"), False),
                "ix_municipio_reply_wa_provider_message": (("tenant_id", "whatsapp_provider_message_id"), True),
            },
        },
    ),
    MUNICIPIO_HANDOFF_REVISION: (
        {
            "table": "municipio_ticket_handoff_event",
            "columns": {
                "id", "tenant_id", "source_model", "ticket_id", "comment_id",
                "event_id", "action", "status", "channel", "reason", "actor_user_id",
                "previous_assignee_user_id", "idempotency_key_hash", "request_digest",
                "projection_contract_version", "contract_version", "created_at",
            },
            "exact_columns": True,
            "constraints": {
                "ck_municipio_handoff_source_model", "ck_municipio_handoff_action_status",
                "ck_municipio_handoff_channel", "ck_municipio_handoff_reason_nonempty",
                "ck_municipio_handoff_reason_bounded", "ck_municipio_handoff_event_id_nonempty",
                "ck_municipio_handoff_digests", "ck_municipio_handoff_contract_versions",
                "uq_municipio_handoff_tenant_event", "uq_municipio_handoff_tenant_idempotency",
                "uq_municipio_handoff_tenant_comment",
            },
            "foreign_keys": {
                ("tenant_id", "tenant_profile", "id", "CASCADE"),
                ("ticket_id", "municipio_ticket", "id", "CASCADE"),
                ("comment_id", "ticket_comentario", "id", "RESTRICT"),
                ("actor_user_id", "user", "id", "RESTRICT"),
                ("previous_assignee_user_id", "user", "id", "RESTRICT"),
            },
            "indexes": {
                "ix_municipio_handoff_ticket": (("tenant_id", "ticket_id", "created_at", "id"), False),
            },
            "triggers": {"trg_municipio_ticket_handoff_event_immutable"},
        },
    ),
    FLASK_SESSIONS_REVISION: (
        {
            "table": "flask_sessions",
            "columns": {"id", "session_id", "data", "expiry"},
            "exact_columns": True,
            "constraints": {
                "flask_sessions_pkey",
                "flask_sessions_session_id_key",
            },
            "foreign_keys": set(),
            "exact_foreign_keys": True,
            "indexes": {
                "flask_sessions_pkey": (("id",), True),
                "flask_sessions_session_id_key": (("session_id",), True),
            },
        },
    ),
}


class CutoverMigrationFailure(RuntimeError):
    """Stable reason code safe to serialize without provider details."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True)
class ExactMigrationPlan:
    script: ScriptDirectory
    source_fingerprints_sha256: Mapping[str, str]
    graph_fingerprint_sha256: str


@dataclass(frozen=True)
class ApprovalEvidence:
    writer_fence: str
    snapshot: str
    parity: str

    def fingerprints(self) -> dict[str, str]:
        return {
            "writer_fence_sha256": _sha256_text(self.writer_fence),
            "snapshot_sha256": _sha256_text(self.snapshot),
            "parity_sha256": _sha256_text(self.parity),
        }


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _validate_sha256(value: str | None, *, reason_code: str) -> str:
    normalized = str(value or "").strip().lower()
    if not SHA256_PATTERN.fullmatch(normalized):
        raise CutoverMigrationFailure(reason_code)
    return normalized


def _translate_preflight_failure(callback, *args, **kwargs):
    try:
        return callback(*args, **kwargs)
    except PreflightFailure as exc:
        raise CutoverMigrationFailure(exc.reason_code) from exc


def _load_database_url(
    environ: Mapping[str, str],
    environment_variable: str | None,
) -> str:
    if not str(environment_variable or "").strip():
        raise CutoverMigrationFailure("database_environment_variable_name_required")
    name = _translate_preflight_failure(
        _validate_environment_variable_name,
        str(environment_variable),
    )
    database_url = str(environ.get(name) or "").strip()
    if not database_url:
        raise CutoverMigrationFailure("database_environment_variable_missing")
    return database_url


def _validate_target_url(
    database_url: str,
    *,
    expected_host_fingerprint_sha256: str | None,
) -> tuple[URL, str]:
    parsed = _translate_preflight_failure(_validate_neon_direct_url, database_url)
    # Keep libpq from silently sourcing credentials from PGPASSWORD/.pgpass;
    # the purpose-specific environment variable must contain the full DSN.
    if not parsed.password:
        raise CutoverMigrationFailure("database_password_missing")
    indirect_parameters = {
        "host",
        "hostaddr",
        "passfile",
        "password",
        "service",
        "servicefile",
        "user",
    }.intersection(str(key).lower() for key in parsed.query)
    if indirect_parameters:
        raise CutoverMigrationFailure("database_indirect_connection_parameter_forbidden")
    expected = _validate_sha256(
        expected_host_fingerprint_sha256,
        reason_code="expected_neon_host_fingerprint_invalid",
    )
    host = str(parsed.host or "").strip().lower().rstrip(".")
    actual = _sha256_text(host)
    if not hmac.compare_digest(actual, expected):
        raise CutoverMigrationFailure("database_neon_host_fingerprint_mismatch")
    return parsed, actual


def _validate_evidence_id(value: str | None, *, reason_code: str) -> str:
    normalized = str(value or "").strip()
    if not EVIDENCE_ID_PATTERN.fullmatch(normalized):
        raise CutoverMigrationFailure(reason_code)
    return normalized


def _approval_evidence(
    *,
    apply: bool,
    writer_fence_evidence_id: str | None,
    snapshot_evidence_id: str | None,
    parity_evidence_id: str | None,
) -> ApprovalEvidence | None:
    if not apply:
        return None
    evidence = ApprovalEvidence(
        writer_fence=_validate_evidence_id(
            writer_fence_evidence_id,
            reason_code="approved_writer_fence_evidence_id_required",
        ),
        snapshot=_validate_evidence_id(
            snapshot_evidence_id,
            reason_code="approved_snapshot_evidence_id_required",
        ),
        parity=_validate_evidence_id(
            parity_evidence_id,
            reason_code="approved_parity_evidence_id_required",
        ),
    )
    if len({evidence.writer_fence, evidence.snapshot, evidence.parity}) != 3:
        raise CutoverMigrationFailure("approved_evidence_ids_must_be_distinct")
    return evidence


def _load_exact_migration_plan(project_root: Path) -> ExactMigrationPlan:
    config = AlembicConfig(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    try:
        script = ScriptDirectory.from_config(config)
        initial = script.get_revision(INITIAL_REVISION)
        revisions = [script.get_revision(value) for value in MIGRATION_STEPS]
    except Exception as exc:
        raise CutoverMigrationFailure("local_migration_graph_unreadable") from exc

    try:
        local_head = _single_migration_head(script)
    except PreflightFailure as exc:
        raise CutoverMigrationFailure(exc.reason_code) from exc

    if local_head != FINAL_MIGRATION_REVISION:
        raise CutoverMigrationFailure("local_migration_head_not_allowlisted")
    if initial is None or any(revision is None for revision in revisions):
        raise CutoverMigrationFailure("local_cutover_revision_missing")
    if set(EXPECTED_MIGRATION_SOURCE_SHA256) != set(MIGRATION_STEPS):
        raise CutoverMigrationFailure("local_cutover_source_allowlist_mismatch")
    extension_revisions = MIGRATION_STEPS[
        MIGRATION_STEPS.index(INBOX_ARTIFACT_REVISION) :
    ]
    if set(POST_SYNC_SCHEMA_REQUIREMENTS) != set(extension_revisions):
        raise CutoverMigrationFailure("local_cutover_schema_allowlist_mismatch")

    typed_revisions = [revision for revision in revisions if revision is not None]
    previous = initial
    for revision in typed_revisions:
        if revision.down_revision != previous.revision:
            raise CutoverMigrationFailure("local_cutover_down_revision_mismatch")
        if set(previous.nextrev) != {revision.revision}:
            raise CutoverMigrationFailure("local_cutover_graph_branches")
        previous = revision
    if set(previous.nextrev):
        raise CutoverMigrationFailure("local_cutover_graph_continues_after_target")

    try:
        path = [
            revision.revision
            for revision in reversed(
                list(
                    script.iterate_revisions(
                        local_head,
                        INITIAL_REVISION,
                    )
                )
            )
        ]
    except Exception as exc:
        raise CutoverMigrationFailure("local_cutover_graph_diverged") from exc
    if path != list(MIGRATION_STEPS):
        raise CutoverMigrationFailure("local_cutover_path_not_exact")

    source_fingerprints: dict[str, str] = {}
    graph_document: list[dict[str, str]] = []
    for revision in typed_revisions:
        upgrade = getattr(revision.module, "upgrade", None)
        if not callable(upgrade):
            raise CutoverMigrationFailure("local_cutover_upgrade_missing")
        try:
            source_bytes = Path(revision.path).read_bytes().replace(b"\r\n", b"\n")
            source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        except Exception as exc:
            raise CutoverMigrationFailure("local_cutover_source_unreadable") from exc
        expected_source_sha256 = EXPECTED_MIGRATION_SOURCE_SHA256.get(
            revision.revision
        )
        if expected_source_sha256 is None:
            raise CutoverMigrationFailure("local_cutover_source_allowlist_mismatch")
        if not hmac.compare_digest(source_sha256, expected_source_sha256):
            raise CutoverMigrationFailure("local_cutover_source_fingerprint_mismatch")
        source_fingerprints[revision.revision] = source_sha256
        graph_document.append(
            {
                "revision": revision.revision,
                "down_revision": str(revision.down_revision),
                "source_sha256": source_sha256,
            }
        )
    return ExactMigrationPlan(
        script=script,
        source_fingerprints_sha256=source_fingerprints,
        graph_fingerprint_sha256=_canonical_sha256(graph_document),
    )


def _configure_transaction(connection: Connection, *, apply: bool) -> None:
    access_mode = "READ WRITE" if apply else "READ ONLY"
    connection.exec_driver_sql(
        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, " + access_mode
    )
    connection.exec_driver_sql(
        f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT_MS}ms'"
    )
    connection.exec_driver_sql(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT_MS}ms'")
    connection.exec_driver_sql(
        "SET LOCAL idle_in_transaction_session_timeout = "
        f"'{IDLE_TRANSACTION_TIMEOUT_MS}ms'"
    )
    connection.exec_driver_sql("SET LOCAL search_path = public, pg_catalog")


def _assert_database_identity(
    connection: Connection,
    *,
    expected_project_fingerprint_sha256: str,
    expected_branch_fingerprint_sha256: str,
) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT
                current_setting('neon.project_id', true) AS project_id,
                current_setting('neon.branch_id', true) AS branch_id,
                pg_is_in_recovery() AS in_recovery
            """
        )
    ).mappings().one()
    project_id = str(row["project_id"] or "").strip().lower()
    branch_id = str(row["branch_id"] or "").strip().lower()
    if not project_id or not branch_id:
        raise CutoverMigrationFailure("database_neon_identity_missing")
    project_fingerprint = _sha256_text(project_id)
    branch_fingerprint = _sha256_text(branch_id)
    if not hmac.compare_digest(project_fingerprint, expected_project_fingerprint_sha256):
        raise CutoverMigrationFailure("database_neon_project_fingerprint_mismatch")
    if not hmac.compare_digest(branch_fingerprint, expected_branch_fingerprint_sha256):
        raise CutoverMigrationFailure("database_neon_branch_fingerprint_mismatch")
    if bool(row["in_recovery"]):
        raise CutoverMigrationFailure("database_neon_target_is_read_replica")

    default_read_only = str(
        connection.execute(text("SHOW default_transaction_read_only")).scalar_one()
    ).strip().lower()
    if default_read_only != "off":
        raise CutoverMigrationFailure("database_neon_target_not_writable")
    return {
        "project_fingerprint_sha256": project_fingerprint,
        "branch_fingerprint_sha256": branch_fingerprint,
        "writable_primary": True,
    }


def _acquire_advisory_lock(connection: Connection) -> None:
    acquired = bool(
        connection.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
            {"lock_key": ADVISORY_LOCK_KEY},
        ).scalar_one()
    )
    if not acquired:
        raise CutoverMigrationFailure("cutover_advisory_lock_unavailable")


def _table_exists(connection: Connection, table_name: str) -> bool:
    return bool(
        connection.execute(
            text("SELECT to_regclass(:qualified_name) IS NOT NULL"),
            {"qualified_name": f"public.{table_name}"},
        ).scalar_one()
    )


def _index_names(connection: Connection, table_name: str) -> set[str]:
    return {
        str(value)
        for value in connection.execute(
            text(
                """
                SELECT indexname
                FROM pg_indexes
                WHERE schemaname = 'public' AND tablename = :table_name
                """
            ),
            {"table_name": table_name},
        ).scalars()
    }


def _index_contract(
    connection: Connection,
    *,
    table_name: str,
    index_name: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        text(
            """
            SELECT
                ARRAY(
                    SELECT pg_get_indexdef(index_row.indexrelid, ordinal, true)
                    FROM generate_series(1, index_row.indnatts) AS ordinal
                    ORDER BY ordinal
                ) AS columns,
                index_row.indisunique AS is_unique,
                index_row.indisvalid AS is_valid,
                index_row.indisready AS is_ready,
                index_row.indpred IS NULL AS is_unfiltered,
                index_row.indexprs IS NULL AS has_plain_columns,
                index_row.indnatts = index_row.indnkeyatts AS has_no_included_columns
            FROM pg_index index_row
            JOIN pg_class index_relation
              ON index_relation.oid = index_row.indexrelid
            JOIN pg_class table_relation
              ON table_relation.oid = index_row.indrelid
            JOIN pg_namespace namespace
              ON namespace.oid = table_relation.relnamespace
            WHERE namespace.nspname = 'public'
              AND table_relation.relname = :table_name
              AND index_relation.relname = :index_name
            """
        ),
        {"table_name": table_name, "index_name": index_name},
    ).mappings().one_or_none()
    if row is None:
        return None
    return {
        "columns": tuple(str(value) for value in (row["columns"] or ())),
        "is_unique": bool(row["is_unique"]),
        "is_valid": bool(row["is_valid"]),
        "is_ready": bool(row["is_ready"]),
        "is_unfiltered": bool(row["is_unfiltered"]),
        "has_plain_columns": bool(row["has_plain_columns"]),
        "has_no_included_columns": bool(row["has_no_included_columns"]),
    }


def _constraint_names(connection: Connection, table_name: str) -> set[str]:
    return {
        str(value)
        for value in connection.execute(
            text(
                """
                SELECT constraint_name
                FROM information_schema.table_constraints
                WHERE table_schema = 'public' AND table_name = :table_name
                """
            ),
            {"table_name": table_name},
        ).scalars()
    }


def _column_names(connection: Connection, table_name: str) -> set[str]:
    return {
        str(value)
        for value in connection.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = :table_name
                """
            ),
            {"table_name": table_name},
        ).scalars()
    }


def _table_row_count(connection: Connection, table_name: str) -> int:
    if table_name != "municipio_chat_idempotency_receipt":
        raise CutoverMigrationFailure("internal_table_count_not_allowlisted")
    return int(
        connection.execute(
            text("SELECT count(*) FROM public.municipio_chat_idempotency_receipt")
        ).scalar_one()
    )


def _trigger_exists(connection: Connection, table_name: str, trigger_name: str) -> bool:
    return bool(
        connection.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_trigger trigger
                    JOIN pg_class relation ON relation.oid = trigger.tgrelid
                    JOIN pg_namespace namespace ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = 'public'
                      AND relation.relname = :table_name
                      AND trigger.tgname = :trigger_name
                      AND NOT trigger.tgisinternal
                )
                """
            ),
            {"table_name": table_name, "trigger_name": trigger_name},
        ).scalar_one()
    )


def _repair_state(connection: Connection) -> dict[str, int]:
    row = connection.execute(
        text(
            """
            SELECT
                count(*) AS rows_present,
                count(*) FILTER (WHERE lower(coalesce(tenant.slug, '')) = 'almacen')
                    AS rows_scoped_to_source,
                count(*) FILTER (WHERE lower(coalesce(tenant.slug, '')) = 'junin')
                    AS rows_scoped_to_target
            FROM public.municipio_ticket ticket
            LEFT JOIN public.tenant_profile tenant ON tenant.id = ticket.tenant_id
            WHERE ticket.id IN (322, 344, 347)
            """
        )
    ).mappings().one()
    return {
        "rows_present": int(row["rows_present"]),
        "rows_scoped_to_source": int(row["rows_scoped_to_source"]),
        "rows_scoped_to_target": int(row["rows_scoped_to_target"]),
    }


def _demo_contract(connection: Connection) -> dict[str, Any]:
    present = _table_exists(connection, "demo_survey_participation")
    indexes = _index_names(connection, "demo_survey_participation") if present else set()
    trigger = (
        _trigger_exists(
            connection,
            "demo_survey_participation",
            EXPECTED_DEMO_TRIGGER,
        )
        if present
        else False
    )
    return {
        "table_present": present,
        "expected_indexes_present": EXPECTED_DEMO_INDEXES.issubset(indexes),
        "immutability_trigger_present": trigger,
    }


def _idempotency_contract(connection: Connection) -> dict[str, Any]:
    table_name = "municipio_chat_idempotency_receipt"
    present = _table_exists(connection, table_name)
    if not present:
        return {
            "table_present": False,
            "expected_columns_present": False,
            "expected_indexes_present": False,
            "expected_constraints_present": False,
            "rows": None,
        }
    return {
        "table_present": True,
        "expected_columns_present": EXPECTED_IDEMPOTENCY_COLUMNS.issubset(
            _column_names(connection, table_name)
        ),
        "expected_indexes_present": EXPECTED_IDEMPOTENCY_INDEXES.issubset(
            _index_names(connection, table_name)
        ),
        "expected_constraints_present": EXPECTED_IDEMPOTENCY_CONSTRAINTS.issubset(
            _constraint_names(connection, table_name)
        ),
        "rows": _table_row_count(connection, table_name),
    }


def _foreign_key_contract(
    connection: Connection,
    table_name: str,
) -> set[tuple[str, str, str, str]]:
    return {
        (
            str(row["column_name"]),
            str(row["referenced_table"]),
            str(row["referenced_column"]),
            str(row["delete_rule"]),
        )
        for row in connection.execute(
            text(
                """
                SELECT
                    key_column.column_name,
                    referenced_column.table_name AS referenced_table,
                    referenced_column.column_name AS referenced_column,
                    referential.delete_rule
                FROM information_schema.table_constraints AS constraint_row
                JOIN information_schema.key_column_usage AS key_column
                  ON key_column.constraint_catalog = constraint_row.constraint_catalog
                 AND key_column.constraint_schema = constraint_row.constraint_schema
                 AND key_column.constraint_name = constraint_row.constraint_name
                JOIN information_schema.referential_constraints AS referential
                  ON referential.constraint_catalog = constraint_row.constraint_catalog
                 AND referential.constraint_schema = constraint_row.constraint_schema
                 AND referential.constraint_name = constraint_row.constraint_name
                JOIN information_schema.constraint_column_usage AS referenced_column
                  ON referenced_column.constraint_catalog =
                     referential.unique_constraint_catalog
                 AND referenced_column.constraint_schema =
                     referential.unique_constraint_schema
                 AND referenced_column.constraint_name =
                     referential.unique_constraint_name
                WHERE constraint_row.table_schema = 'public'
                  AND constraint_row.table_name = :table_name
                  AND constraint_row.constraint_type = 'FOREIGN KEY'
                """
            ),
            {"table_name": table_name},
        ).mappings()
    }


def _territorial_table_contract(
    connection: Connection,
    table_name: str,
) -> dict[str, Any]:
    specification = EXPECTED_TERRITORIAL_SCHEMA.get(table_name)
    if specification is None:
        raise CutoverMigrationFailure("internal_territorial_table_not_allowlisted")
    if not _table_exists(connection, table_name):
        return {
            "table_present": False,
            "exact_columns_present": False,
            "expected_constraints_present": False,
            "exact_foreign_keys_present": False,
            "exact_indexes_valid": False,
        }

    index_contracts = specification["indexes"]
    exact_indexes_valid = True
    for index_name, (expected_columns, expected_unique) in index_contracts.items():
        actual = _index_contract(
            connection,
            table_name=table_name,
            index_name=index_name,
        )
        expected = {
            "columns": expected_columns,
            "is_unique": expected_unique,
            "is_valid": True,
            "is_ready": True,
            "is_unfiltered": True,
            "has_plain_columns": True,
            "has_no_included_columns": True,
        }
        if actual != expected:
            exact_indexes_valid = False
            break

    return {
        "table_present": True,
        "exact_columns_present": (
            _column_names(connection, table_name) == specification["columns"]
        ),
        "expected_constraints_present": specification["constraints"].issubset(
            _constraint_names(connection, table_name)
        ),
        "exact_foreign_keys_present": (
            _foreign_key_contract(connection, table_name)
            == specification["foreign_keys"]
        ),
        "exact_indexes_valid": exact_indexes_valid,
    }


def _territorial_schema_postcheck(
    connection: Connection,
    *,
    required_tables: Sequence[str],
    absent_tables: Sequence[str],
    reason_code: str,
) -> dict[str, Any]:
    contracts = {
        table_name: _territorial_table_contract(connection, table_name)
        for table_name in required_tables
    }
    if not all(all(contract.values()) for contract in contracts.values()):
        raise CutoverMigrationFailure(reason_code)
    if any(_table_exists(connection, table_name) for table_name in absent_tables):
        raise CutoverMigrationFailure(reason_code)
    return contracts


def _platform_table_contract(
    connection: Connection,
    specification: Mapping[str, Any],
) -> dict[str, bool]:
    table_name = str(specification["table"])
    present = _table_exists(connection, table_name)
    if not present:
        return {
            "table_present": False,
            "columns_valid": False,
            "constraints_valid": False,
            "foreign_keys_valid": False,
            "indexes_valid": False,
            "triggers_valid": False,
        }

    expected_columns = set(specification.get("columns", ()))
    actual_columns = _column_names(connection, table_name)
    columns_valid = (
        actual_columns == expected_columns
        if specification.get("exact_columns") is True
        else expected_columns.issubset(actual_columns)
    )
    constraints_valid = set(specification.get("constraints", ())).issubset(
        _constraint_names(connection, table_name)
    )
    expected_foreign_keys = set(specification.get("foreign_keys", ()))
    actual_foreign_keys = _foreign_key_contract(connection, table_name)
    foreign_keys_valid = (
        actual_foreign_keys == expected_foreign_keys
        if specification.get("exact_foreign_keys") is True
        else expected_foreign_keys.issubset(actual_foreign_keys)
    )

    indexes_valid = True
    for index_name, expected_signature in dict(
        specification.get("indexes", {})
    ).items():
        expected_index_columns, expected_unique = expected_signature
        expected_contract = {
            "columns": tuple(expected_index_columns),
            "is_unique": bool(expected_unique),
            "is_valid": True,
            "is_ready": True,
            "is_unfiltered": True,
            "has_plain_columns": True,
            "has_no_included_columns": True,
        }
        if _index_contract(
            connection,
            table_name=table_name,
            index_name=str(index_name),
        ) != expected_contract:
            indexes_valid = False
            break

    triggers_valid = all(
        _trigger_exists(connection, table_name, str(trigger_name))
        for trigger_name in specification.get("triggers", ())
    )
    return {
        "table_present": True,
        "columns_valid": columns_valid,
        "constraints_valid": constraints_valid,
        "foreign_keys_valid": foreign_keys_valid,
        "indexes_valid": indexes_valid,
        "triggers_valid": triggers_valid,
    }


def _territorial_execution_schema_postcheck(
    connection: Connection,
) -> dict[str, dict[str, bool]]:
    execution_extensions = {
        str(specification["table"]): specification
        for specification in POST_SYNC_SCHEMA_REQUIREMENTS[
            TERRITORIAL_EXECUTION_REVISION
        ]
    }
    contracts: dict[str, dict[str, bool]] = {}
    for table_name, base in EXPECTED_TERRITORIAL_SCHEMA.items():
        extension = execution_extensions.get(table_name, {})
        specification = {
            "table": table_name,
            "columns": set(base["columns"]) | set(extension.get("columns", ())),
            "exact_columns": True,
            "constraints": set(base["constraints"])
            | set(extension.get("constraints", ())),
            "foreign_keys": set(base["foreign_keys"])
            | set(extension.get("foreign_keys", ())),
            "exact_foreign_keys": True,
            "indexes": {
                **dict(base["indexes"]),
                **dict(extension.get("indexes", {})),
            },
        }
        contract = _platform_table_contract(connection, specification)
        contracts[table_name] = contract
        if not all(contract.values()):
            raise CutoverMigrationFailure(
                "database_territorial_execution_contract_postcheck_failed"
            )
    return contracts


def _assert_after_territorial_execution(connection: Connection) -> dict[str, Any]:
    prior_contracts = _assert_current_global_writer_authority(connection)
    contracts = _territorial_execution_schema_postcheck(connection)
    return {
        **prior_contracts,
        "territorial_geocoding_contract": contracts,
    }


def _platform_extension_schema_postcheck(
    connection: Connection,
    revision: str,
) -> dict[str, dict[str, bool]]:
    if revision not in POST_SYNC_SCHEMA_REQUIREMENTS:
        raise CutoverMigrationFailure("migration_target_not_allowlisted")

    first_extension_index = MIGRATION_STEPS.index(INBOX_ARTIFACT_REVISION)
    target_index = MIGRATION_STEPS.index(revision)
    contracts: dict[str, dict[str, bool]] = {}
    for required_revision in MIGRATION_STEPS[
        first_extension_index : target_index + 1
    ]:
        for specification in POST_SYNC_SCHEMA_REQUIREMENTS[required_revision]:
            table_name = str(specification["table"])
            contract = _platform_table_contract(connection, specification)
            contracts[table_name] = contract
            if not all(contract.values()):
                raise CutoverMigrationFailure(
                    "database_platform_extension_contract_postcheck_failed"
                )
    return contracts


def _assert_after_post_sync_revision(
    connection: Connection,
    revision: str,
) -> dict[str, Any]:
    if revision not in POST_SYNC_SCHEMA_REQUIREMENTS:
        raise CutoverMigrationFailure("migration_target_not_allowlisted")

    target_index = MIGRATION_STEPS.index(revision)
    execution_index = MIGRATION_STEPS.index(TERRITORIAL_EXECUTION_REVISION)
    prior_contracts = (
        _assert_after_territorial_sync(connection)
        if target_index < execution_index
        else _assert_after_territorial_execution(connection)
    )
    contracts = _platform_extension_schema_postcheck(connection, revision)
    return {
        **prior_contracts,
        "platform_extension_contract": contracts,
    }


def _single_alembic_revision(connection: Connection) -> str:
    if not _table_exists(connection, "alembic_version"):
        raise CutoverMigrationFailure("database_migration_table_missing")
    revisions = [
        str(value).strip()
        for value in connection.execute(
            text("SELECT version_num FROM public.alembic_version ORDER BY version_num")
        ).scalars()
    ]
    if len(revisions) != 1 or not revisions[0]:
        raise CutoverMigrationFailure("database_migration_revision_not_single")
    return revisions[0]


def _require_revision(connection: Connection, expected_revision: str) -> str:
    actual = _single_alembic_revision(connection)
    if actual != expected_revision:
        raise CutoverMigrationFailure("database_migration_revision_unexpected")
    return actual


def _require_base_tables(connection: Connection) -> None:
    required = {
        "alembic_version",
        "archivo_adjunto",
        "demo_survey_participation",
        "municipio_ticket",
        "tenant_profile",
        "user",
    }
    if not all(_table_exists(connection, table_name) for table_name in required):
        raise CutoverMigrationFailure("database_cutover_base_schema_missing")


def _assert_baseline_schema(connection: Connection) -> dict[str, Any]:
    _require_base_tables(connection)
    demo = _demo_contract(connection)
    if not all(demo.values()):
        raise CutoverMigrationFailure("database_demo_survey_contract_invalid")
    idempotency = _idempotency_contract(connection)
    if idempotency["table_present"]:
        raise CutoverMigrationFailure("database_idempotency_table_exists_before_revision")
    repair = _repair_state(connection)
    if repair != {
        "rows_present": len(LEGACY_TICKET_IDS),
        "rows_scoped_to_source": len(LEGACY_TICKET_IDS),
        "rows_scoped_to_target": 0,
    }:
        raise CutoverMigrationFailure("database_legacy_ticket_repair_baseline_drift")
    return {
        "demo_survey_contract_valid": True,
        "idempotency_table_absent": True,
        "legacy_ticket_scope": repair,
    }


def _assert_after_repair(connection: Connection) -> dict[str, Any]:
    demo = _demo_contract(connection)
    if not all(demo.values()):
        raise CutoverMigrationFailure("database_demo_survey_contract_changed")
    idempotency = _idempotency_contract(connection)
    if idempotency["table_present"]:
        raise CutoverMigrationFailure("database_idempotency_table_created_too_early")
    repair = _repair_state(connection)
    if repair != {
        "rows_present": len(LEGACY_TICKET_IDS),
        "rows_scoped_to_source": 0,
        "rows_scoped_to_target": len(LEGACY_TICKET_IDS),
    }:
        raise CutoverMigrationFailure("database_legacy_ticket_repair_postcheck_failed")
    return {
        "demo_survey_contract_valid": True,
        "idempotency_table_absent": True,
        "legacy_ticket_scope": repair,
    }


def _assert_after_idempotency(connection: Connection) -> dict[str, Any]:
    demo = _demo_contract(connection)
    if not all(demo.values()):
        raise CutoverMigrationFailure("database_demo_survey_contract_changed")
    repair = _repair_state(connection)
    if repair != {
        "rows_present": len(LEGACY_TICKET_IDS),
        "rows_scoped_to_source": 0,
        "rows_scoped_to_target": len(LEGACY_TICKET_IDS),
    }:
        raise CutoverMigrationFailure("database_legacy_ticket_repair_regressed")
    idempotency = _idempotency_contract(connection)
    if not (
        idempotency["table_present"]
        and idempotency["expected_columns_present"]
        and idempotency["expected_indexes_present"]
        and idempotency["expected_constraints_present"]
        and idempotency["rows"] == 0
    ):
        raise CutoverMigrationFailure("database_idempotency_contract_postcheck_failed")
    return {
        "demo_survey_contract_valid": True,
        "legacy_ticket_scope": repair,
        "idempotency_contract": idempotency,
    }


def _assert_after_inbound_fifo(connection: Connection) -> dict[str, Any]:
    prior_contracts = _assert_after_idempotency(connection)
    fifo_index = _index_contract(
        connection,
        table_name="whatsapp_inbound_turn",
        index_name=EXPECTED_INBOUND_FIFO_INDEX,
    )
    if fifo_index != {
        "columns": EXPECTED_INBOUND_FIFO_COLUMNS,
        "is_unique": False,
        "is_valid": True,
        "is_ready": True,
        "is_unfiltered": True,
        "has_plain_columns": True,
        "has_no_included_columns": True,
    }:
        raise CutoverMigrationFailure("database_inbound_fifo_index_postcheck_failed")
    return {
        **prior_contracts,
        "inbound_fifo_index": fifo_index,
    }


def _global_writer_authority_contract(
    connection: Connection,
    *,
    require_bootstrap: bool,
) -> dict[str, Any]:
    table_name = "cutover_global_writer_authority"
    if not _table_exists(connection, table_name):
        return {
            "table_present": False,
            "expected_columns_present": False,
            "expected_constraints_present": False,
            "singleton_state_valid": False,
            "singleton_bootstrap_fenced": False,
            "required_state_valid": False,
        }
    columns = _column_names(connection, table_name)
    constraints = _constraint_names(connection, table_name)
    rows = list(
        connection.execute(
            text(
                """
                SELECT authority_key, owner_runtime, epoch,
                       render_fenced, vercel_fenced
                FROM public.cutover_global_writer_authority
                ORDER BY authority_key
                """
            )
        ).mappings()
    )
    singleton_state_valid = False
    singleton_bootstrap_fenced = False
    if len(rows) == 1:
        row = dict(rows[0])
        owner = row.get("owner_runtime")
        epoch = row.get("epoch")
        render_fenced = row.get("render_fenced")
        vercel_fenced = row.get("vercel_fenced")
        epoch_valid = isinstance(epoch, int) and not isinstance(epoch, bool) and epoch >= 0
        booleans_valid = isinstance(render_fenced, bool) and isinstance(
            vercel_fenced, bool
        )
        ownership_safe = (
            owner is None
            and render_fenced is True
            and vercel_fenced is True
        ) or (
            owner == "render" and vercel_fenced is True
        ) or (
            owner == "vercel" and render_fenced is True
        )
        singleton_state_valid = bool(
            row.get("authority_key") == "primary"
            and epoch_valid
            and booleans_valid
            and ownership_safe
        )
        singleton_bootstrap_fenced = bool(
            singleton_state_valid
            and owner is None
            and epoch == 0
            and render_fenced is True
            and vercel_fenced is True
        )
    return {
        "table_present": True,
        "expected_columns_present": EXPECTED_GLOBAL_WRITER_AUTHORITY_COLUMNS.issubset(
            columns
        ),
        "expected_constraints_present": EXPECTED_GLOBAL_WRITER_AUTHORITY_CONSTRAINTS.issubset(
            constraints
        ),
        "singleton_state_valid": singleton_state_valid,
        "singleton_bootstrap_fenced": singleton_bootstrap_fenced,
        "required_state_valid": (
            singleton_bootstrap_fenced
            if require_bootstrap
            else singleton_state_valid
        ),
    }


def _assert_after_global_writer_authority(connection: Connection) -> dict[str, Any]:
    prior_contracts = _assert_after_inbound_fifo(connection)
    contract = _global_writer_authority_contract(
        connection,
        require_bootstrap=True,
    )
    required = (
        "table_present",
        "expected_columns_present",
        "expected_constraints_present",
        "singleton_state_valid",
        "required_state_valid",
    )
    if not all(contract[key] for key in required):
        raise CutoverMigrationFailure(
            "database_global_writer_authority_contract_postcheck_failed"
        )
    return {
        **prior_contracts,
        "global_writer_authority_contract": contract,
    }


def _assert_current_global_writer_authority(connection: Connection) -> dict[str, Any]:
    prior_contracts = _assert_after_inbound_fifo(connection)
    contract = _global_writer_authority_contract(
        connection,
        require_bootstrap=False,
    )
    required = (
        "table_present",
        "expected_columns_present",
        "expected_constraints_present",
        "singleton_state_valid",
        "required_state_valid",
    )
    if not all(contract[key] for key in required):
        raise CutoverMigrationFailure(
            "database_global_writer_authority_contract_invalid"
        )
    return {
        **prior_contracts,
        "global_writer_authority_contract": contract,
    }


def _assert_after_territorial_geocoding(connection: Connection) -> dict[str, Any]:
    prior_contracts = _assert_current_global_writer_authority(connection)
    contracts = _territorial_schema_postcheck(
        connection,
        required_tables=(
            "territorial_geocoding_job",
            "territorial_geocoding_attempt",
        ),
        absent_tables=(
            "territorial_geocoding_review",
            "territorial_geocoding_sync_receipt",
        ),
        reason_code="database_territorial_geocoding_contract_postcheck_failed",
    )
    return {
        **prior_contracts,
        "territorial_geocoding_contract": contracts,
    }


def _assert_after_territorial_review(connection: Connection) -> dict[str, Any]:
    prior_contracts = _assert_current_global_writer_authority(connection)
    contracts = _territorial_schema_postcheck(
        connection,
        required_tables=(
            "territorial_geocoding_job",
            "territorial_geocoding_attempt",
            "territorial_geocoding_review",
        ),
        absent_tables=("territorial_geocoding_sync_receipt",),
        reason_code="database_territorial_review_contract_postcheck_failed",
    )
    return {
        **prior_contracts,
        "territorial_geocoding_contract": contracts,
    }


def _assert_after_territorial_sync(connection: Connection) -> dict[str, Any]:
    prior_contracts = _assert_current_global_writer_authority(connection)
    contracts = _territorial_schema_postcheck(
        connection,
        required_tables=(
            "territorial_geocoding_job",
            "territorial_geocoding_attempt",
            "territorial_geocoding_review",
            "territorial_geocoding_sync_receipt",
        ),
        absent_tables=(),
        reason_code="database_territorial_sync_contract_postcheck_failed",
    )
    return {
        **prior_contracts,
        "territorial_geocoding_contract": contracts,
    }


def _require_allowlisted_cutover_revision(connection: Connection) -> str:
    revision = _single_alembic_revision(connection)
    if revision not in {INITIAL_REVISION, *MIGRATION_STEPS}:
        raise CutoverMigrationFailure("database_migration_revision_unexpected")
    return revision


def _assert_contract_for_revision(
    connection: Connection,
    revision: str,
) -> dict[str, Any]:
    if revision in POST_SYNC_SCHEMA_REQUIREMENTS:
        return _assert_after_post_sync_revision(connection, revision)
    validators = {
        INITIAL_REVISION: _assert_baseline_schema,
        REPAIR_REVISION: _assert_after_repair,
        IDEMPOTENCY_REVISION: _assert_after_idempotency,
        INBOUND_FIFO_REVISION: _assert_after_inbound_fifo,
        GLOBAL_WRITER_AUTHORITY_REVISION: _assert_current_global_writer_authority,
        TERRITORIAL_GEOCODING_REVISION: _assert_after_territorial_geocoding,
        TERRITORIAL_GEOCODING_REVIEW_REVISION: _assert_after_territorial_review,
        TERRITORIAL_GEOCODING_SYNC_REVISION: _assert_after_territorial_sync,
    }
    validator = validators.get(revision)
    if validator is None:
        raise CutoverMigrationFailure("database_migration_revision_unexpected")
    return validator(connection)


def _postcheck_for_applied_revision(
    connection: Connection,
    revision: str,
) -> dict[str, Any]:
    if revision in POST_SYNC_SCHEMA_REQUIREMENTS:
        return _assert_after_post_sync_revision(connection, revision)
    validators = {
        REPAIR_REVISION: _assert_after_repair,
        IDEMPOTENCY_REVISION: _assert_after_idempotency,
        INBOUND_FIFO_REVISION: _assert_after_inbound_fifo,
        GLOBAL_WRITER_AUTHORITY_REVISION: _assert_after_global_writer_authority,
        TERRITORIAL_GEOCODING_REVISION: _assert_after_territorial_geocoding,
        TERRITORIAL_GEOCODING_REVIEW_REVISION: _assert_after_territorial_review,
        TERRITORIAL_GEOCODING_SYNC_REVISION: _assert_after_territorial_sync,
    }
    validator = validators.get(revision)
    if validator is None:
        raise CutoverMigrationFailure("migration_target_not_allowlisted")
    return validator(connection)


def _apply_exact_revision(
    connection: Connection,
    *,
    plan: ExactMigrationPlan,
    expected_current_revision: str,
    target_revision: str,
) -> None:
    if target_revision not in MIGRATION_STEPS or target_revision in {"head", "heads"}:
        raise CutoverMigrationFailure("migration_target_not_allowlisted")
    revision = plan.script.get_revision(target_revision)
    if revision is None or revision.down_revision != expected_current_revision:
        raise CutoverMigrationFailure("migration_step_not_direct_child")
    _require_revision(connection, expected_current_revision)

    migration_context = MigrationContext.configure(connection)
    # Alembic's module-level ``op`` proxy must be removed even when a migration
    # raises; this function can also be imported by a longer-lived operator.
    operations = Operations(migration_context)
    operations._install_proxy()
    try:
        revision.module.upgrade()
    finally:
        operations._remove_proxy()

    # The allowlisted upgrade functions must not manage Alembic state.
    _require_revision(connection, expected_current_revision)
    result = connection.execute(
        text(
            """
            UPDATE public.alembic_version
            SET version_num = :target_revision
            WHERE version_num = :expected_current_revision
            """
        ),
        {
            "target_revision": target_revision,
            "expected_current_revision": expected_current_revision,
        },
    )
    if result.rowcount != 1:
        raise CutoverMigrationFailure("database_migration_revision_update_failed")


def _execute_cutover_transaction(
    connection: Connection,
    *,
    apply: bool,
    plan: ExactMigrationPlan,
    expected_project_fingerprint_sha256: str,
    expected_branch_fingerprint_sha256: str,
) -> dict[str, Any]:
    _configure_transaction(connection, apply=apply)
    identity = _assert_database_identity(
        connection,
        expected_project_fingerprint_sha256=expected_project_fingerprint_sha256,
        expected_branch_fingerprint_sha256=expected_branch_fingerprint_sha256,
    )
    if apply:
        _acquire_advisory_lock(connection)

    revision_before = _require_allowlisted_cutover_revision(connection)
    baseline = _assert_contract_for_revision(connection, revision_before)
    if not apply:
        return {
            "identity": identity,
            "advisory_lock_acquired": False,
            "revision_before": revision_before,
            "revision_after": revision_before,
            "baseline": baseline,
            "steps": [],
        }

    steps: list[dict[str, Any]] = []
    chain = (INITIAL_REVISION, *MIGRATION_STEPS)
    current_index = chain.index(revision_before)
    revision_after = revision_before
    for target_revision in chain[current_index + 1 :]:
        _apply_exact_revision(
            connection,
            plan=plan,
            expected_current_revision=revision_after,
            target_revision=target_revision,
        )
        revision_after = _require_revision(connection, target_revision)
        steps.append(
            {
                "revision": target_revision,
                "postcheck": _postcheck_for_applied_revision(
                    connection,
                    target_revision,
                ),
            }
        )
    return {
        "identity": identity,
        "advisory_lock_acquired": True,
        "revision_before": revision_before,
        "revision_after": revision_after,
        "baseline": baseline,
        "steps": steps,
    }


def run_cutover(
    *,
    database_url: str,
    project_root: Path,
    apply: bool,
    expected_host_fingerprint_sha256: str | None,
    expected_project_fingerprint_sha256: str | None,
    expected_branch_fingerprint_sha256: str | None,
    writer_fence_evidence_id: str | None = None,
    snapshot_evidence_id: str | None = None,
    parity_evidence_id: str | None = None,
) -> dict[str, Any]:
    parsed, host_fingerprint = _validate_target_url(
        database_url,
        expected_host_fingerprint_sha256=expected_host_fingerprint_sha256,
    )
    project_fingerprint = _validate_sha256(
        expected_project_fingerprint_sha256,
        reason_code="expected_neon_project_fingerprint_invalid",
    )
    branch_fingerprint = _validate_sha256(
        expected_branch_fingerprint_sha256,
        reason_code="expected_neon_branch_fingerprint_invalid",
    )
    evidence = _approval_evidence(
        apply=apply,
        writer_fence_evidence_id=writer_fence_evidence_id,
        snapshot_evidence_id=snapshot_evidence_id,
        parity_evidence_id=parity_evidence_id,
    )
    plan = _load_exact_migration_plan(project_root)

    engine_url = parsed.set(drivername="postgresql+psycopg")
    engine = create_engine(
        engine_url,
        poolclass=NullPool,
        connect_args={
            "application_name": "chatboc_neon_cutover_migrator",
            "connect_timeout": 10,
        },
    )
    try:
        with engine.connect() as connection:
            with connection.begin():
                database = _execute_cutover_transaction(
                    connection,
                    apply=apply,
                    plan=plan,
                    expected_project_fingerprint_sha256=project_fingerprint,
                    expected_branch_fingerprint_sha256=branch_fingerprint,
                )
    finally:
        engine.dispose()

    evidence_payload: dict[str, Any] = {
        "approval_evidence_bound": evidence is not None,
        "required_for_apply": ["writer_fence", "snapshot", "parity"],
    }
    if evidence is not None:
        evidence_payload["approval_id_fingerprints"] = evidence.fingerprints()

    return {
        "contract_version": CONTRACT_VERSION,
        "status": "applied" if apply else "ready_to_apply",
        "ready": True,
        "mode": "apply" if apply else "dry_run",
        "database_commit_confirmed": apply,
        "target": {
            "provider": "neon",
            "connection_mode": "direct",
            "tls_required": True,
            "host_fingerprint_sha256": host_fingerprint,
            **database.pop("identity"),
        },
        "plan": {
            "initial_revision": INITIAL_REVISION,
            "accepted_start_revisions": [INITIAL_REVISION, *MIGRATION_STEPS],
            "revisions": list(MIGRATION_STEPS),
            "final_revision": FINAL_MIGRATION_REVISION,
            "graph_fingerprint_sha256": plan.graph_fingerprint_sha256,
            "migration_source_fingerprints_sha256": dict(
                plan.source_fingerprints_sha256
            ),
        },
        "database": database,
        "evidence": evidence_payload,
        "safety": {
            "single_atomic_transaction": True,
            "exact_revision_steps": True,
            "statement_timeout_ms": STATEMENT_TIMEOUT_MS,
            "lock_timeout_ms": LOCK_TIMEOUT_MS,
            "idle_transaction_timeout_ms": IDLE_TRANSACTION_TIMEOUT_MS,
        },
    }


def _failure_payload(
    reason_code: str,
    *,
    apply: bool,
    error_type: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "status": "blocked",
        "ready": False,
        "mode": "apply" if apply else "dry_run",
        "database_commit_confirmed": False,
        "reason_code": reason_code,
    }
    if error_type:
        payload["error_type"] = error_type
    return payload


class _RedactedArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        # argparse normally echoes the rejected token. A mistaken DSN must not
        # become terminal/CI output, even though this CLI never accepts one.
        raise CutoverMigrationFailure("command_arguments_invalid")


def _parser() -> argparse.ArgumentParser:
    parser = _RedactedArgumentParser(description=__doc__)
    parser.add_argument(
        "--environment-variable",
        help="Explicit environment variable containing the direct Neon URL.",
    )
    parser.add_argument("--expected-host-fingerprint-sha256")
    parser.add_argument("--expected-project-fingerprint-sha256")
    parser.add_argument("--expected-branch-fingerprint-sha256")
    parser.add_argument("--approved-writer-fence-evidence-id")
    parser.add_argument("--approved-snapshot-evidence-id")
    parser.add_argument("--approved-parity-evidence-id")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply all exact revisions atomically. Omit for read-only dry-run.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    apply_requested = "--apply" in raw_arguments
    runtime_environment = os.environ if environ is None else environ
    try:
        args = _parser().parse_args(raw_arguments)
        apply_requested = bool(args.apply)
        database_url = _load_database_url(
            runtime_environment,
            args.environment_variable,
        )
        payload = run_cutover(
            database_url=database_url,
            project_root=Path(__file__).resolve().parents[1],
            apply=bool(args.apply),
            expected_host_fingerprint_sha256=args.expected_host_fingerprint_sha256,
            expected_project_fingerprint_sha256=args.expected_project_fingerprint_sha256,
            expected_branch_fingerprint_sha256=args.expected_branch_fingerprint_sha256,
            writer_fence_evidence_id=args.approved_writer_fence_evidence_id,
            snapshot_evidence_id=args.approved_snapshot_evidence_id,
            parity_evidence_id=args.approved_parity_evidence_id,
        )
        exit_code = 0
    except CutoverMigrationFailure as exc:
        payload = _failure_payload(exc.reason_code, apply=apply_requested)
        exit_code = 2
    except Exception as exc:  # Never serialize provider/driver exception text.
        payload = _failure_payload(
            "cutover_migration_runtime_failed",
            apply=apply_requested,
            error_type=type(exc).__name__,
        )
        exit_code = 2

    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
