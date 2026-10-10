from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import unquote

from flask import Flask


PREVIEW_FRONTEND = "https://chatboc-r2-preview.vercel.app"
PRODUCTION_FRONTEND = "https://www.chatboc.ar"


def test_whatsapp_demo_survey_helpers_and_menu_use_configured_preview(monkeypatch):
    from routes import whatsapp_webhook

    app = Flask("whatsapp-demo-survey-preview-test")
    app.config["PUBLIC_ENCUESTAS_CANONICAL_BASE_URL"] = PREVIEW_FRONTEND
    session_context = SimpleNamespace(context_data={})
    monkeypatch.setattr(
        whatsapp_webhook,
        "safe_flag_modified",
        lambda *_args, **_kwargs: None,
    )

    with app.app_context():
        direct_url = whatsapp_webhook._chatboc_demo_survey_url("demo-prioridades")
        fallback_url = whatsapp_webhook._chatboc_demo_survey_url("")
        share_url = whatsapp_webhook._chatboc_demo_survey_share_url(
            "demo-prioridades"
        )
        menu = whatsapp_webhook._build_chatboc_surveys_payload(
            "chatboc_surveys:gobierno:1",
            session_context,
        )

    serialized_menu = json.dumps(menu, ensure_ascii=False)
    assert direct_url.startswith(f"{PREVIEW_FRONTEND}/e/demo-prioridades?")
    assert fallback_url == f"{PREVIEW_FRONTEND}/encuestas"
    assert PREVIEW_FRONTEND in unquote(share_url)
    assert menu["surveys"]
    assert all(
        str(survey.get("public_url") or "").startswith(f"{PREVIEW_FRONTEND}/e/")
        for survey in menu["surveys"]
    )
    assert PREVIEW_FRONTEND in serialized_menu

    combined_contract = "\n".join(
        (direct_url, fallback_url, unquote(share_url), serialized_menu)
    )
    assert PRODUCTION_FRONTEND not in unquote(combined_contract)
