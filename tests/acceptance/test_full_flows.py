import pytest
from unittest.mock import patch, MagicMock
from services.ui.render_whatsapp import render as render_whatsapp

@pytest.mark.legacy
@pytest.mark.contract
def test_whatsapp_renderer_tramite_flow():
    """
    Tests that the WhatsApp renderer correctly formats a 'tramite' payload
    exactly as specified in the user's document.
    """
    payload = {
        "type": "tramite",
        "title": "Licencia de Conducir",
        "summary": "Turnos y requisitos actualizados.",
        "links": [
            { "label":"Sacar turno", "url":"https://juninmendoza.gov.ar/licencia-de-conducir-junin/" },
            { "label":"Requisitos", "url":"https://juninmendoza.gov.ar/requisitos..." }
        ],
        "phones": [ { "label":"Tránsito", "number":"+54 9 261 4000000", "whatsapp":True } ],
        "location": {
            "label":"Centro de Licencias",
            "address":"Hipólito Yrigoyen 123, Junín",
            "gmap":"https://maps.google.com/?q=-33.008,-68.487"
        },
        "hours":[ { "days":"Lun-Vie","time":"08:00-13:00" } ],
        "cta": [
            { "type":"wa", "label":"Chatear con Tránsito", "to":"+5492614000000", "text":"Hola necesito un turno" },
            { "type":"tel", "label":"Llamar ahora", "to":"+542614000000" },
            { "type":"url", "label":"Ir al trámite", "url":"https://juninmendoza.gov.ar/licencia-de-conducir-junin/" }
        ]
    }

    rendered_message = render_whatsapp(payload)

    # We can't do an exact match because the order of sections is not guaranteed by dicts.
    # Instead, we check for the presence of all key parts.
    assert "*Licencia de Conducir*" in rendered_message
    assert "Turnos y requisitos actualizados." in rendered_message
    assert "*Links útiles:*\n1️⃣ Sacar turno: https://juninmendoza.gov.ar/licencia-de-conducir-junin/\n2️⃣ Requisitos: https://juninmendoza.gov.ar/requisitos..." in rendered_message
    assert "*Contacto directo:*\n• Tránsito: +54 9 261 4000000 (WhatsApp: https://wa.me/5492614000000?text=Hola)" in rendered_message
    assert "*Dónde:* Centro de Licencias — Hipólito Yrigoyen 123, Junín\n🗺️ https://maps.google.com/?q=-33.008,-68.487" in rendered_message
    assert "*Horarios:*\n• Lun-Vie: 08:00-13:00" in rendered_message
    assert "*Acciones rápidas:*" in rendered_message
    assert "• Chatear con Tránsito: https://wa.me/5492614000000?text=Hola%20necesito%20un%20turno" in rendered_message
    assert "• Llamar ahora: tel:+542614000000" in rendered_message
    assert "_Decí *menu* para volver._" in rendered_message

@pytest.mark.legacy
@pytest.mark.contract
def test_whatsapp_renderer_reclamo_flow():
    """
    Tests that the WhatsApp renderer correctly formats a 'reclamo' payload.
    """
    payload = {
        "type": "reclamo",
        "title": "Reclamo creado",
        "summary": "Tu reclamo fue generado correctamente.",
        "ticket": {
            "id": "M-566308",
            "status_url": "https://muni.ar/mi-reclamo/M-566308",
            "category": "💧 Pérdida de agua",
            "address": "Isidoro Bousquet 5500"
        },
        "cta": [
            { "type":"url", "label":"Ver estado", "url":"https://muni.ar/mi-reclamo/M-566308" }
        ]
    }

    rendered_message = render_whatsapp(payload)

    assert "*Reclamo creado*" in rendered_message
    assert "Tu reclamo fue generado correctamente." in rendered_message
    assert "*Ticket:* M-566308" in rendered_message
    assert "• Categoría: 💧 Pérdida de agua" in rendered_message
    assert "• Dirección: Isidoro Bousquet 5500" in rendered_message
    assert "• Ver estado: https://muni.ar/mi-reclamo/M-566308" in rendered_message
    assert "*Acciones rápidas:*\n• Ver estado: https://muni.ar/mi-reclamo/M-566308" in rendered_message
    assert "_Decí *menu* para volver._" in rendered_message
