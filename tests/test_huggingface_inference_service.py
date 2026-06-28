from services import huggingface_inference_service as hf


class FakeVector:
    def __init__(self, values):
        self.values = values

    def tolist(self):
        return self.values


class FakeClassification:
    def __init__(self, label, score):
        self.label = label
        self.score = score

    def model_dump(self):
        return {"label": self.label, "score": self.score}


class FakeObject:
    def __init__(self, label, score):
        self.label = label
        self.score = score
        self.box = {"xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10}

    def model_dump(self):
        return {"label": self.label, "score": self.score, "box": self.box}


class FakeClient:
    def feature_extraction(self, text, *, model=None, normalize=None):
        return FakeVector([[0.1, 0.2, 0.3, 0.4]])

    def zero_shot_classification(self, text, *, candidate_labels, multi_label=False, model=None):
        return [FakeClassification(candidate_labels[1], 0.9), FakeClassification(candidate_labels[0], 0.1)]

    def image_classification(self, image, *, model=None, top_k=None):
        return [FakeClassification("pothole", 0.88)]

    def object_detection(self, image, *, model=None, threshold=None):
        return [FakeObject("traffic light", 0.77)]

    def image_to_text(self, image, *, model=None):
        return {"generated_text": "2 chapas galvanizadas"}


class FailingZeroShotClient:
    def zero_shot_classification(self, text, *, candidate_labels, multi_label=False, model=None):
        raise RuntimeError(
            "402 Client Error: Payment Required - You have depleted your monthly included credits. hf_test_secret"
        )


def test_embed_texts_requires_feature_flag(monkeypatch):
    monkeypatch.delenv("HUGGINGFACE_EMBEDDINGS_ENABLED", raising=False)

    assert hf.embed_texts(["hola"], expected_dimension=4) is None


def test_embed_texts_accepts_expected_dimension(monkeypatch):
    monkeypatch.setenv("HUGGINGFACE_EMBEDDINGS_ENABLED", "true")
    monkeypatch.setattr(hf, "_get_client", lambda model=None: FakeClient())

    assert hf.embed_texts(["hola"], expected_dimension=4) == [[0.1, 0.2, 0.3, 0.4]]


def test_embed_texts_rejects_dimension_mismatch(monkeypatch):
    monkeypatch.setenv("HUGGINGFACE_EMBEDDINGS_ENABLED", "true")
    monkeypatch.setattr(hf, "_get_client", lambda model=None: FakeClient())

    assert hf.embed_texts(["hola"], expected_dimension=1024) is None


def test_zero_shot_returns_sorted_dicts(monkeypatch):
    hf.clear_last_huggingface_failure()
    monkeypatch.setenv("HUGGINGFACE_ZERO_SHOT_ENABLED", "true")
    monkeypatch.setattr(hf, "_get_client", lambda model=None: FakeClient())

    result = hf.classify_zero_shot("hay una luminaria rota", ["Arbolado", "Luminaria"])

    assert result[0]["label"] == "Luminaria"
    assert result[0]["score"] == 0.9


def test_zero_shot_records_quota_failure_without_secret(monkeypatch):
    hf.clear_last_huggingface_failure()
    monkeypatch.setenv("HUGGINGFACE_API_TOKEN", "hf_test_secret")
    monkeypatch.setenv("HUGGINGFACE_ZERO_SHOT_ENABLED", "true")
    monkeypatch.setattr(hf, "_get_client", lambda model=None: FailingZeroShotClient())

    result = hf.classify_zero_shot("hay una luminaria rota", ["Arbolado", "Luminaria"])
    failure = hf.get_last_huggingface_failure()

    assert result is None
    assert failure["reason_code"] == "huggingface_quota_or_payment_required"
    assert failure["severity"] == "warning"
    assert failure["task"] == "zero_shot"
    assert "hf_test_secret" not in failure["message"]


def test_analyze_image_for_chatboc(monkeypatch):
    monkeypatch.setenv("VISION_HUGGINGFACE_ENABLED", "true")
    monkeypatch.setattr(hf, "_get_client", lambda model=None: FakeClient())

    result = hf.analyze_image_for_chatboc(b"image")

    assert result == {
        "labels": ["pothole"],
        "objects": ["traffic light"],
        "text": "2 chapas galvanizadas",
    }


def test_image_to_text_uses_optional_huggingface_model(monkeypatch):
    monkeypatch.setenv("VISION_HUGGINGFACE_ENABLED", "true")
    monkeypatch.setenv("HUGGINGFACE_IMAGE_TO_TEXT_MODEL", "test/image-to-text")
    seen = {}

    class TextClient:
        def image_to_text(self, image, *, model=None):
            seen["image"] = image
            seen["model"] = model
            return {"generated_text": "lista escrita a mano"}

    monkeypatch.setattr(hf, "_get_client", lambda model=None: TextClient())

    assert hf.image_to_text(b"image-bytes") == "lista escrita a mano"
    assert seen == {"image": b"image-bytes", "model": "test/image-to-text"}
