import json
import logging
import re
from typing import Any, Optional

from models import (
    db,
    CatalogoItem,
    MarketOrder,
    MarketOrderItem,
    PymePedido,
    PedidoConversacional,
    TenantProfile,
    User,
)
from .email_service import enviar_email_pedido_admin, enviar_email_pedido_cliente
from .notifications import (
    enviar_notificacion_sms,
    enviar_notificacion_whatsapp_con_plantilla,
)
from services.notification_dispatcher import notification_dispatcher
from utils.validators import (
    validate_name,
    validate_email_address,
    normalize_phone,
    validate_address,
)
from services.pedido_pdf import generar_pdf_nota_pedido

logger = logging.getLogger(__name__)


class PedidoService:
    def _crear_market_order_desde_pyme(
        self,
        pedido: PymePedido,
        channel: Optional[str] = None,
    ) -> Optional[MarketOrder]:
        tenant = TenantProfile.query.filter_by(pyme_id=pedido.pyme_id).first()
        if not tenant:
            return None

        status_map = {
            "pendiente": "pending",
            "confirmado": "confirmed",
            "confirmed": "confirmed",
            "en_proceso": "processing",
            "processing": "processing",
            "enviado": "shipped",
            "entregado": "delivered",
            "completado": "completed",
            "cancelado": "cancelled",
            "cancelled": "cancelled",
            "devuelto": "returned",
        }
        status = status_map.get((pedido.estado or "").lower(), "pending")

        existing = MarketOrder.legacy_safe_query().filter_by(
            tenant_id=tenant.id,
            external_provider="pyme_pedido",
            external_order_id=pedido.nro_pedido,
        ).first()
        if existing:
            existing.status = status
            existing.contact_name = pedido.nombre_cliente
            existing.contact_phone = pedido.telefono_cliente
            existing.contact_email = pedido.email_cliente
            existing.channel = channel or existing.channel or "chat"
            existing.total_monetary = pedido.monto_total
            metadata_payload = dict(existing.metadata_payload or {})
            metadata_payload.update(
                {
                    "pyme_pedido_id": pedido.id,
                    "pyme_id": pedido.pyme_id,
                    **(
                        {"source_conversational_id": str(pedido.idempotency_key).split("conv_order_", 1)[1]}
                        if str(getattr(pedido, "idempotency_key", "") or "").startswith("conv_order_")
                        else {}
                    ),
                }
            )
            existing.metadata_payload = metadata_payload
            return existing

        order = MarketOrder(
            tenant_id=tenant.id,
            user_id=pedido.user_id,
            status=status,
            contact_name=pedido.nombre_cliente,
            contact_phone=pedido.telefono_cliente,
            contact_email=pedido.email_cliente,
            channel=channel or "chat",
            total_monetary=pedido.monto_total,
            currency="ARS",
            external_provider="pyme_pedido",
            external_order_id=pedido.nro_pedido,
            metadata_payload={
                "pyme_pedido_id": pedido.id,
                "pyme_id": pedido.pyme_id,
                **(
                    {"source_conversational_id": str(pedido.idempotency_key).split("conv_order_", 1)[1]}
                    if str(getattr(pedido, "idempotency_key", "") or "").startswith("conv_order_")
                    else {}
                ),
            },
        )

        try:
            detalles_items = json.loads(pedido.detalles or "[]")
        except (TypeError, ValueError):
            detalles_items = []

        for item in detalles_items if isinstance(detalles_items, list) else []:
            if not isinstance(item, dict):
                continue
            sku = item.get("sku")
            nombre = item.get("nombre") or item.get("nombre_producto")
            cantidad = item.get("cantidad") or 1
            precio = item.get("precio_unitario") or item.get("precio") or item.get("precio_unitario_original")

            producto = None
            if sku:
                producto = CatalogoItem.query.filter_by(user_id=pedido.pyme_id, sku=sku).first()
            if not producto and nombre:
                producto = CatalogoItem.query.filter_by(user_id=pedido.pyme_id, nombre=nombre).first()

            try:
                cantidad_normalizada = int(float(cantidad))
            except (TypeError, ValueError):
                cantidad_normalizada = 1

            order.items.append(
                MarketOrderItem(
                    product_id=producto.id if producto else None,
                    quantity=max(cantidad_normalizada, 1),
                    price_monetary=precio,
                    currency="ARS",
                    name_snapshot=nombre or (producto.nombre if producto else None),
                )
            )

        db.session.add(order)
        return order

    def sync_market_order_from_pyme(
        self,
        pedido: PymePedido,
        channel: Optional[str] = None,
    ) -> Optional[MarketOrder]:
        order = self._crear_market_order_desde_pyme(pedido, channel=channel)
        if order:
            db.session.commit()
        return order

    def sync_order_model_from_pyme(self, pedido: PymePedido, channel: Optional[str] = None):
        """
        Creates or updates an Order record (new model) from a PymePedido (legacy model).
        This ensures orders appear in the new Admin Panel.
        """
        from models import Order, OrderItem, CatalogoItem

        if not pedido.tenant_id:
            return None

        # Check if Order already exists
        existing_order = Order.query.filter_by(
            tenant_id=pedido.tenant_id,
            id=pedido.nro_pedido # We use nro_pedido as ID if compatible, or map it
        ).first()

        if existing_order:
            return existing_order

        # Create new Order
        new_order = Order(
            id=pedido.nro_pedido, # Use same ID for consistency
            tenant_id=pedido.tenant_id,
            customer_id=pedido.user_id,
            buyer_name=pedido.nombre_cliente,
            buyer_email=pedido.email_cliente,
            buyer_phone=pedido.telefono_cliente,
            status=pedido.estado or 'created',
            channel=channel or 'whatsapp', # Default to whatsapp/chat as PymePedido usually comes from there
            total=pedido.monto_total or 0,
            subtotal=pedido.monto_total or 0, # Assuming no separate tax/shipping yet in legacy
            created_at=pedido.fecha,
            delivery_address={"address": pedido.direccion, "lat": pedido.latitud, "lng": pedido.longitud}
        )

        # Parse items
        try:
            detalles_list = json.loads(pedido.detalles or "[]")
        except:
            detalles_list = []

        for item in detalles_list:
            if not isinstance(item, dict): continue

            # Try to link to catalog item
            sku = item.get("sku")
            nombre = item.get("nombre")

            catalog_item = None
            if sku:
                catalog_item = CatalogoItem.query.filter_by(user_id=pedido.pyme_id, sku=sku).first()
            if not catalog_item and nombre:
                catalog_item = CatalogoItem.query.filter_by(user_id=pedido.pyme_id, nombre=nombre).first()

            qty = int(item.get("cantidad") or 1)
            unit_price = float(item.get("precio_unitario") or 0)

            order_item = OrderItem(
                catalog_item_id=catalog_item.id if catalog_item else None,
                sku=sku or (catalog_item.sku if catalog_item else None),
                title=nombre or "Item",
                quantity=qty,
                unit_price=unit_price,
                total_price=float(item.get("subtotal") or (qty * unit_price))
            )
            new_order.items.append(order_item)

        db.session.add(new_order)
        db.session.commit()
        logger.info(f"Synced PymePedido {pedido.nro_pedido} to Order model.")
        return new_order

    def crear_nuevo_pedido(self, pedido_data: dict) -> PymePedido | None:
        try:
            # Check idempotency first if provided
            idempotency_key = pedido_data.get("idempotency_key")
            if idempotency_key:
                existing = PymePedido.query.filter_by(idempotency_key=idempotency_key).first()
                if existing:
                    logger.info(f"Pedido idempotente encontrado: {existing.nro_pedido}")
                    return existing

            # Validar datos básicos
            if (
                not pedido_data.get("asunto")
                or not pedido_data.get("detalles")
                or not pedido_data.get("rubro")
            ):
                logger.error(
                    "Datos mínimos faltantes para crear pedido: asunto, detalles o rubro."
                )
                return None

            nombre = pedido_data.get("nombre_cliente")
            if nombre and not validate_name(nombre):
                logger.error("Nombre de cliente inválido")
                return None

            email = pedido_data.get("email_cliente")
            if email and not validate_email_address(email):
                logger.error("Email de cliente inválido")
                return None

            telefono = pedido_data.get("telefono_cliente")
            if telefono:
                telefono_normalizado = normalize_phone(telefono)
                if not telefono_normalizado:
                    logger.error("Teléfono de cliente inválido")
                    return None
                pedido_data["telefono_cliente"] = telefono_normalizado

            direccion = pedido_data.get("direccion")
            if direccion and not validate_address(direccion):
                logger.error("Dirección inválida")
                return None

            pyme_id = pedido_data.get("pyme_id")
            if not pyme_id:
                logger.error("pyme_id es requerido para registrar un pedido")
                return None

            tenant_id = pedido_data.get("tenant_id")
            if not tenant_id:
                # Try to resolve from pyme_id
                pyme_user = db.session.get(User, pyme_id)
                if pyme_user and pyme_user.tenant_id:
                    tenant_id = pyme_user.tenant_id

                # Fallback: Find TenantProfile linked to this pyme_id
                if not tenant_id:
                    tenant_linked = TenantProfile.query.filter_by(pyme_id=pyme_id).first()
                    if tenant_linked:
                        tenant_id = tenant_linked.id

            nuevo_pedido = PymePedido(
                pyme_id=pyme_id,
                tenant_id=tenant_id,
                asunto=pedido_data["asunto"],
                detalles=pedido_data["detalles"],
                monto_total=pedido_data.get("monto_total"),
                nombre_cliente=pedido_data.get("nombre_cliente"),
                email_cliente=pedido_data.get("email_cliente"),
                telefono_cliente=pedido_data.get("telefono_cliente"),
                user_id=pedido_data.get("user_id"),  # Puede ser None si es anónimo
                direccion=pedido_data.get("direccion"),
                latitud=pedido_data.get("latitud"),
                longitud=pedido_data.get("longitud"),
                idempotency_key=idempotency_key,
                channel=pedido_data.get("channel"), # Optional, ignored by current __init__ if not added, but safe if added to __init__
            )
            if pedido_data.get("rubro"):
                nuevo_pedido.rubro = pedido_data.get("rubro")
            if pedido_data.get("estado"):
                nuevo_pedido.estado = str(pedido_data.get("estado")).strip()
            db.session.add(nuevo_pedido)
            db.session.commit()
            rubro_log = pedido_data.get("rubro") or getattr(nuevo_pedido, "rubro", None)
            logger.info(
                f"Nuevo pedido '{nuevo_pedido.nro_pedido}' creado para rubro '{rubro_log}' por cliente '{nuevo_pedido.nombre_cliente}'"
            )
            pyme_owner: Optional[User] = None
            empresa_info = None
            try:
                pyme_owner = db.session.get(User, pyme_id)
            except Exception:
                pyme_owner = User.query.get(pyme_id)
            if pyme_owner:
                empresa_info = {
                    "nombre": getattr(pyme_owner, "nombre_empresa", None) or getattr(pyme_owner, "name", None),
                    "direccion": getattr(pyme_owner, "direccion", None),
                    "telefono": getattr(pyme_owner, "telefono", None),
                    "email": getattr(pyme_owner, "email", None),
                }

            pdf_bytes = None
            try:
                pdf_bytes = generar_pdf_nota_pedido(nuevo_pedido, empresa_info=empresa_info)
            except RuntimeError as pdf_missing_dep:
                logger.warning(f"No se pudo generar el PDF del pedido: {pdf_missing_dep}")
            except Exception as e:
                logger.error(f"Error generando PDF de pedido {nuevo_pedido.nro_pedido}: {e}", exc_info=True)

            # Attach ephemeral attributes for downstream consumers (no commit)
            nuevo_pedido.nota_pedido_pdf_generado = bool(pdf_bytes)
            nuevo_pedido._nota_pedido_pdf_bytes = pdf_bytes  # type: ignore[attr-defined]
            nuevo_pedido._empresa_info_pdf = empresa_info  # type: ignore[attr-defined]

            # Dispatch all notifications via hardened service
            try:
                notification_dispatcher.dispatch_order_created(nuevo_pedido, pdf_bytes=pdf_bytes)
            except Exception as e:
                logger.error(f"Error dispatching notifications for order {nuevo_pedido.nro_pedido}: {e}", exc_info=True)

            # Sync to new Order model (for Admin Panel compatibility)
            try:
                self.sync_order_model_from_pyme(nuevo_pedido, channel=pedido_data.get("channel"))
            except Exception as e:
                logger.error(f"Error syncing to Order model for {nuevo_pedido.nro_pedido}: {e}", exc_info=True)
                # Non-blocking, proceed

            try:
                self.sync_market_order_from_pyme(
                    nuevo_pedido,
                    channel=pedido_data.get("channel"),
                )
            except Exception as e:
                # Do NOT rollback here just for market sync failure, as PymePedido is already committed?
                # Actually, logic above does commit. But if this block fails, we shouldn't rollback the PymePedido
                # unless we want all-or-nothing. Given PymePedido is committed lines above, we can't easily rollback
                # without a nested transaction or manual deletion.
                # However, the original code had a rollback here which might be risky if already committed.
                # 'db.session.commit()' was called at line 147. So 'db.session.rollback()' here does nothing
                # to the committed transaction, it only rolls back the *current* flushing of MarketOrder if it failed.
                logger.error(
                    "Error creando MarketOrder para pedido %s: %s",
                    nuevo_pedido.nro_pedido,
                    e,
                    exc_info=True,
                )
            return nuevo_pedido
        except Exception as e:
            db.session.rollback()
            logger.error(
                f"Error al crear nuevo pedido: {e}", exc_info=True
            )  # exc_info=True para ver el traceback
            return None

    def obtener_pedido_por_nro(
        self,
        nro_pedido: str,
        *,
        pyme_id: int | None = None,
        tenant_id: int | None = None,
    ) -> PymePedido | None:
        try:
            nro_normalizado = str(nro_pedido or "").strip()
            if not nro_normalizado:
                return None

            query = PymePedido.query.filter_by(nro_pedido=nro_normalizado)
            if pyme_id is not None:
                query = query.filter_by(pyme_id=pyme_id)
            if tenant_id is not None:
                query = query.filter_by(tenant_id=tenant_id)
            return query.first()
        except Exception as e:
            logger.error(
                f"Error al obtener pedido por número '{nro_pedido}': {e}", exc_info=True
            )
            return None

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _as_list(value: Any) -> list[Any]:
        return value if isinstance(value, list) else []

    @staticmethod
    def _parse_quantity(value: Any) -> int:
        if value is None:
            return 1
        if isinstance(value, (int, float)):
            return max(int(value), 1)
        match = re.search(r"\d+(?:[\.,]\d+)?", str(value))
        if not match:
            return 1
        try:
            return max(int(float(match.group(0).replace(",", "."))), 1)
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _parse_money(value: Any) -> float:
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        raw = str(value).strip()
        if not raw:
            return 0.0
        cleaned = re.sub(r"[^\d,.\-]", "", raw)
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", ".")
        try:
            return float(cleaned)
        except (TypeError, ValueError):
            return 0.0

    def _build_data_from_assisted_marketplace(
        self,
        conversacional: PedidoConversacional,
        tenant: TenantProfile,
        idempotency_key: str,
    ) -> dict[str, Any]:
        metadata = self._as_dict(conversacional.metadata_payload)
        raw_payload = self._as_dict((conversacional.items or [None])[0])
        detected_items = self._as_list(raw_payload.get("items_detectados"))
        unmatched_items = self._as_list(raw_payload.get("no_encontrados"))
        contact = self._as_dict(metadata.get("contact")) or self._as_dict(raw_payload.get("contact"))
        source = self._as_dict(metadata.get("source")) or self._as_dict(raw_payload.get("source"))
        crm_handoff = self._as_dict(metadata.get("crm_handoff")) or self._as_dict(raw_payload.get("crm_handoff"))
        crm_order_draft = (
            self._as_dict(metadata.get("crm_order_draft"))
            or self._as_dict(raw_payload.get("crm_order_draft"))
            or self._as_dict(crm_handoff.get("draft_order"))
        )
        crm_lines = self._as_list(crm_order_draft.get("lines"))

        detalles: list[dict[str, Any]] = []
        total = 0.0

        for line in crm_lines:
            if not isinstance(line, dict):
                continue
            catalog_match = self._as_dict(line.get("catalog_match"))
            quantity = self._parse_quantity(line.get("quantity") or line.get("cantidad") or line.get("qty"))
            price = self._parse_money(
                line.get("price")
                or line.get("precio")
                or line.get("unit_price")
                or catalog_match.get("price")
                or catalog_match.get("precio")
                or catalog_match.get("precio_unitario")
            )
            subtotal = quantity * price
            total += subtotal
            status = str(line.get("status") or "").strip().lower()
            catalog_item_id = (
                line.get("catalog_item_id")
                or line.get("catalogo_item_id")
                or catalog_match.get("catalogo_item_id")
                or catalog_match.get("catalog_item_id")
                or catalog_match.get("product_id")
            )
            has_match = bool(catalog_item_id)
            detalles.append(
                {
                    "nombre": (
                        catalog_match.get("name")
                        or catalog_match.get("nombre")
                        or line.get("source_name")
                        or line.get("name")
                        or line.get("nombre")
                        or "Articulo detectado"
                    ),
                    "cantidad": quantity,
                    "precio_unitario": price,
                    "subtotal": subtotal,
                    "sku": line.get("sku") or catalog_match.get("sku"),
                    "catalogo_item_id": catalog_item_id,
                    "source": "catalog_match" if has_match else "operator_review",
                    "requires_operator_review": bool(line.get("needs_operator_review"))
                    or status not in {"catalog_matched", "resolved", "confirmed"},
                }
            )

        for item in detected_items if not detalles else []:
            if not isinstance(item, dict):
                continue
            quantity = self._parse_quantity(item.get("cantidad") or item.get("quantity"))
            price = self._parse_money(
                item.get("precio_float")
                or item.get("precio_unitario")
                or item.get("unit_price")
                or item.get("precio")
            )
            subtotal = quantity * price
            total += subtotal
            detalles.append(
                {
                    "nombre": item.get("nombre") or item.get("producto") or item.get("title") or item.get("sku") or "Articulo detectado",
                    "cantidad": quantity,
                    "precio_unitario": price,
                    "subtotal": subtotal,
                    "sku": item.get("sku"),
                    "catalogo_item_id": item.get("catalogo_item_id"),
                    "source": "catalog_match",
                }
            )

        for item in unmatched_items if not crm_lines else []:
            if not isinstance(item, dict):
                continue
            quantity = self._parse_quantity(item.get("cantidad") or item.get("quantity"))
            detalles.append(
                {
                    "nombre": item.get("nombre") or item.get("producto") or item.get("descripcion") or item.get("detalle") or "Articulo para revisar",
                    "cantidad": quantity,
                    "precio_unitario": 0,
                    "subtotal": 0,
                    "sku": item.get("sku"),
                    "source": "operator_review",
                    "requires_operator_review": True,
                    "catalog_candidates": item.get("catalog_candidates") if isinstance(item.get("catalog_candidates"), list) else [],
                }
            )

        if not detalles:
            detalles.append(
                {
                    "nombre": metadata.get("request_kind_label") or raw_payload.get("request_kind_label") or "Solicitud asistida",
                    "cantidad": 1,
                    "precio_unitario": 0,
                    "subtotal": 0,
                    "source": "operator_review",
                    "requires_operator_review": True,
                }
            )

        request_label = metadata.get("request_kind_label") or raw_payload.get("request_kind_label") or "pedido asistido"
        return {
            "pyme_id": tenant.pyme_id,
            "tenant_id": tenant.id,
            "asunto": f"{request_label.title()} #{conversacional.id}",
            "detalles": json.dumps(detalles, ensure_ascii=False),
            "monto_total": total or float(conversacional.monto_monetario or 0),
            "nombre_cliente": contact.get("name") or contact.get("nombre"),
            "email_cliente": contact.get("email"),
            "telefono_cliente": contact.get("phone") or contact.get("telefono"),
            "direccion": contact.get("address") or contact.get("direccion"),
            "user_id": conversacional.user_id,
            "rubro": metadata.get("request_kind") or raw_payload.get("request_kind") or "marketplace",
            "estado": "confirmado"
            if str(conversacional.estado or "").strip().lower() in {"confirmed", "confirmado"}
            else "pendiente",
            "idempotency_key": idempotency_key,
            "channel": source.get("channel") or conversacional.origen or "marketplace",
        }

    @staticmethod
    def _mark_conversational_materialized(
        conversacional: PedidoConversacional,
        pedido: PymePedido,
        idempotency_key: str,
    ) -> None:
        metadata_payload = dict(conversacional.metadata_payload or {})
        metadata_payload["crm_state"] = "materialized_order"
        metadata_payload["materialized_order"] = {
            "source_model": "PymePedido",
            "id": pedido.id,
            "nro_pedido": pedido.nro_pedido,
            "idempotency_key": idempotency_key,
        }
        conversacional.metadata_payload = metadata_payload

    def create_from_conversational(self, conversacional: PedidoConversacional) -> Optional[PymePedido]:
        """Creates a PymePedido from a confirmed PedidoConversacional."""
        try:
            if not conversacional.tenant_id:
                logger.error("PedidoConversacional %s missing tenant_id", conversacional.id)
                return None

            tenant = db.session.get(TenantProfile, conversacional.tenant_id)
            if not tenant or not tenant.pyme_id:
                logger.error("Tenant %s or its pyme_id not found for order %s", conversacional.tenant_id, conversacional.id)
                return None

            # Check if already linked via idempotency or similar logic?
            # We use idempotency_key constructed from conversacional.id
            idempotency_key = f"conv_order_{conversacional.id}"
            existing = PymePedido.query.filter_by(idempotency_key=idempotency_key).first()
            if existing:
                if str(conversacional.estado or "").strip().lower() in {"confirmed", "confirmado"}:
                    existing.estado = "confirmado"
                self._mark_conversational_materialized(conversacional, existing, idempotency_key)
                self.sync_market_order_from_pyme(existing, channel=conversacional.origen)
                db.session.commit()
                return existing

            metadata = self._as_dict(conversacional.metadata_payload)
            if metadata.get("contract_version") == "marketplace.assisted_request.v1":
                pedido = self.crear_nuevo_pedido(
                    self._build_data_from_assisted_marketplace(
                        conversacional,
                        tenant,
                        idempotency_key,
                    )
                )
                if pedido:
                    self._mark_conversational_materialized(conversacional, pedido, idempotency_key)
                    db.session.commit()
                return pedido

            # Map items to detalles
            # PedidoConversacional items format:
            # [{"title": "...", "quantity": 1, "unit_price": 100, ...}]
            # PymePedido detalles format:
            # [{"nombre": "...", "cantidad": 1, "precio_unitario": 100, "subtotal": 100}]

            detalles = []
            for item in conversacional.items:
                qty = item.get("quantity", 1)
                price = item.get("unit_price", 0)
                subtotal = qty * price
                detalles.append({
                    "nombre": item.get("title"),
                    "cantidad": qty,
                    "precio_unitario": price,
                    "subtotal": subtotal,
                    "sku": item.get("sku"), # if available
                    "currency_id": item.get("currency_id")
                })

            import json

            # Prepare user contact info
            # User might be updated during checkout, so fetch fresh
            user = db.session.get(User, conversacional.user_id)

            pedido_data = {
                "pyme_id": tenant.pyme_id,
                "tenant_id": tenant.id,
                "asunto": f"Pedido Web #{conversacional.id}",
                "detalles": json.dumps(detalles, ensure_ascii=False),
                "monto_total": float(conversacional.monto_monetario or 0),
                "nombre_cliente": user.name if user else None,
                "email_cliente": user.email if user else None,
                "telefono_cliente": user.telefono if user else None,
                "direccion": user.direccion if user else None,
                "user_id": conversacional.user_id,
                "rubro": "general", # Or derived from tenant
                "idempotency_key": idempotency_key,
                "channel": conversacional.origen
            }

            return self.crear_nuevo_pedido(pedido_data)

        except Exception as e:
            logger.error("Error creating PymePedido from Conversacional %s: %s", conversacional.id, e, exc_info=True)
            return None

    # Puedes añadir más funciones aquí, como actualizar estado, listar pedidos, etc.

    def crear_pedido_desde_carrito(self, pyme_id: int, cart_items: list, cliente_data: dict) -> PymePedido | None:
        """
        Crea un PymePedido a partir de los items del carrito.
        pyme_id: ID del usuario (Pyme) dueño del catálogo.
        cart_items: Lista de items del carrito (obtenida de services.cart.get_summary()).
        cliente_data: Diccionario con información del cliente y del pedido.
                      Debe incluir 'nombre_cliente', 'email_cliente', etc.
                      y opcionalmente 'cliente_user_id' si el cliente está registrado.
                      También 'rubro' para el pedido (de la pyme).
        """
        if not cart_items:
            logger.error("El carrito está vacío, no se puede crear el pedido.")
            return None

        detalles_pedido_items = []
        monto_total_calculado = 0.0
        cliente_user_id = cliente_data.get("cliente_user_id")

        from models import CatalogoItem  # Importación local para evitar circularidad
        from services.common_utils import parse_precio_flexible # Para parsear precios

        for item_carrito in cart_items:
            nombre_producto = item_carrito.get("nombre")
            cantidad_carrito = item_carrito.get("cantidad", 0)

            if not nombre_producto or cantidad_carrito <= 0:
                logger.warning(f"Item de carrito inválido omitido: {item_carrito}")
                continue

            # Buscar el producto en el catálogo de la Pyme especificada por pyme_id
            producto_catalogo = CatalogoItem.query.filter_by(user_id=pyme_id, nombre=nombre_producto).first()

            if not producto_catalogo:
                logger.error(f"Producto '{nombre_producto}' no encontrado en el catálogo de la Pyme {pyme_id}.")
                return None

            precio_str = producto_catalogo.precio
            _, precio_unitario_float, _ = parse_precio_flexible(precio_str)

            if precio_unitario_float is None:
                logger.error(f"No se pudo determinar el precio para el producto '{nombre_producto}'.")
                return None # O manejar error

            item_total = precio_unitario_float * cantidad_carrito

            detalles_pedido_items.append({
                "nombre": nombre_producto,
                "cantidad": cantidad_carrito,
                "precio_unitario": precio_unitario_float,
                "subtotal": item_total,
                "sku": producto_catalogo.sku,
                "categoria": producto_catalogo.categoria
            })
            monto_total_calculado += item_total

        if not detalles_pedido_items:
            logger.error("No se pudieron procesar items válidos del carrito.")
            return None

        import json # Para convertir la lista de detalles a JSON string
        asunto_base = cliente_data.get("asunto")
        if not asunto_base:
            asunto_base = f"Pedido desde carrito - Cliente {cliente_user_id or 'invitado'}"

        pedido_data = {
            "asunto": asunto_base,
            "detalles": json.dumps(detalles_pedido_items, ensure_ascii=False), # Guardar como JSON string
            "rubro": cliente_data.get("rubro", "general_pyme"), # Podría venir del perfil del usuario/pyme
            "nombre_cliente": cliente_data.get("nombre_cliente"),
            "email_cliente": cliente_data.get("email_cliente"),
            "telefono_cliente": cliente_data.get("telefono_cliente"),
            "direccion": cliente_data.get("direccion"),
            "latitud": cliente_data.get("latitud"),
            "longitud": cliente_data.get("longitud"),
            # user_id aquí es el del cliente que realiza el pedido, puede ser None para invitados
            "user_id": cliente_user_id,
            "pyme_id": pyme_id,
            # "monto_total" se asignará directamente al objeto PymePedido más abajo
        }

        # Validaciones de datos del cliente (nombre, email, telefono, direccion)
        # (reutilizando lógica de crear_nuevo_pedido si es aplicable o añadiendo aquí)
        nombre = pedido_data.get("nombre_cliente")
        if nombre and not validate_name(nombre):
            logger.error(f"Nombre de cliente inválido para pedido desde carrito: {nombre}")
            return None

        email = pedido_data.get("email_cliente")
        if email and not validate_email_address(email):
            logger.error(f"Email de cliente inválido para pedido desde carrito: {email}")
            return None

        telefono = pedido_data.get("telefono_cliente")
        if telefono:
            telefono_normalizado = normalize_phone(telefono)
            if not telefono_normalizado:
                logger.error(f"Teléfono de cliente inválido para pedido desde carrito: {telefono}")
                return None
            pedido_data["telefono_cliente"] = telefono_normalizado

        direccion = pedido_data.get("direccion")
        if direccion and not validate_address(direccion):
            logger.error(f"Dirección inválida para pedido desde carrito: {direccion}")
            return None

        try:
            # Crear el objeto PymePedido
            # El constructor de PymePedido ya maneja _generate_nro_pedido
            pyme_id = pedido_data.get("pyme_id") or pyme_id
            if not pyme_id:
                logger.error("pyme_id es requerido para crear el pedido desde carrito")
                return None

            # Resolve tenant_id for robustness
            tenant_id = None
            pyme_user = db.session.get(User, pyme_id)
            if pyme_user and pyme_user.tenant_id:
                tenant_id = pyme_user.tenant_id
            if not tenant_id:
                 tenant_linked = TenantProfile.query.filter_by(pyme_id=pyme_id).first()
                 if tenant_linked:
                     tenant_id = tenant_linked.id

            nuevo_pedido_obj = PymePedido(
                pyme_id=pyme_id,
                tenant_id=tenant_id,
                asunto=pedido_data["asunto"],
                detalles=pedido_data["detalles"],
                monto_total=monto_total_calculado,
                nombre_cliente=pedido_data.get("nombre_cliente"),
                email_cliente=pedido_data.get("email_cliente"),
                telefono_cliente=pedido_data.get("telefono_cliente"),
                user_id=pedido_data.get("user_id"),
                direccion=pedido_data.get("direccion"),
                latitud=pedido_data.get("latitud"),
                longitud=pedido_data.get("longitud"),
            )
            if pedido_data.get("rubro"):
                nuevo_pedido_obj.rubro = pedido_data.get("rubro")

            db.session.add(nuevo_pedido_obj)
            db.session.commit()

            logger.info(
                "Nuevo pedido '%s' creado desde carrito para cliente '%s'. Monto: %s",
                nuevo_pedido_obj.nro_pedido,
                cliente_user_id or "anonimo",
                monto_total_calculado,
            )

            # Enviar notificaciones (reutilizando la lógica centralizada)
            try:
                notification_dispatcher.dispatch_order_created(nuevo_pedido_obj)
            except Exception as e:
                logger.error(f"Error dispatching notifications for cart order {nuevo_pedido_obj.nro_pedido}: {e}")

            # Sync to new Order model (for Admin Panel compatibility)
            try:
                self.sync_order_model_from_pyme(nuevo_pedido_obj, channel="web_widget")
            except Exception as e:
                logger.error(f"Error syncing to Order model for {nuevo_pedido_obj.nro_pedido}: {e}", exc_info=True)

            return nuevo_pedido_obj

        except Exception as e:
            db.session.rollback()
            logger.error(
                f"Error al crear nuevo pedido desde carrito: {e}", exc_info=True
            )
            return None


# Instancia del servicio para usar en otros módulos
servicio_pedidos = PedidoService()
