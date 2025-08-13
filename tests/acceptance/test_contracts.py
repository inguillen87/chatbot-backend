import jsonschema
import pytest

# Schema provided by the user
PAYLOAD_SCHEMA = {
  "type": "object",
  "required": ["type","title","summary"],
  "properties": {
    "type": {"enum": ["info","reclamo","tramite","noticias","menu","error"]},
    "title": {"type":"string"},
    "summary": {"type":"string"},
    "data": {"type":"object"},
    "links": {"type":"array"},
    "phones":{"type":"array"},
    "emails":{"type":"array"},
    "location":{"type":"object"},
    "cta":{"type":"array"},
    "ticket":{"type":"object"},
    "tags":{"type":"array"},
    "last_checked":{"type":"string"},
    "source":{"type":"string"}
  },
  "additionalProperties": True
}

@pytest.mark.legacy
@pytest.mark.contract
def test_payload_tramite_valido():
    """
    Tests that a sample 'tramite' payload conforms to the defined schema.
    """
    # This payload is based on the user's example
    p = {
      "type": "tramite",
      "title": "Licencia de Conducir",
      "summary": "Turnos y requisitos actualizados.",
      "links": [
        { "label":"Sacar turno", "url":"https://juninmendoza.gov.ar/licencia-de-conducir-junin/" }
      ],
      "tags":["municipio","licencias"],
      "source":"scraper"
    }
    jsonschema.validate(instance=p, schema=PAYLOAD_SCHEMA)

@pytest.mark.legacy
@pytest.mark.contract
def test_payload_invalido_sin_required_fields():
    """
    Tests that a payload missing required fields fails validation.
    """
    p = {
      "type": "tramite",
      # "title" is missing
      "summary": "Turnos y requisitos actualizados."
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=p, schema=PAYLOAD_SCHEMA)

@pytest.mark.legacy
@pytest.mark.contract
def test_payload_invalido_bad_type():
    """
    Tests that a payload with an invalid 'type' enum fails validation.
    """
    p = {
      "type": "factura", # not a valid type
      "title": "Factura",
      "summary": "Su factura"
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=p, schema=PAYLOAD_SCHEMA)
