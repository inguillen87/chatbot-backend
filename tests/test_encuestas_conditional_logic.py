from __future__ import annotations

from copy import deepcopy

import pytest

from database import db
from models import EncEncuesta, EncRespuesta, EncRespuestaDetalle
from routes.v2.surveys import _normalize_question
from services.encuestas_service import (
    EncuestaError,
    create_encuesta,
    duplicate_encuesta,
    publicar_encuesta,
    save_respuesta,
    seed_encuesta_respuestas_demo,
    serialize_encuesta,
    serialize_public_encuesta,
    update_encuesta,
    _validate_instrument_payload,
)
from services.encuestas_analytics_service import get_summary


class _User:
    def __init__(self, tenant_id: int = 4):
        self.id = None
        self.municipio_id = tenant_id


def _rule(question_order: int, option_order: int) -> dict:
    return {
        "version": 1,
        "show_if": {
            "question_order": question_order,
            "option_order": option_order,
        },
    }


def _branching_payload(title: str = "Entrevista adaptativa") -> dict:
    return {
        "titulo": title,
        "politica_unicidad": "libre",
        "preguntas": [
            {
                "orden": 1,
                "tipo": "opcion_unica",
                "texto": "Queres profundizar?",
                "obligatoria": True,
                "opciones": [
                    {"orden": 1, "texto": "Si"},
                    {"orden": 2, "texto": "No"},
                ],
            },
            {
                "orden": 2,
                "tipo": "opcion_unica",
                "texto": "Que tema?",
                "obligatoria": True,
                "opciones": [
                    {"orden": 1, "texto": "Obras"},
                    {"orden": 2, "texto": "Servicios"},
                ],
                "conditional_logic": _rule(1, 1),
            },
            {
                "orden": 3,
                "tipo": "abierta",
                "texto": "Contanos mas",
                "obligatoria": True,
                "conditional_logic": _rule(2, 1),
            },
        ],
    }


def _publish(payload: dict):
    user = _User()
    encuesta = create_encuesta(payload, user)
    encuesta, link = publicar_encuesta(encuesta.id, user)
    return db.session.get(EncEncuesta, encuesta.id), link.slug_publico, user


def _ctx(anon_id: str) -> dict:
    return {
        "ip": "127.0.0.1",
        "user_agent": "pytest",
        "anon_id": anon_id,
        "canal": "web",
    }


def _answer(question, *, option_order: int | None = None, text: str | None = None) -> dict:
    if option_order is not None:
        option = next(item for item in question.opciones if item.orden == option_order)
        return {"pregunta_id": question.id, "opcion_id": option.id}
    return {"pregunta_id": question.id, "texto_libre": text}


def _v2_ref_rule(question_ref: str, option_ref: str) -> dict:
    return {
        "version": 2,
        "show_if": {
            "kind": "group",
            "operator": "and",
            "children": [
                {
                    "kind": "option_selected",
                    "question_ref": question_ref,
                    "option_ref": option_ref,
                }
            ],
        },
    }


def test_conditional_logic_round_trips_create_update_duplicate_publish_and_serialize(client):
    with client.application.app_context():
        user = _User()
        encuesta = create_encuesta(_branching_payload(), user)

        stored_rule = encuesta.preguntas[1].logica_condicional
        assert stored_rule == _rule(1, 1)
        assert encuesta.preguntas[0].logica_condicional is None
        assert serialize_encuesta(encuesta)["preguntas"][1]["conditional_logic"] == _rule(1, 1)

        updated_payload = _branching_payload("Entrevista adaptativa actualizada")
        updated_payload["preguntas"][1]["conditional_logic"] = _rule(1, 2)
        encuesta = update_encuesta(encuesta.id, updated_payload, user)
        assert encuesta.preguntas[1].logica_condicional == _rule(1, 2)

        duplicate = duplicate_encuesta(encuesta.id, {}, user)
        assert duplicate.preguntas[1].logica_condicional == _rule(1, 2)

        encuesta, link = publicar_encuesta(encuesta.id, user)
        public_payload = serialize_public_encuesta(encuesta, link.slug_publico)
        assert public_payload["preguntas"][1]["conditional_logic"] == _rule(1, 2)


def test_analytics_completion_uses_only_required_questions_on_each_visible_v2_branch(client):
    with client.application.app_context():
        payload = {
            "titulo": "Completitud por ruta visible",
            "politica_unicidad": "libre",
            "preguntas": [
                {
                    "question_ref": "question-controller",
                    "orden": 1,
                    "tipo": "opcion_unica",
                    "texto": "Que ruta queres responder?",
                    "obligatoria": True,
                    "opciones": [
                        {
                            "option_ref": "option-route-a",
                            "orden": 1,
                            "texto": "Ruta A",
                        },
                        {
                            "option_ref": "option-route-b",
                            "orden": 2,
                            "texto": "Ruta B",
                        },
                    ],
                },
                {
                    "question_ref": "question-route-a-detail",
                    "orden": 2,
                    "tipo": "abierta",
                    "texto": "Detalle A",
                    "obligatoria": True,
                    "conditional_logic": _v2_ref_rule(
                        "question-controller", "option-route-a"
                    ),
                },
                {
                    "question_ref": "question-route-b-detail",
                    "orden": 3,
                    "tipo": "abierta",
                    "texto": "Detalle B",
                    "obligatoria": True,
                    "conditional_logic": _v2_ref_rule(
                        "question-controller", "option-route-b"
                    ),
                },
            ],
        }
        encuesta, slug, _ = _publish(payload)
        controller, route_a, route_b = encuesta.preguntas

        save_respuesta(
            slug,
            {
                "respuestas": [
                    _answer(controller, option_order=1),
                    _answer(route_a, text="Respuesta completa por A"),
                ]
            },
            _ctx("analytics-route-a"),
        )
        save_respuesta(
            slug,
            {
                "respuestas": [
                    _answer(controller, option_order=2),
                    _answer(route_b, text="Respuesta completa por B"),
                ]
            },
            _ctx("analytics-route-b"),
        )

        summary = get_summary(encuesta.id)

        assert summary["total_respuestas"] == 2
        assert summary["respuestas_completas"] == 2
        assert summary["respuestas_incompletas"] == 0
        assert summary["tasa_completitud"] == 100.0


def test_analytics_exposes_visible_and_answered_denominators_without_changing_legacy_percentages(client):
    with client.application.app_context():
        payload = {
            "titulo": "Denominadores por ruta visible",
            "politica_unicidad": "libre",
            "preguntas": [
                {
                    "question_ref": "question-route",
                    "orden": 1,
                    "tipo": "opcion_unica",
                    "texto": "Que ruta queres responder?",
                    "obligatoria": True,
                    "opciones": [
                        {
                            "option_ref": "option-route-a",
                            "orden": 1,
                            "texto": "Ruta A",
                        },
                        {
                            "option_ref": "option-route-b",
                            "orden": 2,
                            "texto": "Ruta B",
                        },
                        {
                            "option_ref": "option-route-c",
                            "orden": 3,
                            "texto": "Ruta C",
                        },
                    ],
                },
                {
                    "question_ref": "question-route-a",
                    "orden": 2,
                    "tipo": "opcion_multiple",
                    "texto": "Seleccion multiple A",
                    "obligatoria": True,
                    "min_selecciones": 1,
                    "max_selecciones": 2,
                    "opciones": [
                        {
                            "option_ref": "option-a-one",
                            "orden": 1,
                            "texto": "A uno",
                        },
                        {
                            "option_ref": "option-a-two",
                            "orden": 2,
                            "texto": "A dos",
                        },
                    ],
                    "conditional_logic": _v2_ref_rule(
                        "question-route", "option-route-a"
                    ),
                },
                {
                    "question_ref": "question-route-b",
                    "orden": 3,
                    "tipo": "opcion_unica",
                    "texto": "Seleccion B",
                    "obligatoria": True,
                    "opciones": [
                        {
                            "option_ref": "option-b-one",
                            "orden": 1,
                            "texto": "B uno",
                        },
                        {
                            "option_ref": "option-b-two",
                            "orden": 2,
                            "texto": "B dos",
                        },
                    ],
                    "conditional_logic": _v2_ref_rule(
                        "question-route", "option-route-b"
                    ),
                },
                {
                    "question_ref": "question-optional",
                    "orden": 4,
                    "tipo": "abierta",
                    "texto": "Comentario opcional visible",
                    "obligatoria": False,
                },
                {
                    "question_ref": "question-never-visible",
                    "orden": 5,
                    "tipo": "opcion_unica",
                    "texto": "Seleccion nunca visible",
                    "obligatoria": False,
                    "opciones": [
                        {
                            "option_ref": "option-never-one",
                            "orden": 1,
                            "texto": "Nunca uno",
                        },
                        {
                            "option_ref": "option-never-two",
                            "orden": 2,
                            "texto": "Nunca dos",
                        },
                    ],
                    "conditional_logic": _v2_ref_rule(
                        "question-route", "option-route-c"
                    ),
                },
            ],
        }
        encuesta, slug, _ = _publish(payload)
        controller, route_a, route_b, optional, never_visible = encuesta.preguntas
        route_a_option_ids = [option.id for option in route_a.opciones]

        response_a = save_respuesta(
            slug,
            {
                "respuestas": [
                    _answer(controller, option_order=1),
                    {
                        "pregunta_id": route_a.id,
                        "opcion_ids": route_a_option_ids,
                    },
                    {
                        "pregunta_id": optional.id,
                        "texto_libre": "   ",
                    },
                ]
            },
            _ctx("analytics-denominators-route-a"),
        )
        assert any(
            detail.pregunta_id == optional.id and detail.texto_libre is None
            for detail in response_a.detalles
        )
        save_respuesta(
            slug,
            {
                "respuestas": [
                    _answer(controller, option_order=2),
                    _answer(route_b, option_order=1),
                ]
            },
            _ctx("analytics-denominators-route-b"),
        )

        # Simulate a historical/corrupt duplicate detail. Legacy row-based
        # counters remain unchanged, while the new rates count a respondent
        # only once per option.
        db.session.add(
            EncRespuestaDetalle(
                respuesta_id=response_a.id,
                pregunta_id=route_a.id,
                opcion_id=route_a_option_ids[0],
            )
        )
        db.session.commit()

        summary = get_summary(encuesta.id)
        questions = {
            item["pregunta_id"]: item for item in summary["preguntas"]
        }

        assert summary["total_respuestas"] == 2
        assert {
            item["total_respuestas"] for item in summary["preguntas"]
        } == {2}

        assert questions[controller.id]["respuestas_elegibles"] == 2
        assert questions[controller.id]["respuestas_respondidas"] == 2
        assert questions[controller.id]["tasa_respuesta_elegible"] == 100.0
        controller_options = questions[controller.id]["opciones"]
        assert [option["porcentaje"] for option in controller_options] == [
            50.0,
            50.0,
            0,
        ]
        assert [
            option["porcentaje_total_encuesta"]
            for option in controller_options
        ] == [50.0, 50.0, 0]

        assert questions[route_a.id]["respuestas_elegibles"] == 1
        assert questions[route_a.id]["respuestas_respondidas"] == 1
        assert questions[route_a.id]["tasa_respuesta_elegible"] == 100.0
        assert questions[route_b.id]["respuestas_elegibles"] == 1
        assert questions[route_b.id]["respuestas_respondidas"] == 1
        assert questions[route_b.id]["tasa_respuesta_elegible"] == 100.0

        assert questions[optional.id]["respuestas_elegibles"] == 2
        assert questions[optional.id]["respuestas_respondidas"] == 0
        assert questions[optional.id]["tasa_respuesta_elegible"] == 0

        assert questions[never_visible.id]["respuestas_elegibles"] == 0
        assert questions[never_visible.id]["respuestas_respondidas"] == 0
        assert questions[never_visible.id]["tasa_respuesta_elegible"] == 0
        for option in questions[never_visible.id]["opciones"]:
            assert option["respuestas_seleccionaron"] == 0
            assert option["porcentaje_total_encuesta"] == 0
            assert option["porcentaje_elegibles"] == 0
            assert option["porcentaje_respuestas_pregunta"] == 0

        route_a_options = questions[route_a.id]["opciones"]
        assert [option["conteo"] for option in route_a_options] == [2, 1]
        assert [option["value"] for option in route_a_options] == [2, 1]
        assert [option["porcentaje"] for option in route_a_options] == [100.0, 50.0]
        assert questions[route_a.id]["series"] == [
            {"label": "A uno", "value": 2},
            {"label": "A dos", "value": 1},
        ]
        for option in route_a_options:
            assert option["respuestas_seleccionaron"] == 1
            assert option["porcentaje_total_encuesta"] == 50.0
            assert option["porcentaje_elegibles"] == 100.0
            assert option["porcentaje_respuestas_pregunta"] == 100.0

        # Multiple-choice percentages are selection rates, not a partition;
        # their sum can legitimately exceed 100 percent.
        assert sum(
            option["porcentaje_respuestas_pregunta"]
            for option in route_a_options
        ) == 200.0


def test_v2_question_normalization_preserves_conditional_contract_and_locked_ids():
    normalized = _normalize_question(
        {
            "id": 42,
            "order_index": 2,
            "type": "single_choice",
            "label": "Tema",
            "conditional_logic": _rule(1, 1),
        },
        2,
    )
    assert normalized["id"] == 42
    assert normalized["conditional_logic"] == _rule(1, 1)

    # An invalid empty object must reach service validation instead of being
    # silently converted to an unconditional question by truthiness fallback.
    invalid = _normalize_question({"conditional_logic": {}}, 1)
    assert invalid["conditional_logic"] == {}


def test_null_and_absent_conditional_logic_are_unconditional(client):
    with client.application.app_context():
        payload = _branching_payload("Sin ramas")
        payload["preguntas"][1]["conditional_logic"] = None
        payload["preguntas"][2].pop("conditional_logic")
        encuesta = create_encuesta(payload, _User())
        assert [question.logica_condicional for question in encuesta.preguntas] == [None, None, None]


@pytest.mark.parametrize(
    "invalid_rule",
    [
        [],
        {},
        {"version": 1, "show_if": {"question_order": 1, "option_order": 1}, "extra": True},
        {"version": True, "show_if": {"question_order": 1, "option_order": 1}},
        {"version": "1", "show_if": {"question_order": 1, "option_order": 1}},
        {"version": 2, "show_if": {"question_order": 1, "option_order": 1}},
        {"version": 1, "show_if": []},
        {"version": 1, "show_if": {"question_order": 1}},
        {"version": 1, "show_if": {"question_order": 1, "option_order": 1, "extra": 2}},
        {"version": 1, "show_if": {"question_order": True, "option_order": 1}},
        {"version": 1, "show_if": {"question_order": 1, "option_order": False}},
        {"version": 1, "show_if": {"question_order": 0, "option_order": 1}},
    ],
)
def test_conditional_logic_schema_is_exact_and_strict(client, invalid_rule):
    with client.application.app_context():
        payload = _branching_payload("Contrato invalido")
        payload["preguntas"][1]["conditional_logic"] = invalid_rule

        with pytest.raises(EncuestaError) as exc_info:
            create_encuesta(payload, _User())

        error = exc_info.value
        assert error.status_code == 400
        assert error.payload["reason_code"] == "survey_conditional_logic_invalid"
        assert isinstance(error.payload.get("context"), dict)


def _instrument_case(case: str) -> dict:
    payload = _branching_payload(f"Instrumento invalido {case}")
    questions = payload["preguntas"]

    if case == "duplicate_question_order":
        questions[1]["orden"] = 1
    elif case == "duplicate_option_order":
        questions[0]["opciones"][1]["orden"] = 1
    elif case == "boolean_question_order":
        questions[1]["orden"] = True
    elif case == "boolean_option_order":
        questions[0]["opciones"][0]["orden"] = True
    elif case == "first_conditional":
        questions[0]["conditional_logic"] = _rule(2, 1)
    elif case == "missing_source":
        questions[1]["conditional_logic"] = _rule(99, 1)
    elif case == "later_source":
        questions[1]["conditional_logic"] = _rule(3, 1)
        questions[2]["tipo"] = "opcion_unica"
        questions[2]["opciones"] = [{"orden": 1, "texto": "A"}]
    elif case == "open_source":
        questions[0]["tipo"] = "abierta"
        questions[0]["opciones"] = []
        questions[1]["conditional_logic"] = _rule(1, 1)
    elif case == "missing_source_option":
        questions[1]["conditional_logic"] = _rule(1, 99)
    elif case == "cycle":
        questions[1]["conditional_logic"] = _rule(3, 1)
        questions[2]["tipo"] = "opcion_unica"
        questions[2]["opciones"] = [{"orden": 1, "texto": "A"}]
        questions[2]["conditional_logic"] = _rule(2, 1)
    elif case == "rating_source":
        questions[0]["tipo"] = "rating_emoji"
        questions[1]["conditional_logic"] = _rule(1, 1)
    else:  # pragma: no cover - keeps future additions explicit
        raise AssertionError(case)
    return payload


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_question_order",
        "duplicate_option_order",
        "boolean_question_order",
        "boolean_option_order",
        "first_conditional",
        "missing_source",
        "later_source",
        "open_source",
        "missing_source_option",
        "cycle",
        "rating_source",
    ],
)
def test_conditional_logic_validates_the_complete_instrument(client, case):
    with client.application.app_context():
        with pytest.raises(EncuestaError) as exc_info:
            create_encuesta(_instrument_case(case), _User())

        assert exc_info.value.status_code == 400
        assert exc_info.value.payload["reason_code"] == "survey_conditional_logic_invalid"


def test_large_backward_only_instrument_is_validated_without_recursion():
    questions = []
    for order in range(1200, 0, -1):
        question = {
            "orden": order,
            "tipo": "opcion_unica",
            "texto": f"Pregunta {order}",
            "opciones": [{"orden": 1, "texto": "Continuar"}],
        }
        if order > 1:
            question["conditional_logic"] = _rule(order - 1, 1)
        questions.append(question)

    normalized = _validate_instrument_payload(questions)
    assert len(normalized) == 1200
    assert normalized[0]["orden"] == 1200
    assert normalized[-1]["orden"] == 1

    for source_order in (2, 3):
        invalid = [
            {
                "orden": 1,
                "tipo": "opcion_unica",
                "texto": "Primera",
                "opciones": [{"orden": 1, "texto": "Si"}],
            },
            {
                "orden": 2,
                "tipo": "opcion_unica",
                "texto": "Segunda",
                "opciones": [{"orden": 1, "texto": "Si"}],
                "conditional_logic": _rule(source_order, 1),
            },
        ]
        if source_order == 3:
            invalid.append(
                {
                    "orden": 3,
                    "tipo": "opcion_unica",
                    "texto": "Tercera",
                    "opciones": [{"orden": 1, "texto": "Si"}],
                }
            )
        with pytest.raises(EncuestaError) as exc_info:
            _validate_instrument_payload(invalid)
        assert exc_info.value.payload["reason_code"] == "survey_conditional_logic_invalid"
        assert exc_info.value.payload["source_question_order"] == source_order


def test_conditional_response_accepts_only_the_visible_branch(client):
    with client.application.app_context():
        encuesta, slug, _ = _publish(_branching_payload())
        first, second, third = encuesta.preguntas

        hidden_branch = save_respuesta(
            slug,
            {"respuestas": [_answer(first, option_order=2)]},
            _ctx("hidden-branch"),
        )
        assert len(hidden_branch.detalles) == 1

        second_only_branch = save_respuesta(
            slug,
            {
                "answeredQuestions": 999,
                "totalQuestions": 0,
                "respuestas": [
                    _answer(first, option_order=1),
                    _answer(second, option_order=2),
                ],
            },
            _ctx("second-branch"),
        )
        assert len(second_only_branch.detalles) == 2

        full_branch = save_respuesta(
            slug,
            {
                "answeredQuestions": -50,
                "totalQuestions": 999,
                "respuestas": [
                    _answer(first, option_order=1),
                    _answer(second, option_order=1),
                    _answer(third, text="Detalle validado"),
                ],
            },
            _ctx("full-branch"),
        )
        assert len(full_branch.detalles) == 3
        assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 3


def test_multiple_choice_question_can_drive_conditional_visibility(client):
    with client.application.app_context():
        payload = {
            "titulo": "Fuente multiple",
            "politica_unicidad": "libre",
            "preguntas": [
                {
                    "orden": 1,
                    "tipo": "opcion_multiple",
                    "texto": "Temas",
                    "obligatoria": True,
                    "min_selecciones": 1,
                    "max_selecciones": 2,
                    "opciones": [
                        {"orden": 1, "texto": "Obras"},
                        {"orden": 2, "texto": "Servicios"},
                    ],
                },
                {
                    "orden": 2,
                    "tipo": "abierta",
                    "texto": "Detalle de servicios",
                    "obligatoria": True,
                    "conditional_logic": _rule(1, 2),
                },
            ],
        }
        encuesta, slug, _ = _publish(payload)
        source, child = encuesta.preguntas
        source_options = sorted(source.opciones, key=lambda option: option.orden)

        hidden = save_respuesta(
            slug,
            {"respuestas": [{"pregunta_id": source.id, "opcion_ids": [source_options[0].id]}]},
            _ctx("multi-hidden"),
        )
        assert len(hidden.detalles) == 1

        visible = save_respuesta(
            slug,
            {
                "respuestas": [
                    {
                        "pregunta_id": source.id,
                        "opcion_ids": [source_options[0].id, source_options[1].id],
                    },
                    _answer(child, text="Detalle"),
                ]
            },
            _ctx("multi-visible"),
        )
        assert len(visible.detalles) == 3


def test_hidden_answer_and_missing_visible_required_are_rejected_without_persisting(client):
    with client.application.app_context():
        encuesta, slug, _ = _publish(_branching_payload())
        first, second, third = encuesta.preguntas

        with pytest.raises(EncuestaError) as hidden_error:
            save_respuesta(
                slug,
                {
                    "respuestas": [
                        _answer(first, option_order=2),
                        _answer(second, option_order=1),
                    ]
                },
                _ctx("hidden-answer"),
            )
        assert hidden_error.value.payload["reason_code"] == "hidden_question_answered"
        assert hidden_error.value.payload["question_order"] == 2
        assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 0

        with pytest.raises(EncuestaError) as missing_second:
            save_respuesta(
                slug,
                {"respuestas": [_answer(first, option_order=1)]},
                _ctx("missing-second"),
            )
        assert missing_second.value.payload["reason_code"] == "required_visible_question_missing"
        assert missing_second.value.payload["question_order"] == 2
        assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 0

        with pytest.raises(EncuestaError) as missing_third:
            save_respuesta(
                slug,
                {
                    "respuestas": [
                        _answer(first, option_order=1),
                        _answer(second, option_order=1),
                    ]
                },
                _ctx("missing-third"),
            )
        assert missing_third.value.payload["reason_code"] == "required_visible_question_missing"
        assert missing_third.value.payload["question_order"] == 3
        assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 0


def _locked_update_payload(encuesta: EncEncuesta) -> dict:
    questions = []
    for question in encuesta.preguntas:
        questions.append(
            {
                "id": question.id,
                "orden": question.orden,
                "tipo": question.tipo,
                "texto": f"{question.texto} editada",
                "obligatoria": question.obligatoria,
                "min_selecciones": question.min_selecciones,
                "max_selecciones": question.max_selecciones,
                "conditional_logic": deepcopy(question.logica_condicional),
                "opciones": [
                    {
                        "id": option.id,
                        "orden": option.orden,
                        "texto": f"{option.texto} editada",
                        "valor": option.valor,
                    }
                    for option in question.opciones
                ],
            }
        )
    return {"preguntas": questions}


def test_structure_lock_preserves_conditional_semantics_after_responses(client):
    with client.application.app_context():
        encuesta, slug, user = _publish(_branching_payload())
        first, second, third = encuesta.preguntas
        save_respuesta(
            slug,
            {
                "respuestas": [
                    _answer(first, option_order=1),
                    _answer(second, option_order=1),
                    _answer(third, text="Persistida"),
                ]
            },
            _ctx("structure-lock"),
        )

        same_structure = _locked_update_payload(encuesta)
        updated = update_encuesta(encuesta.id, same_structure, user)
        assert updated.preguntas[1].logica_condicional == _rule(1, 1)
        assert updated.preguntas[0].texto.endswith("editada")

        changed_logic = _locked_update_payload(updated)
        changed_logic["preguntas"][1]["conditional_logic"] = _rule(1, 2)
        with pytest.raises(EncuestaError) as exc_info:
            update_encuesta(updated.id, changed_logic, user)
        assert exc_info.value.status_code == 409
        assert exc_info.value.payload["reason_code"] == "survey_structure_locked"
        db.session.rollback()
        db.session.expire_all()
        assert db.session.get(EncEncuesta, updated.id).preguntas[1].logica_condicional == _rule(1, 1)


def _instrument_snapshot(encuesta: EncEncuesta) -> dict:
    return {
        "titulo": encuesta.titulo,
        "slug": encuesta.slug,
        "descripcion": encuesta.descripcion,
        "preguntas": [
            {
                "id": question.id,
                "orden": question.orden,
                "tipo": question.tipo,
                "texto": question.texto,
                "obligatoria": question.obligatoria,
                "conditional_logic": deepcopy(question.logica_condicional),
                "opciones": [
                    {
                        "id": option.id,
                        "orden": option.orden,
                        "texto": option.texto,
                        "valor": option.valor,
                    }
                    for option in question.opciones
                ],
            }
            for question in encuesta.preguntas
        ],
    }


def test_invalid_update_is_atomic_even_if_caller_commits_after_error(client):
    with client.application.app_context():
        user = _User()
        encuesta = create_encuesta(_branching_payload("Original atomica"), user)
        before = _instrument_snapshot(encuesta)

        invalid_update = _branching_payload("Titulo que no debe persistir")
        invalid_update["slug"] = "slug-que-no-debe-persistir"
        invalid_update["descripcion"] = "Descripcion que no debe persistir"
        invalid_update["preguntas"][1]["conditional_logic"] = {
            **_rule(1, 1),
            "unexpected": True,
        }

        with pytest.raises(EncuestaError) as exc_info:
            update_encuesta(encuesta.id, invalid_update, user)
        assert exc_info.value.payload["reason_code"] == "survey_conditional_logic_invalid"

        # This intentionally commits instead of rolling back: validation must
        # have happened before title/slug mutation and clear()+flush().
        db.session.commit()
        db.session.expire_all()
        persisted = db.session.get(EncEncuesta, encuesta.id)
        assert _instrument_snapshot(persisted) == before


def test_publish_revalidates_persisted_conditional_instrument(client):
    with client.application.app_context():
        user = _User()
        payload = _branching_payload("Revalidacion al publicar")
        encuesta = create_encuesta(payload, user)
        encuesta.preguntas[1].logica_condicional = _rule(1, 999)
        db.session.commit()

        with pytest.raises(EncuestaError) as exc_info:
            publicar_encuesta(encuesta.id, user)

        assert exc_info.value.status_code == 400
        assert exc_info.value.payload["reason_code"] == "survey_conditional_logic_invalid"
        db.session.refresh(encuesta)
        assert encuesta.estado == "borrador"


def test_rating_emoji_persists_as_single_selection_but_cannot_drive_a_branch(client):
    with client.application.app_context():
        payload = {
            "titulo": "Rating de entrevista",
            "politica_unicidad": "libre",
            "preguntas": [
                {
                    "orden": 1,
                    "tipo": "rating_emoji",
                    "texto": "Como te sentiste?",
                    "obligatoria": True,
                    "opciones": [
                        {"orden": 1, "texto": "Mal"},
                        {"orden": 2, "texto": "Bien"},
                    ],
                }
            ],
        }
        encuesta, slug, _ = _publish(payload)
        question = encuesta.preguntas[0]
        response = save_respuesta(
            slug,
            {"respuestas": [_answer(question, option_order=2)]},
            _ctx("rating"),
        )
        assert len(response.detalles) == 1
        assert response.detalles[0].opcion_id == question.opciones[1].id

        with pytest.raises(EncuestaError):
            save_respuesta(
                slug,
                {
                    "respuestas": [
                        {
                            "pregunta_id": question.id,
                            "opcion_ids": [option.id for option in question.opciones],
                        }
                    ]
                },
                _ctx("rating-multiple"),
            )
        assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 1

        invalid_source = deepcopy(payload)
        invalid_source["titulo"] = "Rating como fuente invalida"
        invalid_source["preguntas"].append(
            {
                "orden": 2,
                "tipo": "abierta",
                "texto": "Explica",
                "conditional_logic": _rule(1, 2),
            }
        )
        with pytest.raises(EncuestaError) as exc_info:
            create_encuesta(invalid_source, _User())
        assert exc_info.value.payload["reason_code"] == "survey_conditional_logic_invalid"

        impossible_rating = deepcopy(payload)
        impossible_rating["titulo"] = "Rating imposible"
        impossible_rating["preguntas"][0]["min_selecciones"] = 2
        with pytest.raises(EncuestaError) as bounds_error:
            create_encuesta(impossible_rating, _User())
        assert bounds_error.value.payload["reason_code"] == "survey_conditional_logic_invalid"
        assert bounds_error.value.payload["field"] == "selection_bounds"


def test_legacy_unconditional_survey_with_historical_noncanonical_order_still_responds(client):
    with client.application.app_context():
        payload = {
            "titulo": "Encuesta legacy",
            "politica_unicidad": "libre",
            "preguntas": [
                {
                    "orden": 1,
                    "tipo": "opcion_unica",
                    "texto": "Continua funcionando?",
                    "obligatoria": True,
                    "opciones": [{"orden": 1, "texto": "Si"}],
                }
            ],
        }
        encuesta, slug, user = _publish(payload)
        question = encuesta.preguntas[0]

        # Emulate an old row created before the v1 instrument invariants.
        question.orden = 0
        db.session.commit()
        publicar_encuesta(encuesta.id, user)

        response = save_respuesta(
            slug,
            {"respuestas": [_answer(question, option_order=1)]},
            _ctx("legacy-order"),
        )
        assert len(response.detalles) == 1
        assert response.detalles[0].pregunta_id == question.id

        duplicate = duplicate_encuesta(encuesta.id, {}, user)
        assert duplicate.preguntas[0].orden == 0
        assert duplicate.preguntas[0].logica_condicional is None


def test_demo_seed_follows_each_conditional_route(client):
    with client.application.app_context():
        payload = _branching_payload("Seed adaptativo")
        encuesta = create_encuesta(payload, _User())
        result = seed_encuesta_respuestas_demo(encuesta.id, _User(), cantidad=24, seed=17)

        assert result["creadas"] == 24
        first, second, third = encuesta.preguntas
        first_yes_id = next(option.id for option in first.opciones if option.orden == 1)
        second_works_id = next(option.id for option in second.opciones if option.orden == 1)
        saw_hidden_second = False
        saw_visible_second = False
        saw_hidden_third = False
        saw_visible_third = False

        for response in EncRespuesta.query.filter_by(encuesta_id=encuesta.id).all():
            option_ids = {detail.opcion_id for detail in response.detalles if detail.opcion_id}
            question_ids = {detail.pregunta_id for detail in response.detalles}
            second_visible = first_yes_id in option_ids
            third_visible = second_visible and second_works_id in option_ids
            assert (second.id in question_ids) is second_visible
            assert (third.id in question_ids) is third_visible
            saw_visible_second = saw_visible_second or second_visible
            saw_hidden_second = saw_hidden_second or not second_visible
            saw_visible_third = saw_visible_third or third_visible
            saw_hidden_third = saw_hidden_third or not third_visible

        assert saw_visible_second and saw_hidden_second
        assert saw_visible_third and saw_hidden_third
