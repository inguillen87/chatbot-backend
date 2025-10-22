from __future__ import annotations

from datetime import datetime, timezone


def get_local_now(offset_hours: int | None = None) -> datetime:
    """Return current UTC datetime. Timezone conversions are handled in the app."""
    return datetime.now(timezone.utc)


def datetime_to_iso_utc(dt: datetime | None) -> str | None:
    """Return an ISO-8601 string in UTC with millisecond precision.

    Many frontend environments (in particular older mobile WebViews and some
    PWA runtimes) only support JavaScript's simplified ISO-8601 grammar which
    requires a timezone designator and limits the fractional seconds to three
    digits.  Python's ``datetime.isoformat`` emits microseconds by default and
    omits the ``Z`` suffix when the value is naive, which was causing the
    mobile dashboard to fail while parsing ticket timestamps.

    The helper normalises any ``datetime`` to UTC, trims the precision to
    milliseconds and always appends the ``Z`` timezone marker so the output is
    compatible with the browser parser.
    """

    if dt is None:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    # ``timespec='milliseconds'`` trims microseconds without rounding issues.
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
