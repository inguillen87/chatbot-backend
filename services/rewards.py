import logging
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


class RecompensasService:
    def __init__(self, rules: Optional[dict] = None):
        self.rules = rules or DEFAULT_RULES

    def _resolve_rules(self, tenant: Optional[TenantProfile]) -> dict:
        if tenant and tenant.configuracion and tenant.configuracion.get("rewards_rules"):
            try:
                return dict(tenant.configuracion.get("rewards_rules"))
            except Exception:
                logger.warning("No se pudieron leer reglas personalizadas de recompensas, usando default")
        return self.rules

    def acreditar_puntos(self, user: User, tenant: Optional[TenantProfile], tipo_evento: str) -> int:
        reglas = self._resolve_rules(tenant)
        delta = int(reglas.get(tipo_evento, 0))
        if delta == 0:
            return user.saldo_puntos

        user.saldo_puntos = (user.saldo_puntos or 0) + delta
        tx = PointsTransaction(
            user_id=user.id,
            tenant_id=getattr(tenant, "id", None),
            tipo=tipo_evento,
            delta=delta,
            saldo_final=user.saldo_puntos,
        )
        db.session.add(tx)
        db.session.commit()
        return user.saldo_puntos

    def canjear_puntos(self, user: User, tenant: Optional[TenantProfile], puntos_necesarios: int) -> bool:
        if puntos_necesarios <= 0:
            return True
        saldo_actual = user.saldo_puntos or 0
        if saldo_actual < puntos_necesarios:
            return False
        user.saldo_puntos = saldo_actual - puntos_necesarios
        tx = PointsTransaction(
            user_id=user.id,
            tenant_id=getattr(tenant, "id", None),
            tipo="canje",
            delta=-puntos_necesarios,
            saldo_final=user.saldo_puntos,
        )
        db.session.add(tx)
        db.session.commit()
        return True

    def obtener_saldo(self, user: User) -> int:
        return user.saldo_puntos or 0

    def historial(self, user: User):
        return (
            PointsTransaction.query.filter_by(user_id=user.id)
            .order_by(PointsTransaction.created_at.desc())
            .all()
        )


def recompensas_service() -> RecompensasService:
    return RecompensasService()

