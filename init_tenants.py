
import json
import os
import uuid
from sqlalchemy import text
from database import db
from models import User, TenantProfile, Rubro, WidgetSettings, WidgetConfig
from werkzeug.security import generate_password_hash

def fix_schema_issues():
    """Applies direct schema fixes for missing columns or constraints."""
    try:
        with db.engine.connect() as conn:
            print("🔧 Checking schema consistency...")

            # 1. catalogo_item.disponible
            check_sql = text("SELECT column_name FROM information_schema.columns WHERE table_name='catalogo_item' AND column_name='disponible'")
            if not conn.execute(check_sql).fetchone():
                print("  ⚠️ Adding 'disponible' to 'catalogo_item'...")
                conn.execute(text("ALTER TABLE catalogo_item ADD COLUMN disponible BOOLEAN DEFAULT true"))
                conn.commit()

            # 2. widget_config.position (Fix length)
            # We assume postgres. If sqlite, this might fail or be ignored, but prod is Postgres.
            try:
                conn.execute(text("ALTER TABLE widget_config ALTER COLUMN position TYPE VARCHAR(50)"))
                conn.commit()
                print("  ✅ Fixed 'widget_config.position' length.")
            except Exception as e:
                print(f"  ⚠️ Could not alter widget_config.position (might be sqlite?): {e}")

            # 3. widget_settings.cta_messages
            check_sql = text("SELECT column_name FROM information_schema.columns WHERE table_name='widget_settings' AND column_name='cta_messages'")
            if not conn.execute(check_sql).fetchone():
                print("  ⚠️ Adding 'cta_messages' to 'widget_settings'...")
                # Use JSON/JSONB depending on dialect, but usually 'JSON' works as alias in SQLAlchemy,
                # here we are writing raw SQL. 'JSONB' is postgres specific.
                try:
                    conn.execute(text("ALTER TABLE widget_settings ADD COLUMN cta_messages JSONB DEFAULT '[]'"))
                except:
                    conn.execute(text("ALTER TABLE widget_settings ADD COLUMN cta_messages JSON DEFAULT '[]'"))
                conn.commit()

            # 4. widget_settings.theme_config
            check_sql = text("SELECT column_name FROM information_schema.columns WHERE table_name='widget_settings' AND column_name='theme_config'")
            if not conn.execute(check_sql).fetchone():
                print("  ⚠️ Adding 'theme_config' to 'widget_settings'...")
                try:
                    conn.execute(text("ALTER TABLE widget_settings ADD COLUMN theme_config JSONB DEFAULT '{}'"))
                except:
                    conn.execute(text("ALTER TABLE widget_settings ADD COLUMN theme_config JSON DEFAULT '{}'"))
                conn.commit()

            print("✅ Schema fixes applied.")
    except Exception as e:
        print(f"❌ Error fixing schema: {e}")

def ensure_rubro_hierarchy():
    """Ensures the categories structure exists: Municipios, Locales Comerciales, etc."""
    print("📂 Verifying Category Hierarchy...")

    # Root: Municipios
    municipios = Rubro.query.filter_by(clave="municipios_root").first()
    if not municipios:
        municipios = Rubro(clave="municipios_root", nombre="Municipios", es_publico=True)
        db.session.add(municipios)
        print("  + Created Root: Municipios")

    # Root: Pymes (Locales Comerciales)
    comerciales = Rubro.query.filter_by(clave="comerciales_root").first()
    if not comerciales:
        comerciales = Rubro(clave="comerciales_root", nombre="Locales Comerciales", es_publico=True)
        db.session.add(comerciales)
        print("  + Created Root: Locales Comerciales")

    db.session.flush()

    # Sub-rubros for Comerciales
    subs = [
        ("almacen_bebidas", "Almacenes y Bebidas"),
        ("kioscos", "Kioscos"),
        ("indumentaria", "Indumentaria"),
        ("farmacia", "Farmacias"),
        ("ferreteria_const", "Ferretería y Construcción"),
        ("gastronomia", "Gastronomía")
    ]

    for key, name in subs:
        sub = Rubro.query.filter_by(clave=key).first()
        if not sub:
            sub = Rubro(clave=key, nombre=name, es_publico=True, padre_id=comerciales.id)
            db.session.add(sub)
            print(f"    + Created Sub: {name}")
        elif sub.padre_id != comerciales.id:
            sub.padre_id = comerciales.id
            db.session.add(sub)

    db.session.commit()
    return municipios, comerciales

def init_tenants():
    print("🚀 Initializing Tenants & Demos...")

    fix_schema_issues()
    municipios_root, comerciales_root = ensure_rubro_hierarchy()

    json_path = os.path.join("data", "demo_rubros.json")
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            demos = json.load(f)
    except FileNotFoundError:
        print(f"❌ Could not find {json_path}")
        return

    email_map = {
        "almacen": "demo+almacen@chatboc.ar",
        "bodega": "demo+bodega@chatboc.ar",
        "ferreteria": "demo+ferreteria@chatboc.ar",
        "local_comercial_general": "demo+local@chatboc.ar",
        "medico_general": "demo+medico@chatboc.ar",
        "municipio": "municipio@chatboc.ar",
    }

    # Map demo keys to specific sub-rubros
    rubro_mapping = {
        "almacen": "almacen_bebidas",
        "bodega": "almacen_bebidas",
        "ferreteria": "ferreteria_const",
        "local_comercial_general": "indumentaria",
        "medico_general": "comerciales_root", # Fallback
        "municipio": "municipios_root"
    }

    for demo in demos:
        key = demo.get("key")
        nombre = demo.get("nombre")
        rubro_clave = demo.get("rubro_clave")
        tipo_chat = demo.get("tipo_chat", "pyme")
        token = demo.get("token")

        print(f"\nProcessing '{key}' ({nombre})...")

        # Resolve correct parent rubro
        mapped_rubro_key = rubro_mapping.get(key, rubro_clave)
        target_rubro = Rubro.query.filter_by(clave=mapped_rubro_key).first()

        # If not found (e.g. medico), create it or fallback
        if not target_rubro:
             target_rubro = Rubro.query.filter_by(clave=rubro_clave).first()
             if not target_rubro:
                parent_id = municipios_root.id if tipo_chat == 'municipio' else comerciales_root.id
                target_rubro = Rubro(clave=rubro_clave, nombre=nombre, es_publico=True, padre_id=parent_id)
                db.session.add(target_rubro)
                db.session.flush()

        # User
        email = email_map.get(key, f"demo+{key}@chatboc.ar")
        user = User.query.filter_by(email=email).first()
        if not user and token: user = User.query.filter_by(token=token).first()

        if not user:
            user = User(
                name=nombre,
                email=email,
                rol="admin",
                tipo_chat=tipo_chat,
                rubro_id=target_rubro.id,
                plan="enterprise",
                nombre_empresa=nombre,
                token=token or str(uuid.uuid4())
            )
            user.set_password("demo1234")
            db.session.add(user)
            db.session.flush()
        else:
            user.rubro_id = target_rubro.id # Update rubro
            user.tipo_chat = tipo_chat
            if token: user.token = token
            db.session.add(user)

        # TenantProfile
        slug = key
        tenant = TenantProfile.query.filter_by(slug=slug).first()
        if not tenant:
            tenant = TenantProfile(
                slug=slug,
                nombre=nombre,
                tipo=tipo_chat,
                dominio=f"{slug}.chatboc.ar",
                configuracion={"menu": {"children": []}, "widget_tokens": []}
            )
            if tipo_chat == "municipio": tenant.municipio_id = user.id
            else: tenant.pyme_id = user.id
            db.session.add(tenant)
        else:
            if tipo_chat == "municipio" and not tenant.municipio_id: tenant.municipio_id = user.id
            elif tipo_chat == "pyme" and not tenant.pyme_id: tenant.pyme_id = user.id

        # WidgetSettings with CTA and Theme
        db.session.flush()
        ws = WidgetSettings.query.filter_by(tenant_id=tenant.id).first()
        if not ws:
            ws = WidgetSettings(tenant_id=tenant.id)

        ws.default_open = True
        ws.welcome_title = f"Hola, bienvenido a {nombre}"
        ws.welcome_subtitle = "Tu asistente virtual 24/7"

        # Theme Config (Dark/Light)
        ws.theme_config = {
            "mode": "system",
            "light": {
                "primary": "#0066ff",
                "secondary": "#ffffff",
                "background": "#ffffff",
                "text": "#000000"
            },
            "dark": {
                "primary": "#0052cc",
                "secondary": "#1a1a1a",
                "background": "#1a1a1a",
                "text": "#ffffff"
            }
        }

        # CTAs (Call to Actions)
        # Different CTAs for Municipio vs Pyme
        if tipo_chat == "municipio":
            ws.cta_messages = [
                {"text": "📅 Ver agenda cultural", "action": "trigger_intent", "payload": "agenda_cultural"},
                {"text": "💡 Iniciar reclamo", "action": "trigger_intent", "payload": "nuevo_reclamo"},
                {"text": "🗳️ Encuestas participativas", "action": "navigate", "payload": "/encuestas"}
            ]
        else:
            ws.cta_messages = [
                {"text": "🛍️ Ver catálogo", "action": "open_catalog", "payload": ""},
                {"text": "🚚 Seguimiento de pedido", "action": "trigger_intent", "payload": "estado_pedido"},
                {"text": "🔥 Promos del día", "action": "trigger_intent", "payload": "promociones"}
            ]

        db.session.add(ws)

        # Legacy WidgetConfig fix
        wc = WidgetConfig.query.filter_by(tenant_id=tenant.id).first()
        if not wc: wc = WidgetConfig(tenant_id=tenant.id)
        wc.welcome_message = f"Hola, bienvenido a {nombre}"
        db.session.add(wc)

        # Special logic for 'municipio' tenant (Widget Token)
        if key == "municipio":
            specific_token = "1146cb3e-eaef-4230-b54e-1c340ac062d8"
            cfg = tenant.configuracion or {}
            tokens = cfg.get("widget_tokens", [])
            if isinstance(tokens, str): tokens = [tokens]
            if specific_token not in tokens:
                tokens.append(specific_token)
                cfg["widget_tokens"] = tokens
                tenant.configuracion = cfg
                db.session.add(tenant)

        db.session.commit()

    # Super Admin & Legacy Fixes (Same as before)
    admin_email = "marcelo@chatboc.ar"
    admin_user = User.query.filter_by(email=admin_email).first()
    if not admin_user:
        admin_user = User(
            name="Marcelo SuperAdmin",
            email=admin_email,
            rol="super_admin",
            tipo_chat="pyme",
            plan="enterprise",
            nombre_empresa="Chatboc Platform",
            token=str(uuid.uuid4())
        )
        admin_user.set_password("Marcelog123")
        db.session.add(admin_user)
        db.session.commit()

    mauricio = User.query.filter_by(email="mauricio@junin.com").first()
    municipio_tenant = TenantProfile.query.filter_by(slug="municipio").first()
    if mauricio and municipio_tenant:
        if mauricio.tenant_id != municipio_tenant.id:
            mauricio.tenant_id = municipio_tenant.id
            db.session.add(mauricio)
            db.session.commit()

    print("\n✅ Initialization complete.")

if __name__ == "__main__":
    from app import app
    with app.app_context():
        init_tenants()
