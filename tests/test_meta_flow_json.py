from copy import deepcopy
import hashlib
import json

import pytest

from services.meta_flow_json import (
    ALLOWED_ACTIONS,
    DATA_API_VERSION,
    FLOW_JSON_VERSION,
    MAX_FLOW_JSON_BYTES,
    FlowBlueprint,
    FlowJsonArtifact,
    FlowJsonValidationError,
    assert_valid_flow_document,
    build_claim_tracking_blueprint,
    build_claim_tracking_flow,
    build_order_checkout_blueprint,
    build_order_checkout_flow,
    build_survey_vote_blueprint,
    build_survey_vote_flow,
    canonical_flow_json,
    compile_flow_blueprint,
    flow_json_sha256,
    validate_flow_document,
)


def _static_document():
    return {
        "version": "7.3",
        "screens": [
            {
                "id": "FORM",
                "title": "Datos",
                "terminal": True,
                "success": True,
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [
                        {
                            "type": "TextInput",
                            "name": "full_name",
                            "label": "Nombre",
                            "required": True,
                        },
                        {
                            "type": "Footer",
                            "label": "Finalizar",
                            "on-click-action": {
                                "name": "complete",
                                "payload": {"full_name": "${form.full_name}"},
                            },
                        },
                    ],
                },
            }
        ],
    }


def _two_screen_static_document():
    return {
        "version": "7.3",
        "screens": [
            {
                "id": "ENTRY",
                "title": "Ingreso",
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [
                        {
                            "type": "TextInput",
                            "name": "full_name",
                            "label": "Nombre",
                            "required": True,
                        },
                        {
                            "type": "Footer",
                            "label": "Continuar",
                            "on-click-action": {
                                "name": "navigate",
                                "next": {"type": "screen", "name": "DONE"},
                                "payload": {},
                            },
                        },
                    ],
                },
            },
            {
                "id": "DONE",
                "title": "Confirmar",
                "terminal": True,
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [
                        {
                            "type": "TextBody",
                            "text": "Hola ${screen.ENTRY.form.full_name}",
                        },
                        {
                            "type": "Footer",
                            "label": "Finalizar",
                            "on-click-action": {
                                "name": "complete",
                                "payload": {
                                    "full_name": "${screen.ENTRY.form.full_name}"
                                },
                            },
                        },
                    ],
                },
            },
        ],
    }


def _open_url_document():
    document = _static_document()
    document["screens"][0]["layout"]["children"].insert(
        1,
        {
            "type": "EmbeddedLink",
            "text": "Terminos",
            "on-click-action": {
                "name": "open_url",
                "url": "https://example.com/terms",
            },
        },
    )
    return document


def _update_data_document():
    return {
        "version": "7.3",
        "screens": [
            {
                "id": "PREFERENCES",
                "title": "Preferencias",
                "terminal": True,
                "data": {
                    "show_details": {"type": "boolean", "__example__": False}
                },
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [
                        {
                            "type": "RadioButtonsGroup",
                            "name": "choice",
                            "label": "Opcion",
                            "data-source": [
                                {
                                    "id": "details",
                                    "title": "Ver detalle",
                                    "on-select-action": {
                                        "name": "update_data",
                                        "payload": {"show_details": True},
                                    },
                                }
                            ],
                        },
                        {
                            "type": "TextBody",
                            "text": "Detalle",
                            "visible": "${data.show_details}",
                        },
                        {
                            "type": "Footer",
                            "label": "Finalizar",
                            "on-click-action": {
                                "name": "complete",
                                "payload": {"choice": "${form.choice}"},
                            },
                        },
                    ],
                },
            }
        ],
    }


def _endpoint_document():
    return build_claim_tracking_flow().document


def _error_codes(document):
    return {error.code for error in validate_flow_document(document).errors}


def _walk_actions(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"on-click-action", "on-select-action", "on-unselect-action"}:
                yield child
            yield from _walk_actions(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_actions(child)


def _footer_action(screen):
    for component in screen["layout"]["children"]:
        if component.get("type") == "Footer":
            return component["on-click-action"]
    raise AssertionError("Footer not found")


@pytest.mark.parametrize(
    "builder",
    [build_claim_tracking_flow, build_order_checkout_flow, build_survey_vote_flow],
    ids=["claim_tracking", "order_checkout", "survey_vote"],
)
def test_example_builders_compile_publishable_endpoint_artifacts(builder):
    artifact = builder()

    assert isinstance(artifact, FlowJsonArtifact)
    assert artifact.document["version"] == FLOW_JSON_VERSION
    assert artifact.document["data_api_version"] == DATA_API_VERSION
    assert set(artifact.document["routing_model"]) == {
        screen["id"] for screen in artifact.document["screens"]
    }
    assert artifact.byte_size == len(artifact.as_bytes())
    assert artifact.byte_size <= MAX_FLOW_JSON_BYTES
    assert artifact.content_sha256 == hashlib.sha256(artifact.as_bytes()).hexdigest()
    assert validate_flow_document(artifact.document).valid


@pytest.mark.parametrize(
    "builder",
    [build_claim_tracking_flow, build_order_checkout_flow, build_survey_vote_flow],
    ids=["claim_tracking", "order_checkout", "survey_vote"],
)
def test_example_complete_payloads_are_minimal_user_data_only(builder):
    artifact = builder()
    terminal = next(
        screen for screen in artifact.document["screens"] if screen.get("terminal") is True
    )
    payload = _footer_action(terminal)["payload"]

    assert payload
    assert all(
        value.startswith("${form.") or value.startswith("${screen.")
        for value in payload.values()
    )
    assert not any("${data." in value for value in payload.values())
    assert not any(key in payload for key in ("flow_token", "order_id", "ticket_id", "status"))

    if artifact.blueprint_name == "order_checkout":
        assert set(payload) == {
            "full_name",
            "phone",
            "delivery_address",
            "delivery_notes",
            "confirm_order",
        }
    elif artifact.blueprint_name == "claim_tracking":
        assert set(payload) == {"ticket_number", "follow_up_note"}
    else:
        assert set(payload) == {"confirm_vote"}


def test_survey_vote_flow_uses_server_data_and_only_returns_final_consent():
    blueprint = build_survey_vote_blueprint()
    artifact = build_survey_vote_flow()

    assert blueprint.endpoint_driven is True
    assert artifact.blueprint_name == "survey_vote"
    assert len(artifact.document["screens"]) == 6
    question = artifact.document["screens"][0]
    option_picker = next(
        component
        for component in question["layout"]["children"]
        if component.get("type") == "RadioButtonsGroup"
    )
    assert option_picker["data-source"] == "${data.options}"
    assert _footer_action(question)["name"] == "data_exchange"
    terminal = artifact.document["screens"][-1]
    assert _footer_action(terminal) == {
        "name": "complete",
        "payload": {"confirm_vote": "${form.confirm_vote}"},
    }
    serialized = artifact.canonical_json
    assert "survey_slug" not in serialized
    assert "question_id" not in serialized


def test_blueprint_is_conceptual_and_compilation_does_not_mutate_it():
    blueprint = build_claim_tracking_blueprint()
    original_screens = deepcopy(blueprint.screens)

    assert isinstance(blueprint, FlowBlueprint)
    assert not hasattr(blueprint, "version")
    artifact = compile_flow_blueprint(blueprint)

    assert blueprint.screens == original_screens
    assert artifact.blueprint_name == blueprint.name
    assert artifact.document is not artifact.document
    with pytest.raises(TypeError, match="FlowBlueprint"):
        compile_flow_blueprint(artifact.document)


def test_canonical_json_and_digest_are_deterministic_across_key_order():
    document = _static_document()
    reordered = {key: document[key] for key in reversed(document)}

    first = canonical_flow_json(document)
    second = canonical_flow_json(reordered)

    assert first == second
    assert flow_json_sha256(document) == flow_json_sha256(reordered)
    assert first == json.dumps(
        json.loads(first),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@pytest.mark.parametrize(
    ("name", "document"),
    [
        ("complete", _static_document()),
        ("navigate", _two_screen_static_document()),
        ("data_exchange", _endpoint_document()),
        ("open_url", _open_url_document()),
        ("update_data", _update_data_document()),
    ],
)
def test_all_meta_73_action_names_are_accepted(name, document):
    report = validate_flow_document(document)
    names = {action.get("name") for action in _walk_actions(document)}

    assert report.valid, report.as_dict()
    assert name in names
    assert names <= ALLOWED_ACTIONS


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda doc: doc.pop("version"), "MISSING_VERSION"),
        (lambda doc: doc.__setitem__("version", "7.2"), "INVALID_VERSION"),
        (lambda doc: doc.__setitem__("version", 7.3), "INVALID_VERSION"),
        (lambda doc: doc.__setitem__("extra", True), "UNKNOWN_TOP_LEVEL_PROPERTY"),
        (lambda doc: doc.pop("screens"), "MISSING_SCREENS"),
        (lambda doc: doc.__setitem__("screens", []), "EMPTY_SCREENS"),
        (lambda doc: doc.__setitem__("screens", {}), "INVALID_SCREENS_TYPE"),
        (
            lambda doc: doc["screens"][0]["layout"]["children"][0].__setitem__("label", None),
            "NULL_NOT_ALLOWED",
        ),
        (
            lambda doc: doc["screens"][0]["layout"]["children"][0].__setitem__("score", float("nan")),
            "NON_FINITE_NUMBER",
        ),
    ],
)
def test_top_level_and_json_domain_failures_are_structured(mutate, expected_code):
    document = _static_document()
    mutate(document)

    report = validate_flow_document(document)

    assert not report.valid
    assert expected_code in {error.code for error in report.errors}
    assert all(error.path.startswith("$") for error in report.errors)
    assert report.as_dict()["valid"] is False


def test_non_object_document_returns_type_and_null_errors_without_crashing():
    report = validate_flow_document(None)

    assert {error.code for error in report.errors} == {
        "INVALID_DOCUMENT_TYPE",
        "NULL_NOT_ALLOWED",
    }


def test_serialized_size_over_10_mib_is_rejected():
    document = _static_document()
    document["screens"][0]["layout"]["children"][0]["label"] = "x" * MAX_FLOW_JSON_BYTES

    assert "FLOW_JSON_TOO_LARGE" in _error_codes(document)


@pytest.mark.parametrize(
    ("screen_id", "expected_code"),
    [
        ("", "MISSING_SCREEN_ID"),
        ("SUCCESS", "RESERVED_SCREEN_ID"),
        ("success", "RESERVED_SCREEN_ID"),
        ("SCREEN_1", "INVALID_SCREEN_ID"),
        ("SCREEN-DASH", "INVALID_SCREEN_ID"),
        ("PANTALLA UNO", "INVALID_SCREEN_ID"),
    ],
)
def test_screen_ids_follow_meta_pattern_and_reserve_success(screen_id, expected_code):
    document = _static_document()
    document["screens"][0]["id"] = screen_id

    assert expected_code in _error_codes(document)


def test_duplicate_screen_ids_are_rejected():
    document = _two_screen_static_document()
    document["screens"][1]["id"] = "ENTRY"

    assert "DUPLICATE_SCREEN_ID" in _error_codes(document)


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda doc: doc["screens"][0]["layout"].__setitem__("type", "GridLayout"),
            "INVALID_LAYOUT_TYPE",
        ),
        (
            lambda doc: doc["screens"][0].pop("layout"),
            "MISSING_LAYOUT",
        ),
        (
            lambda doc: doc["screens"][0]["layout"].__setitem__("children", []),
            "EMPTY_LAYOUT",
        ),
        (
            lambda doc: doc["screens"][0]["layout"]["children"][0].__setitem__("type", "MadeUp"),
            "UNSUPPORTED_COMPONENT",
        ),
        (
            lambda doc: doc["screens"][0].__setitem__("unknown", True),
            "UNKNOWN_SCREEN_PROPERTY",
        ),
    ],
)
def test_screen_layout_and_component_structure_is_strict(mutate, expected_code):
    document = _static_document()
    mutate(document)

    assert expected_code in _error_codes(document)


def test_every_terminal_requires_exactly_one_footer_with_complete():
    missing_footer = _static_document()
    missing_footer["screens"][0]["layout"]["children"] = missing_footer["screens"][0]["layout"]["children"][:1]
    assert "MISSING_TERMINAL_FOOTER" in _error_codes(missing_footer)

    duplicate_footer = _static_document()
    duplicate_footer["screens"][0]["layout"]["children"].append(
        deepcopy(duplicate_footer["screens"][0]["layout"]["children"][-1])
    )
    assert "MORE_THAN_ONE_FOOTER" in _error_codes(duplicate_footer)

    wrong_action = _static_document()
    _footer_action(wrong_action["screens"][0])["name"] = "data_exchange"
    assert "TERMINAL_FOOTER_MUST_COMPLETE" in _error_codes(wrong_action)


def test_flow_requires_a_successful_terminal_and_complete_is_terminal_only():
    no_terminal = _static_document()
    no_terminal["screens"][0].pop("terminal")
    no_terminal["screens"][0].pop("success")
    codes = _error_codes(no_terminal)
    assert "MISSING_TERMINAL_SCREEN" in codes
    assert "COMPLETE_REQUIRES_TERMINAL" in codes

    unsuccessful = _static_document()
    unsuccessful["screens"][0]["success"] = False
    assert "MISSING_SUCCESSFUL_TERMINAL" in _error_codes(unsuccessful)


@pytest.mark.parametrize("invalid_name", ["end_flow", "execute", "DATA_EXCHANGE", ""])
def test_unknown_action_names_are_rejected(invalid_name):
    document = _static_document()
    _footer_action(document["screens"][0])["name"] = invalid_name

    assert "INVALID_ACTION_NAME" in _error_codes(document)


def test_action_specific_shapes_and_placements_are_validated():
    navigate = _two_screen_static_document()
    _footer_action(navigate["screens"][0]).pop("next")
    assert "INVALID_NAVIGATE_TARGET" in _error_codes(navigate)

    open_url = _open_url_document()
    link_action = open_url["screens"][0]["layout"]["children"][1]["on-click-action"]
    link_action["url"] = "http://user:pass@example.com/terms"
    assert "INVALID_OPEN_URL" in _error_codes(open_url)

    open_url_with_payload = _open_url_document()
    open_url_with_payload["screens"][0]["layout"]["children"][1]["on-click-action"]["payload"] = {}
    assert "UNKNOWN_ACTION_PROPERTY" in _error_codes(open_url_with_payload)

    update_data = _update_data_document()
    update_action = update_data["screens"][0]["layout"]["children"][0]["data-source"][0]["on-select-action"]
    update_action["payload"] = {"not_declared": True}
    assert "UNKNOWN_UPDATE_DATA_FIELD" in _error_codes(update_data)


def test_dynamic_open_url_requires_a_static_https_origin():
    for url in (
        "javascript:${form.full_name}",
        "${form.full_name}",
        "https://trusted.example${form.full_name}/continue",
        "https://${form.full_name}/continue",
    ):
        document = _open_url_document()
        document["screens"][0]["layout"]["children"][1]["on-click-action"]["url"] = url
        assert "INVALID_OPEN_URL" in _error_codes(document)

    safe_dynamic_url = _open_url_document()
    safe_dynamic_url["screens"][0]["layout"]["children"][1]["on-click-action"]["url"] = (
        "https://example.com/terms?name=${form.full_name}"
    )
    assert validate_flow_document(safe_dynamic_url).valid


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "${data.status}"},
        {"status": "confirmed"},
        {"status": True},
        {"status": "Prefix ${form.full_name}"},
    ],
)
def test_complete_payload_rejects_server_data_and_static_values(payload):
    document = _static_document()
    document["screens"][0]["data"] = {
        "status": {"type": "string", "__example__": "ready"}
    }
    _footer_action(document["screens"][0])["payload"] = payload

    assert "COMPLETE_PAYLOAD_NOT_USER_DATA" in _error_codes(document)


def test_empty_complete_payload_is_valid_and_minimal():
    document = _static_document()
    _footer_action(document["screens"][0])["payload"] = {}

    assert validate_flow_document(document).valid


def test_data_exchange_requires_data_api_30_and_routing_model():
    missing_data_api = _endpoint_document()
    missing_data_api.pop("data_api_version")
    assert "DATA_API_VERSION_REQUIRED" in _error_codes(missing_data_api)

    wrong_data_api = _endpoint_document()
    wrong_data_api["data_api_version"] = "2.0"
    assert "INVALID_DATA_API_VERSION" in _error_codes(wrong_data_api)

    missing_routing = _endpoint_document()
    missing_routing.pop("routing_model")
    assert "ROUTING_MODEL_REQUIRED" in _error_codes(missing_routing)


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda doc: doc["routing_model"].pop("CLAIM_RESULT"),
            "MISSING_ROUTING_SCREEN",
        ),
        (
            lambda doc: doc["routing_model"].__setitem__("UNKNOWN", []),
            "UNKNOWN_ROUTING_SCREEN",
        ),
        (
            lambda doc: doc["routing_model"]["CLAIM_LOOKUP"].append("UNKNOWN"),
            "UNKNOWN_ROUTING_TARGET",
        ),
        (
            lambda doc: doc["routing_model"]["CLAIM_LOOKUP"].append("CLAIM_LOOKUP"),
            "SELF_ROUTING_NOT_ALLOWED",
        ),
        (
            lambda doc: doc["routing_model"]["CLAIM_RESULT"].append("CLAIM_LOOKUP"),
            "REVERSE_ROUTE_NOT_ALLOWED",
        ),
    ],
)
def test_endpoint_routing_model_rejects_invalid_graph_edges(mutate, expected_code):
    document = _endpoint_document()
    mutate(document)

    assert expected_code in _error_codes(document)


def test_routing_branch_limit_is_ten_per_screen():
    destination_ids = [f"DEST_{letter}" for letter in "ABCDEFGHIJK"]
    terminal_screens = [
        {
            "id": screen_id,
            "terminal": True,
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {
                        "type": "Footer",
                        "label": "Finalizar",
                        "on-click-action": {"name": "complete", "payload": {}},
                    }
                ],
            },
        }
        for screen_id in destination_ids
    ]
    document = {
        "version": "7.3",
        "data_api_version": "3.0",
        "routing_model": {
            "ROOT": destination_ids,
            **{screen_id: [] for screen_id in destination_ids},
        },
        "screens": [
            {
                "id": "ROOT",
                "layout": {
                    "type": "SingleColumnLayout",
                    "children": [
                        {
                            "type": "Footer",
                            "label": "Continuar",
                            "on-click-action": {
                                "name": "data_exchange",
                                "payload": {},
                            },
                        }
                    ],
                },
            },
            *terminal_screens,
        ],
    }

    assert "TOO_MANY_ROUTING_BRANCHES" in _error_codes(document)


def test_routing_detects_disconnected_screens_and_nonterminal_sinks():
    disconnected = _endpoint_document()
    disconnected["routing_model"]["ORPHAN"] = []
    disconnected["screens"].append(
        {
            "id": "ORPHAN",
            "terminal": True,
            "layout": {
                "type": "SingleColumnLayout",
                "children": [
                    {
                        "type": "Footer",
                        "label": "Finalizar",
                        "on-click-action": {"name": "complete", "payload": {}},
                    }
                ],
            },
        }
    )
    assert "DISCONNECTED_SCREEN" in _error_codes(disconnected)

    sink = _endpoint_document()
    sink["screens"][1].pop("terminal")
    sink["screens"][1].pop("success")
    assert "ROUTE_DOES_NOT_REACH_TERMINAL" in _error_codes(sink)


def test_endpoint_navigate_edges_must_exist_in_routing_model():
    document = _endpoint_document()
    first_action = _footer_action(document["screens"][0])
    first_action.clear()
    first_action.update(
        {
            "name": "navigate",
            "next": {"type": "screen", "name": "CLAIM_RESULT"},
            "payload": {},
        }
    )
    document["routing_model"]["CLAIM_LOOKUP"] = []

    assert "NAVIGATE_ROUTE_MISSING" in _error_codes(document)


def test_static_navigation_derives_a_connected_graph_without_routing_model():
    document = _two_screen_static_document()

    assert "routing_model" not in document
    assert validate_flow_document(document).valid


def test_local_global_and_nested_data_references_are_resolved():
    document = _static_document()
    screen = document["screens"][0]
    screen["data"] = {
        "profile": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "__example__": {"name": "Ana"},
        }
    }
    screen["layout"]["children"].insert(
        1,
        {
            "type": "TextBody",
            "text": "${data.profile.name}",
            "visible": "`${form.full_name} != ''`",
        },
    )

    assert validate_flow_document(document).valid
    assert validate_flow_document(_two_screen_static_document()).valid


@pytest.mark.parametrize(
    ("text", "expected_code"),
    [
        ("${form.missing}", "UNKNOWN_FORM_REFERENCE"),
        ("${data.missing}", "UNKNOWN_DATA_REFERENCE"),
        ("${screen.UNKNOWN.form.full_name}", "UNKNOWN_REFERENCE_SCREEN"),
        ("${screen.FORM.form.full_name}", "GLOBAL_REFERENCE_TO_CURRENT_SCREEN"),
        ("${user.full_name}", "INVALID_DYNAMIC_REFERENCE"),
        ("${form.full_name", "MALFORMED_DYNAMIC_REFERENCE"),
    ],
)
def test_invalid_dynamic_references_have_stable_error_codes(text, expected_code):
    document = _static_document()
    document["screens"][0]["layout"]["children"].insert(
        1, {"type": "TextBody", "text": text}
    )

    assert expected_code in _error_codes(document)


def test_data_schema_requires_examples_and_matching_types():
    missing_example = _static_document()
    missing_example["screens"][0]["data"] = {"status": {"type": "string"}}
    assert "MISSING_DATA_EXAMPLE" in _error_codes(missing_example)

    wrong_example = _static_document()
    wrong_example["screens"][0]["data"] = {
        "status": {"type": "string", "__example__": 7}
    }
    assert "DATA_EXAMPLE_TYPE_MISMATCH" in _error_codes(wrong_example)

    unknown_type = _static_document()
    unknown_type["screens"][0]["data"] = {
        "status": {"type": "null", "__example__": "ready"}
    }
    assert "INVALID_DATA_SCHEMA_TYPE" in _error_codes(unknown_type)


def test_validation_exception_preserves_all_structured_errors():
    document = _static_document()
    document["version"] = "1.0"
    document["screens"][0]["id"] = "SUCCESS"

    report = validate_flow_document(document)
    with pytest.raises(FlowJsonValidationError) as captured:
        assert_valid_flow_document(document)

    assert captured.value.errors == report.errors
    assert captured.value.as_dict() == report.as_dict()
    assert "INVALID_VERSION" in str(captured.value)


def test_invalid_blueprint_fails_closed_before_artifact_creation():
    valid = build_order_checkout_blueprint()
    invalid_screen = deepcopy(valid.screens[0])
    invalid_screen["layout"]["type"] = "GridLayout"
    invalid = FlowBlueprint(
        name="invalid_checkout",
        endpoint_driven=True,
        routing_model=valid.routing_model,
        screens=(invalid_screen, *valid.screens[1:]),
    )

    with pytest.raises(FlowJsonValidationError) as captured:
        compile_flow_blueprint(invalid)

    assert "INVALID_LAYOUT_TYPE" in {error.code for error in captured.value.errors}


def test_error_order_and_serialization_are_deterministic():
    document = _static_document()
    document["version"] = "0"
    document["screens"][0]["id"] = "SUCCESS"
    document["screens"][0]["layout"]["children"][0]["label"] = None

    first = validate_flow_document(document)
    second = validate_flow_document(deepcopy(document))

    assert first.errors == second.errors
    assert first.as_dict() == second.as_dict()
    assert list(first.errors) == sorted(first.errors)
