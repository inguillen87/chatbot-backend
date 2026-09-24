"""Independent CRM work items. Imported only when the task module is enabled."""
from database import db
from models import JSONType


class CrmTask(db.Model):
    __tablename__ = 'crm_task'
    id = db.Column(db.String(36), primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey('tenant_profile.id'), nullable=False)
    contact_id = db.Column(db.String(36), db.ForeignKey('contact.id'), nullable=False)
    title = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, nullable=False, default='')
    assignee_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    due_at = db.Column(db.DateTime(timezone=True), nullable=True)
    priority = db.Column(db.String(16), nullable=False, default='normal')
    status = db.Column(db.String(16), nullable=False, default='todo')
    revision = db.Column(db.Integer, nullable=False, default=1)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    updated_by = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False)
    __table_args__ = (
        db.UniqueConstraint('tenant_id', 'id', name='uq_crm_task_tenant_id'),
        db.CheckConstraint("status IN ('todo','in_progress','done','cancelled')", name='ck_crm_task_status'),
        db.CheckConstraint("priority IN ('normal','high','urgent')", name='ck_crm_task_priority'),
        db.CheckConstraint('revision > 0', name='ck_crm_task_revision'),
        db.Index('ix_crm_task_contact_created', 'tenant_id', 'contact_id', 'created_at', 'id'),
        db.Index('ix_crm_task_assignment_due', 'tenant_id', 'assignee_id', 'status', 'due_at'),
    )


class CrmTaskEvent(db.Model):
    __tablename__ = 'crm_task_event'
    id = db.Column(db.String(36), primary_key=True)
    tenant_id = db.Column(db.Integer, nullable=False)
    task_id = db.Column(db.String(36), nullable=False)
    task_revision = db.Column(db.Integer, nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    operation = db.Column(db.String(16), nullable=False)
    reason = db.Column(db.String(1200), nullable=False)
    key_hash = db.Column(db.String(64), nullable=False)
    request_hash = db.Column(db.String(64), nullable=False)
    before_state = db.Column(JSONType, nullable=True)
    after_state = db.Column(JSONType, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False)
    __table_args__ = (
        db.ForeignKeyConstraint(['tenant_id', 'task_id'], ['crm_task.tenant_id', 'crm_task.id'], name='fk_crm_task_event_task'),
        db.UniqueConstraint('tenant_id', 'actor_id', 'key_hash', name='uq_crm_task_event_request'),
        db.UniqueConstraint('tenant_id', 'task_id', 'task_revision', name='uq_crm_task_event_revision'),
        db.CheckConstraint("operation IN ('created','updated')", name='ck_crm_task_event_operation'),
        db.Index('ix_crm_task_event_history', 'tenant_id', 'task_id', 'task_revision'),
    )
