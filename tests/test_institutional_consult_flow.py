import json

import pytest

from services.institutional_consult_flow import build_consult_flow, consult_options

TDF = {"topics": [{"id": "documentation", "label": "🪪 Certificados: CUD y CMO"}, {"id": "rupe", "label": "💳 Pensión RUPE"}],
       "localities": [{"label": "Ushuaia"}, {"label": "Río Grande"}, {"label": "Tolhuin"}]}
JUNIN = {"topics": [{"id": "reclamos", "label": "Reclamos"}, {"id": "tasas", "label": "Tasas municipales"},
                    {"id": "licencia", "label": "Licencia de conducir"}], "localities": []}


def _screen(artifact):
    return json.loads(artifact.as_bytes())["screens"][0]


def test_tdf_shape_matches_the_published_pilot():
    screen = _screen(build_consult_flow(organization="Conversa TDF", knowledge=TDF))
    groups = [c for c in screen["layout"]["children"] if c["type"] == "RadioButtonsGroup"]
    assert [g["name"] for g in groups] == ["ciudad", "tema"]
    assert [o["id"] for o in groups[0]["data-source"]] == ["ushuaia", "rio-grande", "tolhuin"]
    footer = screen["layout"]["children"][-1]
    assert footer["on-click-action"]["payload"] == {"ciudad": "${form.ciudad}", "tema": "${form.tema}"}


def test_a_municipality_without_cities_asks_only_the_topic():
    artifact = build_consult_flow(organization="Municipalidad de Junín", knowledge=JUNIN)
    screen = _screen(artifact)
    assert [c.get("name") for c in screen["layout"]["children"] if c["type"] == "RadioButtonsGroup"] == ["tema"]
    assert screen["title"] == "Municipalidad de Junín"
    assert "data_api_version" not in json.loads(artifact.as_bytes())


def test_options_are_cleaned_and_bounded():
    topics, _ = consult_options({"topics": [{"id": "ok", "label": "x" * 50}, {"id": "Bad Id", "label": "No"},
                                            {"id": "ok", "label": "Duplicado"}] + [{"id": f"t{i}", "label": f"Tema {i}"} for i in range(30)]})
    assert topics[0]["title"].endswith("…") and len(topics[0]["title"]) == 30
    assert len(topics) == 20 and all(t["id"] != "Bad Id" for t in topics)


def test_a_pack_without_topics_is_refused():
    with pytest.raises(ValueError):
        build_consult_flow(organization="X", knowledge={"topics": []})
