from scripts import smoke_huggingface_provider as smoke


def test_smoke_requires_token(monkeypatch):
    monkeypatch.delenv("HUGGINGFACE_API_TOKEN", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)

    result = smoke.smoke_huggingface_zero_shot()

    assert result["ok"] is False
    assert result["reason_code"] == "huggingface_token_missing"
    assert result["secret_values_printed"] is False


def test_smoke_returns_top_label_without_printing_token(monkeypatch):
    def fake_classify_zero_shot(text, labels, multi_label=False):
        return [{"label": labels[0], "score": 0.91}]

    monkeypatch.setenv("HUGGINGFACE_API_TOKEN", "hf_test_secret")

    from services import huggingface_inference_service as hf

    monkeypatch.setattr(hf, "classify_zero_shot", fake_classify_zero_shot)

    result = smoke.smoke_huggingface_zero_shot("luz rota", ["Luminaria", "Otros"])

    assert result == {
        "ok": True,
        "model": "joeddav/xlm-roberta-large-xnli",
        "top_label": "Luminaria",
        "top_score": 0.91,
        "candidate_count": 1,
        "secret_values_printed": False,
    }
