"""Versioned study declarations, separate from participant-facing resources."""
from database import db
from sqlalchemy.types import JSON


class SurveyMethodologyRevision(db.Model):
    __tablename__ = 'survey_methodology_revision'
    __table_args__ = (
        db.ForeignKeyConstraint(['tenant_id', 'survey_id'], ['enc_encuesta.tenant_id', 'enc_encuesta.id'],
                                name='fk_methodology_survey', ondelete='RESTRICT'),
        db.UniqueConstraint('tenant_id', 'survey_id', 'revision', name='uq_methodology_revision'),
        db.CheckConstraint('revision > 0 AND instrument_revision > 0', name='ck_methodology_revisions'),
        db.Index('ix_methodology_scope_revision', 'tenant_id', 'survey_id', 'revision'),
    )
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False)
    survey_id = db.Column(db.Integer, nullable=False)
    revision = db.Column(db.Integer, nullable=False)
    instrument_revision = db.Column(db.Integer, nullable=False)
    fields = db.Column(JSON, nullable=False)
    change_reason = db.Column(db.String(500), nullable=False)
    actor_user_id = db.Column(db.Integer, db.ForeignKey('user.id', ondelete='RESTRICT'), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False)
    digest = db.Column(db.String(64), nullable=False)
    previous_digest = db.Column(db.String(64), nullable=True)
    operation_digest = db.Column(db.String(64), nullable=False)
