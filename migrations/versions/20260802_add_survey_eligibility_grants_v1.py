"""Add opaque eligibility grants and a unified append-only terminal ledger.

Revision ID: 20260802_survey_eligibility_v1
Revises: 20260802_interview_assignment_v1
Create Date: 2026-08-02
"""

from alembic import context, op
import sqlalchemy as sa


revision = "20260802_survey_eligibility_v1"
down_revision = "20260802_interview_assignment_v1"
branch_labels = None
depends_on = None


_REVOCATION_REASONS_SQL = (
    "'administrative_revocation', 'subject_ineligible', "
    "'credential_compromised', 'duplicate_issue', 'other_reviewed'"
)


def _object_names(kind: str, table_name: str) -> set[str]:
    try:
        if context.is_offline_mode():
            return set()
    except (AttributeError, NameError):
        pass
    try:
        inspector = sa.inspect(op.get_bind())
    except sa.exc.NoInspectionAvailable:
        return set()
    getter = getattr(inspector, kind)
    return {str(item.get("name")) for item in getter(table_name) if item.get("name")}


def _create_governance_scope_uniques() -> None:
    dialect = op.get_bind().dialect.name
    existing = _object_names("get_unique_constraints", "survey_governance_release")
    existing |= _object_names("get_indexes", "survey_governance_release")
    definitions = (
        (
            "uq_survey_governance_release_scope_id",
            ["tenant_id", "survey_id", "id"],
        ),
        (
            "uq_survey_governance_release_scope_policy_id",
            ["tenant_id", "survey_id", "id", "eligibility_policy_version"],
        ),
    )
    for name, columns in definitions:
        if name in existing:
            continue
        if dialect == "sqlite":
            # A unique index is a valid SQLite parent key and avoids batch table
            # recreation, which would silently discard existing governance triggers.
            op.create_index(name, "survey_governance_release", columns, unique=True)
        else:
            op.create_unique_constraint(name, "survey_governance_release", columns)


def _drop_governance_scope_uniques() -> None:
    dialect = op.get_bind().dialect.name
    existing_constraints = _object_names(
        "get_unique_constraints", "survey_governance_release"
    )
    existing_indexes = _object_names("get_indexes", "survey_governance_release")
    for name in (
        "uq_survey_governance_release_scope_policy_id",
        "uq_survey_governance_release_scope_id",
    ):
        if name in existing_indexes:
            op.drop_index(name, table_name="survey_governance_release")
        elif name in existing_constraints and dialect != "sqlite":
            op.drop_constraint(name, "survey_governance_release", type_="unique")


def _create_response_eligibility_guard() -> None:
    dialect = op.get_bind().dialect.name
    expression = (
        "(eligibility_decision IS NULL "
        "AND eligibility_contract_version IS NULL "
        "AND eligibility_verified_at IS NULL) OR "
        "(eligibility_decision IS NOT NULL "
        "AND eligibility_decision = 'verified_by_opaque_grant' "
        "AND eligibility_contract_version IS NOT NULL "
        "AND eligibility_contract_version = 'surveys.public_eligibility.v1' "
        "AND eligibility_verified_at IS NOT NULL "
        "AND governance_release_id IS NOT NULL)"
    )
    if dialect == "sqlite":
        for operation in ("INSERT", "UPDATE"):
            op.execute(
                sa.text(
                    f"""
                    CREATE TRIGGER trg_enc_respuesta_eligibility_{operation.lower()}
                    BEFORE {operation} ON enc_respuesta
                    FOR EACH ROW
                    WHEN NOT (
                        (NEW.eligibility_decision IS NULL
                         AND NEW.eligibility_contract_version IS NULL
                         AND NEW.eligibility_verified_at IS NULL)
                        OR
                        (NEW.eligibility_decision IS NOT NULL
                         AND NEW.eligibility_decision = 'verified_by_opaque_grant'
                         AND NEW.eligibility_contract_version IS NOT NULL
                         AND NEW.eligibility_contract_version =
                             'surveys.public_eligibility.v1'
                         AND NEW.eligibility_verified_at IS NOT NULL
                         AND NEW.governance_release_id IS NOT NULL)
                    )
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'invalid opaque survey eligibility response receipt'
                        );
                    END
                    """
                )
            )
    else:
        op.create_check_constraint(
            "ck_enc_respuesta_opaque_eligibility",
            "enc_respuesta",
            expression,
        )


def _drop_response_eligibility_guard() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_enc_respuesta_eligibility_insert")
        op.execute("DROP TRIGGER IF EXISTS trg_enc_respuesta_eligibility_update")
    else:
        op.drop_constraint(
            "ck_enc_respuesta_opaque_eligibility",
            "enc_respuesta",
            type_="check",
        )


def _create_ledger_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION survey_eligibility_history_immutable()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'survey eligibility history is immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for table_name in ("survey_eligibility_grant", "survey_eligibility_terminal"):
            op.execute(
                f"""
                CREATE TRIGGER trg_{table_name}_immutable
                BEFORE UPDATE OR DELETE ON {table_name}
                FOR EACH ROW EXECUTE FUNCTION survey_eligibility_history_immutable()
                """
            )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION survey_eligibility_grant_generation_guard()
            RETURNS trigger AS $$
            DECLARE expected_generation integer;
            BEGIN
                IF NEW.issued_at > CURRENT_TIMESTAMP + INTERVAL '5 minutes' THEN
                    RAISE EXCEPTION 'survey eligibility issued_at is in the future';
                END IF;
                SELECT COALESCE(MAX(generation), 0) + 1
                  INTO expected_generation
                  FROM survey_eligibility_grant
                 WHERE tenant_id = NEW.tenant_id
                   AND release_id = NEW.release_id
                   AND subject_hmac = NEW.subject_hmac;
                IF NEW.generation <> expected_generation THEN
                    RAISE EXCEPTION 'invalid survey eligibility grant generation';
                END IF;
                IF NEW.generation > 1 AND NOT EXISTS (
                    SELECT 1
                      FROM survey_eligibility_grant previous_grant
                      LEFT JOIN survey_eligibility_terminal terminal
                        ON terminal.grant_id = previous_grant.id
                     WHERE previous_grant.tenant_id = NEW.tenant_id
                       AND previous_grant.release_id = NEW.release_id
                       AND previous_grant.subject_hmac = NEW.subject_hmac
                       AND previous_grant.generation = NEW.generation - 1
                       AND previous_grant.review_reference_hmac <>
                           NEW.review_reference_hmac
                       AND (
                           (terminal.id IS NULL
                            AND previous_grant.expires_at <= CURRENT_TIMESTAMP)
                           OR
                           (terminal.disposition = 'revoked'
                            AND terminal.reason_code IN (
                                'administrative_revocation',
                                'credential_compromised',
                                'other_reviewed'
                            ))
                       )
                ) THEN
                    RAISE EXCEPTION 'survey eligibility grant reissue blocked';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_survey_eligibility_grant_generation
            BEFORE INSERT ON survey_eligibility_grant
            FOR EACH ROW EXECUTE FUNCTION survey_eligibility_grant_generation_guard()
            """
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION survey_eligibility_terminal_scope_guard()
            RETURNS trigger AS $$
            BEGIN
                IF NEW.disposition = 'redeemed' AND NOT EXISTS (
                    SELECT 1
                      FROM enc_respuesta response
                      JOIN survey_eligibility_grant grant_row
                        ON grant_row.id = NEW.grant_id
                       AND grant_row.tenant_id = NEW.tenant_id
                       AND grant_row.survey_id = NEW.survey_id
                       AND grant_row.release_id = NEW.release_id
                       AND grant_row.eligibility_policy_version =
                           NEW.eligibility_policy_version
                     WHERE response.id = NEW.response_id
                       AND response.tenant_id = NEW.tenant_id
                       AND response.encuesta_id = NEW.survey_id
                       AND response.governance_release_id = NEW.release_id
                       AND response.governance_eligibility_policy_version =
                           NEW.eligibility_policy_version
                       AND response.eligibility_decision IS NOT NULL
                       AND response.eligibility_decision =
                           'verified_by_opaque_grant'
                       AND response.eligibility_contract_version IS NOT NULL
                       AND response.eligibility_contract_version =
                           'surveys.public_eligibility.v1'
                       AND response.eligibility_verified_at IS NOT NULL
                       AND response.eligibility_verified_at = NEW.created_at
                       AND grant_row.issued_at <= NEW.created_at
                       AND NEW.created_at < grant_row.expires_at
                ) THEN
                    RAISE EXCEPTION 'survey eligibility redemption response scope mismatch';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_survey_eligibility_terminal_scope
            BEFORE INSERT ON survey_eligibility_terminal
            FOR EACH ROW EXECUTE FUNCTION survey_eligibility_terminal_scope_guard()
            """
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION survey_eligibility_response_terminal_guard()
            RETURNS trigger AS $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                      FROM survey_eligibility_terminal terminal
                     WHERE terminal.response_id = OLD.id
                ) AND NOT EXISTS (
                    SELECT 1
                      FROM survey_eligibility_terminal terminal
                     WHERE terminal.response_id = OLD.id
                       AND NEW.tenant_id = terminal.tenant_id
                       AND NEW.encuesta_id = terminal.survey_id
                       AND NEW.governance_release_id = terminal.release_id
                       AND NEW.governance_eligibility_policy_version =
                           terminal.eligibility_policy_version
                       AND NEW.eligibility_decision =
                           'verified_by_opaque_grant'
                       AND NEW.eligibility_contract_version =
                           'surveys.public_eligibility.v1'
                       AND NEW.eligibility_verified_at = terminal.created_at
                ) THEN
                    RAISE EXCEPTION 'survey eligibility response receipt is immutable';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_enc_respuesta_eligibility_terminal_update
            BEFORE UPDATE ON enc_respuesta
            FOR EACH ROW EXECUTE FUNCTION survey_eligibility_response_terminal_guard()
            """
        )
    elif dialect == "sqlite":
        for table_name in ("survey_eligibility_grant", "survey_eligibility_terminal"):
            for operation in ("UPDATE", "DELETE"):
                op.execute(
                    f"""
                    CREATE TRIGGER trg_{table_name}_{operation.lower()}_immutable
                    BEFORE {operation} ON {table_name}
                    FOR EACH ROW
                    BEGIN
                        SELECT RAISE(
                            ABORT,
                            'survey eligibility history is immutable'
                        );
                    END
                    """
                )
        op.execute(
            """
            CREATE TRIGGER trg_survey_eligibility_grant_issued_at
            BEFORE INSERT ON survey_eligibility_grant
            FOR EACH ROW
            WHEN datetime(NEW.issued_at) > datetime('now', '+5 minutes')
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'survey eligibility issued_at is in the future'
                );
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_survey_eligibility_grant_generation
            BEFORE INSERT ON survey_eligibility_grant
            FOR EACH ROW
            WHEN NEW.generation <> (
                    SELECT COALESCE(MAX(generation), 0) + 1
                      FROM survey_eligibility_grant
                     WHERE tenant_id = NEW.tenant_id
                       AND release_id = NEW.release_id
                       AND subject_hmac = NEW.subject_hmac
                 )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'invalid survey eligibility grant generation'
                );
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_survey_eligibility_grant_reissue
            BEFORE INSERT ON survey_eligibility_grant
            FOR EACH ROW
            WHEN NEW.generation > 1 AND NOT EXISTS (
                SELECT 1
                  FROM survey_eligibility_grant previous_grant
                  LEFT JOIN survey_eligibility_terminal terminal
                    ON terminal.grant_id = previous_grant.id
                 WHERE previous_grant.tenant_id = NEW.tenant_id
                   AND previous_grant.release_id = NEW.release_id
                   AND previous_grant.subject_hmac = NEW.subject_hmac
                   AND previous_grant.generation = NEW.generation - 1
                   AND previous_grant.review_reference_hmac <>
                       NEW.review_reference_hmac
                   AND (
                       (terminal.id IS NULL
                        AND datetime(previous_grant.expires_at) <= datetime('now'))
                       OR
                       (terminal.disposition = 'revoked'
                        AND terminal.reason_code IN (
                            'administrative_revocation',
                            'credential_compromised',
                            'other_reviewed'
                        ))
                   )
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'survey eligibility grant reissue blocked'
                );
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_survey_eligibility_terminal_scope
            BEFORE INSERT ON survey_eligibility_terminal
            FOR EACH ROW
            WHEN NEW.disposition = 'redeemed' AND NOT EXISTS (
                SELECT 1
                  FROM enc_respuesta response
                  JOIN survey_eligibility_grant grant_row
                    ON grant_row.id = NEW.grant_id
                   AND grant_row.tenant_id = NEW.tenant_id
                   AND grant_row.survey_id = NEW.survey_id
                   AND grant_row.release_id = NEW.release_id
                   AND grant_row.eligibility_policy_version =
                       NEW.eligibility_policy_version
                 WHERE response.id = NEW.response_id
                   AND response.tenant_id = NEW.tenant_id
                   AND response.encuesta_id = NEW.survey_id
                   AND response.governance_release_id = NEW.release_id
                   AND response.governance_eligibility_policy_version =
                       NEW.eligibility_policy_version
                   AND response.eligibility_decision IS NOT NULL
                   AND response.eligibility_decision =
                       'verified_by_opaque_grant'
                   AND response.eligibility_contract_version IS NOT NULL
                   AND response.eligibility_contract_version =
                       'surveys.public_eligibility.v1'
                   AND response.eligibility_verified_at IS NOT NULL
                   AND response.eligibility_verified_at = NEW.created_at
                   AND datetime(grant_row.issued_at) <= datetime(NEW.created_at)
                   AND datetime(NEW.created_at) < datetime(grant_row.expires_at)
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'survey eligibility redemption response scope mismatch'
                );
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_enc_respuesta_eligibility_terminal_update
            BEFORE UPDATE ON enc_respuesta
            FOR EACH ROW
            WHEN EXISTS (
                SELECT 1
                  FROM survey_eligibility_terminal terminal
                 WHERE terminal.response_id = OLD.id
            ) AND NOT EXISTS (
                SELECT 1
                  FROM survey_eligibility_terminal terminal
                 WHERE terminal.response_id = OLD.id
                   AND NEW.tenant_id = terminal.tenant_id
                   AND NEW.encuesta_id = terminal.survey_id
                   AND NEW.governance_release_id = terminal.release_id
                   AND NEW.governance_eligibility_policy_version =
                       terminal.eligibility_policy_version
                   AND NEW.eligibility_decision =
                       'verified_by_opaque_grant'
                   AND NEW.eligibility_contract_version =
                       'surveys.public_eligibility.v1'
                   AND datetime(NEW.eligibility_verified_at) =
                       datetime(terminal.created_at)
            )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'survey eligibility response receipt is immutable'
                );
            END
            """
        )


def _drop_ledger_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_enc_respuesta_eligibility_terminal_update "
            "ON enc_respuesta"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS survey_eligibility_response_terminal_guard()"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_survey_eligibility_terminal_scope "
            "ON survey_eligibility_terminal"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS survey_eligibility_terminal_scope_guard()"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_survey_eligibility_grant_generation "
            "ON survey_eligibility_grant"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS survey_eligibility_grant_generation_guard()"
        )
        for table_name in ("survey_eligibility_terminal", "survey_eligibility_grant"):
            op.execute(
                f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable ON {table_name}"
            )
        op.execute("DROP FUNCTION IF EXISTS survey_eligibility_history_immutable()")
    elif dialect == "sqlite":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_enc_respuesta_eligibility_terminal_update"
        )
        op.execute("DROP TRIGGER IF EXISTS trg_survey_eligibility_terminal_scope")
        op.execute("DROP TRIGGER IF EXISTS trg_survey_eligibility_grant_reissue")
        op.execute("DROP TRIGGER IF EXISTS trg_survey_eligibility_grant_generation")
        op.execute("DROP TRIGGER IF EXISTS trg_survey_eligibility_grant_issued_at")
        for table_name in ("survey_eligibility_terminal", "survey_eligibility_grant"):
            for operation in ("delete", "update"):
                op.execute(
                    f"DROP TRIGGER IF EXISTS "
                    f"trg_{table_name}_{operation}_immutable"
                )


def upgrade() -> None:
    _create_governance_scope_uniques()

    op.add_column(
        "enc_respuesta",
        sa.Column("eligibility_contract_version", sa.String(length=48), nullable=True),
    )
    op.add_column(
        "enc_respuesta",
        sa.Column("eligibility_decision", sa.String(length=48), nullable=True),
    )
    op.add_column(
        "enc_respuesta",
        sa.Column("eligibility_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    _create_response_eligibility_guard()

    op.create_table(
        "survey_eligibility_grant",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("survey_id", sa.Integer(), nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("grant_ref", sa.String(length=64), nullable=False),
        sa.Column("eligibility_policy_version", sa.String(length=64), nullable=False),
        sa.Column("eligibility_mode", sa.String(length=32), nullable=False),
        sa.Column("subject_namespace", sa.String(length=64), nullable=False),
        sa.Column("subject_hmac", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("subject_key_version", sa.String(length=16), nullable=False),
        sa.Column("authority_namespace", sa.String(length=64), nullable=False),
        sa.Column("authority_adapter_version", sa.String(length=64), nullable=False),
        sa.Column("review_reference_hmac", sa.String(length=64), nullable=False),
        sa.Column("review_key_version", sa.String(length=16), nullable=False),
        sa.Column("credential_digest", sa.String(length=64), nullable=False),
        sa.Column("credential_key_version", sa.String(length=16), nullable=False),
        sa.Column("issued_by_user_id", sa.Integer(), nullable=False),
        sa.Column("issue_idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("issue_request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "issued_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "eligibility_mode IN ('institution_attested', 'manual_review')",
            name="ck_survey_eligibility_grant_mode",
        ),
        sa.CheckConstraint(
            "length(subject_namespace) BETWEEN 2 AND 64 "
            "AND subject_namespace = authority_namespace "
            "AND length(subject_hmac) = 64",
            name="ck_survey_eligibility_grant_subject_hmac",
        ),
        sa.CheckConstraint(
            "length(grant_ref) = 48 AND substr(grant_ref, 1, 5) = 'seg1_'",
            name="ck_survey_eligibility_grant_ref_format",
        ),
        sa.CheckConstraint(
            "length(subject_key_version) BETWEEN 2 AND 16 "
            "AND length(review_key_version) BETWEEN 2 AND 16 "
            "AND length(credential_key_version) BETWEEN 2 AND 16",
            name="ck_survey_eligibility_grant_key_versions",
        ),
        sa.CheckConstraint(
            "generation > 0",
            name="ck_survey_eligibility_grant_generation",
        ),
        sa.CheckConstraint(
            "length(authority_namespace) BETWEEN 2 AND 64 "
            "AND length(authority_adapter_version) BETWEEN 1 AND 64",
            name="ck_survey_eligibility_grant_authority",
        ),
        sa.CheckConstraint(
            "length(review_reference_hmac) = 64",
            name="ck_survey_eligibility_grant_review_hmac",
        ),
        sa.CheckConstraint(
            "length(credential_digest) = 64",
            name="ck_survey_eligibility_grant_credential_digest",
        ),
        sa.CheckConstraint(
            "length(issue_request_hash) = 64",
            name="ck_survey_eligibility_grant_request_hash",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name="ck_survey_eligibility_grant_expiry",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant_profile.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["issued_by_user_id"],
            ["user.id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "survey_id", "release_id", "eligibility_policy_version"],
            [
                "survey_governance_release.tenant_id",
                "survey_governance_release.survey_id",
                "survey_governance_release.id",
                "survey_governance_release.eligibility_policy_version",
            ],
            ondelete="RESTRICT",
            name="fk_survey_eligibility_grant_release_scope",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_survey_eligibility_grant_tenant_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "survey_id",
            "release_id",
            "id",
            name="uq_survey_eligibility_grant_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "survey_id",
            "release_id",
            "id",
            "eligibility_policy_version",
            name="uq_survey_eligibility_grant_scope_policy_id",
        ),
        sa.UniqueConstraint(
            "tenant_id", "grant_ref", name="uq_survey_eligibility_grant_ref"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "release_id",
            "subject_hmac",
            "generation",
            name="uq_survey_eligibility_grant_subject",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "release_id",
            "credential_digest",
            name="uq_survey_eligibility_grant_credential",
        ),
        sa.UniqueConstraint(
            "credential_key_version",
            "credential_digest",
            name="uq_survey_eligibility_grant_key_digest",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "issue_idempotency_key",
            name="uq_survey_eligibility_grant_issue_idem",
        ),
    )
    op.create_index(
        "ix_survey_eligibility_grant_scope_status",
        "survey_eligibility_grant",
        ["tenant_id", "survey_id", "release_id", "expires_at"],
        unique=False,
    )

    op.create_table(
        "survey_eligibility_terminal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("survey_id", sa.Integer(), nullable=False),
        sa.Column("release_id", sa.Integer(), nullable=False),
        sa.Column("grant_id", sa.Integer(), nullable=False),
        sa.Column("disposition", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("reason_code", sa.String(length=40), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("response_id", sa.Integer(), nullable=True),
        sa.Column("eligibility_policy_version", sa.String(length=64), nullable=False),
        sa.Column("submission_payload_hash", sa.String(length=64), nullable=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "(disposition = 'revoked' AND actor_user_id IS NOT NULL "
            "AND reason_code IS NOT NULL "
            f"AND reason_code IN ({_REVOCATION_REASONS_SQL}) "
            "AND idempotency_key IS NOT NULL AND response_id IS NULL "
            "AND submission_payload_hash IS NULL) OR "
            "(disposition = 'redeemed' AND actor_user_id IS NULL "
            "AND reason_code IS NULL AND idempotency_key IS NULL "
            "AND response_id IS NOT NULL AND submission_payload_hash IS NOT NULL "
            "AND length(submission_payload_hash) = 64)",
            name="ck_survey_eligibility_terminal_disposition",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_survey_eligibility_terminal_request_hash",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user.id"]),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "survey_id",
                "release_id",
                "grant_id",
                "eligibility_policy_version",
            ],
            [
                "survey_eligibility_grant.tenant_id",
                "survey_eligibility_grant.survey_id",
                "survey_eligibility_grant.release_id",
                "survey_eligibility_grant.id",
                "survey_eligibility_grant.eligibility_policy_version",
            ],
            ondelete="RESTRICT",
            name="fk_survey_eligibility_terminal_grant_scope",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "grant_id", name="uq_survey_eligibility_terminal_grant"
        ),
        sa.UniqueConstraint(
            "response_id", name="uq_survey_eligibility_terminal_response"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_survey_eligibility_terminal_idem",
        ),
    )
    op.create_index(
        "ix_survey_eligibility_terminal_scope",
        "survey_eligibility_terminal",
        ["tenant_id", "survey_id", "release_id", "disposition", "created_at"],
        unique=False,
    )
    _create_ledger_guards()


def downgrade() -> None:
    try:
        if context.is_offline_mode():
            raise RuntimeError(
                "offline downgrade is refused because eligibility history "
                "cannot be checked safely"
            )
    except (AttributeError, NameError):
        pass
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Keep the emptiness decision valid until the destructive DDL finishes.
        bind.execute(
            sa.text(
                "LOCK TABLE survey_eligibility_terminal, "
                "survey_eligibility_grant, enc_respuesta "
                "IN ACCESS EXCLUSIVE MODE"
            )
        )
    counts = bind.execute(
        sa.text(
            "SELECT "
            "(SELECT COUNT(*) FROM survey_eligibility_grant) + "
            "(SELECT COUNT(*) FROM survey_eligibility_terminal)"
        )
    ).scalar_one()
    response_markers = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM enc_respuesta WHERE "
            "eligibility_contract_version IS NOT NULL OR "
            "eligibility_decision IS NOT NULL OR "
            "eligibility_verified_at IS NOT NULL"
        )
    ).scalar_one()
    if counts or response_markers:
        raise RuntimeError(
            "survey eligibility history exists; downgrade would erase audit records"
        )

    _drop_ledger_guards()
    op.drop_index(
        "ix_survey_eligibility_terminal_scope",
        table_name="survey_eligibility_terminal",
    )
    op.drop_table("survey_eligibility_terminal")
    op.drop_index(
        "ix_survey_eligibility_grant_scope_status",
        table_name="survey_eligibility_grant",
    )
    op.drop_table("survey_eligibility_grant")

    _drop_response_eligibility_guard()
    op.drop_column("enc_respuesta", "eligibility_verified_at")
    op.drop_column("enc_respuesta", "eligibility_decision")
    op.drop_column("enc_respuesta", "eligibility_contract_version")
    _drop_governance_scope_uniques()
