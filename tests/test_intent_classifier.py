import pytest
from services.intent_classifier import IntentClassifier
import os

@pytest.fixture
def classifier():
    """Provides an instance of IntentClassifier."""
    intents_file = os.path.join(os.path.dirname(__file__), "..", "data", "intents.json")
    return IntentClassifier(intents_file)

def test_classify_reclamo_intent(classifier):
    """
    Tests that a text clearly indicating a complaint is classified correctly.
    """
    texto = "Quiero hacer un reclamo por un bache en la calle"
    intent, _ = classifier.classify(texto, rubro='municipios')

    assert intent is not None
    assert intent.get("categoria") == "iniciar_reclamo"

def test_classify_saludo_intent(classifier):
    """
    Tests that a simple greeting is classified correctly.
    """
    texto = "Hola buenos dias"
    intent, _ = classifier.classify(texto, rubro='municipios')

    assert intent is not None
    assert intent.get("categoria") == "saludar"


def test_specific_request_wins_over_embedded_greeting(classifier):
    intent, _ = classifier.classify(
        "Hola buenos dias, quiero hacer un reclamo por un bache en la calle",
        rubro="municipios",
    )

    assert intent is not None
    assert intent.get("categoria") == "iniciar_reclamo"

def test_no_intent_for_ambiguous_text(classifier):
    """
    Tests that ambiguous text does not trigger an intent with high confidence.
    """
    texto = "ayer fui a la plaza"
    intent, score = classifier.classify(texto, rubro='municipios')

    # Assuming the default confidence is high enough that this won't match
    assert intent is None
    assert score < classifier.min_confidence

def test_classify_with_specific_keywords(classifier):
    """
    Tests that keywords from the intents.json file are correctly identified.
    """
    texto = "informacion sobre la licencia de conducir"
    intent, _ = classifier.classify(texto, rubro='municipios')

    assert intent is not None
    # This depends on the content of intents.json, assuming it maps to this
    assert intent.get("categoria") == "licencia_de_conducir"
