import unittest
from unittest.mock import patch

import services.llm_utils as llm_utils

class TestLLMUtilsFallback(unittest.TestCase):
    def test_extract_contact_uses_llm_bridge(self):
        with patch("services.llm_utils.llamar_llm_para_generacion_texto") as mock_llm:
            mock_llm.return_value = '{"nombre_cliente": "Ana"}'
            result = llm_utils.extract_multiple_contact_details_llm(
                "Mi nombre es Ana", ["nombre_cliente"], use_llm=True
            )
            self.assertEqual(result, {"nombre_cliente": "Ana"})
            mock_llm.assert_called_once()

if __name__ == "__main__":
    unittest.main()
