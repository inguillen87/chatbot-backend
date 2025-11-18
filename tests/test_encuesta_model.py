from datetime import datetime, timedelta, timezone

import models
from models import EncEncuesta


def test_encuesta_activa_interprets_naive_schedule(monkeypatch):
    """Naive inicio/fin values should respect the configured timezone offset."""

    monkeypatch.setattr(
        models,
        "_PUBLIC_SURVEY_LOCAL_TZ",
        timezone(timedelta(hours=-3)),
        raising=False,
    )

    encuesta = EncEncuesta(estado="publicada")
    encuesta.inicio_at = datetime(2025, 11, 18, 0, 0, 0)
    encuesta.fin_at = datetime(2025, 11, 18, 23, 59, 0)

    start_utc = datetime(2025, 11, 18, 3, 0, 0, tzinfo=timezone.utc)
    end_utc = datetime(2025, 11, 19, 2, 59, 0, tzinfo=timezone.utc)

    assert not encuesta.esta_activa(at=start_utc - timedelta(seconds=1))
    assert encuesta.esta_activa(at=start_utc)
    assert encuesta.esta_activa(at=end_utc)
    assert not encuesta.esta_activa(at=end_utc + timedelta(seconds=1))
