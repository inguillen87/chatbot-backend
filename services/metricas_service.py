from models import db, PymePedido

class MetricasService:
    def __init__(self, pyme_id):
        self.pyme_id = pyme_id

    def get_total_ingresos(self):
        """
        Calcula el total de ingresos para una pyme.
        """
        total = db.session.query(db.func.sum(PymePedido.monto_total)).filter_by(pyme_id=self.pyme_id).scalar()
        return total or 0

    def get_total_pedidos(self):
        """
        Calcula el total de pedidos para una pyme.
        """
        total = PymePedido.query.filter_by(pyme_id=self.pyme_id).count()
        return total

    def get_top_productos_vendidos(self):
        """
        Obtiene los productos más vendidos para una pyme.
        """
        # This is a placeholder implementation. A more robust implementation would
        # require parsing the 'detalles' field of the PymePedido model.
        return []

    def get_kpis(self):
        """
        Obtiene los KPIs para una pyme.
        """
        # This is a placeholder implementation.
        return {
            "sales_growth": 0.1,
            "customer_acquisition_cost": 50,
            "customer_lifetime_value": 1000,
            "average_order_value": 200
        }

    def get_sales_over_time(self):
        """
        Obtiene las ventas a lo largo del tiempo para una pyme.
        """
        # This is a placeholder implementation.
        return [
            {"date": "2023-01-01", "sales": 1000},
            {"date": "2023-01-02", "sales": 1200},
            {"date": "2023-01-03", "sales": 1500},
            {"date": "2023-01-04", "sales": 1300},
            {"date": "2023-01-05", "sales": 1600},
            {"date": "2023-01-06", "sales": 1800},
            {"date": "2023-01-07", "sales": 2000}
        ]

    def get_top_products(self):
        """
        Obtiene los productos más vendidos para una pyme.
        """
        # This is a placeholder implementation. A more robust implementation would
        # require parsing the 'detalles' field of the PymePedido model.
        return [
            {"product": "Product A", "sales": 5000},
            {"product": "Product B", "sales": 4000},
            {"product": "Product C", "sales": 3000},
            {"product": "Product D", "sales": 2000},
            {"product": "Product E", "sales": 1000}
        ]

    def get_sales_by_region(self):
        """
        Obtiene las ventas por región para una pyme.
        """
        # This is a placeholder implementation.
        return [
            {"region": "North", "sales": 10000},
            {"region": "South", "sales": 8000},
            {"region": "East", "sales": 12000},
            {"region": "West", "sales": 9000}
        ]
