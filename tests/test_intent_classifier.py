from unittest.mock import patch

from services.intent_classifier import fast_reclamo_detect


def test_keyword_claim_routing():
    texto = "Hay un bache enorme en la calle"
    with patch("services.intent_classifier.clasificar_por_kw_y_cv", return_value="Bache") as mock_clasificar, \
         patch("services.intent_classifier.enrutar_a_reclamo", return_value={"intent": "reclamo", "categoria": "Bache"}) as mock_enrutar:
        result = fast_reclamo_detect(texto)
        mock_clasificar.assert_called_once_with(texto)
        mock_enrutar.assert_called_once_with("Bache")
        assert result == {"intent": "reclamo", "categoria": "Bache"}
