from models import db, MunicipioTicket

class MunicipioMetricasService:
    def __init__(self, municipio_id: int):
        self.municipio_id = municipio_id

    def get_total_tickets(self) -> int:
        """Número total de tickets para el municipio."""
        return (
            MunicipioTicket.query.filter_by(municipio_id=self.municipio_id).count()
        )

    def get_open_tickets(self) -> int:
        """Tickets que no están cerrados."""
        return (
            MunicipioTicket.query
            .filter(
                MunicipioTicket.municipio_id == self.municipio_id,
                MunicipioTicket.estado != 'cerrado'
            )
            .count()
        )

    def get_closed_tickets(self) -> int:
        """Tickets cerrados."""
        return (
            MunicipioTicket.query
            .filter_by(municipio_id=self.municipio_id, estado='cerrado')
            .count()
        )

    def get_unique_citizens(self) -> int:
        """Ciudadanos únicos que generaron tickets."""
        return (
            db.session.query(db.func.count(db.func.distinct(MunicipioTicket.user_id)))
            .filter(
                MunicipioTicket.municipio_id == self.municipio_id,
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
