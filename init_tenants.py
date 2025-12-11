
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

            is_sqlite = 'sqlite' in str(db.engine.url)

            def column_exists(table, column):
                if is_sqlite:
                    res = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
                    for row in res:
                        if row[1] == column: return True
                    return False
                else:
                    check_sql = text(f"SELECT column_name FROM information_schema.columns WHERE table_name='{table}' AND column_name='{column}'")
                    return bool(conn.execute(check_sql).fetchone())

            # 1. catalogo_item.disponible
            if not column_exists('catalogo_item', 'disponible'):
                print("  ⚠️ Adding 'disponible' to 'catalogo_item'...")
                conn.execute(text("ALTER TABLE catalogo_item ADD COLUMN disponible BOOLEAN DEFAULT true"))
                conn.commit()

            # 2. widget_config.position (Fix length)
            if not is_sqlite:
                try:
                    conn.execute(text("ALTER TABLE widget_config ALTER COLUMN position TYPE VARCHAR(50)"))
                    conn.commit()
                    print("  ✅ Fixed 'widget_config.position' length.")
                except Exception as e:
                    print(f"  ⚠️ Could not alter widget_config.position: {e}")

            # 3. widget_settings.cta_messages
            if not column_exists('widget_settings', 'cta_messages'):
                print("  ⚠️ Adding 'cta_messages' to 'widget_settings'...")
                if is_sqlite:
                    conn.execute(text("ALTER TABLE widget_settings ADD COLUMN cta_messages JSON DEFAULT '[]'"))
                else:
                    try:
                        conn.execute(text("ALTER TABLE widget_settings ADD COLUMN cta_messages JSONB DEFAULT '[]'"))
                    except:
                        conn.execute(text("ALTER TABLE widget_settings ADD COLUMN cta_messages JSON DEFAULT '[]'"))
                conn.commit()

            # 4. widget_settings.theme_config
            if not column_exists('widget_settings', 'theme_config'):
                print("  ⚠️ Adding 'theme_config' to 'widget_settings'...")
                if is_sqlite:
                    conn.execute(text("ALTER TABLE widget_settings ADD COLUMN theme_config JSON DEFAULT '{}'"))
                else:
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

    # Root: Municipios / Gobierno
    municipios_root = Rubro.query.filter_by(clave="municipios_root").first()
    if not municipios_root:
        municipios_root = Rubro(clave="municipios_root", nombre="Municipios y Gobierno", es_publico=True)
        db.session.add(municipios_root)
        print("  + Created Root: Municipios y Gobierno")

    # Root: Pymes (Locales Comerciales)
    comerciales_root = Rubro.query.filter_by(clave="comerciales_root").first()
    if not comerciales_root:
        comerciales_root = Rubro(clave="comerciales_root", nombre="Locales Comerciales", es_publico=True)
        db.session.add(comerciales_root)
        print("  + Created Root: Locales Comerciales")

    db.session.flush()

    # Define Level 1 Categories under "Locales Comerciales"
    # Structure: (Key, Name, ParentID)
    l1_categories = [
        ("cat_alimentacion", "Alimentación y Bebidas", comerciales_root.id),
        ("cat_retail", "Retail y Comercios", comerciales_root.id),
        ("cat_servicios", "Servicios Profesionales", comerciales_root.id),
        ("cat_salud", "Salud y Bienestar", comerciales_root.id),
        ("cat_produccion", "Producción e Industria", comerciales_root.id),
    ]

    l1_map = {} # Key -> Rubro Object

    for key, name, pid in l1_categories:
        cat = Rubro.query.filter_by(clave=key).first()
        if not cat:
            cat = Rubro(clave=key, nombre=name, es_publico=True, padre_id=pid)
            db.session.add(cat)
            print(f"    + Created Category L1: {name}")
        elif cat.padre_id != pid:
            cat.padre_id = pid
            db.session.add(cat)
        l1_map[key] = cat

    db.session.flush()

    # Define Level 2 (Specific Demos) -> mapped to L1 keys
    # Map demo keys to their L1 parent key
    # If a demo key isn't here, we'll try to guess or put it in root (fallback)
    demo_mapping = {
        "almacen": ("cat_alimentacion", "Almacenes"),
        "bodega": ("cat_alimentacion", "Bodegas y Vinos"), # Moved to alimentacion/bebidas per user request context
        "kiosco": ("cat_alimentacion", "Kioscos"),
        "ferreteria": ("cat_retail", "Ferretería y Construcción"),
        "local_comercial_general": ("cat_retail", "Indumentaria y Moda"),
        "logistica": ("cat_servicios", "Logística y Transporte"),
        "seguros": ("cat_servicios", "Seguros y Riesgos"),
        "fintech": ("cat_servicios", "Fintech y Banca"),
        "inmobiliaria": ("cat_servicios", "Inmobiliaria y Real Estate"),
        "energia": ("cat_servicios", "Energía e Industria"),
        "medico_general": ("cat_salud", "Salud y Medicina"),
        "farmacia": ("cat_salud", "Farmacias"),
        "gastronomia": ("cat_alimentacion", "Gastronomía"),
    }

    # We return the roots and the mapping so init_tenants can use it
    return municipios_root, comerciales_root, l1_map, demo_mapping

def init_tenants():
    print("🚀 Initializing Tenants & Demos...")

    fix_schema_issues()
    municipios_root, comerciales_root, l1_map, demo_mapping = ensure_rubro_hierarchy()

    # Discovery of Pyme Demos from Filesystem
    base_pyme_dir = os.path.join("data", "pyme", "rubros")
    if not os.path.exists(base_pyme_dir):
        print(f"❌ Base pyme dir not found: {base_pyme_dir}")
        return

    demos_found = []
    for entry in os.listdir(base_pyme_dir):
        full_path = os.path.join(base_pyme_dir, entry)
        if os.path.isdir(full_path) and entry != "default":
            config_path = os.path.join(full_path, "config.json")
            if os.path.exists(config_path):
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                        cfg["key"] = entry # Ensure key matches directory name
                        # Defaults if missing
                        if "rubro_clave" not in cfg: cfg["rubro_clave"] = entry
                        if "tipo_chat" not in cfg: cfg["tipo_chat"] = "pyme"
                        demos_found.append(cfg)
                except Exception as e:
                    print(f"⚠️ Error loading config for {entry}: {e}")

    # Also include Municipio from demo_rubros.json if needed, or handle separately.
    json_path = os.path.join("data", "demo_rubros.json")
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            legacy_demos = json.load(f)
            for d in legacy_demos:
                if d.get("key") == "municipio":
                    demos_found.append(d)
    except FileNotFoundError:
        pass

    email_map = {
        "almacen": "demo+almacen@chatboc.ar",
        "bodega": "demo+bodega@chatboc.ar",
        "ferreteria": "demo+ferreteria@chatboc.ar",
        "local_comercial_general": "demo+local@chatboc.ar",
        "medico_general": "demo+medico@chatboc.ar",
        "municipio": "municipio@chatboc.ar",
        "energia": "demo+energia@chatboc.ar",
        "inmobiliaria": "demo+inmobiliaria@chatboc.ar",
        "fintech": "demo+fintech@chatboc.ar",
        "seguros": "demo+seguros@chatboc.ar",
        "logistica": "demo+logistica@chatboc.ar",
    }

    for demo in demos_found:
        key = demo.get("key")
        nombre = demo.get("nombre")
        rubro_clave = demo.get("rubro_clave") or key
        tipo_chat = demo.get("tipo_chat", "pyme")
        token = demo.get("token")

        print(f"\nProcessing '{key}' ({nombre})...")

        # Resolve correct parent rubro
        target_rubro = Rubro.query.filter_by(clave=rubro_clave).first()

        # Determine desired parent ID based on mapping
        desired_parent_id = None
        if tipo_chat == 'municipio':
            desired_parent_id = municipios_root.id
        else:
            # Check mapping
            if rubro_clave in demo_mapping:
                parent_key, readable_name = demo_mapping[rubro_clave]
                # Update name if we want to enforce consistency
                # nombre = readable_name
                if parent_key in l1_map:
                    desired_parent_id = l1_map[parent_key].id

            if not desired_parent_id:
                # Fallback to general root
                desired_parent_id = comerciales_root.id

        # If not found, create it
        if not target_rubro:
             target_rubro = Rubro(clave=rubro_clave, nombre=nombre, es_publico=True, padre_id=desired_parent_id)
             db.session.add(target_rubro)
             db.session.flush()
        else:
             # Update parent if needed (re-organization)
             if target_rubro.padre_id != desired_parent_id:
                 target_rubro.padre_id = desired_parent_id
                 db.session.add(target_rubro)

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
        ws.welcome_title = demo.get("welcome_title", f"Hola, bienvenido a {nombre}")
        ws.welcome_subtitle = demo.get("welcome_subtitle", "Tu asistente virtual 24/7")

        # Theme Config (Dark/Light) - Use demo config if available
        ws.theme_config = demo.get("theme_config", {
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
        })

        # CTAs (Call to Actions)
        # Load from config or fallback
        if demo.get("widget_ctas"):
             ws.cta_messages = demo.get("widget_ctas")
        elif tipo_chat == "municipio":
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
        wc.welcome_message = demo.get("welcome_message", f"Hola, bienvenido a {nombre}")
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
