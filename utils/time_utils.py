from datetime import datetime, timedelta
import os

# Offset from UTC in hours. Defaults to Argentina time (GMT-3)
TIMEZONE_OFFSET = int(os.getenv("TIMEZONE_OFFSET", "-3"))


def get_local_now(offset_hours: int | None = None) -> datetime:
    """Return current datetime adjusted by the configured timezone offset."""
    if offset_hours is None:
        offset_hours = TIMEZONE_OFFSET
    return datetime.utcnow() + timedelta(hours=offset_hours)
