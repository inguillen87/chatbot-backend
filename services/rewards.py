import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from database import db
from models import PointsTransaction, TenantProfile, User

logger = logging.getLogger(__name__)


DEFAULT_RULES = {
    "encuesta": 50,
    "reclamo": 20,
    "sugerencia": 30,
    "compra": 10,
}

DEFAULT_REDEMPTIONS = [
    {"id": "discount_10", "label": "Descuento 10%", "points_cost": 800, "type": "discount"},
    {"id": "priority_support", "label": "Prioridad de atencion", "points_cost": 300, "type": "service"},
    {"id": "free_delivery", "label": "Envio bonificado", "points_cost": 500, "type": "shipping"},
]


def _as_int(value: object) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


class RecompensasService:
    def __init__(self, rules: Optional[dict] = None):
        self.rules = rules or DEFAULT_RULES

    def _lock_user(self, user: User) -> User:
        """Lock and reload the user row to avoid race conditions."""

        return (
            User.query.filter_by(id=user.id)
            .with_for_update()
            .first()
        )

    def _resolve_rules(self, tenant: Optional[TenantProfile]) -> dict:
        if tenant and tenant.configuracion and tenant.configuracion.get("rewards_rules"):
            try:
                return dict(tenant.configuracion.get("rewards_rules"))
            except Exception:
                logger.warning("No se pudieron leer reglas personalizadas de recompensas, usando default")
        return self.rules

    def reglas_para_tenant(self, tenant: Optional[TenantProfile]) -> dict[str, int]:
        reglas = dict(DEFAULT_RULES)
        for key, value in self._resolve_rules(tenant).items():
            reglas[str(key)] = _as_int(value)
        return reglas

    def catalogo_canje_para_tenant(self, tenant: Optional[TenantProfile]) -> list[dict]:
        configured = None
        if tenant and isinstance(tenant.configuracion, dict):
            configured = tenant.configuracion.get("rewards_redemptions")
        items = configured if isinstance(configured, list) and configured else DEFAULT_REDEMPTIONS
        catalog = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            reward_id = str(raw.get("id") or raw.get("key") or "").strip()
            points_cost = _as_int(raw.get("points_cost") or raw.get("cost") or raw.get("puntos"))
            if not reward_id or points_cost <= 0:
                continue
            catalog.append(
                {
                    "id": reward_id,
                    "label": raw.get("label") or raw.get("title") or reward_id,
                    "description": raw.get("description") or raw.get("descripcion"),
                    "points_cost": points_cost,
                    "type": raw.get("type") or raw.get("tipo") or "benefit",
                    "status": raw.get("status") or "available",
                }
            )
        return catalog

    def acreditar_puntos(self, user: User, tenant: Optional[TenantProfile], tipo_evento: str) -> int:
        reglas = self._resolve_rules(tenant)
        delta = int(reglas.get(tipo_evento, 0))
        return self.acreditar_puntos_manual(user, tenant, tipo_evento, delta)

    def acreditar_puntos_manual(self, user: User, tenant: Optional[TenantProfile], tipo: str, cantidad: int) -> int:
        if cantidad == 0:
            return user.saldo_puntos or 0

        with db.session.begin_nested():
            locked_user = self._lock_user(user)
            if locked_user is None:
                raise ValueError("Usuario no encontrado para acreditar puntos")
            locked_user.saldo_puntos = (locked_user.saldo_puntos or 0) + cantidad
            tx = PointsTransaction(
                user_id=locked_user.id,
                tenant_id=getattr(tenant, "id", None),
                tipo=tipo,
                delta=cantidad,
                saldo_final=locked_user.saldo_puntos,
            )
            db.session.add(tx)
        db.session.commit()
        return locked_user.saldo_puntos

    def canjear_puntos(
        self,
        user: User,
        tenant: Optional[TenantProfile],
        puntos_necesarios: int,
        *,
        tipo: str = "canje",
        metadata: Optional[dict] = None,
    ) -> bool:
        if puntos_necesarios <= 0:
            return True
        with db.session.begin_nested():
            locked_user = self._lock_user(user)
            if locked_user is None:
                raise ValueError("Usuario no encontrado para canje")
            saldo_actual = locked_user.saldo_puntos or 0
            if saldo_actual < puntos_necesarios:
                return False
            locked_user.saldo_puntos = saldo_actual - puntos_necesarios
            tx = PointsTransaction(
                user_id=locked_user.id,
                tenant_id=getattr(tenant, "id", None),
                tipo=tipo,
                delta=-puntos_necesarios,
                saldo_final=locked_user.saldo_puntos,
                metadata_payload=metadata or None,
            )
            db.session.add(tx)
        db.session.commit()
        return True

    def obtener_saldo(self, user: User) -> int:
        return user.saldo_puntos or 0

    def historial_query(self, user: User):
        return PointsTransaction.query.filter_by(user_id=user.id).order_by(
            PointsTransaction.created_at.desc()
        )

    def historial(self, user: User):
        return self.historial_query(user).all()

    def historial_serializado(self, user: User, limit: int = 10) -> list[dict]:
        rows = self.historial_query(user).limit(limit).all()
        return [
            {
                "id": tx.id,
                "tipo": tx.tipo,
                "delta": tx.delta,
                "saldo_final": tx.saldo_final,
                "metadata": tx.metadata_payload if isinstance(tx.metadata_payload, dict) else {},
                "created_at": tx.created_at.isoformat() if tx.created_at else None,
            }
            for tx in rows
        ]

    def perfil_usuario(self, user: User, tenant: Optional[TenantProfile], pending_points: int = 0) -> dict:
        balance = self.obtener_saldo(user)
        catalog = self.catalogo_canje_para_tenant(tenant)
        return {
            "wallet": {
                "balance": balance,
                "saldo": balance,
                "pending_cart_points": pending_points,
                "estimated_after_cart": max(balance - pending_points, 0),
            },
            "rules": self.reglas_para_tenant(tenant),
            "available_redemptions": [
                {**item, "redeemable": balance >= int(item.get("points_cost") or 0)}
                for item in catalog
            ],
            "history": self.historial_serializado(user),
            "summary": {
                "redemptions_available": len(catalog),
                "redeemable_now": sum(1 for item in catalog if balance >= int(item.get("points_cost") or 0)),
            },
        }

    def buscar_canje_idempotente(self, user: User, tenant: Optional[TenantProfile], idempotency_key: Optional[str]) -> Optional[PointsTransaction]:
        if not idempotency_key:
            return None
        rows = (
            PointsTransaction.query.filter_by(user_id=user.id, tenant_id=getattr(tenant, "id", None), tipo="reward_redeem")
            .order_by(PointsTransaction.created_at.desc())
            .limit(50)
            .all()
        )
        for row in rows:
            metadata = row.metadata_payload if isinstance(row.metadata_payload, dict) else {}
            if metadata.get("idempotency_key") == idempotency_key:
                return row
        return None

    def canjear_beneficio(self, user: User, tenant: Optional[TenantProfile], reward_id: str, *, idempotency_key: Optional[str] = None) -> dict:
        existing = self.buscar_canje_idempotente(user, tenant, idempotency_key)
        if existing:
            metadata = existing.metadata_payload if isinstance(existing.metadata_payload, dict) else {}
            return {
                "ok": True,
                "duplicate": True,
                "redemption_id": metadata.get("redemption_id") or f"red_{existing.id}",
                "reward_id": metadata.get("reward_id"),
                "status": "redeemed",
                "balance": existing.saldo_final,
                "idempotency_key": idempotency_key,
            }

        reward = next((item for item in self.catalogo_canje_para_tenant(tenant) if item["id"] == reward_id), None)
        if reward is None:
            return {"ok": False, "reason_code": "reward_not_found", "message": "Beneficio no encontrado", "status_code": 404}

        points_cost = int(reward["points_cost"])
        balance = self.obtener_saldo(user)
        if balance < points_cost:
            return {
                "ok": False,
                "reason_code": "insufficient_points",
                "message": "Saldo de puntos insuficiente",
                "status_code": 400,
                "points_required": points_cost,
                "points_available": balance,
            }

        redemption_id = f"red_{uuid.uuid4().hex[:12]}"
        ok = self.canjear_puntos(
            user,
            tenant,
            points_cost,
            tipo="reward_redeem",
            metadata={
                "reward_id": reward_id,
                "reward_label": reward.get("label"),
                "redemption_id": redemption_id,
                "idempotency_key": idempotency_key,
                "redeemed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        if not ok:
            return {
                "ok": False,
                "reason_code": "insufficient_points",
                "message": "Saldo de puntos insuficiente",
                "status_code": 400,
                "points_required": points_cost,
                "points_available": self.obtener_saldo(user),
            }

        db.session.refresh(user)
        return {
            "ok": True,
            "duplicate": False,
            "redemption_id": redemption_id,
            "reward_id": reward_id,
            "reward": reward,
            "status": "redeemed",
            "balance": self.obtener_saldo(user),
            "idempotency_key": idempotency_key,
        }

    def apply_welcome_points(self, user: User, tenant: Optional[TenantProfile]):
        """Acredita puntos de bienvenida si la regla está configurada."""

        reglas = self._resolve_rules(tenant)
        welcome = int(reglas.get("welcome_points", 0))
        if welcome <= 0:
            return

        self.acreditar_puntos(user, tenant, "welcome_points")


def recompensas_service() -> RecompensasService:
    return RecompensasService()
