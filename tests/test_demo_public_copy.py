from services.demo_experience_contract import build_demo_experience_contract
from services.demo_pillar_catalog import category_label_for_rubro, demo_pillars
from services.demo_sandbox_contract import build_demo_whatsapp_sandbox_contract
from services.demo_surveys import build_demo_surveys_votings_contract


def _category_by_slug(slug: str) -> dict:
    return next(
        category
        for pillar in demo_pillars()
        for category in pillar.get("categories") or []
        if category.get("slug") == slug
    )


def test_public_demo_catalog_uses_curated_spanish_labels_without_changing_slugs():
    assert _category_by_slug("colegio_publico")["label"] == "Colegio público"
    assert _category_by_slug("campana_electoral")["label"] == "Campaña electoral"
    assert _category_by_slug("legislador")["label"] == "Legislador o diputado"
    assert _category_by_slug("ferreteria")["label"] == "Ferretería"
    assert _category_by_slug("medico_general")["label"] == "Clínica o consultorio"
    assert _category_by_slug("logistica")["label"] == "Logística"

    assert category_label_for_rubro("colegio_publico") == "Colegio público"
    assert category_label_for_rubro("campana_electoral") == "Campaña electoral"
    assert category_label_for_rubro("legislador") == "Legislador o diputado"
    assert category_label_for_rubro("gobiernos_y_municipios") == "Gobiernos y municipios"
    assert category_label_for_rubro("legislador_o_diputado") == "Legislador o diputado"


def test_public_demo_suggestions_and_scripts_keep_identifiers_and_fix_copy():
    experience = build_demo_experience_contract(
        tenant_type="municipio",
        rubro_label="Municipio Demo",
    )
    assert experience["tenant_type"] == "municipio"
    assert experience["guided_onboarding"]["starter_prompts"] == [
        "Quiero iniciar un reclamo por alumbrado público.",
        "Necesito saber cómo sacar un turno para licencia.",
        "¿Dónde reporto baches con ubicación?",
    ]
    assert next(
        action for action in experience["quick_actions"] if action["id"] == "qa_reclamo"
    )["intent"] == "iniciar_reclamo"

    sandbox = build_demo_whatsapp_sandbox_contract(
        tenant_slug="municipio",
        sector="gobierno",
        rubro="municipio",
        sandbox_number="+14155238886",
        join_phrase="join brief-yesterday",
    )
    scripts = {item["id"]: item for item in sandbox["scenario_scripts"]}
    assert scripts["municipal_claim_location"]["label"] == "Reclamo con ubicación"
    assert scripts["municipal_claim_location"]["expected_result"] == "ticket"
    assert scripts["municipal_live_vote"]["label"] == "Votación ciudadana"


def test_public_demo_survey_questions_use_opening_marks_and_accents():
    contract = build_demo_surveys_votings_contract(
        sector="empresas",
        tenant_slug="bodega",
        rubro="bodega",
    )
    items = {item["title"]: item for item in contract["all_items"]}

    assert items["Encuesta de preferencias de productos"]["question"] == "¿Qué línea te interesa más?"
    assert "Votación de promo de la semana" in items
    assert items["Consulta de entrega y envío"]["question"] == "¿Cómo preferís recibir tu pedido?"
    assert contract["primary_action"]["action_id"] == "mostrar_menu_encuestas"
