from services.common_utils import formatear_opciones


def test_options_have_numbers_and_help():
    base = [
        {"texto": "Confirmar", "action_id": "ok"},
        {"texto": "Cancelar", "action_id": "cancel"},
    ]
    result = formatear_opciones(base)
    textos = [opt["texto"] for opt in result]
    assert textos[0].startswith("1. ")
    assert textos[1].startswith("2. ")
    assert textos[2] == "3. Repetir"
    assert textos[3] == "4. Ayuda"

