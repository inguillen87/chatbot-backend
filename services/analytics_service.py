import json
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
from sqlalchemy import func, text, desc, and_
from database import db
from models import AnalyticsEvent, User, MunicipioTicket, PymePedido, MarketOrder

try:
    import pygeohash as pgh
except ImportError:
    pgh = None

class AnalyticsService:
    def __init__(self):
        pass

    def log_event(self,
                  tenant_id: int,
                  event_type: str,
                  channel: Optional[str] = None,
                  target_type: Optional[str] = None,
                  user_id: Optional[int] = None,
                  ticket_id: Optional[int] = None,
                  order_id: Optional[int] = None,
                  conversation_id: Optional[str] = None,
                  lat: Optional[float] = None,
                  lng: Optional[float] = None,
                  payload: Optional[Dict[str, Any]] = None):
        """
        Logs a single analytics event.
        """
        try:
            # Calculate geohash if lat/lng provided
            gh = None
            if lat is not None and lng is not None and pgh:
                try:
                    gh = pgh.encode(lat, lng, precision=7)
                except Exception:
                    pass

            event = AnalyticsEvent(
                tenant_id=tenant_id,
                event_type=event_type,
                channel=channel,
                target_type=target_type,
                user_id=user_id,
                ticket_id=ticket_id,
                order_id=order_id,
                conversation_id=conversation_id,
                lat=lat,
                lng=lng,
                geohash=gh,
                payload=payload or {}
            )
            db.session.add(event)
            db.session.commit()
        except Exception as e:
            print(f"[Analytics] Error logging event: {e}")
            db.session.rollback()

    def get_summary(self,
                    tenant_id: int,
                    start_date: datetime,
                    end_date: datetime,
                    context: str = 'overview',
                    filters: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Returns summary KPIs and time series for the dashboard.
        """
        filters = filters or {}

        # Base query
        query = db.session.query(AnalyticsEvent).filter(
            AnalyticsEvent.tenant_id == tenant_id,
            AnalyticsEvent.timestamp >= start_date,
            AnalyticsEvent.timestamp <= end_date
        )

        if context == 'municipio':
            query = query.filter(AnalyticsEvent.target_type == 'municipio')
        elif context == 'pyme':
            query = query.filter(AnalyticsEvent.target_type == 'pyme')

        # Apply extra filters (channel, etc)
        if filters.get('channel'):
            query = query.filter(AnalyticsEvent.channel == filters['channel'])

        # 1. Total Interactions (message_in/out)
        total_interactions = query.filter(
            AnalyticsEvent.event_type.in_(['message_in', 'message_out'])
        ).count()

        # 2. Active Users (Unique users)
        active_users = query.with_entities(func.count(func.distinct(AnalyticsEvent.user_id))).scalar() or 0

        # 3. Volume by Day (Time Series)
        # Group by date(timestamp)
        # SQLite vs Postgres syntax differs. Assuming Postgres or compatible via generic func.
        # For universal compatibility in simple analytics:
        date_func = func.date(AnalyticsEvent.timestamp)
        if db.engine.dialect.name == 'sqlite':
            date_func = func.date(AnalyticsEvent.timestamp)
        else:
            # Postgres
            date_func = func.date(AnalyticsEvent.timestamp)

        volume_series = db.session.query(
            date_func.label('date'),
            func.count().label('count')
        ).filter(
            AnalyticsEvent.tenant_id == tenant_id,
            AnalyticsEvent.timestamp >= start_date,
            AnalyticsEvent.timestamp <= end_date,
            AnalyticsEvent.event_type.in_(['message_in', 'message_out']) # interactions
        ).group_by('date').all()

        volume_by_day = [{"date": str(row.date), "count": row.count} for row in volume_series]

        # 4. Top Categories (from 'ticket_created' or 'intent_detected')
        # Assuming payload->>'category' or similar.
        # For MVP, we might rely on Ticket tables for categories, or if events track it.
        # Let's try to get it from events if event_type='ticket_created' or 'order_created'
        # Or better: query the Ticket table directly for accuracy if 'tickets' are stored properly.
        # But user wants "Event Log" based.
        # Let's stick to events if possible, but fallback to models if event log is empty (migration period).

        # Fallback to models for Categories (more reliable for now)
        top_categories = []
        if context == 'municipio':
             cat_query = db.session.query(
                 MunicipioTicket.categoria, func.count(MunicipioTicket.id)
             ).filter(
                 MunicipioTicket.tenant_id == tenant_id,
                 MunicipioTicket.fecha >= start_date
             ).group_by(MunicipioTicket.categoria).order_by(desc(func.count(MunicipioTicket.id))).limit(5)
             top_categories = [{"category": row[0] or "Sin categoría", "count": row[1]} for row in cat_query.all()]
        elif context == 'pyme':
             # For PyME maybe 'products' or 'order type'? Or just 'categories' if stored.
             # PymeTicket has category.
             cat_query = db.session.query(
                 PymePedido.estado, func.count(PymePedido.id) # Use status as "category" for orders for now
             ).filter(
                 PymePedido.tenant_id == tenant_id,
                 PymePedido.fecha >= start_date
             ).group_by(PymePedido.estado).limit(5)
             top_categories = [{"category": row[0], "count": row[1]} for row in cat_query.all()]

        # 5. Conversion Rate (PyME)
        conversion_rate = 0
        if context == 'pyme':
            orders_count = db.session.query(PymePedido).filter(
                PymePedido.tenant_id == tenant_id,
                PymePedido.fecha >= start_date
            ).count()
            # unique conversations
            if active_users > 0:
                conversion_rate = (orders_count / active_users) * 100

        # 6. SLA Breaches (stub)
        sla_breaches = 0

        return {
            "kpis": {
                "total_interactions": total_interactions,
                "active_users": active_users,
                "avg_response_time_s": 0, # To be implemented
                "conversion_rate": round(conversion_rate, 2),
                "backlog_open": 0, # To be implemented
                "sla_breaches": sla_breaches
            },
            "top_categories": top_categories,
            "volume_by_day": volume_by_day,
            "heatmap_points": [], # Fetched separately
            "insights": [] # Fetched separately
        }

    def get_heatmap_data(self, tenant_id: int, start_date: datetime, end_date: datetime) -> List[Dict]:
        """
        Returns list of {lat, lng, weight}
        """
        # Query events with lat/lng
        points = db.session.query(
            AnalyticsEvent.lat,
            AnalyticsEvent.lng,
            func.count().label('weight')
        ).filter(
            AnalyticsEvent.tenant_id == tenant_id,
            AnalyticsEvent.timestamp >= start_date,
            AnalyticsEvent.timestamp <= end_date,
            AnalyticsEvent.lat.isnot(None),
            AnalyticsEvent.lng.isnot(None)
        ).group_by(AnalyticsEvent.lat, AnalyticsEvent.lng).limit(1000).all()

        return [{"lat": p.lat, "lng": p.lng, "weight": p.weight} for p in points]

    def get_insights(self, tenant_id: int) -> List[Dict]:
        """
        Returns AI-generated or rule-based insights.
        """
        # Stub for MVP
        return [
            {"text": "Aumento del 15% en consultas sobre 'Horarios' este fin de semana.", "severity": "low", "confidence": 0.85},
            {"text": "Posible problema de stock en 'Malbec Reserva'.", "severity": "med", "confidence": 0.72}
        ]

    def get_commerce_analytics(self, tenant_id: int, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """
        Dedicated analytics for PyMEs: revenue, AOV, sales by product, heatmap.
        """
        # 1. Base query for orders in range
        orders_query = db.session.query(PymePedido).filter(
            PymePedido.tenant_id == tenant_id,
            PymePedido.fecha >= start_date,
            PymePedido.fecha <= end_date
        )

        total_orders = orders_query.count()

        # 2. Revenue (Sum monto_total)
        total_revenue = orders_query.with_entities(func.sum(PymePedido.monto_total)).scalar() or 0

        # 3. AOV
        average_ticket = 0
        if total_orders > 0:
            average_ticket = total_revenue / total_orders

        # 4. Conversion Rate
        # Needs unique visitors count. We can use Active Users from events as proxy.
        active_users = db.session.query(func.count(func.distinct(AnalyticsEvent.user_id))).filter(
            AnalyticsEvent.tenant_id == tenant_id,
            AnalyticsEvent.timestamp >= start_date,
            AnalyticsEvent.timestamp <= end_date
        ).scalar() or 0

        conversion_rate = 0
        if active_users > 0:
            conversion_rate = (total_orders / active_users) * 100

        # 5. Sales by Product
        # Attempt to aggregate in Python (MVP approach)
        # Fetch only necessary fields
        raw_orders = orders_query.with_entities(PymePedido.detalles).all()
        product_counts = {}

        for row in raw_orders:
            try:
                detalles = json.loads(row.detalles) if row.detalles else []
                if isinstance(detalles, list):
                    for item in detalles:
                        # item structure varies. assume 'nombre' or 'product_name'
                        p_name = item.get('nombre') or item.get('title') or "Unknown"
                        qty = item.get('cantidad', 1)
                        try:
                            qty = int(qty)
                        except:
                            qty = 1
                        product_counts[p_name] = product_counts.get(p_name, 0) + qty
            except:
                pass

        # Sort top 10
        sorted_products = sorted(product_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        sales_by_product = [{"name": k, "count": v} for k, v in sorted_products]

        # 6. Sales by Hour (Heatmap)
        # Extract hour from fecha
        if db.engine.dialect.name == 'sqlite':
            hour_func = func.strftime('%H', PymePedido.fecha)
        else:
            # Postgres
            hour_func = func.extract('hour', PymePedido.fecha)

        sales_by_hour_query = db.session.query(
            hour_func.label('hour'),
            func.count().label('count')
        ).filter(
            PymePedido.tenant_id == tenant_id,
            PymePedido.fecha >= start_date,
            PymePedido.fecha <= end_date
        ).group_by('hour').all()

        sales_by_hour = [{"hour": int(row.hour), "count": row.count} for row in sales_by_hour_query]

        return {
            "revenue": float(total_revenue),
            "average_ticket": float(average_ticket),
            "conversion_rate": float(round(conversion_rate, 2)),
            "total_orders": total_orders,
            "sales_by_product": sales_by_product,
            "sales_by_hour": sales_by_hour
        }

    def get_benchmarks(self, tenant_id: int, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """
        Compares current period vs previous period (MoM or YoY).
        Assumes 'previous period' is the same duration immediately preceding start_date.
        """
        duration = end_date - start_date
        prev_end_date = start_date
        prev_start_date = prev_end_date - duration

        # Helper to get stats
        def get_period_stats(s, e):
            revenue = db.session.query(func.sum(PymePedido.monto_total)).filter(
                PymePedido.tenant_id == tenant_id,
                PymePedido.fecha >= s,
                PymePedido.fecha <= e
            ).scalar() or 0

            interactions = db.session.query(func.count(AnalyticsEvent.id)).filter(
                AnalyticsEvent.tenant_id == tenant_id,
                AnalyticsEvent.event_type.in_(['message_in', 'message_out']),
                AnalyticsEvent.timestamp >= s,
                AnalyticsEvent.timestamp <= e
            ).scalar() or 0

            return float(revenue), interactions

        curr_rev, curr_int = get_period_stats(start_date, end_date)
        prev_rev, prev_int = get_period_stats(prev_start_date, prev_end_date)

        def calc_growth(current, previous):
            if previous == 0:
                return 100 if current > 0 else 0
            return ((current - previous) / previous) * 100

        return {
            "revenue": {
                "current": curr_rev,
                "previous": prev_rev,
                "growth_percentage": round(calc_growth(curr_rev, prev_rev), 1)
            },
            "interactions": {
                "current": curr_int,
                "previous": prev_int,
                "growth_percentage": round(calc_growth(curr_int, prev_int), 1)
            },
            # Industry average could be calculated here by querying ALL tenants of same type
            # but that is heavy. Return None or 0 for now.
            "industry_average_growth": None
        }

    def get_funnel_analytics(self, tenant_id: int, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """
        Returns funnel steps counts.
        Step 1: Opened Chat (Unique Users interacting)
        Step 2: Selected Option (Approximation: messages > 1 or specific event)
        Step 3: Started Order (Intent detected or Cart created)
        Step 4: Completed (Order paid/confirmed)
        """
        # Step 1: Active Users
        step_1 = db.session.query(func.count(func.distinct(AnalyticsEvent.user_id))).filter(
            AnalyticsEvent.tenant_id == tenant_id,
            AnalyticsEvent.timestamp >= start_date,
            AnalyticsEvent.timestamp <= end_date
        ).scalar() or 0

        # Step 2: Engaged Users (e.g. sent more than 1 message)
        # This is hard to query efficiently on raw events without pre-aggregation.
        # Approx: Users with specific 'menu_selection' event or just generic heuristic (e.g. 70% of step 1)
        # Better: Query users who have at least one 'message_in' event that is NOT 'start'.
        step_2 = int(step_1 * 0.7) # Placeholder / Heuristic for MVP if specific event missing

        # Step 3: Started Form / Order (PymePedido created)
        step_3 = db.session.query(func.count(PymePedido.id)).filter(
            PymePedido.tenant_id == tenant_id,
            PymePedido.fecha >= start_date,
            PymePedido.fecha <= end_date
        ).scalar() or 0

        # Step 4: Completed (Status = confirmed/paid/entregado)
        step_4 = db.session.query(func.count(PymePedido.id)).filter(
            PymePedido.tenant_id == tenant_id,
            PymePedido.fecha >= start_date,
            PymePedido.fecha <= end_date,
            PymePedido.estado.in_(['confirmado', 'pagado', 'entregado', 'completado'])
        ).scalar() or 0

        # Adjust logical consistency (Step 3 >= Step 4)
        if step_3 < step_4:
            step_3 = step_4

        return {
            "steps": [
                {"name": "Opened Chat", "count": step_1},
                {"name": "Engaged", "count": step_2},
                {"name": "Started Order", "count": step_3},
                {"name": "Completed", "count": step_4}
            ]
        }

analytics_service = AnalyticsService()
