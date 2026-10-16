import unittest
from unittest.mock import patch

try:
    import pandas as pd
except Exception:  # pragma: no cover - pandas might not be installed
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas no disponible")

from services import google_docai
from services.google_docai import _consolidar_filas


class DocAIConsolidationTests(unittest.TestCase):
    def tearDown(self):
        google_docai.NLP_SPACY = None

    def test_text_cleanup_loads_spacy_only_for_non_empty_input(self):
        class FakeToken:
            def __init__(self, text, *, is_space=False):
                self.text = text
                self.is_space = is_space

        class FakePipeline:
            def __call__(self, _text):
                return [FakeToken("vino"), FakeToken(" ", is_space=True), FakeToken("malbec")]

        google_docai.NLP_SPACY = None
        with patch.object(
            google_docai,
            "get_spacy_model",
            return_value=FakePipeline(),
        ) as model_loader:
            self.assertEqual(google_docai.limpiar_texto_spacy(""), "")
            model_loader.assert_not_called()
            self.assertEqual(
                google_docai.limpiar_texto_spacy("  vino malbec  "),
                "vino malbec",
            )
            model_loader.assert_called_once_with()

    def test_consolidar_filas_combine(self):
        if pd is None:
            self.skipTest("pandas no disponible")
        df = pd.DataFrame([
            ["COD1", "", "", "10"],
            ["", "Vino Malbec", "750ml", ""]
        ], columns=["codigo", "nombre", "descripcion", "precio"])
        result = _consolidar_filas(df)
        self.assertEqual(len(result), 1)
        fila = result.iloc[0].tolist()
        self.assertEqual(fila[0], "COD1")
        self.assertEqual(fila[1], "Vino Malbec")
        self.assertEqual(fila[2], "750ml")
        self.assertEqual(fila[3], "10")

if __name__ == '__main__':
    unittest.main()
