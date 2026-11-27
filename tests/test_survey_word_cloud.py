from types import SimpleNamespace

from services import encuestas_analytics_service as analytics


def _response(question_id: int, *texts: str):
    return SimpleNamespace(
        detalles=[
            SimpleNamespace(pregunta_id=question_id, texto_libre=text)
            for text in texts
        ]
    )


def _survey(*, privacy_mode: str = "legacy", estado: str = "publicada"):
    return SimpleNamespace(privacy_mode=privacy_mode, estado=estado)


def test_word_cloud_normalizes_accents_and_removes_sensitive_or_unsafe_terms():
    terms = analytics._normalize_word_cloud_terms(
        "Gestión, GESTION y rápida; la mierda 2026 "
        "persona@example.test +54 9 236 412-3456 "
        "https://example.test/ruta otro.example/path 192.0.2.5/admin "
        f"@persona vos xy abc123 {'a' * 41}"
    )

    assert terms == {"gestion", "rapida"}


def test_word_cloud_counts_each_term_once_per_distinct_response_and_applies_threshold():
    cloud = analytics._build_word_cloud(
        encuesta=_survey(),
        pregunta_id=7,
        responses=[
            _response(7, "Gestión gestión clara", "gestión"),
            _response(7, "gestion útil"),
            _response(7, "gestión clara"),
        ],
        population_size=3,
    )

    assert cloud == {
        "terms": [{"term": "gestion", "count": 3}],
        "responses_considered": 3,
        "min_count": 3,
        "truncated": False,
    }


def test_word_cloud_caps_terms_and_orders_by_count_then_alphabetically():
    generated_terms = [
        f"termino{suffix}"
        for suffix in (
            "aaa", "aab", "aac", "aad", "aae", "aaf", "aag", "aah", "aai", "aaj",
            "aba", "abb", "abc", "abd", "abe", "abf", "abg", "abh", "abi", "abj",
            "aca", "acb", "acc", "acd", "ace", "acf", "acg", "ach", "aci", "acj",
            "ada", "adb", "adc", "add", "ade", "adf", "adg", "adh", "adi", "adj",
            "aea", "aeb", "aec", "aed", "aee", "aef", "aeg", "aeh", "aei", "aej",
            "afa", "afb", "afc", "afd", "afe", "aff", "afg", "afh", "afi", "afj",
            "aga",
        )
    ]
    text = " ".join(generated_terms)

    cloud = analytics._build_word_cloud(
        encuesta=_survey(),
        pregunta_id=9,
        responses=[_response(9, text), _response(9, text), _response(9, text)],
        population_size=3,
    )

    assert cloud is not None
    assert len(cloud["terms"]) == analytics.SURVEY_WORD_CLOUD_TERM_LIMIT
    assert [item["term"] for item in cloud["terms"]] == sorted(generated_terms)[:60]


def test_word_cloud_reports_bounded_response_reading():
    responses = [_response(11, "transporte") for _ in range(501)]

    cloud = analytics._build_word_cloud(
        encuesta=_survey(),
        pregunta_id=11,
        responses=responses,
        population_size=501,
    )

    assert cloud is not None
    assert cloud["responses_considered"] == 500
    assert cloud["terms"] == [{"term": "transporte", "count": 500}]
    assert cloud["truncated"] is True


def test_word_cloud_is_null_for_source_anonymous_small_or_active_cohorts():
    responses = [_response(13, "seguridad") for _ in range(5)]

    assert analytics._build_word_cloud(
        encuesta=_survey(privacy_mode="source_anonymous", estado="publicada"),
        pregunta_id=13,
        responses=responses,
        population_size=5,
    ) is None
    assert analytics._build_word_cloud(
        encuesta=_survey(privacy_mode="source_anonymous", estado="cerrada"),
        pregunta_id=13,
        responses=responses[:4],
        population_size=5,
    ) is None

    cloud = analytics._build_word_cloud(
        encuesta=_survey(privacy_mode="source_anonymous", estado="cerrada"),
        pregunta_id=13,
        responses=responses,
        population_size=5,
    )
    assert cloud == {
        "terms": [{"term": "seguridad", "count": 5}],
        "responses_considered": 5,
        "min_count": 5,
        "truncated": False,
    }
