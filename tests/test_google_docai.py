import unittest

try:
    import pandas as pd
except Exception:  # pragma: no cover - pandas might not be installed
    pd = None

if pd is None:
    raise unittest.SkipTest("pandas no disponible")

from services.google_docai import _consolidar_filas

class DocAIConsolidationTests(unittest.TestCase):
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
