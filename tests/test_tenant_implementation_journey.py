from services.tenant_implementation_journey import build_implementation_journey


def _channel(channel_id, status="ready", *, reason=None, action=True):
    ready = status == "ready"
    actions = []
    if action:
        actions = [
            {
                "id": f"open_{channel_id}",
                "label": f"Abrir {channel_id}",
                "href": f"/perfil?tab={channel_id}",
                "kind": "link",
                "primary": True,
            }
        ]
    return {
        "id": channel_id,
        "label": channel_id.replace("_", " ").title(),
        "status": status,
        "ready": ready,
        "locked": status in {"locked", "blocked"},
        "evidence": [f"estado {status}"],
        "reason_code": reason,
        "actions": actions,
    }


def _all_sources():
    ids = (
        "institutional_branding",
        "whatsapp",
        "widget",
        "templates",
        "live_chat",
        "knowledge_content",
        "team_routing",
        "crm",
        "identity_auth",
        "accessibility",
        "territorial_intelligence",
        "public_intake_security",
        "analytics_surveys",
    )
    return [_channel(channel_id) for channel_id in ids]


def test_launch_journey_is_ordered_and_exposes_only_current_next_action():
    channels = _all_sources()
    channels[0] = _channel(
        "institutional_branding",
        "action_required",
        reason="institutional_branding_incomplete",
    )
    channels[1] = _channel("whatsapp", "locked", reason="plan_full_required")

    payload = build_implementation_journey(channels)

    assert payload["contract_version"] == "tenant.implementation_journey.v1"
    assert [item["id"] for item in payload["stages"]] == [
        "institutional_identity",
        "channels",
        "knowledge",
        "team",
        "validation_release",
    ]
    assert payload["summary"]["current_stage_id"] == "institutional_identity"
    assert payload["summary"]["next_action"]["id"] == "open_institutional_branding"
    assert payload["stages"][1]["status"] == "blocked"
    assert payload["summary"]["blocked"] == 1


def test_launch_journey_never_infers_a_missing_source_as_ready():
    channels = [item for item in _all_sources() if item["id"] != "knowledge_content"]

    payload = build_implementation_journey(channels)
    knowledge = next(item for item in payload["stages"] if item["id"] == "knowledge")

    assert knowledge["status"] == "not_published"
    assert knowledge["published"] is False
    assert knowledge["ready"] is False
    assert knowledge["primary_action"] is None
    assert knowledge["reason_codes"] == ["implementation_source_not_published"]
    assert payload["summary"]["published"] == 4


def test_launch_journey_is_ready_only_when_every_published_stage_is_ready():
    payload = build_implementation_journey(_all_sources())

    assert payload["summary"] == {
        "total": 5,
        "ready": 5,
        "blocked": 0,
        "published": 5,
        "progress": 100,
        "current_stage_id": None,
        "next_action": None,
    }
    assert all(item["ready"] is True for item in payload["stages"])


def test_launch_journey_omits_api_actions_and_does_not_echo_unrelated_fields():
    channels = _all_sources()
    channels[0]["status"] = "action_required"
    channels[0]["ready"] = False
    channels[0]["actions"] = [
        {
            "id": "mutating_api",
            "label": "No exponer",
            "href": "/api/private",
            "kind": "api",
            "primary": True,
            "secret": "do-not-copy",
        },
        {
            "id": "safe_link",
            "label": "Configurar identidad",
            "href": "/perfil?tab=perfil",
            "kind": "link",
            "primary": False,
            "secret": "do-not-copy",
        },
    ]

    payload = build_implementation_journey(channels)

    assert payload["summary"]["next_action"]["id"] == "safe_link"
    assert "do-not-copy" not in str(payload)
    assert "/api/private" not in str(payload)
