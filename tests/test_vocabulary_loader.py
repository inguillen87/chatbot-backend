from services import vocabulary_loader


def reset_vocab_cache():
    vocabulary_loader.get_ticket_vocabulary.cache_clear()  # type: ignore[attr-defined]
    vocabulary_loader.get_name_prefix_stopwords.cache_clear()  # type: ignore[attr-defined]


def test_stopwords_merge_spacy_and_custom(monkeypatch):
    class DummyDefaults:
        stop_words = {"El", "ella"}

    class DummyModel:
        Defaults = DummyDefaults

    monkeypatch.setattr(vocabulary_loader, "get_spacy_model", lambda: DummyModel())
    reset_vocab_cache()
    vocab = vocabulary_loader.get_ticket_vocabulary()
    assert "el" in vocab.stopwords
    assert "porfavor" in vocab.stopwords
    reset_vocab_cache()


def test_name_prefix_stopwords_shared(monkeypatch):
    class DummyDefaults:
        stop_words = set()

    class DummyModel:
        Defaults = DummyDefaults

    monkeypatch.setattr(vocabulary_loader, "get_spacy_model", lambda: DummyModel())
    reset_vocab_cache()
    vocab = vocabulary_loader.get_ticket_vocabulary()
    # Terms coming from validators and responder modules should be present once combined.
    for token in ("deseo", "hola", "solicito"):
        assert token in vocab.name_prefix_stopwords
    assert "quería" in vocab.name_prefix_stopwords
    reset_vocab_cache()


def test_name_prefix_stopwords_do_not_initialize_spacy(monkeypatch):
    reset_vocab_cache()

    def _unexpected_spacy_load():
        raise AssertionError("name-prefix vocabulary must remain dependency-light")

    monkeypatch.setattr(vocabulary_loader, "get_spacy_model", _unexpected_spacy_load)

    prefixes = vocabulary_loader.get_name_prefix_stopwords()

    assert "deseo" in prefixes
    assert "quería" in prefixes
    reset_vocab_cache()
