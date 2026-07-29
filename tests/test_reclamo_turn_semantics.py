import pytest

from services.reclamo_turn_semantics import (
    ReclamoTurnIntent,
    apply_reclamo_corrections,
    classify_reclamo_confirmation_turn,
    classify_reclamo_photo_turn,
    extract_reclamo_corrections,
)


@pytest.mark.parametrize(
    "utterance",
    [
        "Nada más.",
        "No, nada más, gracias",
        "Eso es todo",
        "No hace falta una foto",
        "Seguimos sin foto",
        "No tengo foto",
    ],
)
def test_photo_prompt_understands_natural_skip_phrases(utterance):
    decision = classify_reclamo_photo_turn(utterance)

    assert decision.intent is ReclamoTurnIntent.SKIP_PHOTO


@pytest.mark.parametrize(
    "utterance",
    [
        "Sí, quiero agregar una foto",
        "Te mando una imagen ahora",
        "Quiero adjuntar la foto",
    ],
)
def test_photo_prompt_understands_natural_add_phrases(utterance):
    decision = classify_reclamo_photo_turn(utterance)

    assert decision.intent is ReclamoTurnIntent.ADD_PHOTO


def test_photo_prompt_does_not_use_dangerous_substring_matching():
    assert classify_reclamo_photo_turn("Bueno, después veo").intent is ReclamoTurnIntent.UNKNOWN
    assert classify_reclamo_photo_turn("No sé todavía").intent is ReclamoTurnIntent.UNKNOWN


def test_photo_prompt_explicit_action_is_authoritative():
    decision = classify_reclamo_photo_turn(
        "",
        action="reclamo_adjuntar_foto_no",
    )

    assert decision.intent is ReclamoTurnIntent.SKIP_PHOTO


def test_photo_prompt_cancel_action_is_authoritative():
    decision = classify_reclamo_photo_turn("", action="reclamo_cancelar")

    assert decision.intent is ReclamoTurnIntent.CANCEL


@pytest.mark.parametrize(
    "utterance, expected",
    [
        ("📷", ReclamoTurnIntent.ADD_PHOTO),
        ("🚫", ReclamoTurnIntent.SKIP_PHOTO),
        ("⏭️", ReclamoTurnIntent.SKIP_PHOTO),
        ("❌", ReclamoTurnIntent.CANCEL),
    ],
)
def test_photo_prompt_accepts_only_isolated_control_emoji(utterance, expected):
    decision = classify_reclamo_photo_turn(utterance)

    assert decision.intent is expected
    assert decision.reason == "isolated_emoji"


@pytest.mark.parametrize("utterance", ["📷 después", "🚫 tal vez", "❌ no sé"])
def test_photo_prompt_does_not_treat_embedded_emoji_as_an_action(utterance):
    assert classify_reclamo_photo_turn(utterance).intent is ReclamoTurnIntent.UNKNOWN


@pytest.mark.parametrize(
    "utterance, expected_address",
    [
        (
            "La ubicación no estuvo bien guardada. La ubicación era Sarmiento, "
            "esquina San Martín, en la Plaza de Jujuy. ¿Puedes modificar el reclamo?",
            "Sarmiento, esquina San Martín, en la Plaza de Jujuy",
        ),
        (
            "Sí, quiero modificar la dirección. La dirección es Sarmiento y "
            "Salaberry, en la esquina.",
            "Sarmiento y Salaberry, en la esquina",
        ),
        (
            "Quiero editar datos. Dirección don bosco 56 esquina sarmiento, Plaza Junín",
            "don bosco 56 esquina sarmiento, Plaza Junín",
        ),
        (
            "Sí, quiero agregar que la ubicación es Don Bosco 55, esquina "
            "Sarmiento, Plaza Junín, en la esquina.",
            "Don Bosco 55, esquina Sarmiento, Plaza Junín, en la esquina",
        ),
    ],
)
def test_confirmation_extracts_real_world_address_corrections(utterance, expected_address):
    decision = classify_reclamo_confirmation_turn(utterance)

    assert decision.intent is ReclamoTurnIntent.CORRECTION
    assert decision.corrections == {"direccion": expected_address}
    assert "coordenadas" in decision.clear_fields


def test_address_correction_invalidates_old_geocoding_when_applied():
    draft = {
        "direccion": "San Martín, Mendoza, Argentina",
        "coordenadas": {"lat": -33.0, "lon": -68.4},
        "map_search_url": "https://maps.example/old",
        "categoria": "Arbolado",
    }
    decision = classify_reclamo_confirmation_turn(
        "La dirección es Don Bosco 56 esquina Sarmiento, Plaza Junín."
    )

    apply_reclamo_corrections(draft, decision)

    assert draft["direccion"] == "Don Bosco 56 esquina Sarmiento, Plaza Junín"
    assert "coordenadas" not in draft
    assert "map_search_url" not in draft
    assert draft["categoria"] == "Arbolado"


def test_confirmation_extracts_multiple_labelled_corrections_separated_by_commas():
    decision = classify_reclamo_confirmation_turn(
        "Ubicación: Don Bosco 56, categoría: Arbolado, descripción: rama caída"
    )

    assert decision.intent is ReclamoTurnIntent.CORRECTION
    assert decision.corrections == {
        "direccion": "Don Bosco 56",
        "categoria": "Arbolado",
        "descripcion": "rama caída",
    }


@pytest.mark.parametrize(
    "utterance",
    [
        "Sí, quiero agregar una foto",
        "Sí, te mando una imagen ahora",
    ],
)
def test_embedded_si_with_photo_request_never_confirms(utterance):
    decision = classify_reclamo_confirmation_turn(utterance)

    assert decision.intent is ReclamoTurnIntent.ADD_PHOTO


@pytest.mark.parametrize(
    "utterance",
    [
        "Y mi pin para poder preguntar en el futuro?",
        "¿Cuál es mi PIN?",
        "¿Cómo consulto el estado de mi reclamo?",
        "Necesito el número de seguimiento del ticket",
    ],
)
def test_tracking_questions_never_confirm_a_claim(utterance):
    decision = classify_reclamo_confirmation_turn(utterance)

    assert decision.intent is ReclamoTurnIntent.FOLLOW_UP_QUESTION


@pytest.mark.parametrize(
    "utterance",
    [
        "sí",
        "Sí, confirmo",
        "Todo correcto",
        "Los datos están bien",
    ],
)
def test_only_complete_affirmative_utterances_confirm(utterance):
    decision = classify_reclamo_confirmation_turn(utterance)

    assert decision.intent is ReclamoTurnIntent.CONFIRM


@pytest.mark.parametrize(
    "utterance, expected",
    [
        ("✅", ReclamoTurnIntent.CONFIRM),
        ("👍", ReclamoTurnIntent.CONFIRM),
        ("✏️", ReclamoTurnIntent.EDIT),
        ("❌", ReclamoTurnIntent.CANCEL),
        ("📷", ReclamoTurnIntent.ADD_PHOTO),
    ],
)
def test_confirmation_accepts_only_isolated_control_emoji(utterance, expected):
    decision = classify_reclamo_confirmation_turn(utterance)

    assert decision.intent is expected
    assert decision.reason == "isolated_emoji"


@pytest.mark.parametrize("utterance", ["✅ después", "👍 no sé", "📷 mañana"])
def test_confirmation_does_not_treat_embedded_emoji_as_an_action(utterance):
    assert classify_reclamo_confirmation_turn(utterance).intent is ReclamoTurnIntent.UNKNOWN


def test_affirmative_word_inside_unrelated_sentence_is_not_confirmation():
    decision = classify_reclamo_confirmation_turn(
        "Sí, después quiero consultar otra cosa sobre el barrio"
    )

    assert decision.intent is ReclamoTurnIntent.UNKNOWN


@pytest.mark.parametrize(
    "utterance",
    [
        "Quiero modificar la dirección",
        "Necesito corregir los datos",
        "La ubicación está mal",
    ],
)
def test_explicit_edit_request_without_value_opens_editing_instead_of_confirming(utterance):
    decision = classify_reclamo_confirmation_turn(utterance)

    assert decision.intent is ReclamoTurnIntent.EDIT


def test_direct_photo_payload_wins_over_empty_caption():
    decision = classify_reclamo_confirmation_turn("", has_photo=True)

    assert decision.intent is ReclamoTurnIntent.ADD_PHOTO


@pytest.mark.parametrize(
    "action",
    ["mostrar_menu_reclamos", "iniciar_reclamo", "crear_reclamo"],
)
def test_legacy_new_claim_actions_remain_supported(action):
    decision = classify_reclamo_confirmation_turn("", action=action)

    assert decision.intent is ReclamoTurnIntent.NEW_CLAIM


def test_unlabelled_free_text_is_not_silently_used_as_a_correction():
    assert extract_reclamo_corrections("Don Bosco 56, esquina Sarmiento") == {}


def test_apply_rejects_non_correction_decision():
    with pytest.raises(ValueError, match="reclamo_correction_decision_required"):
        apply_reclamo_corrections(
            {},
            classify_reclamo_confirmation_turn("sí"),
        )
