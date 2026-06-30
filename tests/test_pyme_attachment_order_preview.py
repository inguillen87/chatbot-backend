from unittest.mock import patch

from services.order_attachment_preview import build_order_attachment_preview


def _pipeline_ids(preview):
    return [step["id"] for step in preview["intake_experience"]["pipeline"]]


def _operator_task_ids(preview):
    return [step["id"] for step in preview["crm_handoff"]["operator_next_steps"]]


def _assert_public_copy_without_internal_jargon(*parts):
    visible_copy = "\n".join(part for part in parts if part)
    for forbidden in ("CRM", "IA", "tenant admin", "auditoria", "operativo", "operativa"):
        assert forbidden not in visible_copy


@patch("services.order_attachment_preview.buscar_item_en_catalogo")
@patch("services.order_attachment_preview.extraer_lista_pedido_de_texto_con_llm")
def test_build_order_attachment_preview_returns_structured_preview(mock_extract, mock_match):
    class _Match:
        id = 11
        nombre = "Malbec Reserva Catalogo"
        sku = "MALB-01"
        precio = "12500"
        cantidad = 8

    mock_match.side_effect = [_Match(), None]
    mock_extract.return_value = [
        {"nombre_producto_ocr": "Malbec Reserva", "cantidad_ocr": 2},
        {"nombre_producto_ocr": "Cabernet", "cantidad_ocr": 1},
    ]

    preview = build_order_attachment_preview(
        texto_extraido="2 Malbec Reserva\n1 Cabernet",
        pyme_id_context=10,
        telefono="+5492617778888",
        direccion="Mitre 200",
        channel="web",
    )

    assert "pedido preliminar" in preview["message"].lower()
    assert len(preview["items_detectados"]) == 2
    assert preview["confirmation_card"]["items_count"] == 2
    assert preview["items_detectados"][0]["catalog_match"]["sku"] == "MALB-01"
    assert preview["options_list"][0]["action_id"] == "finalizar_pedido_pyme"

    assert preview["channel"] == "web"
    assert preview["anonymous_intake"] is True
    assert preview["catalog_matching"] is True
    assert preview["needs_operator_review"] is True
    assert preview["catalog_match_summary"] == {
        "matched": 1,
        "unmatched": 1,
        "total": 2,
        "detected": 2,
        "catalog_matching": True,
        "needs_operator_review": True,
    }

    intake = preview["intake_experience"]
    assert intake["contract_version"] == "marketplace.assisted_intake_experience.v1"
    assert intake["channel"] == "web"
    assert intake["anonymous_intake"] is True
    assert intake["catalog_matching"] is True
    assert intake["needs_operator_review"] is True
    assert _pipeline_ids(preview) == ["ocr", "ai_parse", "catalog_match", "crm_handoff", "contact"]
    assert intake["pipeline"][2]["status"] == "pending_review"
    assert intake["pipeline"][4]["status"] == "done"
    _assert_public_copy_without_internal_jargon(
        intake["title"],
        intake["summary"],
        *(f"{step['label']} {step['description']}" for step in intake["pipeline"]),
        *(f"{capability['label']} {capability['description']}" for capability in intake["capabilities"]),
        preview["crm_handoff"]["label"],
        *(f"{step['label']} {step['description']}" for step in preview["crm_handoff"]["operator_next_steps"]),
    )

    assert preview["crm_handoff"]["active_channel"] == "web"
    assert preview["crm_handoff"]["recommended_next_action"] == "revisar_y_responder"
    assert preview["crm_handoff"]["summary"]["unmatched_items"] == ["Cabernet"]
    assert "resolve_catalog_matches" in _operator_task_ids(preview)
    assert "confirm_stock_price" in _operator_task_ids(preview)

    assisted_request = preview["assisted_request"]
    assert assisted_request["contract_version"] == "marketplace.assisted_request.v1"
    assert assisted_request["mode"] == "order_note_upload"
    assert assisted_request["channel"] == "web"
    assert assisted_request["anonymous_intake"] is True
    assert assisted_request["document_profile"]["catalog_matching"] is True
    assert assisted_request["source"]["text_preview"].startswith("2 Malbec")
    assert assisted_request["detected_items"] == preview["items_detectados"]
    assert assisted_request["match_summary"] == preview["catalog_match_summary"]
    assert assisted_request["intake_experience"] == preview["intake_experience"]
    assert assisted_request["crm_handoff"] == preview["crm_handoff"]


@patch("services.order_attachment_preview.buscar_item_en_catalogo")
@patch("services.order_attachment_preview.extraer_lista_pedido_de_texto_con_llm")
def test_build_order_attachment_preview_falls_back_when_items_are_not_detected(mock_extract, mock_match):
    mock_match.return_value = None
    mock_extract.return_value = []

    preview = build_order_attachment_preview(
        texto_extraido="nota borrosa sin items claros",
        pyme_id_context=10,
        channel="WhatsApp Business",
    )

    assert "todavia no logre separar productos" in preview["message"].lower()
    assert preview["items_detectados"] == []
    assert preview["options_list"][0]["action_id"] == "pyme_hacer_pedido"
    assert preview["channel"] == "whatsapp"
    assert preview["anonymous_intake"] is True
    assert preview["catalog_matching"] is True
    assert preview["needs_operator_review"] is True
    assert preview["catalog_match_summary"] == {
        "matched": 0,
        "unmatched": 0,
        "total": 0,
        "detected": 0,
        "catalog_matching": True,
        "needs_operator_review": True,
    }

    assert _pipeline_ids(preview) == ["ocr", "ai_parse", "catalog_match", "crm_handoff", "contact"]
    assert preview["intake_experience"]["pipeline"][1]["status"] == "pending_review"
    assert preview["intake_experience"]["pipeline"][2]["status"] == "pending_review"
    assert preview["intake_experience"]["pipeline"][3]["status"] == "pending_review"
    assert preview["intake_experience"]["pipeline"][4]["status"] == "pending_review"
    assert preview["intake_experience"]["crm_handoff"] == preview["crm_handoff"]

    assert preview["crm_handoff"]["recommended_next_action"] == "revisar_y_responder"
    assert preview["crm_handoff"]["summary"]["detected"] == 0
    assert "review_ocr_to_items" in _operator_task_ids(preview)
    assert "confirm_contact_channel" in _operator_task_ids(preview)

    assisted_request = preview["assisted_request"]
    assert assisted_request["contract_version"] == "marketplace.assisted_request.v1"
    assert assisted_request["channel"] == "whatsapp"
    assert assisted_request["detected_items"] == []
    assert assisted_request["source"]["channel"] == "whatsapp"
    assert assisted_request["intake_experience"]["crm_handoff"]["summary"]["needs_operator_review"] is True
