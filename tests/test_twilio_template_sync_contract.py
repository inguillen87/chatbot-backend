from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_twilio_content_templates.js"


def test_twilio_sync_uses_canonical_marketplace_webview_urls():
    content = SCRIPT.read_text(encoding="utf-8")

    assert "/checkout/{{" not in content
    assert "/catalogo/{{" not in content
    assert "/t/{{3}}/checkout" in content
    assert "/t/{{2}}/market" in content


def test_twilio_sync_declares_finance_templates_and_webviews():
    content = SCRIPT.read_text(encoding="utf-8")

    expected_templates = {
        "chatboc_finance_account_onboarding_v1",
        "chatboc_finance_kyc_review_v1",
        "chatboc_finance_credit_offer_v1",
        "chatboc_finance_collection_due_v1",
        "chatboc_finance_secure_payment_v1",
        "chatboc_finance_document_signature_v1",
        "chatboc_finance_support_case_v1",
        "chatboc_finance_account_status_v1",
        "chatboc_finance_remittance_transfer_v1",
        "chatboc_finance_insurance_claim_v1",
        "chatboc_finance_fee_financing_v1",
        "chatboc_finance_tax_payment_v1",
    }
    missing = [template for template in expected_templates if template not in content]

    assert missing == []
    assert "/finanzas/{{2}}/alta/" in content
    assert "/finanzas/{{2}}/operacion/" in content
    assert "/finanzas/{{2}}/cuentas/" in content
    assert "/finanzas/{{2}}/transferencias/" in content
    assert "/finanzas/{{2}}/seguros/" in content
    assert "/finanzas/{{2}}/financiacion/" in content
