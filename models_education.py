from database import db
from datetime import datetime, timezone

class School(db.Model):
    __tablename__ = 'edu_schools'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenant_profile.id'), nullable=False, unique=True)
    name = db.Column(db.String(200), nullable=False)

    tenant = db.relationship("TenantProfile", backref=db.backref("school", uselist=False))

class Campus(db.Model):
    __tablename__ = 'edu_campuses'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('edu_schools.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    address = db.Column(db.String(500))

    school = db.relationship("School", backref="campuses")

class Student(db.Model):
    __tablename__ = 'edu_students'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('edu_schools.id'), nullable=False)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    grade = db.Column(db.String(50))
    identifier = db.Column(db.String(100)) # e.g. student ID number

    school = db.relationship("School", backref="students")

class Guardian(db.Model):
    __tablename__ = 'edu_guardians'

    id = db.Column(db.Integer, primary_key=True)
    school_id = db.Column(db.Integer, db.ForeignKey('edu_schools.id'), nullable=False)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), index=True)
    phone_number = db.Column(db.String(50), index=True)

    school = db.relationship("School", backref="guardians")

class StudentGuardianRelation(db.Model):
    __tablename__ = 'edu_student_guardian_relations'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('edu_students.id'), nullable=False)
    guardian_id = db.Column(db.Integer, db.ForeignKey('edu_guardians.id'), nullable=False)
    relationship_type = db.Column(db.String(50)) # e.g. Mother, Father, Tutor
    is_primary = db.Column(db.Boolean, default=False)

    student = db.relationship("Student", backref="guardian_relations")
    guardian = db.relationship("Guardian", backref="student_relations")
