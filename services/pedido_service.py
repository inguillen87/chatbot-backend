# src/services/pedido_service.py

import logging
import uuid
from models import db, PymePedido # Asegúrate que PymePedido esté importado desde models
import re
from datetime import datetime
from models import db, PymePedido  # Asegúrate que PymePedido esté importado desde models
from .email_service import enviar_email_pedido_admin, enviar_sms, enviar_whatsapp

logger = logging.getLogger(__name__)

class PedidoService:
    def crear_nuevo_pedido(self, pedido_data: dict) -> PymePedido | None:
        try:
            # Validar datos básicos
            if not pedido_data.get("asunto") or not pedido_data.get("detalles") or not pedido_data.get("rubro"):
                logger.error("Datos mínimos faltantes para crear pedido: asunto, detalles o rubro.")
                return None

            nuevo_pedido = PymePedido(
                asunto=pedido_data["asunto"],
                detalles=pedido_data["detalles"],
                rubro=pedido_data["rubro"],
                nombre_cliente=pedido_data.get("nombre_cliente"),
                email_cliente=pedido_data.get("email_cliente"),
                telefono_cliente=pedido_data.get("telefono_cliente"),
                user_id=pedido_data.get("user_id") # Puede ser None si es anónimo
            )
            db.session.add(nuevo_pedido)
            db.session.commit()
            logger.info(f"Nuevo pedido '{nuevo_pedido.nro_pedido}' creado para rubro '{nuevo_pedido.rubro}' por cliente '{nuevo_pedido.nombre_cliente}'")
            logger.info(
                f"Nuevo pedido '{nuevo_pedido.nro_pedido}' creado para rubro '{nuevo_pedido.rubro}' por cliente '{nuevo_pedido.nombre_cliente}'"
            )
            try:
                enviar_email_pedido_admin(nuevo_pedido)
            except Exception as e:
                logger.error(f"Error enviando email de pedido: {e}")
            try:
                if nuevo_pedido.telefono_cliente:
                    telefono = re.sub(r"\D", "", nuevo_pedido.telefono_cliente)
                    if not telefono.startswith("+") and len(telefono) > 8:
                        telefono = "+549" + telefono
                    enviar_sms(telefono, f"Tu pedido {nuevo_pedido.nro_pedido} fue registrado")
                    enviar_whatsapp(telefono, f"Tu pedido {nuevo_pedido.nro_pedido} fue registrado")
            except Exception as e:
                logger.error(f"Error enviando SMS/WhatsApp de pedido: {e}")
            return nuevo_pedido
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error al crear nuevo pedido: {e}", exc_info=True) # exc_info=True para ver el traceback
            return None

    def obtener_pedido_por_nro(self, nro_pedido: str) -> PymePedido | None:
        try:
            return PymePedido.query.filter_by(nro_pedido=nro_pedido).first()
        except Exception as e:
            logger.error(f"Error al obtener pedido por número '{nro_pedido}': {e}", exc_info=True)
            return None

    # Puedes añadir más funciones aquí, como actualizar estado, listar pedidos, etc.

# Instancia del servicio para usar en otros módulos
servicio_pedidos = PedidoService()