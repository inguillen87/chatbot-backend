from database import db
from datetime import datetime, timezone

class AIUsageLog(db.Model):
    __tablename__ = 'ai_usage_logs'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, index=True) # nullable for global/anonymous
    channel = db.Column(db.String(50), nullable=False, index=True) # web, widget, whatsapp, voice
    actor_type = db.Column(db.String(50), nullable=False) # citizen, agent, system
    actor_id = db.Column(db.String(100), index=True)
    request_id = db.Column(db.String(64), unique=True, nullable=False, index=True)

    model = db.Column(db.String(100), nullable=False)
    prompt_version = db.Column(db.String(50))
    tool_names = db.Column(db.String(255)) # comma-separated list of tools used

    input_tokens = db.Column(db.Integer, default=0)
    output_tokens = db.Column(db.Integer, default=0)
    cached_tokens = db.Column(db.Integer, default=0)

    latency_ms = db.Column(db.Integer, nullable=False)
    estimated_cost = db.Column(db.Float, default=0.0)

    status = db.Column(db.String(50), nullable=False) # completed, failed, throttled

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)

class TenantBudget(db.Model):
    __tablename__ = 'tenant_budgets'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, unique=True, nullable=False)
    monthly_budget = db.Column(db.Float, nullable=False)
    soft_limit_threshold = db.Column(db.Float, default=0.8) # 80%
    hard_limit_threshold = db.Column(db.Float, default=1.0) # 100%

    current_spend = db.Column(db.Float, default=0.0)
    last_alert_sent = db.Column(db.DateTime)

    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class CostRollupHourly(db.Model):
    __tablename__ = 'cost_rollups_hourly'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, index=True, nullable=False)
    date_hour = db.Column(db.DateTime, index=True, nullable=False) # Truncated to hour
    channel = db.Column(db.String(50), nullable=False)
    model = db.Column(db.String(100), nullable=False)

    total_requests = db.Column(db.Integer, default=0)
    total_input_tokens = db.Column(db.Integer, default=0)
    total_output_tokens = db.Column(db.Integer, default=0)
    total_cost = db.Column(db.Float, default=0.0)

    db.UniqueConstraint('tenant_id', 'date_hour', 'channel', 'model', name='uix_hourly_rollup')

class CostRollupDaily(db.Model):
    __tablename__ = 'cost_rollups_daily'

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, index=True, nullable=False)
    date_day = db.Column(db.Date, index=True, nullable=False) # Truncated to day
    channel = db.Column(db.String(50), nullable=False)
    model = db.Column(db.String(100), nullable=False)

    total_requests = db.Column(db.Integer, default=0)
    total_input_tokens = db.Column(db.Integer, default=0)
    total_output_tokens = db.Column(db.Integer, default=0)
    total_cost = db.Column(db.Float, default=0.0)

    db.UniqueConstraint('tenant_id', 'date_day', 'channel', 'model', name='uix_daily_rollup')
