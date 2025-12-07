
import json
import os
import uuid
from database import db
from models import User, TenantProfile, Rubro
from werkzeug.security import generate_password_hash

def init_tenants():
    print("🚀 Initializing Tenants from demo_rubros.json...")

    json_path = os.path.join("data", "demo_rubros.json")
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            demos = json.load(f)
    except FileNotFoundError:
        print(f"❌ Could not find {json_path}")
        return

    # Map keys to emails for known legacy users to avoid duplicates
    email_map = {
        "almacen": "demo+almacen@chatboc.ar",
        "bodega": "demo+bodega@chatboc.ar",
        "ferreteria": "demo+ferreteria@chatboc.ar",
        "local_comercial_general": "demo+local@chatboc.ar",
        "medico_general": "demo+medico@chatboc.ar",
        "municipio": "municipio@chatboc.ar",
    }

    for demo in demos:
        key = demo.get("key")
        nombre = demo.get("nombre")
        rubro_clave = demo.get("rubro_clave")
        tipo_chat = demo.get("tipo_chat", "pyme")
        token = demo.get("token")

        print(f"\nProcessing '{key}' ({nombre})...")

        # 1. Rubro
        rubro = Rubro.query.filter_by(clave=rubro_clave).first()
        if not rubro:
            print(f"  Creating Rubro '{rubro_clave}'...")
            rubro = Rubro(
                clave=rubro_clave,
                nombre=nombre,
                es_publico=(tipo_chat == "municipio"),
                descripcion=demo.get("descripcion")
            )
            db.session.add(rubro)
            db.session.flush()
        else:
            print(f"  Rubro '{rubro_clave}' exists.")

        # 2. User
        email = email_map.get(key, f"demo+{key}@chatboc.ar")
        user = User.query.filter_by(email=email).first()

        # Fallback to find by token if email check fails but user might exist
        if not user and token:
             user = User.query.filter_by(token=token).first()

        if not user:
            print(f"  Creating User '{email}'...")
            user = User(
                name=nombre,
                email=email,
                rol="admin",
                tipo_chat=tipo_chat,
                rubro_id=rubro.id,
                plan="enterprise",
                nombre_empresa=nombre,
                token=token or str(uuid.uuid4())
            )
            user.set_password("demo1234")
            db.session.add(user)
            db.session.flush()
        else:
            print(f"  User '{email}' exists.")
            # Update critical fields
            user.tipo_chat = tipo_chat
            user.rubro_id = rubro.id
            if token and user.token != token:
                user.token = token
            db.session.add(user) # Mark for update

        # 3. TenantProfile
        # Use key as slug, but normalize if needed. The keys in json look slug-safe.
        slug = key
        tenant = TenantProfile.query.filter_by(slug=slug).first()

        if not tenant:
            print(f"  Creating TenantProfile '{slug}'...")
            tenant = TenantProfile(
                slug=slug,
                nombre=nombre,
                tipo=tipo_chat,
                dominio=f"{slug}.chatboc.ar",
                configuracion={
                    "menu": {"children": []},
                    "widget_tokens": []
                }
            )
            if tipo_chat == "municipio":
                tenant.municipio_id = user.id
            else:
                tenant.pyme_id = user.id

            db.session.add(tenant)
        else:
            print(f"  TenantProfile '{slug}' exists.")
            # Ensure ownership
            if tipo_chat == "municipio" and not tenant.municipio_id:
                tenant.municipio_id = user.id
            elif tipo_chat == "pyme" and not tenant.pyme_id:
                tenant.pyme_id = user.id
            db.session.add(tenant)

        # 4. Inject specific widget token for municipio if missing
        if key == "municipio":
            specific_token = "1146cb3e-eaef-4230-b54e-1c340ac062d8"
            cfg = tenant.configuracion or {}
            tokens = cfg.get("widget_tokens", [])
            if isinstance(tokens, str): tokens = [tokens]

            if specific_token not in tokens:
                print(f"  Adding specific widget token to '{slug}'...")
                tokens.append(specific_token)
                cfg["widget_tokens"] = tokens
                tenant.configuracion = cfg
                db.session.add(tenant)

        db.session.commit()

    print("\n✅ Initialization complete.")

if __name__ == "__main__":
    from app import app
    with app.app_context():
        init_tenants()
