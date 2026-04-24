import unittest


from routes.whatsapp_webhook import MAX_TWILIO_BODY_LENGTH, _split_message


SAMPLE_TEXT = (
    "*🗞️ Noticias Recientes*\n"
    "📰 *asfdafdasdf*\n"
    "_asdfasdfasfd_\n"
    "📅 11/10/2025 17:42 hs - 16/10/2025 21:39 hs\n"
    "sadfasfasdf\n"
    "*🎭 Próximos Eventos*\n"
    "🎭 *Encuentro Femenino de Vóley*\n"
    "_Domingo 31_\n"
    "📅 09/11/2025 06:00 hs - 16/11/2025 13:00 hs\n"
    "📍 Polideportivo Posta El Retamo\n"
    "Encuentro Femenino de Vóley\n"
    "🎭 *Torneo de Fútbol \"Desafío Libertadores\"*\n"
    "_Domingo 31_\n"
    "📅 10/11/2025 07:00 hs\n"
    "📍 Club Social y Deportivo Los Barriales\n"
    "Torneo de Fútbol \"Desafío Libertadores\"\n"
    "🎭 *Expo Educativa 2026*\n"
    "_Viernes 29_\n"
    "📅 10/11/2025 07:00 hs\n"
    "📍 Centro Universitario del Este\n"
    "Expo Educativa 2026\n"
    "🎭 *Capacitación Internacional \"Taller de Juegos\" (para docentes de jardines maternales)*\n"
    "_Viernes 29_\n"
    "📅 18/11/2025 15:30 hs\n"
    "📍 Casa del Bicentenario\n"
    "Capacitación Internacional \"Taller de Juegos\" (para docentes de jardines maternales)\n"
    "🎭 *sadfasdfsafasfasfd*\n"
    "_asdfasdfasfasfasfasf_\n"
    "📅 08/10/2025 09:00 hs - 12/10/2025 14:00 hs\n"
    "asdfasfasfasfasfasfsdaf a asdfsadfsadfasfasff asdfasf safasf 5346346346346\n"
    "https://chatbot-backend-2e14.onrender.com/media/archivos/Gemini_Generated_Image_tnfkiutnfkiutnfk_2.png\n"
    "🔗 https://www.juninmendoza.gov.ar/obras/\n"
    "🎭 *Torneo de Fútbol \"Desafío Libertadores\"*\n"
    "_Domingo 31_\n"
    "📅 07/10/2025 20:22 hs\n"
    "📍 Club Social y Deportivo Los Barriales\n"
    "Torneo de Fútbol \"Desafío Libertadores\"\n"
    "🎭 *Encuentro Femenino de Vóley*\n"
    "_Domingo 31_\n"
    "📅 07/10/2025 20:22 hs\n"
    "📍 Polideportivo Posta El Retamo\n"
    "Encuentro Femenino de Vóley\n"
    "🎭 *Entrega de Certificados del Curso de Lengua de Señas*\n"
    "_Sábado 30_\n"
    "📅 07/10/2025 20:22 hs\n"
    "📍 Centro Universitario del Este\n"
    "Entrega de Certificados del Curso de Lengua de Señas\n"
    "🎭 *Capacitación Internacional \"Taller de Juegos\" (para docentes de jardines maternales)*\n"
    "_Viernes 29_\n"
    "📅 07/10/2025 20:22 hs\n"
    "📍 Casa del Bicentenario\n"
    "Capacitación Internacional \"Taller de Juegos\" (para docentes de jardines maternales)\n"
    "🎭 *Expo Educativa 2026*\n"
    "_Viernes 29_\n"
    "📅 07/10/2025 20:22 hs\n"
    "📍 Centro Universitario del Este\n"
    "Expo Educativa 2026\n"
    "---\n"
    "Seguinos en nuestras redes:"
)


class SplitMessageTests(unittest.TestCase):
    def test_split_message_respects_twilio_byte_limit(self) -> None:
        chunks = _split_message(SAMPLE_TEXT)

        self.assertGreater(len(chunks), 1)
        reconstructed = "\n\n".join(chunk.strip() for chunk in chunks if chunk)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.encode("utf-8")), MAX_TWILIO_BODY_LENGTH)

        # Ensure we did not lose relevant content while splitting.
        self.assertIn("Encuentro Femenino de Vóley", reconstructed)
        self.assertIn("Expo Educativa 2026", reconstructed)

    def test_split_message_returns_original_when_short(self) -> None:
        text = "Hola 👋"
        chunks = _split_message(text)
        self.assertEqual([text], chunks)


if __name__ == "__main__":
    unittest.main()
