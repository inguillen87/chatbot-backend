from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, JSON, Text, Boolean, Float, ForeignKey
from database import db

#
# PR-04: Moderation and Policy Models
#

class PolicySet(db.Model):
    __tablename__ = "policy_sets"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=True, index=True)
    name = Column(String(100), nullable=False)
    is_active = Column(Boolean, default=True)

class PolicyRule(db.Model):
    __tablename__ = "policy_rules"
    id = Column(Integer, primary_key=True)
    policy_set_id = Column(Integer, ForeignKey("policy_sets.id"), nullable=False)
    action = Column(String(50), nullable=False) # block, redact, allow, warn
    rule_type = Column(String(50), nullable=False) # regex, ai_classifier, rate_limit
    configuration = Column(JSON, nullable=True) # {"pattern": "badword"}
    target = Column(String(50), default="input") # input, output

class ModerationEvent(db.Model):
    __tablename__ = "moderation_events"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=True, index=True)
    channel = Column(String(50), nullable=False)
    action_taken = Column(String(50), nullable=False)
    rule_triggered = Column(Integer, ForeignKey("policy_rules.id"), nullable=True)
    input_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

#
# PR-05: Audit & Traceability Models
#

class AIRequestLog(db.Model):
    __tablename__ = "ai_request_logs"
    id = Column(Integer, primary_key=True)
    request_id = Column(String(64), unique=True, index=True, nullable=False)
    tenant_id = Column(Integer, nullable=True, index=True)
    channel = Column(String(50), nullable=False)
    actor_type = Column(String(50), nullable=False) # user, anon, agent, system
    actor_id = Column(String(100), nullable=True)
    session_id = Column(String(100), nullable=True)

    prompt_key = Column(String(100), nullable=True)
    prompt_version = Column(String(50), nullable=True)
    model = Column(String(100), nullable=False)

    latency_ms = Column(Integer, nullable=False)
    token_usage_prompt = Column(Integer, default=0)
    token_usage_completion = Column(Integer, default=0)

    decision_status = Column(String(50), nullable=False) # completed, refused, error
    prev_hash = Column(String(64), nullable=True)
    hash = Column(String(64), nullable=False) # Chained hash for auditability

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class AIToolCallLog(db.Model):
    __tablename__ = "ai_tool_call_logs"
    id = Column(Integer, primary_key=True)
    request_log_id = Column(Integer, ForeignKey("ai_request_logs.id"), nullable=False)
    tool_call_id = Column(String(100), nullable=False)
    tool_name = Column(String(100), nullable=False)
    arguments = Column(Text, nullable=True)
    result = Column(Text, nullable=True)
    latency_ms = Column(Integer, nullable=True)
    is_error = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
