import unittest
from unittest.mock import patch

from routes.encuestas_publicas import (
    ENCUESTAS_PUBLIC_CONTRACT_VERSION,
    ENCUESTAS_PUBLIC_RESPONSE_CONTRACT_VERSION,
    _serialize_public_survey_v1,
)


class EncuestasPublicContractTestCase(unittest.TestCase):
    def test_public_survey_v1_wrapper_contract(self):
        fake_survey = object()
        wrapped = {"id": 10, "slug": "satisfaccion-demo"}
        with patch("routes.encuestas_publicas._serialize_survey", return_value=wrapped):
            payload = _serialize_public_survey_v1(fake_survey)

        self.assertEqual(payload["contract_version"], ENCUESTAS_PUBLIC_CONTRACT_VERSION)
        self.assertEqual(payload["encuesta"], wrapped)

    def test_response_contract_version_constant(self):
        self.assertEqual(ENCUESTAS_PUBLIC_RESPONSE_CONTRACT_VERSION, "encuestas.public_response.v1")


if __name__ == "__main__":
    unittest.main()
