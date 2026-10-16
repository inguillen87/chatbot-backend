from __future__ import annotations

import json
import re
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any


CONTRACT_VERSION = "chatboc.qa_preview_institutional_surveys.v1"
TARGET_USER_EMAIL = "mauricio@junin.com"
TARGET_TENANT_SLUG = "junin"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "encuestas_bootstrap"
    / "qa_preview_institutional_surveys.v1.json"
)
_SLUG_PATTERN = re.compile(r"^demo-gobierno-junin-[a-z0-9]+(?:-[a-z0-9]+)*$")


class InstitutionalSurveyManifestError(ValueError):
    """Stable, non-secret validation error for the versioned demo manifest."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _require(condition: bool, reason_code: str) -> None:
    if not condition:
        raise InstitutionalSurveyManifestError(reason_code)


def _validate_manifest(raw: Any) -> dict[str, Any]:
    _require(isinstance(raw, dict), "manifest_not_object")
    _require(raw.get("contract_version") == CONTRACT_VERSION, "manifest_contract_invalid")
    _require(
        str(raw.get("target_user_email") or "").strip().lower() == TARGET_USER_EMAIL,
        "manifest_target_user_invalid",
    )
    _require(
        set(raw.get("allowed_runtime_environments") or []) == {"qa", "preview"},
        "manifest_environment_scope_invalid",
    )

    initial_policy = raw.get("initial_response_policy") or {}
    _require(initial_policy.get("count") == 0, "manifest_initial_responses_not_zero")
    _require(
        initial_policy.get("synthetic_seed_enabled") is False,
        "manifest_persisted_synthetic_seed_enabled",
    )
    _require(initial_policy.get("municipal_truth") is False, "manifest_truth_label_invalid")

    mirror = raw.get("demo_mirror_policy") or {}
    _require(mirror.get("tenant_slug") == TARGET_TENANT_SLUG, "manifest_mirror_tenant_invalid")
    _require(mirror.get("sector") == "gobierno", "manifest_mirror_sector_invalid")
    _require(mirror.get("response_mode") == "synthetic", "manifest_mirror_mode_invalid")
    _require(int(mirror.get("response_count") or 0) > 0, "manifest_mirror_count_invalid")
    _require(mirror.get("municipal_truth") is False, "manifest_mirror_truth_invalid")
    _require(mirror.get("official") is False, "manifest_mirror_official_invalid")

    privacy = raw.get("privacy") or {}
    _require(privacy.get("mode") == "source_anonymous", "manifest_privacy_mode_invalid")
    _require(bool(privacy.get("policy_version")), "manifest_privacy_version_missing")
    _require(bool(privacy.get("policy_url")), "manifest_privacy_url_missing")
    _require(privacy.get("consent_required") is True, "manifest_privacy_consent_invalid")
    _require(
        isinstance(privacy.get("retention_days"), int)
        and 1 <= int(privacy["retention_days"]) <= 3650,
        "manifest_privacy_retention_invalid",
    )

    surveys = raw.get("surveys")
    _require(isinstance(surveys, list) and len(surveys) == 3, "manifest_survey_count_invalid")
    slugs: set[str] = set()
    keys: set[str] = set()
    titles: set[str] = set()
    for survey_index, survey in enumerate(surveys):
        _require(isinstance(survey, dict), f"manifest_survey_{survey_index}_invalid")
        key = str(survey.get("key") or "").strip()
        slug = str(survey.get("slug") or "").strip().lower()
        title = str(survey.get("titulo") or "").strip()
        description = str(survey.get("descripcion") or "").strip()
        _require(bool(key) and key not in keys, f"manifest_survey_{survey_index}_key_invalid")
        _require(bool(_SLUG_PATTERN.fullmatch(slug)), f"manifest_survey_{survey_index}_slug_invalid")
        _require(slug not in slugs, f"manifest_survey_{survey_index}_slug_duplicate")
        _require(title not in titles, f"manifest_survey_{survey_index}_title_duplicate")
        _require(title.startswith("Junín "), f"manifest_survey_{survey_index}_title_scope_invalid")
        _require("(DEMO NO OFICIAL)" in title, f"manifest_survey_{survey_index}_label_missing")
        _require(
            description.startswith("DEMO NO OFICIAL") and "cero respuestas" in description,
            f"manifest_survey_{survey_index}_description_invalid",
        )
        _require(bool(survey.get("document_ref")), f"manifest_survey_{survey_index}_document_ref_missing")
        _require(
            bool(survey.get("content_origin_ref")),
            f"manifest_survey_{survey_index}_origin_ref_missing",
        )
        survey_type = str(survey.get("tipo") or "").strip().lower()
        _require(survey_type in {"encuesta", "votacion"}, "manifest_survey_type_invalid")
        expected_live_vote = survey_type == "votacion"
        _require(
            survey.get("es_votacion_envivo") is expected_live_vote,
            "manifest_survey_live_vote_invalid",
        )
        _require(
            survey.get("mostrar_resultados_envivo") is expected_live_vote,
            "manifest_survey_live_results_invalid",
        )

        questions = survey.get("preguntas")
        _require(
            isinstance(questions, list) and len(questions) >= 1,
            f"manifest_survey_{survey_index}_questions_invalid",
        )
        question_refs: set[str] = set()
        for question_index, question in enumerate(questions):
            _require(isinstance(question, dict), "manifest_question_invalid")
            question_ref = str(question.get("question_ref") or "").strip()
            _require(
                bool(question_ref) and question_ref not in question_refs,
                "manifest_question_ref_invalid",
            )
            _require(question.get("tipo") == "opcion_unica", "manifest_question_type_invalid")
            _require(bool(str(question.get("texto") or "").strip()), "manifest_question_text_missing")
            options = question.get("opciones")
            _require(
                isinstance(options, list) and 2 <= len(options) <= 8,
                "manifest_question_options_invalid",
            )
            option_refs: set[str] = set()
            option_values: set[str] = set()
            for option in options:
                _require(isinstance(option, dict), "manifest_option_invalid")
                option_ref = str(option.get("option_ref") or "").strip()
                option_value = str(option.get("valor") or "").strip()
                _require(
                    bool(option_ref) and option_ref not in option_refs,
                    "manifest_option_ref_invalid",
                )
                _require(
                    bool(option_value) and option_value not in option_values,
                    "manifest_option_value_invalid",
                )
                _require(bool(str(option.get("texto") or "").strip()), "manifest_option_text_missing")
                option_refs.add(option_ref)
                option_values.add(option_value)
            question_refs.add(question_ref)
        keys.add(key)
        slugs.add(slug)
        titles.add(title)
    return raw


@lru_cache(maxsize=1)
def _load_cached() -> dict[str, Any]:
    try:
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstitutionalSurveyManifestError("manifest_unreadable") from exc
    return _validate_manifest(raw)


def load_institutional_demo_survey_manifest() -> dict[str, Any]:
    """Return an isolated copy so callers cannot mutate the cached contract."""

    return deepcopy(_load_cached())


def build_persisted_survey_payload(
    survey: dict[str, Any],
    *,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = manifest or load_institutional_demo_survey_manifest()
    privacy = source["privacy"]
    return {
        "document_ref": survey["document_ref"],
        "slug": survey["slug"],
        "titulo": survey["titulo"],
        "descripcion": survey["descripcion"],
        "tipo": survey["tipo"],
        "requiere_identidad": False,
        "politica_unicidad": "libre",
        "anonimo_permitido": True,
        "privacy_mode": privacy["mode"],
        "privacy_policy_version": privacy["policy_version"],
        "privacy_policy_url": privacy["policy_url"],
        "privacy_consent_required": privacy["consent_required"],
        "response_retention_days": privacy["retention_days"],
        "es_votacion_envivo": survey["es_votacion_envivo"],
        "mostrar_resultados_envivo": survey["mostrar_resultados_envivo"],
        "permitir_comentarios": False,
        "puntos_recompensa": 0,
        "tags": list(survey.get("tags") or []),
        "preguntas": deepcopy(survey["preguntas"]),
    }


def build_junin_demo_templates() -> list[dict[str, Any]]:
    """Project the same slugs/titles into the explicitly synthetic chat demo."""

    manifest = load_institutional_demo_survey_manifest()
    label = manifest["demo_mirror_policy"]["label"]
    templates: list[dict[str, Any]] = []
    for survey in manifest["surveys"]:
        first_question = survey["preguntas"][0]
        templates.append(
            {
                "id": survey["slug"].removeprefix("demo-gobierno-junin-"),
                "slug": survey["slug"],
                "tenant_slug": TARGET_TENANT_SLUG,
                "tipo": survey["tipo"],
                "titulo": survey["titulo"],
                "descripcion": survey["descripcion"],
                "pregunta": first_question["texto"],
                "opciones": [option["texto"] for option in first_question["opciones"]],
                "institutional_demo": True,
                "official": False,
                "municipal_truth": False,
                "data_mode": "synthetic_demo_scenario",
                "content_origin": "seed_demo",
                "content_origin_ref": survey["content_origin_ref"],
                "disclaimer": label,
            }
        )
    return templates


__all__ = [
    "CONTRACT_VERSION",
    "InstitutionalSurveyManifestError",
    "MANIFEST_PATH",
    "TARGET_TENANT_SLUG",
    "TARGET_USER_EMAIL",
    "build_junin_demo_templates",
    "build_persisted_survey_payload",
    "load_institutional_demo_survey_manifest",
]
