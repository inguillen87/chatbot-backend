import ast
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")

from services.whatsapp_experience import _qa_playbook_payload


ROOT = Path(__file__).resolve().parents[1]


def _load_script_scenarios() -> dict[str, list[str]]:
    script_path = ROOT / "scripts" / "qa_whatsapp_flows.py"
    module = ast.parse(script_path.read_text(encoding="utf-8"), filename=str(script_path))
    for node in module.body:
        if isinstance(node, ast.Assign):
            target_names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if "QA_SCENARIOS" in target_names:
                value = ast.literal_eval(node.value)
                return {str(key): [str(item) for item in items] for key, items in value.items()}
    raise AssertionError("QA_SCENARIOS not found in scripts/qa_whatsapp_flows.py")


def _approved_template(template_id: str) -> dict:
    return {
        "id": template_id,
        "status": {"approved": True, "content_sid": f"HX_{template_id}"},
        "readiness": {"state": "approved", "severity": "ready"},
    }


def _ready_flow(flow_id: str) -> dict:
    return {
        "id": flow_id,
        "status": "ready",
        "meta_flow_blueprint": {
            "flow_name": f"chatboc_{flow_id}",
            "category": "QA",
            "endpoint_mode": "data_exchange",
            "screens": [{"id": "summary"}, {"id": "confirmation"}],
            "completion_event": f"{flow_id}_completed",
            "data_contract": ["tenant_slug", "contact_key", "session_token"],
        },
    }


def _build_playbook() -> dict:
    template_ids = {
        "welcome_menu",
        "gov_claim_sla",
        "gov_claim_created",
        "gov_claim_status_update",
        "pyme_catalog_invite",
        "order_checkout",
        "pyme_order_ready",
        "pyme_payment_link",
        "survey_invite",
        "gov_survey_invite",
        "school_family_case_created",
        "school_payment_due",
        "school_receipt_ready",
        "finance_account_onboarding",
        "finance_kyc_review",
        "finance_credit_offer",
        "finance_collection_due",
        "finance_secure_payment",
        "finance_document_signature",
        "finance_support_case",
        "finance_account_status",
        "finance_remittance_transfer",
        "finance_insurance_claim",
        "finance_fee_financing",
        "finance_tax_payment",
    }
    return _qa_playbook_payload(
        SimpleNamespace(slug="qa-tenant"),
        channel_ready=True,
        template_blueprint={
            "operational_template_groups": {
                "qa": {"items": [_approved_template(template_id) for template_id in sorted(template_ids)]}
            }
        },
        webview_blueprint={
            "flows": [
                _ready_flow("claim_tracking_helpdesk"),
                _ready_flow("catalog_order_builder"),
                _ready_flow("survey_vote"),
                _ready_flow("order_checkout"),
                _ready_flow("finance_credit_collection_signature"),
                _ready_flow("finance_account_servicing"),
                _ready_flow("finance_remittance_transfer"),
                _ready_flow("finance_insurance_claim"),
                _ready_flow("finance_fee_financing_tax"),
            ]
        },
        integration_access={"enabled": True},
    )


def test_whatsapp_qa_playbook_matches_executable_script_matrix():
    script_scenarios = _load_script_scenarios()
    playbook = _build_playbook()
    contract_scenarios = {
        str(item["id"]): [str(case) for case in item.get("script_cases", [])]
        for item in playbook.get("scenarios", [])
    }

    required_full_flows = {
        "gov_claim_text_to_tracking",
        "gov_claim_location_to_tracking",
        "gov_claim_audio_accessible",
        "pyme_catalog_order_checkout",
        "chatboc_demo_hub",
        "survey_vote_realtime",
        "school_family_case",
        "finance_onboarding_collection_signature",
        "finance_account_servicing",
        "finance_remittance_transfer",
        "finance_insurance_claim",
        "finance_fee_financing_tax",
    }

    assert required_full_flows.issubset(script_scenarios)
    assert set(contract_scenarios) == set(script_scenarios)
    assert playbook["scenario_count"] == len(script_scenarios)

    for scenario_id, script_cases in script_scenarios.items():
        assert contract_scenarios[scenario_id] == script_cases

    all_cases = [case for cases in contract_scenarios.values() for case in cases]
    assert len(all_cases) == len(set(all_cases))

    command_parts = playbook["local_command"].split()
    assert command_parts[-1] == "scripts/qa_whatsapp_flows.py"
    assert (ROOT / command_parts[-1]).exists()

    assert all(
        scenario["meta_flow_coverage"]["ready"]
        for scenario in playbook["scenarios"]
    )
