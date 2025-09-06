from datetime import datetime, timezone


def get_local_now(offset_hours: int | None = None) -> datetime:
    """Return current UTC datetime. Timezone conversions are handled in the app."""
    return datetime.now(timezone.utc)
