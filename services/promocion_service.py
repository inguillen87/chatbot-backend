import logging
from datetime import datetime
from typing import List, Optional, Dict, Any
from sqlalchemy import func, or_ # Importar func y or_
from sqlalchemy.orm import aliased # Importar aliased
from models import db, Promocion, PromocionAlcance, CatalogoItem, User # User para pyme_user_id
from services.common_utils import parse_precio_flexible # Para obtener precio float del item

logger = logging.getLogger(__name__)

class PromocionService:

    @staticmethod
    def get_promocion_by_id(promocion_id: str, pyme_user_id: int) -> Optional[Promocion]:
        return Promocion.query.filter_by(id=promocion_id, pyme_user_id=pyme_user_id).first()

    @staticmethod
    def get_promociones_for_pyme(pyme_user_id: int, activas_unicamente: bool = False) -> List[Promocion]:
        query = Promocion.query.filter_by(pyme_user_id=pyme_user_id)
        if activas_unicamente:
            now = datetime.utcnow()
            query = query.filter(
                Promocion.is_active == True, # noqa E712
                Promocion.fecha_inicio <= now,
                (Promocion.fecha_fin == None) | (Promocion.fecha_fin >= now) # noqa E711
            )
        return query.order_by(Promocion.created_at.desc()).all()

    # Las funciones CRUD (crear, actualizar, eliminar) están mayormente en las rutas (routes/promociones.py)
    # por simplicidad en este ejemplo. En una app más grande, estarían aquí.

    @staticmethod
    def obtener_promociones_aplicables_a_item(
        pyme_user_id: int,
        item_catalogo: CatalogoItem,
        cantidad: int = 1,
        cliente_user_id: Optional[int] = None # Para promos con límite por cliente
    ) -> List[Dict[str, Any]]:
        """
        Busca y devuelve promociones activas que aplican a un CatalogoItem específico.
        Devuelve una lista de diccionarios con la promoción y detalles del descuento.
        """
        now = datetime.utcnow()
        promociones_aplicables_info = []

        # Construir la query base para promociones activas de la PYME
        query_promociones_base = Promocion.query.filter(
            Promocion.pyme_user_id == pyme_user_id,
            Promocion.is_active == True, # noqa E712
            Promocion.fecha_inicio <= now,
            (Promocion.fecha_fin == None) | (Promocion.fecha_fin >= now) # noqa E711
        )

        Alcance = aliased(PromocionAlcance)

        condiciones_alcance = []
        condiciones_alcance.append(
            (Alcance.tipo_alcance == "PRODUCTO") & (Alcance.catalogo_item_id == item_catalogo.id)
        )
        if item_catalogo.categoria: # Asegurarse que item_catalogo.categoria no sea None o vacío
            condiciones_alcance.append(
                (Alcance.tipo_alcance == "CATEGORIA") & (func.lower(Alcance.nombre_categoria) == func.lower(item_catalogo.categoria))
            )
        if item_catalogo.marca: # Asegurarse que item_catalogo.marca no sea None o vacío
            condiciones_alcance.append(
                (Alcance.tipo_alcance == "MARCA") & (func.lower(Alcance.nombre_marca) == func.lower(item_catalogo.marca))
            )

        query_promociones_con_alcance = query_promociones_base.join(
            Promocion.alcances.of_type(Alcance)
        ).filter(or_(*condiciones_alcance)).distinct()

        for promo in query_promociones_con_alcance.all():
            if promo.tipo_promocion in ["TOTAL_CARRITO_DESCUENTO_PORCENTAJE", "TOTAL_CARRITO_DESCUENTO_FIJO"]:
                continue

            if promo.tipo_promocion.startswith("CANTIDAD_MINIMA_") and \
               (promo.cantidad_minima_aplicable is not None and cantidad < promo.cantidad_minima_aplicable):
                logger.debug(f"Promo {promo.id} no aplica a item {item_catalogo.id} por cantidad insuficiente ({cantidad} < {promo.cantidad_minima_aplicable})")
                continue

            # TODO: Verificar límites de uso (general y por cliente)

            precio_original_item_str, precio_original_item_float, moneda_original = parse_precio_flexible(item_catalogo.precio)

            if precio_original_item_float is None:
                logger.warning(f"Item {item_catalogo.id} ('{item_catalogo.nombre}') no tiene precio parseable ('{item_catalogo.precio}'), no se puede aplicar promo {promo.id}")
                continue

            promo_info_para_item = {
                "promocion_id": promo.id,
                "nombre_promocion": promo.nombre_promocion,
                "descripcion_publica": promo.descripcion_publica,
                "tipo_promocion": promo.tipo_promocion,
                "precio_original_unitario": precio_original_item_float,
                "precio_con_descuento_unitario": precio_original_item_float,
                "descuento_aplicado_total_items": 0.0,
                "aplica_a_cantidad": cantidad,
                "moneda": moneda_original or "ARS"
            }

            if promo.tipo_promocion.startswith("PORCENTAJE_") and promo.valor_descuento is not None:
                descuento_unitario = precio_original_item_float * (promo.valor_descuento / 100.0)
                promo_info_para_item["precio_con_descuento_unitario"] = round(precio_original_item_float - descuento_unitario, 2)
                promo_info_para_item["descuento_aplicado_total_items"] = round(descuento_unitario * cantidad, 2)

            elif promo.tipo_promocion == "CANTIDAD_MINIMA_DESCUENTO_FIJO_PRODUCTO" and promo.valor_descuento is not None:
                if promo.cantidad_minima_aplicable is None or cantidad >= promo.cantidad_minima_aplicable:
                    descuento_total_para_grupo = promo.valor_descuento
                    precio_total_original_grupo = precio_original_item_float * cantidad
                    precio_total_con_descuento_grupo = precio_total_original_grupo - descuento_total_para_grupo

                    promo_info_para_item["precio_con_descuento_unitario"] = round(precio_total_con_descuento_grupo / cantidad, 2) if cantidad > 0 else precio_original_item_float
                    promo_info_para_item["descuento_aplicado_total_items"] = round(descuento_total_para_grupo, 2)

            elif promo.tipo_promocion == "COMPRA_X_LLEVA_Y_PRODUCTOS" and \
                 promo.cantidad_condicion_x is not None and promo.cantidad_resultado_y is not None and \
                 promo.cantidad_condicion_x > 0 and promo.cantidad_resultado_y > promo.cantidad_condicion_x:

                if cantidad >= promo.cantidad_resultado_y:
                    num_grupos_promo = cantidad // promo.cantidad_resultado_y
                    items_pagados_en_grupos_promo = num_grupos_promo * promo.cantidad_condicion_x
                    items_gratis_en_grupos_promo = num_grupos_promo * (promo.cantidad_resultado_y - promo.cantidad_condicion_x)
                    items_restantes_fuera_promo = cantidad % promo.cantidad_resultado_y

                    costo_total = (items_pagados_en_grupos_promo + items_restantes_fuera_promo) * precio_original_item_float
                    descuento_total_promo = (items_gratis_en_grupos_promo * precio_original_item_float)

                    promo_info_para_item["precio_con_descuento_unitario"] = round(costo_total / cantidad, 2) if cantidad > 0 else precio_original_item_float
                    promo_info_para_item["descuento_aplicado_total_items"] = round(descuento_total_promo, 2)
                    promo_info_para_item["descripcion_publica"] = f"{promo.descripcion_publica} (Llevas {promo.cantidad_resultado_y}, pagas {promo.cantidad_condicion_x})"

            if promo_info_para_item["descuento_aplicado_total_items"] > 0:
                promociones_aplicables_info.append(promo_info_para_item)

        promociones_aplicables_info.sort(key=lambda p: p["descuento_aplicado_total_items"], reverse=True)
        return promociones_aplicables_info

    @staticmethod
    def aplicar_promociones_al_carrito(pyme_user_id: int, items_carrito: List[Dict[str, Any]], cliente_user_id: Optional[int] = None) -> Dict[str, Any]:
        logger.info(f"Aplicando promociones al carrito para PYME {pyme_user_id}, {len(items_carrito)} tipos de items.")
        items_finales_con_promo = []
        total_original_carrito_calculado = 0.0
        total_descuentos_items_carrito = 0.0
        promociones_aplicadas_al_carrito_nombres = set()

        for item_carr_info in items_carrito:
            item_catalogo = db.session.get(CatalogoItem, item_carr_info["catalogo_item_id"])
            if not item_catalogo:
                logger.warning(f"Item de catálogo ID {item_carr_info['catalogo_item_id']} no encontrado al aplicar promos al carrito.")
                # Añadir el item sin promo para que el carrito siga siendo consistente
                precio_orig_str_faltante, precio_orig_float_faltante, moneda_orig_faltante = parse_precio_flexible(str(item_carr_info.get("precio_unitario_original", "0")))
                subtotal_original_faltante = item_carr_info["cantidad"] * (precio_orig_float_faltante or 0.0)
                total_original_carrito_calculado += subtotal_original_faltante
                items_finales_con_promo.append({
                    "catalogo_item_id": item_carr_info["catalogo_item_id"],
                    "nombre_producto": item_carr_info.get("nombre_producto", "Desconocido"),
                    "cantidad": item_carr_info["cantidad"],
                    "precio_unitario_original": precio_orig_float_faltante or 0.0,
                    "subtotal_original": round(subtotal_original_faltante, 2),
                    "subtotal_con_descuento": round(subtotal_original_faltante, 2),
                    "descuento_aplicado_linea": 0.0,
                    "moneda": moneda_orig_faltante or item_carr_info.get("moneda", "ARS"),
                    "promocion_aplicada_info": None
                })
                continue

            cantidad_en_carrito = item_carr_info["cantidad"]
            precio_orig_str, precio_orig_float, moneda_orig = parse_precio_flexible(item_catalogo.precio)

            if precio_orig_float is None:
                logger.warning(f"Item {item_catalogo.id} ('{item_catalogo.nombre}') en carrito no tiene precio válido ('{item_catalogo.precio}'). Se usará 0.0 para cálculo de esta línea.")
                precio_orig_float = 0.0

            subtotal_original_item = cantidad_en_carrito * precio_orig_float
            total_original_carrito_calculado += subtotal_original_item

            lista_promos_item = PromocionService.obtener_promociones_aplicables_a_item(
                pyme_user_id, item_catalogo, cantidad_en_carrito, cliente_user_id
            )

            mejor_promo_aplicada_a_linea_item = None
            subtotal_item_con_descuento = subtotal_original_item
            descuento_esta_linea = 0.0

            if lista_promos_item:
                # obtener_promociones_aplicables_a_item ya devuelve ordenada por mayor descuento. Tomamos la primera.
                mejor_promo_info_dict = lista_promos_item[0]

                descuento_esta_linea = mejor_promo_info_dict.get("descuento_aplicado_total_items", 0)
                # El descuento no puede ser mayor que el subtotal original del ítem
                descuento_esta_linea = min(descuento_esta_linea, subtotal_original_item)

                subtotal_item_con_descuento = round(subtotal_original_item - descuento_esta_linea, 2)
                total_descuentos_items_carrito += descuento_esta_linea

                mejor_promo_aplicada_a_linea_item = {
                    "promocion_id": mejor_promo_info_dict["promocion_id"],
                    "nombre_promocion": mejor_promo_info_dict["nombre_promocion"],
                    "descripcion_publica": mejor_promo_info_dict["descripcion_publica"],
                    "descuento_logrado_en_linea": round(descuento_esta_linea, 2)
                }
                promociones_aplicadas_al_carrito_nombres.add(mejor_promo_info_dict["nombre_promocion"])

            items_finales_con_promo.append({
                "catalogo_item_id": item_catalogo.id,
                "nombre_producto": item_catalogo.nombre,
                "sku": item_catalogo.sku,
                "presentacion": item_catalogo.unidad, # o el campo que corresponda
                "cantidad": cantidad_en_carrito,
                "precio_unitario_original": precio_orig_float,
                "subtotal_original": round(subtotal_original_item, 2),
                "subtotal_con_descuento": subtotal_item_con_descuento,
                "descuento_aplicado_linea": round(descuento_esta_linea, 2),
                "moneda": moneda_orig or "ARS",
                "promocion_aplicada_info": mejor_promo_aplicada_a_linea_item
            })

        subtotal_para_promos_de_carrito = round(total_original_carrito_calculado - total_descuentos_items_carrito, 2)
        descuento_adicional_sobre_total_carrito = 0.0
        promo_total_carrito_aplicada_info = None

        now = datetime.utcnow()
        promos_total_carrito_candidatas = Promocion.query.filter(
            Promocion.pyme_user_id == pyme_user_id,
            Promocion.is_active == True, # noqa E712
            Promocion.fecha_inicio <= now,
            (Promocion.fecha_fin == None) | (Promocion.fecha_fin >= now), # noqa E711
            Promocion.tipo_promocion.in_([
                "TOTAL_CARRITO_DESCUENTO_PORCENTAJE",
                "TOTAL_CARRITO_DESCUENTO_FIJO"
            ])
        ).order_by(Promocion.monto_minimo_carrito.desc().nullslast(), Promocion.valor_descuento.desc()).all() # Priorizar

        mejor_descuento_carrito_actual = 0 # Para encontrar la mejor promo de carrito
        promo_carrito_seleccionada_obj = None

        for promo_tc in promos_total_carrito_candidatas:
            descuento_potencial_para_esta_promo_tc = 0
            if promo_tc.monto_minimo_carrito is None or subtotal_para_promos_de_carrito >= promo_tc.monto_minimo_carrito:
                if promo_tc.tipo_promocion == "TOTAL_CARRITO_DESCUENTO_PORCENTAJE" and promo_tc.valor_descuento is not None:
                    descuento_potencial_para_esta_promo_tc = subtotal_para_promos_de_carrito * (promo_tc.valor_descuento / 100.0)
                elif promo_tc.tipo_promocion == "TOTAL_CARRITO_DESCUENTO_FIJO" and promo_tc.valor_descuento is not None:
                    descuento_potencial_para_esta_promo_tc = promo_tc.valor_descuento

                if descuento_potencial_para_esta_promo_tc > mejor_descuento_carrito_actual:
                    mejor_descuento_carrito_actual = descuento_potencial_para_esta_promo_tc
                    promo_carrito_seleccionada_obj = promo_tc

        if promo_carrito_seleccionada_obj:
            descuento_adicional_sobre_total_carrito = round(mejor_descuento_carrito_actual, 2)
            descuento_adicional_sobre_total_carrito = min(descuento_adicional_sobre_total_carrito, subtotal_para_promos_de_carrito) # No descontar más que el subtotal

            promo_total_carrito_aplicada_info = {
                "promocion_id": promo_carrito_seleccionada_obj.id,
                "nombre_promocion": promo_carrito_seleccionada_obj.nombre_promocion,
                "descripcion_publica": promo_carrito_seleccionada_obj.descripcion_publica,
                "tipo_promocion": promo_carrito_seleccionada_obj.tipo_promocion,
                "descuento_sobre_total_aplicado": descuento_adicional_sobre_total_carrito
            }
            promociones_aplicadas_al_carrito_nombres.add(promo_carrito_seleccionada_obj.nombre_promocion)
            logger.info(f"Aplicada promoción de total de carrito: {promo_carrito_seleccionada_obj.nombre_promocion}, Descuento: {descuento_adicional_sobre_total_carrito}")

        total_descuentos_final_carrito = round(total_descuentos_items_carrito + descuento_adicional_sobre_total_carrito, 2)
        total_final_carrito_con_todo = round(total_original_carrito_calculado - total_descuentos_final_carrito, 2)

        return {
            "items_detalle": items_finales_con_promo,
            "subtotal_despues_promos_item": subtotal_para_promos_de_carrito,
            "promo_total_carrito_aplicada_info": promo_total_carrito_aplicada_info,
            "total_original_calculado": round(total_original_carrito_calculado, 2),
            "total_final_con_descuento": total_final_carrito_con_todo,
            "total_ahorrado_final": total_descuentos_final_carrito,
            "promociones_aplicadas_nombres": list(promociones_aplicadas_al_carrito_nombres)
        }

# Instancia del servicio para ser importada
promocion_service = PromocionService()
