"""A typed question in a business demo gets a real answer, without calling a model."""

from services.demo_response_engine import maybe_handle_demo_interaction


def ask(key, text):
    return maybe_handle_demo_interaction(
        pregunta=text,
        tipo_chat="pyme",
        demo_metadata={"key": key, "nombre": "Demo", "resources": []},
    )


def test_product_price_and_delivery_in_one_answer():
    answer = ask("ferreteria", "hola, tienen taladro percutor? cuanto sale y hacen envios a godoy cruz?")

    body = answer["message_body"]
    assert "taladro percutor" in body
    assert "$ 189.000" in body
    assert "Entregamos en obra" in body
    assert "precios son de ejemplo" in body
    assert answer["fuente"] == "demo_offline"
    assert answer["options_list"]


def test_accents_and_plurals_do_not_matter():
    assert "Malbec Reserva" in ask("bodega", "¿Qué MALBEC tienen?")["message_body"]
    assert "zapatillas urbanas" in ask("local_comercial_general", "busco zapatillas")["message_body"]


def test_several_products_come_as_a_short_list():
    body = ask("ferreteria", "necesito cemento, arena y ladrillos")["message_body"]

    assert body.startswith("De eso tenemos:")
    assert body.count("\n- ") == 3


def test_a_word_inside_another_word_is_not_a_match():
    # "arena" must not answer for "carenado"; nothing matches, so the script's own
    # "no entendí" still guides the visitor back to the menu.
    answer = ask("ferreteria", "carenado de moto")

    assert "arena" not in answer["message_body"]
    assert "menú" in answer["message_body"]


def test_payment_question_alone_is_answered():
    assert "cuotas" in ask("bodega", "¿aceptan tarjeta?")["message_body"]


def test_service_rubros_answer_in_their_own_words():
    clinic = ask("medico_general", "necesito un turno con el pediatra, atienden con obra social?")["message_body"]
    assert clinic.startswith("Sí, atendemos pediatría")
    assert "obras sociales y prepagas" in clinic
    assert "¿Querés que te busque un turno?" in clinic
    assert "pedido" not in clinic

    insurance = ask("seguros", "me chocaron el auto, como hago la denuncia?")["message_body"]
    assert "número de póliza o la patente" in insurance

    homes = ask("inmobiliaria", "busco alquiler, que requisitos piden?")["message_body"]
    assert "$ 520.000 por mes" in homes
    assert "seguro de caución" in homes
    assert "¿Coordinamos una visita?" in homes


def test_a_topic_alone_is_answered_without_inventing_a_product():
    body = ask("logistica", "quiero hacer el seguimiento de mi paquete")["message_body"]
    assert "código de seguimiento" in body

    wholesale = ask("almacen", "venden por bulto para kiosco?")["message_body"]
    assert "lista mayorista" in wholesale
    assert "precios son de ejemplo" not in wholesale


def click(key, target):
    return maybe_handle_demo_interaction(
        pregunta="",
        tipo_chat="pyme",
        demo_metadata={"key": key, "nombre": "Demo", "resources": []},
        action_id=f"demo_action:{key}:{target}",
    )


def test_a_scripted_leaf_answers_its_own_sample_question():
    answer = click("medico_general", "pediatría")
    body = answer["message_body"]

    assert body.startswith("Guardia pediátrica 24/7.")
    assert "Sí, atendemos pediatría" in body
    assert "_" not in body
    assert answer["options_list"]


def test_a_leaf_without_catalogue_still_offers_a_way_back():
    answer = click("energia", "kpis_de_pozos")

    assert "Probá escribirme algo como: «" in answer["message_body"]
    assert any("menú" in str(option).lower() for option in answer["options_list"])
