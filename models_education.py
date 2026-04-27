from datetime import datetime, timezone

from database import db
from sqlalchemy import UniqueConstraint


def _utc_now():
    return datetime.now(timezone.utc)


class School(db.Model):
    __tablename__ = "edu_schools"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant_profile.id"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    school_type = db.Column(db.String(20), nullable=False, default="public")
    jurisdiction = db.Column(db.String(120), nullable=True)
    brand_name = db.Column(db.String(200), nullable=True)
    status = db.Column(db.String(30), nullable=False, default="active")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now)

    tenant = db.relationship("TenantProfile", backref=db.backref("schools", lazy="dynamic"))


class Campus(db.Model):
    __tablename__ = "edu_campuses"

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey("edu_schools.id"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    address = db.Column(db.String(500), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    timezone = db.Column(db.String(64), nullable=True)
    is_main = db.Column(db.Boolean, nullable=False, default=False)

    school = db.relationship("School", backref=db.backref("campuses", lazy="dynamic"))


class AcademicLevel(db.Model):
    __tablename__ = "edu_academic_levels"

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey("edu_schools.id"), nullable=False, index=True)
    code = db.Column(db.String(30), nullable=False)
    name = db.Column(db.String(100), nullable=False)

    school = db.relationship("School", backref=db.backref("academic_levels", lazy="dynamic"))

    __table_args__ = (UniqueConstraint("school_id", "code", name="uq_edu_academic_level_school_code"),)


class Shift(db.Model):
    __tablename__ = "edu_shifts"

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey("edu_schools.id"), nullable=False, index=True)
    code = db.Column(db.String(30), nullable=False)
    name = db.Column(db.String(100), nullable=False)

    school = db.relationship("School", backref=db.backref("shifts", lazy="dynamic"))

    __table_args__ = (UniqueConstraint("school_id", "code", name="uq_edu_shift_school_code"),)


class CourseSection(db.Model):
    __tablename__ = "edu_course_sections"

    id = db.Column(db.Integer, primary_key=True)
    campus_id = db.Column(db.Integer, db.ForeignKey("edu_campuses.id"), nullable=False, index=True)
    academic_year = db.Column(db.Integer, nullable=False)
    level_id = db.Column(db.Integer, db.ForeignKey("edu_academic_levels.id"), nullable=False, index=True)
    grade = db.Column(db.String(20), nullable=False)
    division = db.Column(db.String(20), nullable=False)
    shift_id = db.Column(db.Integer, db.ForeignKey("edu_shifts.id"), nullable=False, index=True)
    homeroom_staff_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    campus = db.relationship("Campus", backref=db.backref("sections", lazy="dynamic"))
    level = db.relationship("AcademicLevel")
    shift = db.relationship("Shift")

    __table_args__ = (
        UniqueConstraint(
            "campus_id",
            "academic_year",
            "level_id",
            "grade",
            "division",
            "shift_id",
            name="uq_edu_section_unique_slot",
        ),
    )


class Student(db.Model):
    __tablename__ = "edu_students"

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey("edu_schools.id"), nullable=False, index=True)
    campus_id = db.Column(db.Integer, db.ForeignKey("edu_campuses.id"), nullable=True, index=True)
    external_ref = db.Column(db.String(120), nullable=True)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    document_type = db.Column(db.String(30), nullable=True)
    document_number = db.Column(db.String(50), nullable=True, index=True)
    birth_date = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(30), nullable=False, default="active")
    section_id = db.Column(db.Integer, db.ForeignKey("edu_course_sections.id"), nullable=True, index=True)

    # legacy compatibility
    grade = db.Column(db.String(50), nullable=True)
    identifier = db.Column(db.String(100), nullable=True)

    school = db.relationship("School", backref=db.backref("students", lazy="dynamic"))
    campus = db.relationship("Campus")
    section = db.relationship("CourseSection")


class Guardian(db.Model):
    __tablename__ = "edu_guardians"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant_profile.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)

    # legacy compatibility
    school_id = db.Column(db.Integer, db.ForeignKey("edu_schools.id"), nullable=True, index=True)

    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    document_number = db.Column(db.String(50), nullable=True, index=True)
    email = db.Column(db.String(120), index=True)
    phone_number = db.Column(db.String(50), index=True)
    verification_status = db.Column(db.String(30), nullable=False, default="pending")
    verification_context = db.Column(db.JSON, nullable=True)
    preferred_channel = db.Column(db.String(30), nullable=True)
    language = db.Column(db.String(10), nullable=True, default="es")
    is_billing_contact = db.Column(db.Boolean, nullable=False, default=False)

    tenant = db.relationship("TenantProfile", backref=db.backref("guardians", lazy="dynamic"))
    user = db.relationship("User", backref=db.backref("guardian_profiles", lazy="dynamic"))
    school = db.relationship("School", backref=db.backref("legacy_guardians", lazy="dynamic"))


class StudentGuardianRelation(db.Model):
    __tablename__ = "edu_student_guardian_relations"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("edu_students.id"), nullable=False, index=True)
    guardian_id = db.Column(db.Integer, db.ForeignKey("edu_guardians.id"), nullable=False, index=True)
    relationship_type = db.Column(db.String(50), nullable=True)
    custody_scope = db.Column(db.String(50), nullable=True)
    can_pickup = db.Column(db.Boolean, nullable=False, default=False)
    can_receive_billing = db.Column(db.Boolean, nullable=False, default=False)
    can_receive_sensitive_updates = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(30), nullable=False, default="active")

    # legacy compatibility
    is_primary = db.Column(db.Boolean, nullable=False, default=False)

    student = db.relationship("Student", backref=db.backref("guardian_relations", lazy="dynamic"))
    guardian = db.relationship("Guardian", backref=db.backref("student_relations", lazy="dynamic"))

    __table_args__ = (UniqueConstraint("student_id", "guardian_id", name="uq_edu_student_guardian_relation"),)


class FamilyVerificationAttempt(db.Model):
    __tablename__ = "edu_family_verification_attempts"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant_profile.id"), nullable=False, index=True)
    guardian_id = db.Column(db.Integer, db.ForeignKey("edu_guardians.id"), nullable=True, index=True)
    verification_method = db.Column(db.String(40), nullable=False, default="phone")
    verification_value = db.Column(db.String(180), nullable=False)
    status = db.Column(db.String(30), nullable=False, default="failed")
    context_json = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utc_now)


class SchoolCaseAlias(db.Model):
    __tablename__ = "edu_school_case_aliases"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant_profile.id"), nullable=False, index=True)
    school_id = db.Column(db.Integer, db.ForeignKey("edu_schools.id"), nullable=False, index=True)
    campus_id = db.Column(db.Integer, db.ForeignKey("edu_campuses.id"), nullable=True, index=True)
    section_id = db.Column(db.Integer, db.ForeignKey("edu_course_sections.id"), nullable=True, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("edu_students.id"), nullable=True, index=True)
    guardian_id = db.Column(db.Integer, db.ForeignKey("edu_guardians.id"), nullable=True, index=True)
    case_type = db.Column(db.String(80), nullable=False, index=True)
    sensitivity_level = db.Column(db.String(30), nullable=False, default="internal")
    channel = db.Column(db.String(30), nullable=False, default="api")
    ticket_type = db.Column(db.String(20), nullable=False)
    ticket_id = db.Column(db.Integer, nullable=False, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utc_now)

    school = db.relationship("School")
    campus = db.relationship("Campus")
    section = db.relationship("CourseSection")
    student = db.relationship("Student")
    guardian = db.relationship("Guardian")

    __table_args__ = (
        UniqueConstraint("ticket_type", "ticket_id", name="uq_edu_school_case_ticket_ref"),
    )
