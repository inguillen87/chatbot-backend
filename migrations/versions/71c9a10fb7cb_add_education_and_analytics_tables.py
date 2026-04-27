"""add education and analytics tables

Revision ID: 71c9a10fb7cb
Revises: de53397ead1d
Create Date: 2026-04-25 23:44:20.309424

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '71c9a10fb7cb'
down_revision = 'de53397ead1d'
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = inspector.get_columns(table_name)
    return any(col["name"] == column_name for col in columns)


def upgrade():
    if not _has_column("tenant_profile", "vertical"):
        op.add_column("tenant_profile", sa.Column("vertical", sa.String(length=50), nullable=True))
    if not _has_column("tenant_profile", "subvertical"):
        op.add_column("tenant_profile", sa.Column("subvertical", sa.String(length=50), nullable=True))
    if not _has_column("tenant_profile", "capabilities_json"):
        op.add_column("tenant_profile", sa.Column("capabilities_json", sa.JSON(), nullable=True))

    if not _has_table("edu_schools"):
        op.create_table(
            "edu_schools",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant_profile.id"), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("school_type", sa.String(length=20), nullable=False, server_default="public"),
            sa.Column("jurisdiction", sa.String(length=120), nullable=True),
            sa.Column("brand_name", sa.String(length=200), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("ix_edu_schools_tenant_id", "edu_schools", ["tenant_id"])

    if not _has_table("edu_campuses"):
        op.create_table(
            "edu_campuses",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("school_id", sa.Integer(), sa.ForeignKey("edu_schools.id"), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("address", sa.String(length=500), nullable=True),
            sa.Column("phone", sa.String(length=50), nullable=True),
            sa.Column("email", sa.String(length=120), nullable=True),
            sa.Column("timezone", sa.String(length=64), nullable=True),
            sa.Column("is_main", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.create_index("ix_edu_campuses_school_id", "edu_campuses", ["school_id"])

    if not _has_table("edu_academic_levels"):
        op.create_table(
            "edu_academic_levels",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("school_id", sa.Integer(), sa.ForeignKey("edu_schools.id"), nullable=False),
            sa.Column("code", sa.String(length=30), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.UniqueConstraint("school_id", "code", name="uq_edu_academic_level_school_code"),
        )
        op.create_index("ix_edu_academic_levels_school_id", "edu_academic_levels", ["school_id"])

    if not _has_table("edu_shifts"):
        op.create_table(
            "edu_shifts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("school_id", sa.Integer(), sa.ForeignKey("edu_schools.id"), nullable=False),
            sa.Column("code", sa.String(length=30), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.UniqueConstraint("school_id", "code", name="uq_edu_shift_school_code"),
        )
        op.create_index("ix_edu_shifts_school_id", "edu_shifts", ["school_id"])

    if not _has_table("edu_course_sections"):
        op.create_table(
            "edu_course_sections",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("campus_id", sa.Integer(), sa.ForeignKey("edu_campuses.id"), nullable=False),
            sa.Column("academic_year", sa.Integer(), nullable=False),
            sa.Column("level_id", sa.Integer(), sa.ForeignKey("edu_academic_levels.id"), nullable=False),
            sa.Column("grade", sa.String(length=20), nullable=False),
            sa.Column("division", sa.String(length=20), nullable=False),
            sa.Column("shift_id", sa.Integer(), sa.ForeignKey("edu_shifts.id"), nullable=False),
            sa.Column("homeroom_staff_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
            sa.UniqueConstraint(
                "campus_id",
                "academic_year",
                "level_id",
                "grade",
                "division",
                "shift_id",
                name="uq_edu_section_unique_slot",
            ),
        )
        op.create_index("ix_edu_course_sections_campus_id", "edu_course_sections", ["campus_id"])
        op.create_index("ix_edu_course_sections_level_id", "edu_course_sections", ["level_id"])
        op.create_index("ix_edu_course_sections_shift_id", "edu_course_sections", ["shift_id"])

    if not _has_table("edu_students"):
        op.create_table(
            "edu_students",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("school_id", sa.Integer(), sa.ForeignKey("edu_schools.id"), nullable=False),
            sa.Column("campus_id", sa.Integer(), sa.ForeignKey("edu_campuses.id"), nullable=True),
            sa.Column("external_ref", sa.String(length=120), nullable=True),
            sa.Column("first_name", sa.String(length=100), nullable=False),
            sa.Column("last_name", sa.String(length=100), nullable=False),
            sa.Column("document_type", sa.String(length=30), nullable=True),
            sa.Column("document_number", sa.String(length=50), nullable=True),
            sa.Column("birth_date", sa.Date(), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
            sa.Column("section_id", sa.Integer(), sa.ForeignKey("edu_course_sections.id"), nullable=True),
            sa.Column("grade", sa.String(length=50), nullable=True),
            sa.Column("identifier", sa.String(length=100), nullable=True),
        )
        op.create_index("ix_edu_students_school_id", "edu_students", ["school_id"])
        op.create_index("ix_edu_students_campus_id", "edu_students", ["campus_id"])
        op.create_index("ix_edu_students_document_number", "edu_students", ["document_number"])
        op.create_index("ix_edu_students_section_id", "edu_students", ["section_id"])

    if not _has_table("edu_guardians"):
        op.create_table(
            "edu_guardians",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant_profile.id"), nullable=False),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
            sa.Column("school_id", sa.Integer(), sa.ForeignKey("edu_schools.id"), nullable=True),
            sa.Column("first_name", sa.String(length=100), nullable=False),
            sa.Column("last_name", sa.String(length=100), nullable=False),
            sa.Column("document_number", sa.String(length=50), nullable=True),
            sa.Column("email", sa.String(length=120), nullable=True),
            sa.Column("phone_number", sa.String(length=50), nullable=True),
            sa.Column("verification_status", sa.String(length=30), nullable=False, server_default="pending"),
            sa.Column("verification_context", sa.JSON(), nullable=True),
            sa.Column("preferred_channel", sa.String(length=30), nullable=True),
            sa.Column("language", sa.String(length=10), nullable=True),
            sa.Column("is_billing_contact", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.create_index("ix_edu_guardians_tenant_id", "edu_guardians", ["tenant_id"])
        op.create_index("ix_edu_guardians_user_id", "edu_guardians", ["user_id"])
        op.create_index("ix_edu_guardians_school_id", "edu_guardians", ["school_id"])
        op.create_index("ix_edu_guardians_document_number", "edu_guardians", ["document_number"])
        op.create_index("ix_edu_guardians_email", "edu_guardians", ["email"])
        op.create_index("ix_edu_guardians_phone_number", "edu_guardians", ["phone_number"])

    if not _has_table("edu_student_guardian_relations"):
        op.create_table(
            "edu_student_guardian_relations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("student_id", sa.Integer(), sa.ForeignKey("edu_students.id"), nullable=False),
            sa.Column("guardian_id", sa.Integer(), sa.ForeignKey("edu_guardians.id"), nullable=False),
            sa.Column("relationship_type", sa.String(length=50), nullable=True),
            sa.Column("custody_scope", sa.String(length=50), nullable=True),
            sa.Column("can_pickup", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("can_receive_billing", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("can_receive_sensitive_updates", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
            sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.UniqueConstraint("student_id", "guardian_id", name="uq_edu_student_guardian_relation"),
        )
        op.create_index("ix_edu_student_guardian_relations_student_id", "edu_student_guardian_relations", ["student_id"])
        op.create_index("ix_edu_student_guardian_relations_guardian_id", "edu_student_guardian_relations", ["guardian_id"])

    if not _has_table("edu_family_verification_attempts"):
        op.create_table(
            "edu_family_verification_attempts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant_profile.id"), nullable=False),
            sa.Column("guardian_id", sa.Integer(), sa.ForeignKey("edu_guardians.id"), nullable=True),
            sa.Column("verification_method", sa.String(length=40), nullable=False, server_default="phone"),
            sa.Column("verification_value", sa.String(length=180), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="failed"),
            sa.Column("context_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
        op.create_index("ix_edu_family_verification_attempts_tenant_id", "edu_family_verification_attempts", ["tenant_id"])
        op.create_index("ix_edu_family_verification_attempts_guardian_id", "edu_family_verification_attempts", ["guardian_id"])

    if not _has_table("edu_school_case_aliases"):
        op.create_table(
            "edu_school_case_aliases",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenant_profile.id"), nullable=False),
            sa.Column("school_id", sa.Integer(), sa.ForeignKey("edu_schools.id"), nullable=False),
            sa.Column("campus_id", sa.Integer(), sa.ForeignKey("edu_campuses.id"), nullable=True),
            sa.Column("section_id", sa.Integer(), sa.ForeignKey("edu_course_sections.id"), nullable=True),
            sa.Column("student_id", sa.Integer(), sa.ForeignKey("edu_students.id"), nullable=True),
            sa.Column("guardian_id", sa.Integer(), sa.ForeignKey("edu_guardians.id"), nullable=True),
            sa.Column("case_type", sa.String(length=80), nullable=False),
            sa.Column("sensitivity_level", sa.String(length=30), nullable=False, server_default="internal"),
            sa.Column("channel", sa.String(length=30), nullable=False, server_default="api"),
            sa.Column("ticket_type", sa.String(length=20), nullable=False),
            sa.Column("ticket_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.UniqueConstraint("ticket_type", "ticket_id", name="uq_edu_school_case_ticket_ref"),
        )
        op.create_index("ix_edu_school_case_aliases_tenant_id", "edu_school_case_aliases", ["tenant_id"])
        op.create_index("ix_edu_school_case_aliases_school_id", "edu_school_case_aliases", ["school_id"])
        op.create_index("ix_edu_school_case_aliases_campus_id", "edu_school_case_aliases", ["campus_id"])
        op.create_index("ix_edu_school_case_aliases_section_id", "edu_school_case_aliases", ["section_id"])
        op.create_index("ix_edu_school_case_aliases_student_id", "edu_school_case_aliases", ["student_id"])
        op.create_index("ix_edu_school_case_aliases_guardian_id", "edu_school_case_aliases", ["guardian_id"])
        op.create_index("ix_edu_school_case_aliases_case_type", "edu_school_case_aliases", ["case_type"])
        op.create_index("ix_edu_school_case_aliases_ticket_id", "edu_school_case_aliases", ["ticket_id"])


def downgrade():
    if _has_table("edu_school_case_aliases"):
        op.drop_table("edu_school_case_aliases")
    if _has_table("edu_family_verification_attempts"):
        op.drop_table("edu_family_verification_attempts")
    if _has_table("edu_student_guardian_relations"):
        op.drop_table("edu_student_guardian_relations")
    if _has_table("edu_guardians"):
        op.drop_table("edu_guardians")
    if _has_table("edu_students"):
        op.drop_table("edu_students")
    if _has_table("edu_course_sections"):
        op.drop_table("edu_course_sections")
    if _has_table("edu_shifts"):
        op.drop_table("edu_shifts")
    if _has_table("edu_academic_levels"):
        op.drop_table("edu_academic_levels")
    if _has_table("edu_campuses"):
        op.drop_table("edu_campuses")
    if _has_table("edu_schools"):
        op.drop_table("edu_schools")

    if _has_column("tenant_profile", "capabilities_json"):
        op.drop_column("tenant_profile", "capabilities_json")
    if _has_column("tenant_profile", "subvertical"):
        op.drop_column("tenant_profile", "subvertical")
    if _has_column("tenant_profile", "vertical"):
        op.drop_column("tenant_profile", "vertical")
