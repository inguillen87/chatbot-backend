from __future__ import annotations

import uuid

from models import JSONType, db


class TenantBlueprintApplication(db.Model):
    """Immutable tenant-scoped receipt for one blueprint application."""

    __tablename__ = "tenant_blueprint_application"

    CONTRACT_VERSION = "tenant.blueprint.application.v1"
    VALID_STATUSES = frozenset({"applied"})

    id = db.Column(
        db.String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    tenant_id = db.Column(
        db.Integer,
        db.ForeignKey("tenant_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    contract_version = db.Column(
        db.String(64), nullable=False, default=CONTRACT_VERSION
    )
    blueprint_id = db.Column(db.String(64), nullable=False)
    blueprint_version = db.Column(db.String(32), nullable=False)
    manifest_digest = db.Column(db.String(64), nullable=False)
    request_digest = db.Column(db.String(64), nullable=False)
    idempotency_key_hash = db.Column(db.String(64), nullable=False)
    status = db.Column(
        db.String(20), nullable=False, default="applied", server_default="applied"
    )
    application_snapshot = db.Column(JSONType, nullable=False, default=dict)
    applied_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, server_default=db.func.now()
    )

    __table_args__ = (
        db.UniqueConstraint(
            "tenant_id",
            "blueprint_id",
            "blueprint_version",
            name="uq_tenant_blueprint_application_version",
        ),
        db.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_tenant_blueprint_application_idempotency",
        ),
        db.CheckConstraint(
            "status = 'applied'",
            name="ck_tenant_blueprint_application_status",
        ),
        db.CheckConstraint(
            "length(manifest_digest) = 64 AND "
            "length(request_digest) = 64 AND "
            "length(idempotency_key_hash) = 64",
            name="ck_tenant_blueprint_application_digests",
        ),
        db.Index(
            "ix_tenant_blueprint_application_tenant_created",
            "tenant_id",
            "created_at",
            "id",
        ),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "contract_version": self.contract_version,
            "tenant_id": self.tenant_id,
            "blueprint_id": self.blueprint_id,
            "blueprint_version": self.blueprint_version,
            "manifest_digest": self.manifest_digest,
            "request_digest": self.request_digest,
            "status": self.status,
            "application_snapshot": self.application_snapshot or {},
            "applied_by_user_id": self.applied_by_user_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
