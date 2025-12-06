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

    def canjear_puntos(self, user: User, tenant: Optional[TenantProfile], puntos_necesarios: int) -> bool:
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
                tipo="canje",
                delta=-puntos_necesarios,
                saldo_final=locked_user.saldo_puntos,
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

    def apply_welcome_points(self, user: User, tenant: Optional[TenantProfile]):
        """Acredita puntos de bienvenida si la regla está configurada."""

        reglas = self._resolve_rules(tenant)
        welcome = int(reglas.get("welcome_points", 0))
        if welcome <= 0:
            return

        self.acreditar_puntos(user, tenant, "welcome_points")


def recompensas_service() -> RecompensasService:
    return RecompensasService()

