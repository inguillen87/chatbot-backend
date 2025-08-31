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

    def test_parse_text_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tmp:
            tmp.write(SAMPLE)
            path = tmp.name

        try:
            events = parse_agenda_file(path)
            self.assertEqual(len(events), 6)
            self.assertEqual(events[0]["day"], "Jueves 28")
        finally:
            os.remove(path)

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
