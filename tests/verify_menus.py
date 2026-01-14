
import sys
import os

# Add repo root to python path
sys.path.append(os.getcwd())

from flask import Flask
from services.common_utils import _get_main_menu_payload
from services.municipio_responder import _get_catalogo_menu

app = Flask(__name__)
app.config["BACKEND_URL"] = "https://mock-backend.com"
app.config["APP_BASE_URL"] = "https://mock-backend.com"

class MockUser:
    def __init__(self, id, nombre, empresa_id=None, municipio_id=None, tipo_chat="municipio"):
        self.id = id
        self.nombre = nombre
        self.empresa_id = empresa_id
        self.municipio_id = municipio_id
        self.tipo_chat = tipo_chat

def verify_welcome_menu():
    print("--- Verifying Welcome Menu ---")

    # Mock context
    user = MockUser(id=4, nombre="Junin Admin", municipio_id=4)
    viewer = MockUser(id=99, nombre="Marcelo")

    context = {
        "viewer_user_obj": viewer,
        "user_obj": user,
        "profile_name": "Marcelo",
        "channel": "whatsapp",
        "municipio_config_actual": {"nombre": "Municipalidad de Junín", "bot_name": "Juni"},
        "chat_db_context_data": {}
    }

    payload = _get_main_menu_payload(context)

    # Checks
    print(f"Generar Audio: {payload.get('generar_audio')}")
    print(f"Audio Text Present: {bool(payload.get('audio_text'))}")
    if payload.get('audio_text'):
        print(f"Audio Text Start: {payload.get('audio_text')[:50]}...")

    options = payload.get('categorias', [])[0].get('botones', [])
    print(f"Options Count: {len(options)}")

    has_catalogo = any(b['action_id'] == 'mostrar_menu_catalogo' for b in options)
    print(f"Has Catalogo Option: {has_catalogo}")

    if payload.get('generar_audio') is True and payload.get('audio_text') and has_catalogo:
        print("✅ Welcome Menu Verification Passed")
    else:
        print("❌ Welcome Menu Verification Failed")
    print("-" * 30)

def verify_catalog_menu():
    print("--- Verifying Catalog Menu ---")

    with app.app_context():
        # Mock context
        user = MockUser(id=4, nombre="Junin Admin", municipio_id=4)

        context = {
            "user_obj": user,
            "profile_name": "Marcelo",
            "chat_db_context_data": {}
        }

        # We need to access _get_catalogo_menu.
        # Note: _get_catalogo_menu might rely on `services.pymes` or others.
        # It takes (context).

        try:
            payload = _get_catalogo_menu(context)

            # Check buttons for links
            buttons = payload.get('options_list', [])

            link_buttons = [b for b in buttons if b.get('type') == 'url']
            print(f"Link Buttons Found: {len(link_buttons)}")

            tenant_param_found = False
            for btn in link_buttons:
                url = btn.get('url', '')
                print(f"Checking URL: {url}")
                if 'tenant_id=' in url or 'owner_id=' in url:
                    tenant_param_found = True

            if tenant_param_found:
                 print("✅ Catalog Menu Verification Passed (Tenant Params Found)")
            else:
                 print("❌ Catalog Menu Verification Failed (No Tenant Params)")

        except Exception as e:
            print(f"❌ Catalog Menu Verification Failed with Error: {e}")
            import traceback
            traceback.print_exc()

    print("-" * 30)

if __name__ == "__main__":
    verify_welcome_menu()
    verify_catalog_menu()
