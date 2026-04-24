import logging
from datetime import datetime, timezone, timedelta
from app import db
from models_analytics_k import AIUsageLog, CostRollupHourly, CostRollupDaily
from sqlalchemy import func

logger = logging.getLogger(__name__)

def perform_hourly_rollup(target_hour_override=None):
    """
    Rolls up AIUsageLogs into CostRollupHourly.
    Called periodically (e.g. via Celery beat or cron).
    """
    logger.info("Starting hourly rollup for AI usage logs.")
    try:
        if target_hour_override:
            target_hour = target_hour_override
        else:
            now = datetime.now(timezone.utc)
            target_hour = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

        end_hour = target_hour + timedelta(hours=1)

        results = db.session.query(
            AIUsageLog.tenant_id,
            AIUsageLog.channel,
            AIUsageLog.model,
            func.count(AIUsageLog.id).label('total_requests'),
            func.sum(AIUsageLog.input_tokens).label('total_input'),
            func.sum(AIUsageLog.output_tokens).label('total_output'),
            func.sum(AIUsageLog.estimated_cost).label('total_cost')
        ).filter(
            AIUsageLog.created_at >= target_hour,
            AIUsageLog.created_at < end_hour,
            AIUsageLog.tenant_id.isnot(None)
        ).group_by(
            AIUsageLog.tenant_id,
            AIUsageLog.channel,
            AIUsageLog.model
        ).all()

        for row in results:
            # Check if exists
            rollup = CostRollupHourly.query.filter_by(
                tenant_id=row.tenant_id,
                date_hour=target_hour,
                channel=row.channel,
                model=row.model
            ).first()

            if not rollup:
                rollup = CostRollupHourly(
                    tenant_id=row.tenant_id,
                    date_hour=target_hour,
                    channel=row.channel,
                    model=row.model
                )
                db.session.add(rollup)

            rollup.total_requests = row.total_requests
            rollup.total_input_tokens = row.total_input or 0
            rollup.total_output_tokens = row.total_output or 0
            rollup.total_cost = row.total_cost or 0.0

        db.session.commit()
        logger.info(f"Completed hourly rollup for {len(results)} groups.")
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error during hourly rollup: {e}")

def perform_daily_rollup(target_date_override=None):
    """
    Rolls up CostRollupHourly into CostRollupDaily.
    """
    logger.info("Starting daily rollup for AI usage logs.")
    try:
        if target_date_override:
            target_date = target_date_override
        else:
            now = datetime.now(timezone.utc)
            target_date = (now - timedelta(days=1)).date()

        # We aggregate from the hourly table
        results = db.session.query(
            CostRollupHourly.tenant_id,
            CostRollupHourly.channel,
            CostRollupHourly.model,
            func.sum(CostRollupHourly.total_requests).label('total_requests'),
            func.sum(CostRollupHourly.total_input_tokens).label('total_input'),
            func.sum(CostRollupHourly.total_output_tokens).label('total_output'),
            func.sum(CostRollupHourly.total_cost).label('total_cost')
        ).filter(
            func.date(CostRollupHourly.date_hour) == target_date
        ).group_by(
            CostRollupHourly.tenant_id,
            CostRollupHourly.channel,
            CostRollupHourly.model
        ).all()

        for row in results:
            rollup = CostRollupDaily.query.filter_by(
                tenant_id=row.tenant_id,
                date_day=target_date,
                channel=row.channel,
                model=row.model
            ).first()

            if not rollup:
                rollup = CostRollupDaily(
                    tenant_id=row.tenant_id,
                    date_day=target_date,
                    channel=row.channel,
                    model=row.model
                )
                db.session.add(rollup)

            rollup.total_requests = row.total_requests or 0
            rollup.total_input_tokens = row.total_input or 0
            rollup.total_output_tokens = row.total_output or 0
            rollup.total_cost = row.total_cost or 0.0

        db.session.commit()
        logger.info(f"Completed daily rollup for {len(results)} groups.")
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error during daily rollup: {e}")
