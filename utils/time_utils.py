from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ARG_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def get_local_now(offset_hours: int | None = None) -> datetime:
    """Return current datetime in Argentina's timezone (UTC-3)."""
    now = datetime.now(ARG_TZ)
    if offset_hours:
        now += timedelta(hours=offset_hours)
    return now
