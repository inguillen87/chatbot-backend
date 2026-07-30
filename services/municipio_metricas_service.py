from models import db, MunicipioTicket, TenantProfile
from services.tenant_ticket_scope import (
    municipio_ticket_scope_filter,
    resolve_unique_tenant_for_owner,
    scoped_municipio_ticket_query,
    tenant_owner_ids,
)

class MunicipioMetricasService:
    def __init__(self, municipio_id: int, *, tenant_id: int | None = None):
        self.municipio_id = municipio_id
        self.tenant = self._resolve_tenant(tenant_id)

    def _resolve_tenant(self, tenant_id: int | None) -> TenantProfile | None:
        try:
            owner_id = int(self.municipio_id)
        except (TypeError, ValueError):
            return None
        if tenant_id is not None:
            try:
                tenant = db.session.get(TenantProfile, int(tenant_id))
            except (TypeError, ValueError):
                return None
            return tenant if tenant is not None and owner_id in tenant_owner_ids(tenant) else None
        try:
            resolution = resolve_unique_tenant_for_owner(owner_id)
        except ValueError:
            return None
        return resolution.tenant if resolution.status == "unique" else None

    def _ticket_query(self):
        return scoped_municipio_ticket_query(self.tenant)

    def get_total_tickets(self) -> int:
        """Número total de tickets para el municipio."""
        return self._ticket_query().count()

    def get_open_tickets(self) -> int:
        """Tickets que no están cerrados."""
        return (
            self._ticket_query()
            .filter(MunicipioTicket.estado != 'cerrado')
            .count()
        )

    def get_closed_tickets(self) -> int:
        """Tickets cerrados."""
        return (
            self._ticket_query()
            .filter(MunicipioTicket.estado == 'cerrado')
            .count()
        )

    def get_unique_citizens(self) -> int:
        """Ciudadanos únicos que generaron tickets."""
        return (
            db.session.query(db.func.count(db.func.distinct(MunicipioTicket.user_id)))
            .filter(
                municipio_ticket_scope_filter(self.tenant),
                MunicipioTicket.user_id.isnot(None)
            )
            .scalar()
            or 0
        )

    def get_resolution_rate(self) -> float:
        """Relación entre tickets cerrados y totales."""
        total = self.get_total_tickets()
        cerrados = self.get_closed_tickets()
        return round(cerrados / total, 2) if total else 0.0
