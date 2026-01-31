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
