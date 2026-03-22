from services import spacy_loader


def test_get_spacy_model_falls_back_to_blank_spanish(monkeypatch):
    spacy_loader.get_spacy_model.cache_clear()

    def _raise_missing_model(_model_name):
        raise OSError("missing model")

    monkeypatch.setattr(spacy_loader.spacy, "load", _raise_missing_model)

    nlp = spacy_loader.get_spacy_model()

    assert nlp is not None
    assert nlp.lang == "es"
    assert nlp.vocab.vectors.shape[0] == 0

    spacy_loader.get_spacy_model.cache_clear()
