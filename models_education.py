from app import db
from datetime import datetime, timezone
import uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON

# Reusing JSON type abstraction logic
JSONType = JSONB().with_variant(JSON(), "sqlite")

class School(db.Model):
    __tablename__ = 'edu_school'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenant_profile.id'), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    school_type = db.Column(db.String(50), nullable=False, default="private") # public | private
    jurisdiction = db.Column(db.String(100), nullable=True)
    brand_name = db.Column(db.String(255), nullable=True)
    status = db.Column(db.String(50), default="active")

    tenant = db.relationship('TenantProfile', backref=db.backref('schools', lazy='dynamic'))

class Campus(db.Model):
    __tablename__ = 'edu_campus'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('edu_school.id'), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    address = db.Column(db.String(512), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    email = db.Column(db.String(255), nullable=True)
    timezone = db.Column(db.String(50), default="America/Argentina/Mendoza")
    is_main = db.Column(db.Boolean, default=False)

    school = db.relationship('School', backref=db.backref('campuses', lazy='dynamic'))

class AcademicLevel(db.Model):
    __tablename__ = 'edu_academic_level'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('edu_school.id'), nullable=False, index=True)
    code = db.Column(db.String(50), nullable=False) # inicial | primaria | secundaria | superior
    name = db.Column(db.String(100), nullable=False)

    school = db.relationship('School')

class Shift(db.Model):
    __tablename__ = 'edu_shift'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('edu_school.id'), nullable=False, index=True)
    code = db.Column(db.String(50), nullable=False) # manana | tarde | vespertino | jornada_completa
    name = db.Column(db.String(100), nullable=False)

    school = db.relationship('School')

class CourseSection(db.Model):
    __tablename__ = 'edu_course_section'

    id = db.Column(db.Integer, primary_key=True)
    campus_id = db.Column(db.Integer, db.ForeignKey('edu_campus.id'), nullable=False)
    level_id = db.Column(db.Integer, db.ForeignKey('edu_academic_level.id'), nullable=False)
    shift_id = db.Column(db.Integer, db.ForeignKey('edu_shift.id'), nullable=False)

    academic_year = db.Column(db.String(20), nullable=False)
    grade = db.Column(db.String(50), nullable=False) # e.g. "1ro", "2do"
    division = db.Column(db.String(50), nullable=False) # e.g. "A", "B"
    homeroom_staff_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    campus = db.relationship('Campus')
    level = db.relationship('AcademicLevel')
    shift = db.relationship('Shift')
    homeroom_staff = db.relationship('User')

class Student(db.Model):
    __tablename__ = 'edu_student'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('edu_school.id'), nullable=False, index=True)
    campus_id = db.Column(db.Integer, db.ForeignKey('edu_campus.id'), nullable=True)
    section_id = db.Column(db.Integer, db.ForeignKey('edu_course_section.id'), nullable=True)

    external_ref = db.Column(db.String(100), nullable=True, index=True)
    first_name = db.Column(db.String(255), nullable=False)
    last_name = db.Column(db.String(255), nullable=False)
    document_type = db.Column(db.String(20), default="DNI")
    document_number = db.Column(db.String(50), nullable=False, index=True)
    birth_date = db.Column(db.Date, nullable=True)
    status = db.Column(db.String(50), default="active") # active, inactive, graduated

    school = db.relationship('School')

class Guardian(db.Model):
    __tablename__ = 'edu_guardian'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenant_profile.id'), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)

    first_name = db.Column(db.String(255), nullable=False)
    last_name = db.Column(db.String(255), nullable=False)
    document_number = db.Column(db.String(50), nullable=False, index=True)
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(50), nullable=True, index=True)

    verification_status = db.Column(db.String(50), default="pending") # pending, verified, rejected
    preferred_channel = db.Column(db.String(50), default="whatsapp")
    language = db.Column(db.String(10), default="es")
    is_billing_contact = db.Column(db.Boolean, default=False)

    tenant = db.relationship('TenantProfile')
    user = db.relationship('User')

class StudentGuardianRelation(db.Model):
    __tablename__ = 'edu_student_guardian'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('edu_student.id'), nullable=False, index=True)
    guardian_id = db.Column(db.Integer, db.ForeignKey('edu_guardian.id'), nullable=False, index=True)

    relationship_type = db.Column(db.String(50), nullable=False) # madre, padre, tutor, otro
    custody_scope = db.Column(db.String(50), default="full") # full, partial, none
    can_pickup = db.Column(db.Boolean, default=False)
    can_receive_billing = db.Column(db.Boolean, default=False)
    can_receive_sensitive_updates = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(50), default="active")

    student = db.relationship('Student', backref=db.backref('guardians', lazy='dynamic'))
    guardian = db.relationship('Guardian', backref=db.backref('students', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('student_id', 'guardian_id', name='uq_student_guardian'),
    )
