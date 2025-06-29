import os
from flask import Blueprint, request, jsonify, send_from_directory
from models import CatalogoItem, QA, ArchivoAdjunto
from routes.auth import token_requerido
from services.qdrant_search import (
    buscar_catalogo_qdrant,
    DEFAULT_SEARCH_LIMIT,
    coleccion_catalogo_para_rubro,
    CATALOGO_PYME,
    CATALOGO_MUNICIPIO,
)
try:
    from services.upload_processor import (
        subir_catalogo as _subir_catalogo,
        CATALOGO_FOLDER,
    )
except ImportError:  # fallback for older versions
    from services.upload_processor import subir_catalogo as _subir_catalogo
    CATALOGO_FOLDER = os.path.join("data", "catalogos")
from services.utils import (
    calcular_precio_por_unidad,
    limpiar_texto_base,
    parse_precio_flexible,
    parse_cantidad_flexible,
)

catalogo_bp = Blueprint('catalogo', __name__, url_prefix='/catalogo')


@catalogo_bp.route('/cargar', methods=['POST'])
def cargar_catalogo():
    """Alias que reutiliza la lógica de ``subir_catalogo``."""
    return _subir_catalogo()

# Import current_app for logging
from flask import current_app

@catalogo_bp.route('/public/<int:pyme_user_id>/descargar', methods=['GET'])
def descargar_catalogo_publico(pyme_user_id: int):
    """Permite a cualquier visitante descargar el catálogo más reciente de una Pyme específica."""
    current_app.logger.info(f"[CATALOGO_PUBLICO] Solicitud de descarga para pyme_user_id: {pyme_user_id}")

    adj = (
        ArchivoAdjunto.query
        .filter_by(user_id=pyme_user_id, tipo="catalogo")
        .order_by(ArchivoAdjunto.fecha.desc())
        .first()
    )

    if not adj:
        current_app.logger.warning(f"[CATALOGO_PUBLICO] No se encontró catálogo para pyme_user_id: {pyme_user_id}")
        return jsonify({"error": "Catálogo no disponible para esta empresa."}), 404

    # Construct absolute path to CATALOGO_FOLDER if it's relative
    # CATALOGO_FOLDER is typically 'data/catalogos'
    # send_from_directory expects directory relative to app root or absolute
    # For simplicity, ensure CATALOGO_FOLDER is correctly defined (e.g., relative to app.root_path or absolute)
    # Assuming CATALOGO_FOLDER is like 'data/catalogos' relative to the instance or app root.
    # If CATALOGO_FOLDER is defined like os.path.join("data", "catalogos"), it's relative to where the app runs.
    # For send_from_directory, if it's not an absolute path, it's typically relative to app.root_path.
    # Let's ensure it's robust.

    # UPLOAD_PROCESSOR_CATALOGO_FOLDER should be the same as services.upload_processor.CATALOGO_FOLDER
    # It's already imported at the top of this file.

    try:
        current_app.logger.info(f"[CATALOGO_PUBLICO] Sirviendo archivo: {adj.filename} desde {CATALOGO_FOLDER} para pyme_user_id: {pyme_user_id}. Nombre original: {adj.nombre_original}")
        return send_from_directory(
            CATALOGO_FOLDER,
            adj.filename,
            as_attachment=True,
            download_name=adj.nombre_original or adj.filename  # Use original filename for download
        )
    except FileNotFoundError:
        current_app.logger.error(f"[CATALOGO_PUBLICO] Archivo de catálogo no encontrado en el servidor: {adj.filename} para pyme_user_id: {pyme_user_id} en carpeta {CATALOGO_FOLDER}")
        return jsonify({"error": "Archivo de catálogo no encontrado en el servidor."}), 404
    except Exception as e:
        current_app.logger.error(f"[CATALOGO_PUBLICO] Error al servir archivo {adj.filename} para pyme_user_id {pyme_user_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al intentar descargar el catálogo."}), 500


@catalogo_bp.route('/archivos', methods=['GET'])
@token_requerido
def listar_archivos(user):
    """Lista los archivos de catálogo disponibles para el token."""
    catalogos = (
        ArchivoAdjunto.query.filter_by(user_id=user.id, tipo="catalogo")
        .order_by(ArchivoAdjunto.fecha.desc())
        .all()
    )
    data = [
        {"nombre": c.nombre_original or c.filename, "url": f"/catalogo/archivo/{c.filename}"}
        for c in catalogos
    ]
    return jsonify(data)


@catalogo_bp.route('/descargar', methods=['GET'])
@token_requerido
def descargar_catalogo(user):
    """Descarga el archivo de catálogo más reciente del usuario."""
    adj = (
        ArchivoAdjunto.query.filter_by(user_id=user.id, tipo="catalogo")
        .order_by(ArchivoAdjunto.fecha.desc())
        .first()
    )
    if not adj:
        return jsonify({"error": "No hay catálogo disponible"}), 404
    return send_from_directory(CATALOGO_FOLDER, adj.filename, as_attachment=True)


@catalogo_bp.route('/archivo/<path:filename>', methods=['GET'])
@token_requerido
def descargar_archivo(user, filename):
    """Devuelve el archivo del catálogo si pertenece al usuario."""
    adj = ArchivoAdjunto.query.filter_by(filename=filename, tipo="catalogo", user_id=user.id).first()
    if not adj:
        return jsonify({"error": "Archivo no encontrado"}), 404
    return send_from_directory(CATALOGO_FOLDER, filename, as_attachment=True)


def _formatear_producto(data: dict) -> dict:
    """Normaliza un diccionario de producto al formato universal."""
    precio_pack = None
    precio_unitario = None

    precio_float = data.get("precio_float")
    precio_str = data.get("precio_str")

    if precio_float is not None:
        precio_pack = precio_float
        precio_unitario = calcular_precio_por_unidad(
            precio_float, data.get("unidad") or data.get("presentacion", "")
        )
    elif isinstance(precio_str, str) and precio_str.strip():
        from services.utils import parse_precio_flexible

        _, parsed_float, _ = parse_precio_flexible(precio_str)
        if parsed_float is not None:
            precio_pack = parsed_float
            precio_unitario = calcular_precio_por_unidad(
                parsed_float, data.get("unidad") or data.get("presentacion", "")
            )
        else:
            precio_pack = precio_str.strip()

    if precio_unitario is None:
        precio_unitario = precio_pack

    if isinstance(precio_pack, str) and not precio_pack:
        precio_pack = None

    return {
        "nombre": data.get("nombre", ""),
        "categoria": data.get("categoria") or data.get("categoria_qdrant", ""),
        "descripcion": data.get("descripcion") or None,
        "sku": data.get("sku") or None,
        "presentacion": data.get("unidad") or data.get("presentacion", ""),
        "talles": data.get("talles"),
        "colores": data.get("colores"),
        "precio_unitario": precio_unitario,
        "precio_pack": precio_pack if precio_pack != precio_unitario else None,
        "stock": data.get("cantidad"),
        "marca": data.get("marca"),
        "imagen_url": data.get("imagen_url"),
    }


def _agrupar_variantes(productos: list[dict]) -> list[dict]:
    """Agrupa productos por nombre y marca consolidando sus variantes."""
    grupos: dict[tuple, dict] = {}
    for prod in productos:
        clave = (prod.get("nombre"), prod.get("marca"))
        base = grupos.setdefault(
            clave,
            {
                "nombre": prod.get("nombre"),
                "marca": prod.get("marca"),
                "categoria": prod.get("categoria"),
                "descripcion": prod.get("descripcion"),
                "sku": prod.get("sku"),
                "imagen_url": prod.get("imagen_url"),
                "variants": [],
            },
        )
        variante = {
            "presentacion": prod.get("presentacion"),
            "talles": prod.get("talles"),
            "colores": prod.get("colores"),
            "precio_unitario": prod.get("precio_unitario"),
            "precio_pack": prod.get("precio_pack"),
            "stock": prod.get("stock"),
        }
        variante = {k: v for k, v in variante.items() if v not in (None, "")}
        base["variants"].append(variante)
    return list(grupos.values())


@catalogo_bp.route('', methods=['GET'])
@token_requerido
def listar_catalogo(user, *args, **kwargs):
    categoria = request.args.get("categoria")
    precio_min = request.args.get("precio_min")
    precio_max = request.args.get("precio_max")
    stock_min = request.args.get("stock_min")

    consulta = CatalogoItem.query.filter_by(user_id=user.id)
    if categoria:
        consulta = consulta.filter_by(categoria=categoria)
    items = consulta.all()
    if not items:
        return jsonify(
            {
                "mensaje": "No hay productos cargados en el catálogo. "
                "Contactá a la empresa para más info."
            }
        )

    productos = []
    for item in items:
        prod = _formatear_producto(
            {
                "nombre": item.nombre,
                "categoria": item.categoria,
                "descripcion": item.descripcion,
                "sku": item.sku,
                "unidad": item.unidad,
                "precio_str": item.precio,
                "cantidad": item.cantidad,
                "marca": item.marca,
            }
        )

        precio_val = None
        if isinstance(prod.get("precio_unitario"), (int, float)):
            precio_val = float(prod["precio_unitario"])
        else:
            _, f_val, _ = parse_precio_flexible(str(prod.get("precio_unitario")))
            precio_val = f_val

        if precio_min and precio_val is not None and precio_val < float(precio_min):
            continue
        if precio_max and precio_val is not None and precio_val > float(precio_max):
            continue

        if stock_min:
            stock_val = parse_cantidad_flexible(prod.get("stock"))
            if stock_val is not None and stock_val < float(stock_min):
                continue

        productos.append(prod)

    productos = sorted(
        productos,
        key=lambda p: parse_precio_flexible(p.get("precio_unitario"))[1] or 0,
    )
    productos = _agrupar_variantes(productos)
    return jsonify(productos)


@catalogo_bp.route('/buscar', methods=['GET'])
@token_requerido
def buscar_en_catalogo(user):
    consulta = request.args.get('q', '')
    if not consulta:
        return jsonify([])

    try:
        limite = int(request.args.get('limite', DEFAULT_SEARCH_LIMIT))
    except (TypeError, ValueError):
        limite = DEFAULT_SEARCH_LIMIT

    coleccion = coleccion_catalogo_para_rubro(user.rubro)
    resultados = buscar_catalogo_qdrant(
        user.id,
        consulta,
        limite=limite,
        coleccion=coleccion,
    )
    productos = []
    for hit in resultados:
        if hasattr(hit, 'payload') and isinstance(hit.payload, dict):
            productos.append(_formatear_producto(hit.payload))
    productos = _agrupar_variantes(productos)
    return jsonify(productos)


@catalogo_bp.route('/faq_texto', methods=['GET'])
@token_requerido
def faq_texto(user):
    """Devuelve las preguntas y respuestas de las FAQs en texto limpio."""
    if not getattr(user, 'rubro_id', None):
        return jsonify([])
    faqs = QA.query.filter_by(rubro_id=user.rubro_id).all()
    textos = []
    for faq in faqs:
        if faq.question and faq.answer:
            texto = f"{faq.question} {faq.answer}"
            textos.append(limpiar_texto_base(texto))
    return jsonify(textos)


@catalogo_bp.route('/textos_perfil', methods=['GET'])
@token_requerido
def textos_perfil(user):
    """Devuelve los textos de catálogo preparados para el ranker."""
    items = CatalogoItem.query.filter_by(user_id=user.id).all()
    textos = [limpiar_texto_base(it.texto) for it in items if getattr(it, 'texto', None)]
    return jsonify(textos)


@catalogo_bp.route('/resumen', methods=['GET'])
@token_requerido
def resumen_catalogo(user):
    """Devuelve un resumen del catálogo agrupado por categoría."""
    items = CatalogoItem.query.filter_by(user_id=user.id).all()
    if not items:
        return jsonify({"total": 0, "categorias": []})

    categorias: dict[str, int] = {}
    for it in items:
        cat = it.categoria or "Sin categoría"
        categorias[cat] = categorias.get(cat, 0) + 1

    data = {
        "total": len(items),
        "categorias": [
            {"nombre": nombre, "cantidad": cantidad}
            for nombre, cantidad in sorted(categorias.items())
        ],
    }
    return jsonify(data)
