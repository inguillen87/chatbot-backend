from __future__ import annotations

from copy import deepcopy

import pytest

from database import db
from models import EncEncuesta, EncRespuesta
from services.encuestas_service import (
    EncuestaError,
    _normalize_conditional_logic,
    compile_survey_visibility,
    create_encuesta,
    duplicate_encuesta,
    publicar_encuesta,
    save_respuesta,
    seed_encuesta_respuestas_demo,
    serialize_encuesta,
    serialize_public_encuesta,
    update_encuesta,
)


class _User:
    def __init__(self, tenant_id: int = 4):
        self.id = None
        self.municipio_id = tenant_id
        self.rol = "admin"


def _leaf(question_ref: str, option_ref: str) -> dict:
    return {
        "kind": "option_selected",
        "question_ref": question_ref,
        "option_ref": option_ref,
    }


def _group(operator: str, *children: dict) -> dict:
    return {"kind": "group", "operator": operator, "children": list(children)}


def _rule_v2(operator: str, *children: dict) -> dict:
    return {"version": 2, "show_if": _group(operator, *children)}


def _rule_v1(question_order: int, option_order: int) -> dict:
    return {
        "version": 1,
        "show_if": {
            "question_order": question_order,
            "option_order": option_order,
        },
    }


def _v2_payload(title: str = "Logica compuesta v2") -> dict:
    return {
        "titulo": title,
        "politica_unicidad": "libre",
        "preguntas": [
            {
                "question_ref": "source-topics",
                "orden": 1,
                "tipo": "opcion_multiple",
                "texto": "Que temas te interesan?",
                "obligatoria": True,
                "min_selecciones": 1,
                "max_selecciones": 2,
                "opciones": [
                    {
                        "option_ref": "topic-works",
                        "orden": 1,
                        "texto": "Obras",
                    },
                    {
                        "option_ref": "topic-services",
                        "orden": 2,
                        "texto": "Servicios",
                    },
                ],
            },
            {
                "question_ref": "source-urgency",
                "orden": 2,
                "tipo": "opcion_unica",
                "texto": "Que urgencia tiene?",
                "obligatoria": False,
                "opciones": [
                    {
                        "option_ref": "urgency-high",
                        "orden": 1,
                        "texto": "Alta",
                    },
                    {
                        "option_ref": "urgency-low",
                        "orden": 2,
                        "texto": "Baja",
                    },
                ],
            },
            {
                "question_ref": "detail-and",
                "orden": 3,
                "tipo": "abierta",
                "texto": "Detalle urgente de obras",
                "obligatoria": True,
                "conditional_logic": _rule_v2(
                    "and",
                    _leaf("source-topics", "topic-works"),
                    _leaf("source-urgency", "urgency-high"),
                ),
            },
            {
                "question_ref": "detail-or",
                "orden": 4,
                "tipo": "abierta",
                "texto": "Detalle alternativo",
                "obligatoria": True,
                "conditional_logic": _rule_v2(
                    "or",
                    _leaf("source-topics", "topic-services"),
                    _leaf("source-urgency", "urgency-low"),
                ),
            },
        ],
    }


def _ctx(anon_id: str) -> dict:
    return {
        "ip": "127.0.0.1",
        "user_agent": "pytest-v2",
        "anon_id": anon_id,
        "canal": "web",
    }


def _option_id(question, option_ref: str) -> int:
    return next(
        option.id for option in question.opciones if option.logical_ref == option_ref
    )


def _publish(payload: dict):
    user = _User()
    survey = create_encuesta(payload, user)
    survey, link = publicar_encuesta(survey.id, user)
    return db.session.get(EncEncuesta, survey.id), link.slug_publico, user


def test_v2_round_trips_update_duplicate_publish_and_public_serialization(client):
    with client.application.app_context():
        user = _User()
        payload = _v2_payload()
        survey = create_encuesta(payload, user)
        expected = payload["preguntas"][2]["conditional_logic"]

        assert survey.preguntas[2].logica_condicional == expected
        assert serialize_encuesta(survey)["preguntas"][2]["conditional_logic"] == expected

        updated_payload = deepcopy(payload)
        updated_payload["titulo"] = "Logica compuesta v2 actualizada"
        updated_payload["preguntas"][2]["conditional_logic"] = _rule_v2(
            "or",
            _leaf("source-topics", "topic-works"),
            _leaf("source-urgency", "urgency-high"),
        )
        survey = update_encuesta(survey.id, updated_payload, user)
        assert survey.preguntas[2].logica_condicional["show_if"]["operator"] == "or"

        duplicate = duplicate_encuesta(survey.id, {}, user)
        assert [question.logical_ref for question in duplicate.preguntas] == [
            question.logical_ref for question in survey.preguntas
        ]
        assert [
            [option.logical_ref for option in question.opciones]
            for question in duplicate.preguntas
        ] == [
            [option.logical_ref for option in question.opciones]
            for question in survey.preguntas
        ]
        assert duplicate.preguntas[2].logica_condicional == survey.preguntas[2].logica_condicional

        duplicate, link = publicar_encuesta(duplicate.id, user)
        public_payload = serialize_public_encuesta(duplicate, link.slug_publico)
        assert public_payload["preguntas"][2]["conditional_logic"]["version"] == 2


@pytest.mark.parametrize(
    "invalid_rule",
    [
        {"version": 2, "show_if": _leaf("question-a", "option-a")},
        {
            "version": 2,
            "show_if": {
                **_group("and", _leaf("question-a", "option-a")),
                "extra": True,
            },
        },
        {"version": 2, "show_if": _group("xor", _leaf("question-a", "option-a"))},
        {"version": 2, "show_if": _group("and")},
        {
            "version": 2,
            "show_if": _group(
                "and",
                {
                    **_leaf("question-a", "option-a"),
                    "extra": True,
                },
            ),
        },
        {
            "version": 2,
            "show_if": _group("and", _leaf("question with spaces", "option-a")),
        },
        {
            "version": 2,
            "show_if": _group(
                "and",
                _leaf("question-a", "option-a"),
                _leaf("question-a", "option-a"),
            ),
        },
        {
            "version": 2,
            "show_if": _group(
                "and",
                _group(
                    "or",
                    _group(
                        "and",
                        _group("or", _leaf("question-a", "option-a")),
                    ),
                ),
            ),
        },
    ],
)
def test_v2_tree_contract_is_closed_and_bounded(invalid_rule):
    with pytest.raises(EncuestaError) as exc_info:
        _normalize_conditional_logic(invalid_rule)

    assert exc_info.value.status_code == 400
    assert exc_info.value.payload["contract_version"] == "surveys.conditional_logic.v2"
    assert exc_info.value.payload["reason_code"] == "survey_conditional_logic_invalid"


def test_v2_enforces_children_leaf_and_node_limits():
    too_many_children = {
        "version": 2,
        "show_if": _group(
            "and",
            *[_leaf(f"question-{index}", f"option-{index}") for index in range(17)],
        ),
    }
    too_many_leaves = {
        "version": 2,
        "show_if": _group(
            "or",
            *[
                _group(
                    "and",
                    *[
                        _leaf(f"question-{group}-{index}", f"option-{group}-{index}")
                        for index in range(11)
                    ],
                )
                for group in range(3)
            ],
        ),
    }
    leaf_index = 0
    node_groups = []
    for group_index in range(16):
        nested = []
        for _ in range(2):
            nested.append(
                _group(
                    "and",
                    _leaf(f"question-node-{leaf_index}", f"option-node-{leaf_index}"),
                )
            )
            leaf_index += 1
        node_groups.append(_group("or", *nested))
    too_many_nodes = {"version": 2, "show_if": _group("and", *node_groups)}

    for rule, limit_key in (
        (too_many_children, "max_children"),
        (too_many_leaves, "max_leaves"),
        (too_many_nodes, "max_nodes"),
    ):
        with pytest.raises(EncuestaError) as exc_info:
            _normalize_conditional_logic(rule)
        assert limit_key in exc_info.value.payload["context"]


@pytest.mark.parametrize(
    ("source_question_ref", "source_option_ref", "target_index"),
    [
        ("missing-question", "topic-works", 2),
        ("source-topics", "urgency-high", 2),
        ("detail-or", "missing-option", 2),
        ("detail-and", "missing-option", 3),
    ],
)
def test_v2_rejects_missing_cross_question_or_non_selectable_refs(
    client,
    source_question_ref,
    source_option_ref,
    target_index,
):
    with client.application.app_context():
        payload = _v2_payload(f"Refs invalidas {source_question_ref} {target_index}")
        payload["preguntas"][target_index]["conditional_logic"] = _rule_v2(
            "and",
            _leaf(source_question_ref, source_option_ref),
        )

        with pytest.raises(EncuestaError) as exc_info:
            create_encuesta(payload, _User())

        assert exc_info.value.payload["contract_version"] == "surveys.conditional_logic.v2"
        assert exc_info.value.payload["reason_code"] == "survey_conditional_logic_invalid"


def test_v2_rejects_impossible_and_for_distinct_single_choice_options(client):
    with client.application.app_context():
        payload = _v2_payload("AND imposible")
        payload["preguntas"][2]["conditional_logic"] = _rule_v2(
            "and",
            _leaf("source-urgency", "urgency-high"),
            _group(
                "and",
                _leaf("source-urgency", "urgency-low"),
            ),
        )

        with pytest.raises(EncuestaError) as exc_info:
            create_encuesta(payload, _User())

        assert exc_info.value.payload["contract_version"] == "surveys.conditional_logic.v2"
        assert exc_info.value.payload["reason_code"] == "survey_conditional_logic_invalid"


def test_v2_rejects_impossible_nested_and_without_treating_or_as_conjunction(client):
    with client.application.app_context():
        impossible_branch = _v2_payload("AND imposible dentro de OR")
        impossible_branch["preguntas"][2]["conditional_logic"] = _rule_v2(
            "or",
            _group(
                "and",
                _leaf("source-urgency", "urgency-high"),
                _leaf("source-urgency", "urgency-low"),
            ),
            _leaf("source-topics", "topic-works"),
        )

        with pytest.raises(EncuestaError) as exc_info:
            create_encuesta(impossible_branch, _User())

        assert exc_info.value.payload["reason_code"] == "survey_conditional_logic_invalid"

        valid_alternatives = _v2_payload("Alternativas OR validas")
        valid_alternatives["preguntas"][2]["conditional_logic"] = _rule_v2(
            "or",
            _leaf("source-urgency", "urgency-high"),
            _leaf("source-urgency", "urgency-low"),
        )

        survey = create_encuesta(valid_alternatives, _User())
        assert survey.preguntas[2].logica_condicional["show_if"]["operator"] == "or"
        assert "opcion unica" in exc_info.value.payload["detail"]


def test_v2_and_or_response_semantics_and_hidden_answer_rejection(client):
    with client.application.app_context():
        survey, slug, _ = _publish(_v2_payload())
        topics, urgency, detail_and, detail_or = survey.preguntas
        works_id = _option_id(topics, "topic-works")
        services_id = _option_id(topics, "topic-services")
        high_id = _option_id(urgency, "urgency-high")

        and_branch = save_respuesta(
            slug,
            {
                "respuestas": [
                    {"pregunta_id": topics.id, "opcion_ids": [works_id]},
                    {"pregunta_id": urgency.id, "opcion_id": high_id},
                    {"pregunta_id": detail_and.id, "texto_libre": "Urgente"},
                ]
            },
            _ctx("v2-and"),
        )
        assert {detail.pregunta_id for detail in and_branch.detalles} == {
            topics.id,
            urgency.id,
            detail_and.id,
        }

        or_branch = save_respuesta(
            slug,
            {
                "respuestas": [
                    {"pregunta_id": topics.id, "opcion_ids": [services_id]},
                    {"pregunta_id": detail_or.id, "texto_libre": "Servicios"},
                ]
            },
            _ctx("v2-or"),
        )
        assert {detail.pregunta_id for detail in or_branch.detalles} == {
            topics.id,
            detail_or.id,
        }

        before = EncRespuesta.query.filter_by(encuesta_id=survey.id).count()
        with pytest.raises(EncuestaError) as hidden_error:
            save_respuesta(
                slug,
                {
                    "respuestas": [
                        {"pregunta_id": topics.id, "opcion_ids": [services_id]},
                        {"pregunta_id": urgency.id, "opcion_id": high_id},
                        {"pregunta_id": detail_and.id, "texto_libre": "Oculta"},
                        {"pregunta_id": detail_or.id, "texto_libre": "Visible"},
                    ]
                },
                _ctx("v2-hidden"),
            )
        assert hidden_error.value.payload["reason_code"] == "hidden_question_answered"
        assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == before


def test_compiled_visibility_is_dual_version_and_ignores_hidden_source_stale_selections(client):
    with client.application.app_context():
        v1_payload = {
            "titulo": "Plan v1",
            "preguntas": [
                {
                    "orden": 1,
                    "tipo": "opcion_unica",
                    "texto": "Continuar?",
                    "opciones": [
                        {"orden": 1, "texto": "Si"},
                        {"orden": 2, "texto": "No"},
                    ],
                },
                {
                    "orden": 2,
                    "tipo": "abierta",
                    "texto": "Detalle",
                    "conditional_logic": _rule_v1(1, 1),
                },
            ],
        }
        v1_survey = create_encuesta(v1_payload, _User())
        v1_plan = compile_survey_visibility(v1_survey)
        yes_id = v1_survey.preguntas[0].opciones[0].id
        assert [q.orden for q in v1_plan.evaluate()] == [1]
        assert [
            q.orden
            for q in v1_plan.evaluate(
                selected_option_ids_by_question_id={
                    v1_survey.preguntas[0].id: {yes_id}
                }
            )
        ] == [1, 2]

        v2_chain = {
            "titulo": "Plan v2 con fuente oculta",
            "preguntas": [
                {
                    "question_ref": "gate",
                    "orden": 1,
                    "tipo": "opcion_unica",
                    "texto": "Abrir rama?",
                    "opciones": [
                        {"option_ref": "gate-yes", "orden": 1, "texto": "Si"},
                        {"option_ref": "gate-no", "orden": 2, "texto": "No"},
                    ],
                },
                {
                    "question_ref": "hidden-source",
                    "orden": 2,
                    "tipo": "opcion_unica",
                    "texto": "Color",
                    "opciones": [
                        {"option_ref": "color-red", "orden": 1, "texto": "Rojo"},
                        {"option_ref": "color-blue", "orden": 2, "texto": "Azul"},
                    ],
                    "conditional_logic": _rule_v2(
                        "and", _leaf("gate", "gate-yes")
                    ),
                },
                {
                    "question_ref": "descendant",
                    "orden": 3,
                    "tipo": "abierta",
                    "texto": "Detalle rojo",
                    "conditional_logic": _rule_v2(
                        "and", _leaf("hidden-source", "color-red")
                    ),
                },
            ],
        }
        v2_survey = create_encuesta(v2_chain, _User())
        gate, hidden_source, _ = v2_survey.preguntas
        gate_no_id = _option_id(gate, "gate-no")
        stale_red_id = _option_id(hidden_source, "color-red")
        v2_plan = compile_survey_visibility(v2_survey)
        visible = v2_plan.evaluate(
            selected_option_ids_by_question_id={
                gate.id: [gate_no_id],
                hidden_source.id: [stale_red_id],
            }
        )
        assert [question.logical_ref for question in visible] == ["gate"]


def test_v2_demo_seed_uses_the_same_visibility_plan(client):
    with client.application.app_context():
        survey = create_encuesta(_v2_payload("Seed v2"), _User())
        result = seed_encuesta_respuestas_demo(
            survey.id,
            _User(),
            cantidad=16,
            seed=23,
        )
        assert result["creadas"] == 16

        plan = compile_survey_visibility(survey)
        for response in EncRespuesta.query.filter_by(encuesta_id=survey.id).all():
            selected_ids: dict[int, set[int]] = {}
            answered_question_ids = set()
            for detail in response.detalles:
                answered_question_ids.add(detail.pregunta_id)
                if detail.opcion_id is not None:
                    selected_ids.setdefault(detail.pregunta_id, set()).add(
                        detail.opcion_id
                    )
            visible_ids = {
                question.id
                for question in plan.evaluate(
                    selected_option_ids_by_question_id=selected_ids
                )
            }
            assert answered_question_ids == visible_ids
