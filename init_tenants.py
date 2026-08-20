
import json
import os
import builtins
import sys
import uuid
from sqlalchemy import text
from database import db
from models import User, TenantProfile, Rubro, WidgetSettings, WidgetConfig
from werkzeug.security import generate_password_hash
from utils.roles import is_authorized_superadmin_email, is_super_admin_role


def _safe_console_text(value):
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return str(value).encode(encoding, errors="replace").decode(encoding, errors="replace")


def print(*args, **kwargs):  # noqa: A001 - keep legacy bootstrap output safe.
    builtins.print(*[_safe_console_text(arg) for arg in args], **kwargs)


def _revoke_unauthorized_superadmins() -> int:
    """Remove global privileges and rotate credentials for legacy fixed accounts."""

    revoked = 0
    for user in User.query.all():
        if not is_super_admin_role(getattr(user, "rol", None)):
            continue
        if is_authorized_superadmin_email(getattr(user, "email", None)):
            continue

        tenant_bound = any(
            getattr(user, field, None)
            for field in ("tenant_id", "tenant_slug", "municipio_id", "pyme_id", "empresa_id")
        )
        user.rol = "admin" if tenant_bound else "usuario"
        user.set_password(uuid.uuid4().hex + uuid.uuid4().hex)
        user.token = str(uuid.uuid4())
        user.entity_token = None
        user.password_reset_selector = None
        user.password_reset_verifier_hash = None
        user.password_reset_sent_at = None
        db.session.add(user)
        revoked += 1

    if revoked:
        db.session.commit()
    return revoked


def fix_schema_issues():
    """Applies direct schema fixes for missing columns or constraints."""
    print("🔧 Checking schema consistency...")
    is_sqlite = 'sqlite' in str(db.engine.url)

    def safe_execute(conn, sql, description):
        try:
            conn.execute(text(sql))
            conn.commit()
            print(f"  ✅ {description} applied.")
        except Exception as e:
            print(f"  ❌ Error applying {description}: {e}")
            conn.rollback()

    try:
        with db.engine.connect() as conn:
            def column_exists(table, column):
                try:
                    if is_sqlite:
                        res = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
                        for row in res:
                            if row[1] == column: return True
                        return False
                    else:
                        check_sql = text(f"SELECT column_name FROM information_schema.columns WHERE table_name='{table}' AND column_name='{column}'")
                        result = conn.execute(check_sql).fetchone()
                        return bool(result)
                except Exception as e:
                    print(f"  ⚠️ Error checking existence of {table}.{column}: {e}")
                    conn.rollback()
                    return False

            # 1. catalogo_item.disponible
            if not column_exists('catalogo_item', 'disponible'):
                print("  ⚠️ Adding 'disponible' to 'catalogo_item'...")
                safe_execute(conn, "ALTER TABLE catalogo_item ADD COLUMN disponible BOOLEAN DEFAULT true", "Add disponible column")

            # 2. widget_config.position (Fix length)
            if not is_sqlite:
                print("  ⚠️ Checking 'widget_config.position' length...")
                safe_execute(conn, "ALTER TABLE widget_config ALTER COLUMN position TYPE VARCHAR(50)", "Fix widget_config.position length")

            # 3. widget_settings.cta_messages
            if not column_exists('widget_settings', 'cta_messages'):
                print("  ⚠️ Adding 'cta_messages' to 'widget_settings'...")
                if is_sqlite:
                    safe_execute(conn, "ALTER TABLE widget_settings ADD COLUMN cta_messages JSON DEFAULT '[]'", "Add cta_messages (sqlite)")
                else:
                    try:
                        conn.execute(text("ALTER TABLE widget_settings ADD COLUMN cta_messages JSONB DEFAULT '[]'"))
                        conn.commit()
                        print("  ✅ Add cta_messages (JSONB) applied.")
                    except Exception:
                        conn.rollback()
                        safe_execute(conn, "ALTER TABLE widget_settings ADD COLUMN cta_messages JSON DEFAULT '[]'", "Add cta_messages (JSON)")

            # 4. widget_settings.theme_config
            if not column_exists('widget_settings', 'theme_config'):
                print("  ⚠️ Adding 'theme_config' to 'widget_settings'...")
                if is_sqlite:
                    safe_execute(conn, "ALTER TABLE widget_settings ADD COLUMN theme_config JSON DEFAULT '{}'", "Add theme_config (sqlite)")
                else:
                    try:
                        conn.execute(text("ALTER TABLE widget_settings ADD COLUMN theme_config JSONB DEFAULT '{}'"))
                        conn.commit()
                        print("  ✅ Add theme_config (JSONB) applied.")
                    except Exception:
                        conn.rollback()
                        safe_execute(conn, "ALTER TABLE widget_settings ADD COLUMN theme_config JSON DEFAULT '{}'", "Add theme_config (JSON)")

            # 5. tenant_profile.is_active
            if not column_exists('tenant_profile', 'is_active'):
                print("  ⚠️ Adding 'is_active' to 'tenant_profile'...")
                safe_execute(conn, "ALTER TABLE tenant_profile ADD COLUMN is_active BOOLEAN DEFAULT true", "Add is_active to tenant_profile")

            print("✅ Schema consistency check finished.")
    except Exception as e:
        print(f"❌ Critical error in schema check: {e}")

def ensure_rubro_hierarchy():
    """Ensures the categories structure exists: Municipios, Locales Comerciales, etc."""
    print("📂 Verifying Category Hierarchy...")

    # Root: Soluciones para Sector Público (Government)
    municipios_root = Rubro.query.filter_by(clave="municipios_root").first()
    if not municipios_root:
        municipios_root = Rubro(clave="municipios_root", nombre="Soluciones para Sector Público", es_publico=True)
        db.session.add(municipios_root)
        print("  + Created Root: Soluciones para Sector Público")
    else:
        municipios_root.nombre = "Soluciones para Sector Público"
        municipios_root.es_publico = True
        db.session.add(municipios_root)

    # Root: Soluciones para Empresas (Pyme/Enterprise)
    comerciales_root = Rubro.query.filter_by(clave="comerciales_root").first()
    if not comerciales_root:
        comerciales_root = Rubro(clave="comerciales_root", nombre="Soluciones para Empresas", es_publico=True)
        db.session.add(comerciales_root)
        print("  + Created Root: Soluciones para Empresas")
    else:
        comerciales_root.nombre = "Soluciones para Empresas"
        comerciales_root.es_publico = True
        db.session.add(comerciales_root)

    db.session.flush()

    # Define Level 1 Categories under "Soluciones para Empresas"
    # Structure: (Key, Name, ParentID)
    l1_categories = [
        ("cat_alimentacion", "Alimentación y Bebidas", comerciales_root.id),
        ("cat_retail", "Retail y Comercios", comerciales_root.id),
        ("cat_servicios", "Servicios Profesionales", comerciales_root.id),
        ("cat_salud", "Salud y Bienestar", comerciales_root.id),
        ("cat_industria", "Producción e Industria", comerciales_root.id),
    ]

    l1_map = {} # Key -> Rubro Object

    for key, name, pid in l1_categories:
        cat = Rubro.query.filter_by(clave=key).first()
        if not cat:
            cat = Rubro(clave=key, nombre=name, es_publico=True, padre_id=pid)
            db.session.add(cat)
            print(f"    + Created Category L1: {name}")
        else:
            cat.nombre = name
            cat.es_publico = True
            if cat.padre_id != pid:
                cat.padre_id = pid
            db.session.add(cat)
        l1_map[key] = cat

    db.session.flush()

    # Define Level 2 (Specific Demos) -> mapped to L1 keys
    # Map demo keys to their L1 parent key
    # If a demo key isn't here, we'll try to guess or put it in root (fallback)
    demo_mapping = {
        "almacen": ("cat_alimentacion", "Almacenes y Mercados"),
        "bodega": ("cat_alimentacion", "Bodegas y Vinos"),
        "kiosco": ("cat_alimentacion", "Kioscos"),
        "ferreteria": ("cat_retail", "Ferretería y Construcción"),
        "local_comercial_general": ("cat_retail", "Indumentaria y Moda"),
        "logistica": ("cat_servicios", "Logística y Transporte"),
        "seguros": ("cat_servicios", "Seguros y Riesgos"),
        "fintech": ("cat_servicios", "Fintech y Banca"),
        "inmobiliaria": ("cat_servicios", "Inmobiliaria y Real Estate"),
        "energia": ("cat_industria", "Energía e Industria"),
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

    # Ensure 'municipio' is present even if files are missing
    if not any(d.get("key") == "municipio" for d in demos_found):
        print("  ⚠️ 'municipio' config not found on disk. Injecting default configuration...")
        demos_found.append({
            "key": "municipio",
            "nombre": "Municipio Inteligente",
            "rubro_clave": "municipio",
            "tipo_chat": "municipio",
            "welcome_title": "Municipio de Demo",
            "welcome_subtitle": "Asistente Ciudadano",
            "theme_config": {
                "mode": "system",
                "light": {"primary": "#006c3f", "secondary": "#d4a01a", "background": "#ffffff", "text": "#000000"},
                "dark": {"primary": "#005230", "secondary": "#b08516", "background": "#1a1a1a", "text": "#ffffff"}
            }
        })

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
        "servill": "info@servill.ar",
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
        desired_name = nombre

        if tipo_chat == 'municipio':
            desired_parent_id = municipios_root.id
        else:
            # Check mapping
            if rubro_clave in demo_mapping:
                parent_key, readable_name = demo_mapping[rubro_clave]
                # Optional: Update rubro name to be "Industry Standard" name instead of generic
                if readable_name:
                    desired_name = readable_name
                if parent_key in l1_map:
                    desired_parent_id = l1_map[parent_key].id

            if not desired_parent_id:
                # Fallback to general root
                desired_parent_id = comerciales_root.id

        # If not found, create it
        if not target_rubro:
             target_rubro = Rubro(clave=rubro_clave, nombre=desired_name, es_publico=True, padre_id=desired_parent_id)
             db.session.add(target_rubro)
             db.session.flush()
        else:
             # Update parent/name if needed
             target_rubro.es_publico = True
             if target_rubro.padre_id != desired_parent_id:
                 target_rubro.padre_id = desired_parent_id
             if target_rubro.nombre != desired_name:
                 target_rubro.nombre = desired_name
             db.session.add(target_rubro)

        # User
        email = email_map.get(key, f"demo+{key}@chatboc.ar")
        user = User.query.filter_by(email=email).first()
        if not user and token: user = User.query.filter_by(token=token).first()

        if not user:
            password_env = "SERVILL_BOOTSTRAP_PASSWORD" if key == "servill" else "DEMO_BOOTSTRAP_PASSWORD"
            password = os.getenv(password_env) or uuid.uuid4().hex + uuid.uuid4().hex
            rol = "admin_pyme" if key == "servill" else "admin"
            user = User(
                name=nombre,
                email=email,
                rol=rol,
                tipo_chat=tipo_chat,
                rubro_id=target_rubro.id,
                plan="enterprise",
                nombre_empresa=nombre,
                token=token or str(uuid.uuid4())
            )
            user.set_password(password)
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
            if tipo_chat == "municipio":
                tenant.municipio_id = user.id
                tenant.pyme_id = None
            else:
                tenant.pyme_id = user.id
                tenant.municipio_id = None
            db.session.add(tenant)
        else:
            # Ensure mutual exclusivity for existing tenants to avoid constraint violation
            if tipo_chat == "municipio":
                if tenant.municipio_id != user.id:
                    tenant.municipio_id = user.id
                if tenant.pyme_id is not None:
                    tenant.pyme_id = None
            elif tipo_chat == "pyme":
                if tenant.pyme_id != user.id:
                    tenant.pyme_id = user.id
                if tenant.municipio_id is not None:
                    tenant.municipio_id = None

        # WidgetSettings with CTA and Theme
        db.session.flush()
        ws = WidgetSettings.query.filter_by(tenant_id=tenant.id).first()
        if not ws:
            ws = WidgetSettings(tenant_id=tenant.id)

        ws.default_open = True
        ws.welcome_title = demo.get("welcome_title", f"Bienvenido a {nombre}")
        ws.welcome_subtitle = demo.get("welcome_subtitle", "Asistente Virtual con IA")

        # Theme Config (Dark/Light) - Use demo config if available
        # Ensure we set a valid professional default if missing
        default_theme = {
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
        ws.theme_config = demo.get("theme_config", default_theme)

        # CTAs (Call to Actions) - Professional & Industry Specific
        # Load from config or build standardized professional CTAs
        if demo.get("widget_ctas"):
             ws.cta_messages = demo.get("widget_ctas")
        elif tipo_chat == "municipio":
            ws.cta_messages = [
                {"text": "📊 Ver Dashboard Ciudadano", "action": "navigate", "payload": "/portal/dashboard"},
                {"text": "💡 Iniciar Reclamo", "action": "trigger_intent", "payload": "nuevo_reclamo"},
                {"text": "📅 Agenda Cultural", "action": "trigger_intent", "payload": "agenda_cultural"},
                {"text": "🗳️ Participación Ciudadana", "action": "navigate", "payload": "/encuestas"}
            ]
        elif key == "bodega":
             ws.cta_messages = [
                {"text": "🍷 Catálogo de Vinos", "action": "open_catalog", "payload": ""},
                {"text": "🍇 Reservar Visita", "action": "trigger_intent", "payload": "reservar_visita"},
                {"text": "🚛 Estado de Pedido", "action": "trigger_intent", "payload": "estado_pedido"},
                {"text": "📈 Dashboard Ventas", "action": "navigate", "payload": "/portal/dashboard"}
             ]
        elif key == "ferreteria":
             ws.cta_messages = [
                {"text": "🛠️ Ver Herramientas", "action": "open_catalog", "payload": ""},
                {"text": "🏗️ Cotizar Materiales", "action": "trigger_intent", "payload": "cotizar_materiales"},
                {"text": "🚚 Envíos a Obra", "action": "trigger_intent", "payload": "envios"},
                {"text": "📊 Panel de Control", "action": "navigate", "payload": "/portal/dashboard"}
             ]
        else: # Generic Pyme
            ws.cta_messages = [
                {"text": "🛍️ Ver Catálogo", "action": "open_catalog", "payload": ""},
                {"text": "🚚 Seguimiento de Pedido", "action": "trigger_intent", "payload": "estado_pedido"},
                {"text": "🔥 Promociones", "action": "trigger_intent", "payload": "promociones"},
                {"text": "📊 Dashboard Demo", "action": "navigate", "payload": "/portal/dashboard"}
             ]

        db.session.add(ws)

        # Legacy WidgetConfig fix
        wc = WidgetConfig.query.filter_by(tenant_id=tenant.id).first()
        if not wc: wc = WidgetConfig(tenant_id=tenant.id)
        wc.welcome_message = demo.get("welcome_message", f"Hola, bienvenido a {nombre}")
        db.session.add(wc)

        # Special logic for 'municipio' tenant (Widget Token)
        if key == "municipio":
            specific_token = str(os.getenv("DEMO_WIDGET_TOKEN_JUNIN") or "").strip()
            cfg = tenant.configuracion or {}
            tokens = cfg.get("widget_tokens", [])
            if isinstance(tokens, str): tokens = [tokens]
            if specific_token and specific_token not in tokens:
                tokens.append(specific_token)
                cfg["widget_tokens"] = tokens
                tenant.configuracion = cfg
                db.session.add(tenant)

        db.session.commit()

    # Platform superadmin access is provisioned through the explicit email
    # allowlist and Clerk. Reconcile historical fixed-credential accounts too.
    revoked_superadmins = _revoke_unauthorized_superadmins()
    if revoked_superadmins:
        print(f"  Revoked {revoked_superadmins} unauthorized legacy superadmin account(s).")

    mauricio = User.query.filter_by(email="mauricio@junin.com").first()
    municipio_tenant = TenantProfile.query.filter_by(slug="municipio").first()
    if mauricio and municipio_tenant:
        print(f"  🔍 Checking user mauricio@junin.com (ID: {mauricio.id}) against tenant 'municipio' (ID: {municipio_tenant.id})...")
        if mauricio.tenant_id != municipio_tenant.id:
            print(f"  🔧 Backfilling tenant_id for mauricio@junin.com: {municipio_tenant.id}")
            mauricio.tenant_id = municipio_tenant.id
            db.session.add(mauricio)
            db.session.commit()
    else:
        print("  ⚠️ User mauricio@junin.com or tenant 'municipio' not found.")

    # --- Cleanup Legacy/Orphan Rubros ---
    print("\n🧹 Cleaning up legacy rubros...")
    valid_ids = {municipios_root.id, comerciales_root.id}
    # Add L1 IDs
    for r in l1_map.values():
        valid_ids.add(r.id)

    # Re-fetch valid L2s based on our known keys
    valid_keys = set(demo_mapping.keys())
    valid_keys.add("municipio")
    valid_keys.add("municipios_root")
    valid_keys.add("comerciales_root")
    valid_keys.update(l1_map.keys())

    all_rubros = Rubro.query.all()
    for r in all_rubros:
        if r.es_publico:
            # If it's not in our known list of keys AND not a child of a valid parent, hide it.
            if r.clave not in valid_keys:
                print(f"  - Hiding legacy rubro: {r.nombre} ({r.clave})")
                r.es_publico = False
                db.session.add(r)

    db.session.commit()

    print("\n✅ Initialization complete.")

if __name__ == "__main__":
    from app import app
    with app.app_context():
        init_tenants()
