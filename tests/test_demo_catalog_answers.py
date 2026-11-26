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
