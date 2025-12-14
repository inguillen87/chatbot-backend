import os
from flask import Blueprint, request, jsonify, send_from_directory, render_template, g, url_for
from models import CatalogoItem, QA, ArchivoAdjunto, User, CatalogoModalidad, TenantProfile
from routes.auth import token_requerido
from socket_service import emit_tenant_update
from services.qdrant_search import (
    buscar_catalogo_qdrant,
    DEFAULT_SEARCH_LIMIT,
    coleccion_catalogo_para_rubro,
    CATALOGO_PYME,
    CATALOGO_MUNICIPIO,
)
from services.catalog_seed import ensure_seed_catalog
try:
    from services.upload_processor import (
        subir_catalogo as _subir_catalogo,
        CATALOGO_FOLDER,
    )
except ImportError:  # fallback for older versions
    from services.upload_processor import subir_catalogo as _subir_catalogo
    CATALOGO_FOLDER = os.path.join("data", "catalogos")
from services.common_utils import (
    calcular_precio_por_unidad,
    limpiar_texto_base,
    parse_precio_flexible,
    parse_cantidad_flexible,
)

catalogo_bp = Blueprint('catalogo', __name__, url_prefix='/catalogo')

from werkzeug.utils import secure_filename
from services.intelligent_catalog_processor import IntelligentCatalogProcessor
import tempfile

@catalogo_bp.route('/upload/form', methods=['GET'])
@token_requerido
def upload_catalog_form(user):
    token_value = request.args.get("token") or getattr(g, "auth_token", None)
    upload_endpoint = url_for('catalogo.upload_catalog')
    list_endpoint = url_for('catalogo.listar_catalogo')
    return render_template(
        'admin/pyme_catalog_upload.html',
        token=token_value,
        upload_url=upload_endpoint,
        list_url=list_endpoint,
        user=user,
    )

@catalogo_bp.route('/upload', methods=['POST'])
@token_requerido
def upload_catalog(user):
    """
    Endpoint para subir un archivo de catálogo (PDF, Excel, Word, Imagen).
    El archivo es procesado inteligentemente para extraer los productos.
    """
    if 'file' not in request.files:
        return jsonify({"error": "No se encontró el archivo ('file' part)"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No se seleccionó ningún archivo"}), 400

    if file:
        original_filename = secure_filename(file.filename)

        # Usar un directorio temporal seguro
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = os.path.join(tmpdir, original_filename)
            file.save(filepath)

            try:
                # Llamar al nuevo servicio de procesamiento inteligente
                processor = IntelligentCatalogProcessor(user_id=user.id)
                success = processor.process_file(filepath, original_filename)

                if success:
                    # Emit socket update
                    tenant = None
                    if user.tipo_chat == 'municipio' and user.municipio_id:
                        tenant = TenantProfile.query.filter_by(municipio_id=user.municipio_id).first()
                    elif getattr(user, 'rubro_id', None): # Pyme
                        tenant = TenantProfile.query.filter_by(pyme_id=user.id).first()
                        if not tenant and user.empresa_id:
                             tenant = TenantProfile.query.filter_by(pyme_id=user.empresa_id).first()

                    if tenant:
                        emit_tenant_update(tenant.slug, 'catalog_update', {'source': 'upload'})

                    return jsonify({"mensaje": "Catálogo subido y procesado exitosamente."}), 202
                else:
                    return jsonify({"error": "No se pudo procesar el catálogo. Revise los logs para más detalles."}), 500

            except Exception as e:
                # Loggear el error en un ambiente de producción
                # from flask import current_app
                # current_app.logger.error(f"Error fatal en upload_catalog: {e}", exc_info=True)
                return jsonify({"error": f"Error crítico al procesar el catálogo: {e}"}), 500

    return jsonify({"error": "Archivo no válido"}), 400


@catalogo_bp.route('/cargar', methods=['POST'])
def cargar_catalogo():
    """
    DEPRECADO: Este endpoint está obsoleto. Usar /upload en su lugar.
    Alias que reutiliza la lógica de ``subir_catalogo``.
    """
    return _subir_catalogo()


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


_CATEGORY_FALLBACK_IMAGES: dict[str, str] = {
    "educación": "https://images.unsplash.com/photo-1516383740770-fbcc5ccbece0?auto=format&fit=crop&w=900&q=80",
    "ambiente": "https://images.unsplash.com/photo-1501004318641-b39e6451bec6?auto=format&fit=crop&w=900&q=80",
    "salud": "https://images.unsplash.com/photo-1584467735871-5884e44b1f4d?auto=format&fit=crop&w=900&q=80",
    "producción local": "https://images.unsplash.com/photo-1504674900247-0877df9cc836?auto=format&fit=crop&w=900&q=80",
    "economía circular": "https://images.unsplash.com/photo-1503594384566-461fe158e797?auto=format&fit=crop&w=900&q=80",
    "deporte": "https://images.unsplash.com/photo-1431329842981-433c86325f43?auto=format&fit=crop&w=900&q=80",
}

_DEMO_IMAGE_FALLBACKS: dict[str, str] = {
    "kit-escolar": "https://images.unsplash.com/photo-1503676260728-1c00da094a0b?auto=format&fit=crop&w=900&q=80",
    "arbol-nativo": "https://images.unsplash.com/photo-1501785888041-af3ef285b470?auto=format&fit=crop&w=900&q=80",
    "bono-hospital": "https://images.unsplash.com/photo-1505751172876-fa1923c5c528?auto=format&fit=crop&w=900&q=80",
    "bolson-saludable": "https://images.unsplash.com/photo-1466978913421-dad2ebd01d17?auto=format&fit=crop&w=900&q=80",
    "canje-electronicos": "https://images.unsplash.com/photo-1518770660439-4636190af475?auto=format&fit=crop&w=900&q=80",
}

_GENERIC_PRODUCT_PLACEHOLDER = (
    "https://images.unsplash.com/photo-1503602642458-232111445657?auto=format&fit=crop&w=900&q=80"
)


def _fallback_image_for_item(imagen_url: str | None, data: dict, categoria_normalizada: str) -> str | None:
    """Return a resilient image URL for demo assets even when legacy CDN links fail."""

    if imagen_url and "cdn.chatboc.ar" not in imagen_url.lower():
        return imagen_url

    candidates = [
        data.get("sku"),
        data.get("nombre"),
        data.get("imagen_url"),
    ]
    for cand in candidates:
        if not cand:
            continue
        key = str(cand).lower()
        for demo_key, demo_url in _DEMO_IMAGE_FALLBACKS.items():
            if demo_key in key:
                return demo_url

    return _CATEGORY_FALLBACK_IMAGES.get(categoria_normalizada.lower()) or _GENERIC_PRODUCT_PLACEHOLDER


def _moneda_desde_texto(precio_str: str | None) -> str | None:
    if not precio_str:
        return None

    texto = precio_str.lower()
    if "pt" in texto or "punto" in texto:
        return "PTS"
    if "usd" in texto or "u$s" in texto:
        return "USD"
    if "$" in precio_str:
        return "ARS"
    return None


def _formatear_producto(data: dict) -> dict:
    """Normaliza un diccionario de producto al formato universal."""
    precio_pack = None
    precio_unitario = None

    precio_float = data.get("precio_float")
    precio_str = data.get("precio_str")
    moneda_detectada = _moneda_desde_texto(precio_str)

    if precio_float is not None:
        precio_pack = precio_float
        unidad_str = data.get("unidad") or data.get("presentacion", "")
        cantidad_int = parse_cantidad_flexible(unidad_str)
        if cantidad_int is not None and cantidad_int > 0:
            precio_unitario = calcular_precio_por_unidad(precio_float, cantidad_int)
        else:
            precio_unitario = precio_float # Default to pack price if quantity not parsable

    elif isinstance(precio_str, str) and precio_str.strip():
        from services.common_utils import parse_precio_flexible

        _, parsed_float, moneda_precio = parse_precio_flexible(precio_str)
        moneda_detectada = moneda_detectada or moneda_precio
        if parsed_float is not None:
            precio_pack = parsed_float
            unidad_str = data.get("unidad") or data.get("presentacion", "")
            cantidad_int = parse_cantidad_flexible(unidad_str)
            if cantidad_int is not None and cantidad_int > 0:
                precio_unitario = calcular_precio_por_unidad(parsed_float, cantidad_int)
            else:
                precio_unitario = parsed_float # Default to pack price
        else:
            precio_pack = precio_str.strip()
            precio_unitario = precio_pack # If price string couldn't be parsed to float, unit price is also the string

    if precio_unitario is None: # Fallback if it's still None
        precio_unitario = precio_pack

    if isinstance(precio_pack, str) and not precio_pack:
        precio_pack = None

    # Priorizar descripción corta si existe
    descripcion_final = data.get("descripcion_corta") or data.get("descripcion") or None

    # Información de promoción
    # En Qdrant se guarda como "promocion_texto", en CatalogoItem es "promocion_info"
    # La función procesar_y_embedear_catalogo en upload_processor.py mapea
    # prod_dict_final.get("promocion_texto", "") a CatalogoItem.promocion_info
    # Así que al leer de CatalogoItem, es "promocion_info".
    # Al leer de Qdrant (data), es "promocion_texto".
    promo_info = data.get("promocion_texto") # Desde Qdrant
    if not promo_info and "promocion_info" in data: # Desde CatalogoItem (si 'data' es un dict de su __dict__)
        promo_info = data.get("promocion_info")

    categoria_normalizada = (data.get("categoria") or data.get("categoria_qdrant", "")).strip()
    imagen_url = _fallback_image_for_item(data.get("imagen_url"), data, categoria_normalizada)

    precio_texto = precio_str or (str(precio_pack) if precio_pack is not None else None)
    precio_puntos = data.get("precio_puntos")
    moneda_estandar = moneda_detectada or data.get("moneda")
    if precio_puntos is not None and not moneda_estandar:
        moneda_estandar = "PTS"
        if precio_unitario is None:
            precio_unitario = precio_puntos

    modalidad = data.get("modalidad")
    if isinstance(modalidad, str):
        modalidad_norm = modalidad.strip().lower()
        if modalidad_norm == "venta":
            modalidad = "compra"
        elif modalidad_norm in {"donación", "donacion"}:
            modalidad = "donacion"
        elif modalidad_norm in {"canje", "puntos"}:
            modalidad = "canje"
    if not modalidad:
        if moneda_estandar == "PTS":
            modalidad = "canje"
        else:
            try:
                valor_numerico = float(precio_unitario) if precio_unitario is not None else None
            except (TypeError, ValueError):
                valor_numerico = None
            if valor_numerico == 0:
                modalidad = "donacion"
            else:
                modalidad = "compra"
    modalidad = modalidad or "compra"

    modalidad_valor = CatalogoModalidad.infer(
        data.get("modalidad"),
        moneda=moneda_estandar,
        precio_puntos=data.get("precio_puntos"),
        precio_value=precio_float if precio_float is not None else precio_pack,
    ).value

    unidad_display = data.get("unidad") or data.get("presentacion", "") or data.get("unidad_original","") or "u"

    return {
        "nombre": data.get("nombre", ""),
        "marca": data.get("marca"), # Añadido aquí para consistencia en la estructura base
        "categoria": categoria_normalizada,
        "descripcion": descripcion_final, # Usa la descripción corta si está disponible
        "promocion_info": promo_info if promo_info else None, # Añadido campo de promoción
        "sku": data.get("sku") or None,
        "presentacion": unidad_display, # Añadido fallback a unidad_original
        "unidad": unidad_display,
        "quantityLabel": unidad_display,
        "quantity_label": unidad_display,
        "talles": data.get("talles"),
        "colores": data.get("colores"),
        "precio_unitario": precio_unitario,
        "precio_pack": precio_pack if precio_pack != precio_unitario else None,
        "precio_texto": precio_texto,
        "precio_puntos":
            precio_puntos
            if moneda_estandar == "PTS" and precio_puntos is not None
            else (precio_unitario if moneda_estandar == "PTS" else None),
        "moneda": moneda_estandar,
        "modalidad": modalidad,
        "stock": data.get("cantidad") or data.get("stock"), # Qdrant tiene "stock", CatalogoItem "cantidad"
        "imagen_url": imagen_url,
        # Podríamos añadir aquí una lista de acciones sugeridas para el bot
        # "acciones_sugeridas": ["agregar_carrito", "mas_detalles"] # Ejemplo
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

    catalog_owner = _resolve_catalog_owner(user)
    if getattr(catalog_owner, "tipo_chat", None) == "municipio":
        ensure_seed_catalog(catalog_owner)

    consulta = CatalogoItem.query.options(*CatalogoItem.legacy_safe_options()).filter_by(
        user_id=catalog_owner.id
    )
    if categoria:
        consulta = consulta.filter_by(categoria=categoria)
    items = consulta.all()
    if not items:
        response = jsonify([])
        response.headers["X-Catalogo-Vacio"] = "1"
        return response

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
                "imagen_url": item.imagen_url,
                "descripcion_corta": item.descripcion_corta,
                "promocion_info": item.promocion_info,
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




@catalogo_bp.route('/resumen_catalogo', methods=['GET'])
@token_requerido
def resumen_catalogo(user):
    """Devuelve un resumen del catálogo agrupado por categoría."""
    items = (
        CatalogoItem.query.options(*CatalogoItem.legacy_safe_options())
        .filter_by(user_id=user.id)
        .all()
    )
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


@catalogo_bp.route('/publico/<int:pyme_user_id>/descargar', methods=['GET'])
def descargar_catalogo_publico(pyme_user_id):
    """
    Permite la descarga pública del catálogo más reciente de una PYME específica.
    Este endpoint no requiere token de cliente final.
    """
    from flask import current_app # Importar aquí para acceso al logger y config
    from models import User, db # Importar User y db para la sesión

    pyme_user = db.session.get(User, pyme_user_id)

    if not pyme_user or pyme_user.rol not in ["admin", "empleado"]:
        current_app.logger.warning(f"Intento de descarga de catálogo para PYME no válida o usuario no admin/empleado ID: {pyme_user_id}")
        return jsonify({"error": "PYME no encontrada o no válida."}), 404

    # Opcional: Añadir comprobación de una flag en pyme_user para permitir descarga pública
    # if not getattr(pyme_user, 'permitir_descarga_catalogo_publica', True): # Asumir True si no existe
    #     current_app.logger.info(f"Descarga pública de catálogo denegada para PYME ID: {pyme_user_id} (configuración).")
    #     return jsonify({"error": "Esta PYME no permite la descarga pública de su catálogo."}), 403

    adj = (
        ArchivoAdjunto.query.filter_by(user_id=pyme_user.id, tipo="catalogo")
        .order_by(ArchivoAdjunto.fecha.desc())
        .first()
    )
    if not adj:
        current_app.logger.info(f"No hay catálogo disponible para descarga pública para PYME ID: {pyme_user_id}")
        return jsonify({"error": "No hay catálogo disponible para esta PYME."}), 404

    # Asegurarse que CATALOGO_FOLDER es accesible aquí.
    # Si CATALOGO_FOLDER se definió al inicio del archivo, ya está disponible.
    # Si no, importarlo o definirlo.
    # from services.upload_processor import CATALOGO_FOLDER (si está allí)
    # O si está en config: current_app.config.get("CATALOGO_FOLDER_PATH")
    # Por ahora, asumimos que CATALOGO_FOLDER (definido al inicio de este archivo) es correcto.

    current_app.logger.info(f"Proporcionando descarga pública del catálogo '{adj.filename}' para PYME ID: {pyme_user_id} desde la carpeta {CATALOGO_FOLDER}")

    # Verificar que el archivo exista antes de intentar enviarlo
    if not os.path.exists(os.path.join(CATALOGO_FOLDER, adj.filename)):
        current_app.logger.error(f"El archivo de catálogo '{adj.filename}' no fue encontrado en la ruta esperada: {os.path.join(CATALOGO_FOLDER, adj.filename)} para PYME ID: {pyme_user_id}")
        return jsonify({"error": "Archivo de catálogo no encontrado en el servidor."}), 500

    return send_from_directory(CATALOGO_FOLDER, adj.filename, as_attachment=True)

@catalogo_bp.route('/compartir', methods=['POST'])
@token_requerido
def compartir_catalogo(user):
    """Comparte un catálogo con otro usuario."""
    from models import CatalogoCompartido, User, db

    data = request.get_json()
    if not data:
        return jsonify({"error": "No se proporcionaron datos"}), 400

    catalogo_id = data.get('catalogo_id')
    shared_with_email = data.get('email')

    if not catalogo_id or not shared_with_email:
        return jsonify({"error": "Faltan datos requeridos (catalogo_id, email)"}), 400

    catalogo = ArchivoAdjunto.query.filter_by(id=catalogo_id, user_id=user.id, tipo="catalogo").first()
    if not catalogo:
        return jsonify({"error": "Catálogo no encontrado o no te pertenece"}), 404

    shared_with_user = User.query.filter_by(email=shared_with_email).first()
    if not shared_with_user:
        return jsonify({"error": "Usuario con quien compartir no encontrado"}), 404

    # Verificar si ya está compartido
    existente = CatalogoCompartido.query.filter_by(
        catalogo_id=catalogo_id,
        owner_id=user.id,
        shared_with_id=shared_with_user.id
    ).first()
    if existente:
        return jsonify({"mensaje": "Este catálogo ya está compartido con este usuario."}), 200

    nuevo_compartido = CatalogoCompartido(
        catalogo_id=catalogo_id,
        owner_id=user.id,
        shared_with_id=shared_with_user.id
    )
    db.session.add(nuevo_compartido)
    db.session.commit()

    return jsonify({"mensaje": "Catálogo compartido exitosamente."}), 201

@catalogo_bp.route('/compartidos', methods=['GET'])
@token_requerido
def listar_compartidos(user):
    """Lista los catálogos que han sido compartidos con el usuario."""
    from models import CatalogoCompartido

    compartidos = CatalogoCompartido.query.filter_by(shared_with_id=user.id).all()
    data = []
    for c in compartidos:
        data.append({
            "nombre": c.catalogo.nombre_original or c.catalogo.filename,
            "url": f"/catalogo/archivo/{c.catalogo.filename}",
            "compartido_por": c.owner.email,
            "fecha_compartido": c.fecha_compartido.isoformat()
        })
    return jsonify(data)
def _resolve_catalog_owner(user: User) -> User:
    """Return the owning account that should manage the catalog entries."""

    empresa = getattr(user, "empresa", None)
    if empresa is not None:
        return empresa
    return user

