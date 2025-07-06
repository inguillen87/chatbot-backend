from flask import Blueprint, request, jsonify, current_app
from models import db, User, Promocion, PromocionAlcance, CatalogoItem
from routes.auth import token_requerido, admin_o_empleado_requerido
from datetime import datetime
import uuid # Asegurar que uuid esté importado para los defaults de los modelos

promociones_bp = Blueprint('promociones_bp', __name__, url_prefix='/api/pymes/<int:pyme_id>/promociones')

# --- Función de Serialización ---
def serializar_promocion_completa(promocion: Promocion) -> dict:
    alcances_list = []
    # Asegurarse de que la relación 'alcances' esté cargada.
    # Si se usa lazy='dynamic' en el modelo, promo.alcances es una query, no una lista.
    # Se puede hacer promo.alcances.all() o quitar el lazy='dynamic' si siempre se quieren cargar.
    # Por ahora, asumimos que es iterable directamente (ej. lazy='select' o 'joined').
    # Si es lazy='dynamic', sería: for alcance in promocion.alcances.all():
    for alcance in promocion.alcances:
        alcance_data = {
            "id": alcance.id,
            "tipo_alcance": alcance.tipo_alcance,
            "catalogo_item_id": alcance.catalogo_item_id,
            "nombre_categoria": alcance.nombre_categoria,
            "nombre_marca": alcance.nombre_marca
        }
        # Intentar obtener el nombre del producto si es un alcance de producto y el item existe
        if alcance.tipo_alcance == "PRODUCTO" and alcance.catalogo_item_id:
            item = db.session.get(CatalogoItem, alcance.catalogo_item_id) # Usar db.session.get
            if item:
                alcance_data["nombre_producto_asociado"] = item.nombre
        alcances_list.append(alcance_data)

    return {
        "id": promocion.id,
        "pyme_user_id": promocion.pyme_user_id,
        "nombre_promocion": promocion.nombre_promocion,
        "descripcion_publica": promocion.descripcion_publica,
        "tipo_promocion": promocion.tipo_promocion,
        "valor_descuento": promocion.valor_descuento,
        "cantidad_condicion_x": promocion.cantidad_condicion_x,
        "cantidad_resultado_y": promocion.cantidad_resultado_y,
        "cantidad_minima_aplicable": promocion.cantidad_minima_aplicable,
        "monto_minimo_carrito": promocion.monto_minimo_carrito,
        "fecha_inicio": promocion.fecha_inicio.isoformat() if promocion.fecha_inicio else None,
        "fecha_fin": promocion.fecha_fin.isoformat() if promocion.fecha_fin else None,
        "is_active": promocion.is_active,
        "codigo_promocion": promocion.codigo_promocion,
        "uso_maximo_general": promocion.uso_maximo_general,
        "usos_actuales_general": promocion.usos_actuales_general,
        "uso_maximo_por_cliente": promocion.uso_maximo_por_cliente,
        "created_at": promocion.created_at.isoformat() if promocion.created_at else None,
        "updated_at": promocion.updated_at.isoformat() if promocion.updated_at else None,
        "alcances": alcances_list
    }

# Helper para verificar si el usuario actual tiene permisos sobre la pyme_id de la URL
def check_pyme_permission(current_user, pyme_id_from_url):
    if not current_user:
        return False
    if current_user.rol == 'admin' and (current_user.empresa_id is None or current_user.empresa_id == current_user.id) and current_user.id == pyme_id_from_url:
        return True
    if current_user.rol == 'empleado' and current_user.empresa_id == pyme_id_from_url:
        return True
    return False

@promociones_bp.route('', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def crear_promocion(current_user, pyme_id):
    if not check_pyme_permission(current_user, pyme_id):
        return jsonify({"error": "No tiene permiso para gestionar promociones de esta PYME."}), 403

    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body debe ser JSON"}), 400

    nombre = data.get('nombre_promocion')
    tipo = data.get('tipo_promocion')

    # Lista de tipos de promoción válidos (ejemplo, debería estar centralizada o ser más robusta)
    tipos_promocion_validos = [
        "PORCENTAJE_PRODUCTO", "PORCENTAJE_CATEGORIA", "PORCENTAJE_MARCA",
        "COMPRA_X_LLEVA_Y_PRODUCTOS", "CANTIDAD_MINIMA_DESCUENTO_FIJO_PRODUCTO",
        "CANTIDAD_MINIMA_DESCUENTO_PORCENTAJE_PRODUCTO",
        "TOTAL_CARRITO_DESCUENTO_PORCENTAJE", "TOTAL_CARRITO_DESCUENTO_FIJO"
    ]
    if not nombre or not tipo:
        return jsonify({"error": "nombre_promocion y tipo_promocion son requeridos."}), 400
    if tipo not in tipos_promocion_validos:
        return jsonify({"error": f"tipo_promocion inválido. Valores permitidos: {', '.join(tipos_promocion_validos)}"}), 400

    # TODO: Validaciones más específicas según el tipo_promocion
    # Ej: si es PORCENTAJE_*, valor_descuento debe existir y ser un float.
    # Ej: si es COMPRA_X_LLEVA_Y, cantidad_condicion_x y cantidad_resultado_y deben existir.

    try:
        fecha_inicio_dt = datetime.fromisoformat(data['fecha_inicio']) if data.get('fecha_inicio') else datetime.utcnow()
        fecha_fin_dt = datetime.fromisoformat(data['fecha_fin']) if data.get('fecha_fin') else None
    except ValueError:
        return jsonify({"error": "Formato de fecha_inicio o fecha_fin inválido. Usar ISO 8601 (YYYY-MM-DDTHH:MM:SS)."}), 400


    nueva_promocion = Promocion(
        pyme_user_id=pyme_id,
        nombre_promocion=nombre,
        descripcion_publica=data.get('descripcion_publica'),
        tipo_promocion=tipo,
        valor_descuento=data.get('valor_descuento'),
        cantidad_condicion_x=data.get('cantidad_condicion_x'),
        cantidad_resultado_y=data.get('cantidad_resultado_y'),
        cantidad_minima_aplicable=data.get('cantidad_minima_aplicable'),
        monto_minimo_carrito=data.get('monto_minimo_carrito'),
        fecha_inicio=fecha_inicio_dt,
        fecha_fin=fecha_fin_dt,
        is_active=data.get('is_active', True),
        codigo_promocion=data.get('codigo_promocion'),
        uso_maximo_general=data.get('uso_maximo_general'),
        uso_maximo_por_cliente=data.get('uso_maximo_por_cliente')
    )
    # El ID de Promocion (UUID) se genera automáticamente por el default en el modelo.

    alcances_data = data.get('alcances', [])
    if isinstance(alcances_data, list):
        for alcance_data in alcances_data:
            tipo_alcance = alcance_data.get('tipo_alcance')
            if not tipo_alcance or tipo_alcance not in ["PRODUCTO", "CATEGORIA", "MARCA"]:
                db.session.rollback() # Si un alcance es inválido, no crear la promo
                return jsonify({"error": f"Tipo de alcance '{tipo_alcance}' inválido en alcances."}), 400

            # Validar que los IDs/nombres necesarios existan según el tipo_alcance
            if tipo_alcance == "PRODUCTO" and not alcance_data.get('catalogo_item_id'):
                db.session.rollback()
                return jsonify({"error": "catalogo_item_id es requerido para alcance de tipo PRODUCTO."}), 400
            if tipo_alcance == "CATEGORIA" and not alcance_data.get('nombre_categoria'):
                db.session.rollback()
                return jsonify({"error": "nombre_categoria es requerido para alcance de tipo CATEGORIA."}), 400
            if tipo_alcance == "MARCA" and not alcance_data.get('nombre_marca'):
                db.session.rollback()
                return jsonify({"error": "nombre_marca es requerido para alcance de tipo MARCA."}), 400

            nuevo_alcance = PromocionAlcance(
                promocion_id=nueva_promocion.id,
                tipo_alcance=tipo_alcance,
                catalogo_item_id=alcance_data.get('catalogo_item_id'),
                nombre_categoria=alcance_data.get('nombre_categoria'),
                nombre_marca=alcance_data.get('nombre_marca')
            )
            db.session.add(nuevo_alcance) # Añadir alcances a la sesión
            # La relación se manejará por SQLAlchemy al hacer commit,
            # ya que nueva_promocion.id está disponible y se asigna a nuevo_alcance.promocion_id.
            # Alternativamente, nueva_promocion.alcances.append(nuevo_alcance) también funciona
            # si la relación está configurada con cascade.

    db.session.add(nueva_promocion) # Añadir la promoción principal a la sesión

    try:
        db.session.commit()
        return jsonify(serializar_promocion_completa(nueva_promocion)), 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error creando promoción para PYME {pyme_id}: {e}", exc_info=True)
        return jsonify({"error": f"Error interno al crear la promoción: {str(e)}"}), 500

@promociones_bp.route('', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def listar_promociones(current_user, pyme_id):
    if not check_pyme_permission(current_user, pyme_id):
        return jsonify({"error": "No tiene permiso para ver promociones de esta PYME."}), 403

    # Considerar paginación para listas largas
    # page = request.args.get('page', 1, type=int)
    # per_page = request.args.get('per_page', 20, type=int)
    # promociones_paginadas = Promocion.query.filter_by(pyme_user_id=pyme_id).order_by(Promocion.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    # promociones = promociones_paginadas.items
    # total = promociones_paginadas.total

    promociones = Promocion.query.filter_by(pyme_user_id=pyme_id).order_by(Promocion.is_active.desc(), Promocion.fecha_inicio.desc()).all()

    # Serialización mejorada para la lista
    return jsonify([serializar_promocion_completa(p) for p in promociones])


@promociones_bp.route('/<string:promocion_id>', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def obtener_promocion(current_user, pyme_id, promocion_id):
    if not check_pyme_permission(current_user, pyme_id):
        return jsonify({"error": "No tiene permiso para ver esta promoción."}), 403

    promocion = Promocion.query.filter_by(id=promocion_id, pyme_user_id=pyme_id).first_or_404()
    return jsonify(serializar_promocion_completa(promocion))


@promociones_bp.route('/<string:promocion_id>', methods=['PUT'])
@token_requerido
@admin_o_empleado_requerido
def actualizar_promocion(current_user, pyme_id, promocion_id):
    if not check_pyme_permission(current_user, pyme_id):
        return jsonify({"error": "No tiene permiso para actualizar esta promoción."}), 403

    promocion = Promocion.query.filter_by(id=promocion_id, pyme_user_id=pyme_id).first_or_404()
    data = request.get_json()
    if not data:
        return jsonify({"error": "Request body debe ser JSON"}), 400

    # Actualizar campos principales de Promocion
    for key, value in data.items():
        if hasattr(promocion, key) and key not in ['id', 'pyme_user_id', 'created_at', 'updated_at', 'alcances', 'usos_actuales_general']:
            if key in ['fecha_inicio', 'fecha_fin'] and value is not None:
                try: setattr(promocion, key, datetime.fromisoformat(value))
                except ValueError: return jsonify({"error": f"Formato de {key} inválido."}), 400
            else:
                setattr(promocion, key, value)

    # Actualizar alcances: estrategia común es borrar existentes y recrear con los nuevos.
    # Esto es más simple que hacer un diff y manejar actualizaciones/creaciones/borrados individuales.
    if 'alcances' in data and isinstance(data['alcances'], list):
        # Borrar alcances existentes para esta promoción
        PromocionAlcance.query.filter_by(promocion_id=promocion.id).delete()

        # Crear nuevos alcances
        for alcance_data in data['alcances']:
            tipo_alcance_upd = alcance_data.get('tipo_alcance')
            if not tipo_alcance_upd or tipo_alcance_upd not in ["PRODUCTO", "CATEGORIA", "MARCA"]:
                db.session.rollback()
                return jsonify({"error": f"Tipo de alcance '{tipo_alcance_upd}' inválido al actualizar."}), 400

            # Validaciones de campos requeridos para el alcance
            if tipo_alcance_upd == "PRODUCTO" and not alcance_data.get('catalogo_item_id'):
                 db.session.rollback(); return jsonify({"error": "catalogo_item_id es req. para alcance PRODUCTO."}), 400
            # ... (validaciones similares para CATEGORIA y MARCA) ...

            nuevo_alcance_upd = PromocionAlcance(
                promocion_id=promocion.id,
                tipo_alcance=tipo_alcance_upd,
                catalogo_item_id=alcance_data.get('catalogo_item_id'),
                nombre_categoria=alcance_data.get('nombre_categoria'),
                nombre_marca=alcance_data.get('nombre_marca')
            )
            db.session.add(nuevo_alcance_upd)

    try:
        db.session.commit()
        return jsonify(serializar_promocion_completa(promocion)), 200
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error actualizando promoción {promocion_id} PYME {pyme_id}: {e}", exc_info=True)
        return jsonify({"error": f"Error interno al actualizar: {str(e)}"}), 500


@promociones_bp.route('/<string:promocion_id>', methods=['DELETE'])
@token_requerido
@admin_o_empleado_requerido
def eliminar_promocion(current_user, pyme_id, promocion_id):
    if not check_pyme_permission(current_user, pyme_id):
        return jsonify({"error": "No tiene permiso para eliminar esta promoción."}), 403

    promocion = Promocion.query.filter_by(id=promocion_id, pyme_user_id=pyme_id).first_or_404()
    try:
        # Los alcances se borran en cascada por la configuración del modelo (cascade="all, delete-orphan")
        db.session.delete(promocion)
        db.session.commit()
        return jsonify({"mensaje": "Promoción eliminada"}), 200 # o 204 No Content
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error eliminando promoción {promocion_id} PYME {pyme_id}: {e}", exc_info=True)
        return jsonify({"error": f"Error interno al eliminar: {str(e)}"}), 500

# Endpoints para activar/desactivar
@promociones_bp.route('/<string:promocion_id>/activar', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def activar_promocion(current_user, pyme_id, promocion_id):
    if not check_pyme_permission(current_user, pyme_id):
        return jsonify({"error": "No tiene permiso para modificar esta promoción."}), 403
    promocion = Promocion.query.filter_by(id=promocion_id, pyme_user_id=pyme_id).first_or_404()
    promocion.is_active = True
    try:
        db.session.commit()
        return jsonify(serializar_promocion_completa(promocion)), 200
    except Exception as e:
        db.session.rollback(); return jsonify({"error": str(e)}), 500

@promociones_bp.route('/<string:promocion_id>/desactivar', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def desactivar_promocion(current_user, pyme_id, promocion_id):
    if not check_pyme_permission(current_user, pyme_id):
        return jsonify({"error": "No tiene permiso para modificar esta promoción."}), 403
    promocion = Promocion.query.filter_by(id=promocion_id, pyme_user_id=pyme_id).first_or_404()
    promocion.is_active = False
    try:
        db.session.commit()
        return jsonify(serializar_promocion_completa(promocion)), 200
    except Exception as e:
        db.session.rollback(); return jsonify({"error": str(e)}), 500
