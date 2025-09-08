import unittest
from services.municipio_responder import (
    handle_contactos_utiles_inicio,
    handle_contactos_utiles_mostrar_categoria,
    ConversationState,
    CONTEXTO_MUNICIPIO,
)


class ContactosUtilesTest(unittest.TestCase):
    def test_show_contacts_and_stay_in_menu(self):
        context = {"chat_db_context_data": {}, "channel": "web"}
        resp = handle_contactos_utiles_inicio(context, None)
        self.assertIn("options_list", resp)
        buttons = resp["options_list"]
        self.assertTrue(any(b["texto"] == "Emergencias" for b in buttons))
        ctx = context["chat_db_context_data"][CONTEXTO_MUNICIPIO]
        self.assertEqual(
            ctx["estado_conversacion"],
            ConversationState.ESPERANDO_SELECCION_CONTACTO_CATEGORIA.name,
        )
        selected = ctx["contactos_categorias"]["emergencias"]
        resp2 = handle_contactos_utiles_mostrar_categoria(context, None, selected)
        self.assertIn("Policía", resp2["message_body"])
        self.assertTrue(resp2["options_list"])  # categories again
        self.assertEqual(
            context["chat_db_context_data"][CONTEXTO_MUNICIPIO]["estado_conversacion"],
            ConversationState.ESPERANDO_SELECCION_CONTACTO_CATEGORIA.name,
        )


if __name__ == "__main__":
    unittest.main()
