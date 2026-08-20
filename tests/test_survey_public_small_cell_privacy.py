from copy import deepcopy

from services.encuestas_analytics_service import _apply_public_small_cell_policy


def _payload(*, total=6, option_counts=(6, 0), timeline_counts=(6,), cell_counts=(6,)):
    return {
        "contract_version": "surveys.live_results.v2",
        "result_version": 44,
        "snapshot_version": "9:6:44:filters",
        "encuesta_id": 9,
        "total_respuestas": total,
        "empty_state": {"is_empty": False, "title": "Resultados"},
        "preguntas": [
            {
                "id": 10,
                "total_votos": sum(option_counts),
                "opciones": [
                    {
                        "id": index + 1,
                        "label": f"Opcion {index + 1}",
                        "value": count,
                        "votos": count,
                        "porcentaje": 0.0,
                    }
                    for index, count in enumerate(option_counts)
                ],
            }
        ],
        "timeline_minute": [
            {"timestamp": f"2026-07-30T10:{index:02d}:00Z", "total": count}
            for index, count in enumerate(timeline_counts)
        ],
        "momentum": {
            "last_window": total,
            "previous_window": 0,
            "delta": total,
            "last_10m": total,
            "previous_10m": 0,
            "trend": "subiendo",
        },
        "heatmap": {
            "enabled": True,
            "points": [{"count": count} for count in cell_counts],
            "cells": [{"count": count} for count in cell_counts],
            "metadata": {
                "points_count": len(cell_counts),
                "cells_count": len(cell_counts),
                "raw_points_count": total,
            },
        },
        "live_telemetry": {
            "has_responses": total > 0,
            "responses_total": total,
            "responses_last_hour": total,
            "participation_per_minute": 0.1,
            "trend": "subiendo",
        },
        "kpis": {
            "responses_last_hour": total,
            "participation_per_minute": 0.1,
            "heatmap_coverage_cells": len(cell_counts),
            "leader": {"pregunta_id": 10},
            "leader_label": "Opcion 1",
        },
        "ai_summary": f"Hay {total} respuestas.",
        "ai_insights": [f"La opcion lider tiene {option_counts[0]} votos."],
        "ai_signal": {
            "mode": "local",
            "summary": {"text": "Resumen exacto"},
            "collection": {"item_count": 2},
            "recommended_actions": [{"id": "inspect"}],
        },
        "ai_layers": {"features": [{"count": cell_counts[0]}]},
        "operator_recommendations": [{"id": "inspect"}],
        "render_contract": {
            "supports": ["bars", "timeline", "heatmap", "csv_export"]
        },
        "ui_actions": [
            {"id": "refresh_live_results"},
            {"id": "export_live_csv"},
        ],
    }


def test_legacy_live_results_remain_compatible_and_mark_policy_disabled():
    original = _payload(total=2, option_counts=(1, 1), timeline_counts=(1,), cell_counts=(1,))

    result = _apply_public_small_cell_policy(
        deepcopy(original),
        privacy_mode="legacy",
        minimum_cell_size=5,
    )

    assert result["total_respuestas"] == 2
    assert result["preguntas"][0]["opciones"][0]["votos"] == 1
    assert result["timeline_minute"]
    assert result["heatmap"]["cells"]
    assert result["privacy"]["enabled"] is False
    assert result["privacy"]["suppressed_surfaces"] == []


def test_source_anonymous_cohort_below_k_hides_every_exact_public_surface():
    result = _apply_public_small_cell_policy(
        _payload(total=2, option_counts=(1, 1), timeline_counts=(1, 1), cell_counts=(1, 1)),
        privacy_mode="source_anonymous",
        minimum_cell_size=5,
        results_final=True,
    )

    assert result["total_respuestas"] is None
    assert result["total_respuestas_bucket"] == "<5"
    assert result["result_version"] is None
    assert result["snapshot_version"].startswith("private:")
    assert result["preguntas"][0]["total_votos"] is None
    assert all(
        option["votos"] is None and option["porcentaje"] is None
        for option in result["preguntas"][0]["opciones"]
    )
    assert result["timeline_minute"] == []
    assert result["heatmap"]["points"] == []
    assert result["heatmap"]["cells"] == []
    assert result["live_telemetry"]["responses_total"] is None
    assert result["kpis"]["leader"] is None
    assert result["ai_insights"] == []
    assert result["ai_signal"]["mode"] == "privacy_suppressed"
    assert "csv_export" not in result["render_contract"]["supports"]
    assert "export_live_csv" not in [item["id"] for item in result["ui_actions"]]
    assert result["privacy"]["cohort_size_disclosed"] is False
    assert result["privacy"]["reason_code"] == "minimum_cell_size_not_met"


def test_one_small_option_suppresses_entire_question_to_prevent_subtraction():
    result = _apply_public_small_cell_policy(
        _payload(total=6, option_counts=(5, 1), timeline_counts=(6,), cell_counts=(6,)),
        privacy_mode="source_anonymous",
        minimum_cell_size=5,
        results_final=True,
    )

    assert result["total_respuestas"] == 6
    assert result["preguntas"][0]["suppressed"] is True
    assert all(option["votos"] is None for option in result["preguntas"][0]["opciones"])
    assert result["heatmap"]["cells"] == [{"count": 6}]
    assert result["timeline_minute"][0]["total"] == 6
    assert result["kpis"]["leader"] is None
    assert result["privacy"]["cohort_size_disclosed"] is True
    assert result["privacy"]["suppressed_surfaces"] == ["question_results"]


def test_zero_cells_and_cells_at_k_are_safe_to_publish():
    result = _apply_public_small_cell_policy(
        _payload(total=5, option_counts=(5, 0), timeline_counts=(5,), cell_counts=(5,)),
        privacy_mode="source_anonymous",
        minimum_cell_size=5,
        results_final=True,
    )

    assert result["total_respuestas"] == 5
    assert result["preguntas"][0]["opciones"][0]["votos"] == 5
    assert result["preguntas"][0]["opciones"][1]["votos"] == 0
    assert result["timeline_minute"][0]["total"] == 5
    assert result["heatmap"]["cells"] == [{"count": 5}]
    assert result["privacy"]["detailed_results_suppressed"] is False


def test_one_small_map_cell_hides_whole_map_instead_of_leaking_by_subtraction():
    result = _apply_public_small_cell_policy(
        _payload(total=11, option_counts=(11, 0), timeline_counts=(11,), cell_counts=(7, 4)),
        privacy_mode="source_anonymous",
        minimum_cell_size=5,
        results_final=True,
    )

    assert result["total_respuestas"] is None
    assert result["total_respuestas_bucket"] == ">=5"
    assert result["preguntas"][0]["total_votos"] is None
    assert result["timeline_minute"] == []
    assert result["heatmap"]["points"] == []
    assert result["heatmap"]["cells"] == []
    assert "raw_points_count" not in result["heatmap"]["metadata"]
    assert result["kpis"]["heatmap_coverage_cells"] is None
    assert result["privacy"]["geo_remainder_protected"] is True
    assert set(result["privacy"]["suppressed_surfaces"]) >= {
        "cohort_total",
        "question_results",
        "timeline",
        "heatmap",
    }


def test_all_geo_cells_below_k_redact_exact_suppression_metadata():
    payload = _payload(
        total=10,
        option_counts=(10, 0),
        timeline_counts=(10,),
        cell_counts=(0,),
    )
    payload["heatmap"]["metadata"]["aggregation"] = {
        "cell_count": 0,
        "total_cell_count": 1,
        "geo_response_count": 4,
        "safe_cell_count": 0,
        "safe_response_count": 0,
        "suppressed_cell_count": 1,
        "suppressed_response_count": 4,
        "has_suppressed_cells": True,
        "partial": True,
    }

    result = _apply_public_small_cell_policy(
        payload,
        privacy_mode="source_anonymous",
        minimum_cell_size=5,
        results_final=True,
    )

    assert result["total_respuestas"] == 10
    assert result["heatmap"]["points"] == []
    assert result["heatmap"]["cells"] == []
    assert "raw_points_count" not in result["heatmap"]["metadata"]
    aggregation = result["heatmap"]["metadata"]["aggregation"]
    for key in (
        "cell_count",
        "total_cell_count",
        "geo_response_count",
        "safe_cell_count",
        "safe_response_count",
        "suppressed_cell_count",
        "suppressed_response_count",
    ):
        assert aggregation[key] is None
    assert aggregation["privacy_redacted"] is True
    assert "heatmap" in result["privacy"]["suppressed_surfaces"]


def test_active_source_anonymous_snapshots_never_release_rolling_results():
    selected = []
    for total, option_counts in ((0, (0, 0)), (4, (4, 0)), (5, (5, 0)), (7, (7, 0)), (8, (7, 1))):
        result = _apply_public_small_cell_policy(
            _payload(
                total=total,
                option_counts=option_counts,
                timeline_counts=(total,),
                cell_counts=(total,),
            ),
            privacy_mode="source_anonymous",
            minimum_cell_size=5,
            results_final=False,
        )
        selected.append(
            {
                "total": result["total_respuestas"],
                "bucket": result["total_respuestas_bucket"],
                "snapshot": result["snapshot_version"],
                "questions": result["preguntas"],
                "timeline": result["timeline_minute"],
                "heatmap": result["heatmap"]["cells"],
                "reason": result["privacy"]["reason_code"],
                "final": result["privacy"]["results_final"],
            }
        )

    assert all(item == selected[0] for item in selected[1:])
    assert selected[0]["total"] is None
    assert selected[0]["bucket"] == "withheld_until_close"
    assert selected[0]["reason"] == "source_anonymous_results_withheld_until_close"
    assert selected[0]["final"] is False
