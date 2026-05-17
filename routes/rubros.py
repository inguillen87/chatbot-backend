from flask import Blueprint, jsonify, current_app, request
from models import Rubro
from services.demo_pillar_catalog import catalog_resources_for_rubro, curated_demo_rubros

# Define blueprint without prefix here so it can be mounted flexibly in app.py
# (e.g. at /rubros AND /api/rubros)
rubros_bp = Blueprint("rubros", __name__)


def load_demo_rubros(*args, **kwargs):
    from services.demo_registry import load_demo_rubros as _load_demo_rubros

    return _load_demo_rubros(*args, **kwargs)


def _widget_preview_for_rubro(item: dict) -> dict:
    """Return UX/branding presets so frontend can render premium widget cards."""

    demo = item.get("demo") if isinstance(item.get("demo"), dict) else {}
    tipo = str((demo.get("tipo_chat") or item.get("tipo_chat") or "pyme")).strip().lower()
    segment = str((demo.get("segment") or "")).strip().lower()
    education = item.get("education_profile") if isinstance(item.get("education_profile"), dict) else {}

    if education.get("is_education"):
        return {
            "preset": "education-campus",
            "primary_color": "#1D4ED8",
            "accent_color": "#10B981",
            "gradient_start": "#0F172A",
            "gradient_end": "#1D4ED8",
            "logo_animation": "academy-pulse",
            "motion_level": "pro",
            "glassmorphism": True,
        }

    if tipo == "municipio" or "gob" in segment:
        return {
            "preset": "civic-premium",
            "primary_color": "#006CFF",
            "accent_color": "#00C2FF",
            "gradient_start": "#0B1F66",
            "gradient_end": "#006CFF",
            "logo_animation": "orbit-glow",
            "motion_level": "pro",
            "glassmorphism": True,
        }

    return {
        "preset": "commerce-neon",
        "primary_color": "#7C3AED",
        "accent_color": "#22D3EE",
        "gradient_start": "#0F172A",
        "gradient_end": "#7C3AED",
        "logo_animation": "pulse-ring",
        "motion_level": "pro",
        "glassmorphism": True,
    }


def _education_profile_for_rubro(item: dict) -> dict:
    nombre = str(item.get("nombre") or "").strip().lower()
    clave = str(item.get("clave") or "").strip().lower()
    descripcion = str(item.get("descripcion") or "").strip().lower()
    text = " ".join([nombre, clave, descripcion])

    keywords = ("colegio", "escuela", "educacion", "educación", "instituto", "jardin", "jardín")
    is_education = any(keyword in text for keyword in keywords)
    if not is_education:
        return {"is_education": False}

    institution_type = "general"
    if "privad" in text:
        institution_type = "private"
    elif "public" in text or "públic" in text or "estatal" in text:
        institution_type = "public"

    return {
        "is_education": True,
        "institution_type": institution_type,
        "features": [
            "asistencia_y_inasistencias",
            "comunicaciones_familiares",
            "agenda_academica",
            "tramites_secretaria",
        ],
    }


_DEMO_ROOTS = {
    "gobierno": {"id": 1, "nombre": "Soluciones para Sector Publico", "clave": "gobierno"},
    "empresas": {"id": 2, "nombre": "Soluciones para Empresas", "clave": "empresas"},
    "educacion": {"id": 3, "nombre": "Colegios e instituciones educativas", "clave": "educacion"},
}


def _demo_parent_id(sector: str | None, tipo_chat: str | None = None) -> int:
    normalized_sector = str(sector or "").strip().lower()
    normalized_tipo = str(tipo_chat or "").strip().lower()
    if normalized_sector == "educacion":
        return 3
    if normalized_sector == "gobierno" or normalized_tipo == "municipio":
        return 1
    return 2


def _ensure_demo_roots(lista_rubros: list[dict]) -> None:
    existing_ids = {item.get("id") for item in lista_rubros}
    existing_claves = {str(item.get("clave") or "").strip().lower() for item in lista_rubros}
    for root in _DEMO_ROOTS.values():
        if root["id"] in existing_ids or root["clave"] in existing_claves:
            continue
        lista_rubros.append(
            {
                "id": root["id"],
                "nombre": root["nombre"],
                "clave": root["clave"],
                "descripcion": "Pilar demo Chatboc",
                "padre_id": None,
                "es_publico": True,
                "is_virtual": True,
                "demo_pillar": True,
            }
        )


def _append_curated_demo_rubros(lista_rubros: list[dict]) -> None:
    existing_claves = {
        str(item.get("clave") or item.get("key") or "").strip().lower()
        for item in lista_rubros
    }
    for curated in curated_demo_rubros():
        clave = str(curated.get("slug") or curated.get("key") or "").strip().lower()
        if not clave or clave in existing_claves:
            continue
        sector = str(curated.get("sector") or curated.get("pillar") or "").strip().lower()
        virtual_item = {
            "id": None,
            "nombre": curated.get("label") or clave,
            "clave": clave,
            "descripcion": f"Demo {curated.get('label') or clave}",
            "es_publico": True,
            "padre_id": _demo_parent_id(sector, curated.get("tipo_chat")),
            "is_virtual": True,
            "demo": {
                "key": clave,
                "slug": clave,
                "label": curated.get("label") or clave,
                "tipo_chat": curated.get("tipo_chat"),
                "segment": sector,
                "subsegment": curated.get("subvertical"),
                "resources": curated.get("resources") or catalog_resources_for_rubro(clave, sector),
                "sample_prompts": curated.get("sample_prompts") or [],
            },
        }
        virtual_item["education_profile"] = _education_profile_for_rubro(virtual_item)
        virtual_item["widget_preview"] = _widget_preview_for_rubro(virtual_item)
        lista_rubros.append(virtual_item)
        existing_claves.add(clave)


@rubros_bp.route("/", methods=["GET"], strict_slashes=False)
def get_all_rubros():
    """Return the list of rubros."""
    try:
        demo_mode_enabled = bool(current_app.config.get("ENABLE_DEMO_MODE", False))
        # Filter only public rubros to avoid exposing hidden legacy/test data
        # and to reduce the payload size if many hidden items exist.
        rubros = Rubro.query.filter_by(es_publico=True).order_by(Rubro.nombre.asc()).all()
        demo_entries = load_demo_rubros(require_owner=False) if demo_mode_enabled else []
        demo_lookup_by_id = {demo.rubro_id: demo for demo in demo_entries if demo.rubro_id}
        demo_lookup_by_clave = {demo.rubro_clave: demo for demo in demo_entries if demo.rubro_clave}

        lista_rubros = []
        for rubro in rubros:
            item = {
                "id": rubro.id,
                "nombre": rubro.nombre,
                "clave": rubro.clave,
                "descripcion": rubro.descripcion,
                "es_publico": bool(rubro.es_publico),
                "padre_id": rubro.padre_id,
            }
            item["education_profile"] = _education_profile_for_rubro(item)

            demo_meta = demo_lookup_by_id.get(rubro.id) or demo_lookup_by_clave.get(
                rubro.clave
            )
            if demo_meta:
                item["demo"] = demo_meta.to_public_dict()

            item["widget_preview"] = _widget_preview_for_rubro(item)
            lista_rubros.append(item)

        matched_demo_keys = {
            demo_meta.key for demo_meta in demo_lookup_by_id.values() if demo_meta
        }
        matched_demo_keys.update(
            demo_lookup_by_clave.get(rubro.clave).key
            for rubro in rubros
            if demo_lookup_by_clave.get(rubro.clave)
        )

        # Inject missing demo entries if not present
        if not lista_rubros:
            # If completely empty, assume we need full demo injection
            pass

        # Inject demos that are not matched in DB
        for demo in demo_entries:
            if demo.key in matched_demo_keys:
                continue

            # Heuristic to assign parent ID based on segment if missing from DB
            padre_id = None
            if demo.segment == "Gobiernos":
                padre_id = 1
            elif demo.segment == "Empresas":
                padre_id = 2

            virtual_item = {
                    "id": demo.rubro_id, # Might be None
                    "nombre": demo.label,
                    "clave": demo.rubro_clave or demo.key,
                    "descripcion": demo.descripcion,
                    "es_publico": True,
                    "padre_id": padre_id,
                    "demo": demo.to_public_dict(),
                    "is_virtual": True
                }
            virtual_item["education_profile"] = _education_profile_for_rubro(virtual_item)
            virtual_item["widget_preview"] = _widget_preview_for_rubro(virtual_item)
            lista_rubros.append(virtual_item)

        if demo_mode_enabled:
            _ensure_demo_roots(lista_rubros)
            _append_curated_demo_rubros(lista_rubros)

        # Check for format=tree
        if request.args.get("format") == "tree":
            return jsonify(_build_tree(lista_rubros))

        # If the client did NOT ask for tree, but the list is dominated by virtual items
        # we return the flat list. The frontend is responsible for building hierarchy if needed,
        # or calling with format=tree.
        # However, to avoid "undefined" errors if the frontend expects real IDs,
        # we ensure virtual items have temporary IDs or keys.

        return jsonify(lista_rubros)
    except Exception as e:  # pragma: no cover - log unexpected errors
        current_app.logger.exception(f"Error al obtener la lista de rubros: {e}")
        return jsonify({"error": "Error interno al obtener los rubros."}), 500

def _build_tree(flat_list):
    """Builds a nested tree structure from a flat list of rubros."""
    # Assign temporary negative IDs to virtual items without ID to allow tree building
    temp_id_counter = -1
    for item in flat_list:
        if item.get('id') is None:
            item['id'] = temp_id_counter
            temp_id_counter -= 1

    node_map = {item['id']: {**item, 'children': []} for item in flat_list}

    tree = []
    orphans = []

    for item in flat_list:
        node = node_map[item['id']]
        padre_id = item.get('padre_id')

        if padre_id and padre_id in node_map:
            parent = node_map[padre_id]
            parent['children'].append(node)
        elif padre_id:
            # Parent exists conceptually (e.g. 1 or 2) but wasn't in list?
            # If we injected roots, this shouldn't happen.
            orphans.append(node)
        else:
            # Root node
            tree.append(node)

    # If we have orphans pointing to missing roots, add them to top level as fallback
    tree.extend(orphans)

    return tree
