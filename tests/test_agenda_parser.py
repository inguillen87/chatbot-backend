import os
import tempfile
import unittest

try:
    from docx import Document
except ImportError:  # pragma: no cover - optional dependency for tests
    Document = None

from utils.agenda_parser import parse_agenda_text, parse_agenda_file

SAMPLE = """*AGENDA MUNICIPAL*

*Jueves 28*

🕑9.30 hs.
✅Entrega de reconocimientos a los cuatro primeros Presidentes del HCD en democracia.
📍HCD

*Viernes 29*
🕑10.00 hs.
✅Expo Educativa 2026
📍Centro Universitario del Este

🕑18.30 hs.
✅Capacitación Internacional "Taller de Juegos" (para docentes de jardines maternales)
📍Casa del Bicentenario 

*Sábado 30*
🕑 11.30 hs.
✅ Entrega de Certificados del Curso de Lengua de Señas
📍Centro Universitario del Este

*Domingo 31*
🕑9.00 a 16.00 hs.
✅Encuentro Femenino de Vóley
📍Polideportivo Posta El Retamo

🕑10.00 hs. 
✅Torneo de Fútbol "Desafío Libertadores"
📍 Club Social y Deportivo Los Barriales
"""

SIMPLE_SAMPLE = """¡Buenas noches!
AGENDA MUNICIPAL

Jueves 28

🕑9.30 hs.
✅Entrega de reconocimientos a los cuatro primeros Presidentes del HCD en democracia.
📍HCD

Viernes 29
🕑10.00 hs.
✅Expo Educativa 2026
📍Centro Universitario del Este

🕑18.30 hs.
✅Capacitación Internacional "Taller de Juegos" (para docentes de jardines maternales)
📍Casa del Bicentenario
"""

BULLET_SAMPLE = """Gemini_Generated_Image_tnfkiutnfkiutnfk_2.png
• 🗞️ Noticias Recientes

Nuevo parque central
Inauguración con autoridades y vecinos.
📅 11/10/2025 17:42 hs - 16/10/2025 21:39 hs

🎭 Próximos Eventos
Festival de Teatro Comunitario
Entrada libre y gratuita.
📅 08/10/2025 09:00 hs - 08/10/2025 14:00 hs
📍Teatro Municipal
/data/archivos/cartel_festival.png
🔗 https://example.com/festival
"""


class TestAgendaParser(unittest.TestCase):
    def test_parse_sample(self):
        events = parse_agenda_text(SAMPLE)
        self.assertEqual(len(events), 6)
        self.assertEqual(
            events[0],
            {
                "day": "Jueves 28",
                "time": "9.30 hs.",
                "title": "Entrega de reconocimientos a los cuatro primeros Presidentes del HCD en democracia.",
                "location": "HCD",
            },
        )
        self.assertEqual(events[-1]["location"], "Club Social y Deportivo Los Barriales")

    def test_parse_sample_without_asterisks(self):
        events = parse_agenda_text(SIMPLE_SAMPLE)
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["day"], "Jueves 28")
        self.assertEqual(events[0]["time"], "9.30 hs.")
        self.assertEqual(events[0]["location"], "HCD")

    def test_parse_text_file(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(SAMPLE)
            path = tmp.name

        try:
            events = parse_agenda_file(path)
            self.assertEqual(len(events), 6)
            self.assertEqual(events[0]["day"], "Jueves 28")
        finally:
            os.remove(path)

    def test_parse_bullet_template(self):
        events = parse_agenda_text(BULLET_SAMPLE)
        self.assertEqual(len(events), 2)
        first, second = events
        self.assertEqual(first["title"], "Nuevo parque central")
        self.assertIn("17:42 hs", first["time"])
        self.assertIn("Noticias", first.get("tags", [" "])[0])
        self.assertEqual(first.get("tipo_post"), "noticia")

        self.assertEqual(second["title"], "Festival de Teatro Comunitario")
        self.assertEqual(second.get("tipo_post"), "evento")
        self.assertEqual(second.get("location"), "Teatro Municipal")
        self.assertEqual(second.get("enlace"), "https://example.com/festival")
        self.assertTrue(second.get("imagen_url", "").endswith("cartel_festival.png"))

    @unittest.skipIf(Document is None, "python-docx not installed")
    def test_parse_docx_file(self):
        document = Document()
        for line in SAMPLE.splitlines():
            document.add_paragraph(line)

        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            document.save(tmp.name)
            path = tmp.name

        try:
            events = parse_agenda_file(path)
            self.assertEqual(events[0]["day"], "Jueves 28")
            self.assertEqual(len(events), 6)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
