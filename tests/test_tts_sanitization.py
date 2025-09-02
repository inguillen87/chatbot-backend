import unittest
from services.tts_orchestrator import sanitize_for_tts


class TestTTSSanitization(unittest.TestCase):
    def test_removes_emojis_and_symbols(self):
        self.assertEqual(sanitize_for_tts("Hola 😄!!"), "Hola")

    def test_expands_time_abbreviation(self):
        self.assertEqual(
            sanitize_for_tts("La oficina abre a las 9 hs."),
            "La oficina abre a las 9 horas.",
        )
        self.assertEqual(
            sanitize_for_tts("Abierto 24hrs"),
            "Abierto 24 horas",
        )


if __name__ == "__main__":
    unittest.main()
