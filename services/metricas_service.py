"""Observed business metrics with an exact tenant and explicit data limits."""
from copy import deepcopy
from math import isfinite

from models import db, PymePedido, Role, TenantProfile, UserRole
from services.analytics.repository import _pyme_entity_scope_filter
from services.tenant_ticket_scope import resolve_unique_tenant_for_owner
from utils.roles import (ROLE_ANALYTICS_VIEWER, ROLE_SUPERADMIN, ROLE_TENANT_ADMIN, canonical_role,
                         is_authorized_superadmin_user, normalize_tenant_type)
from utils.tenant_admin_access import resolve_consistent_user_tenant


BUSINESS_METRICS_CONTRACT = "business.metrics.v1"
# Conservative currency support for this projection; never infer a currency.
_KNOWN_CURRENCIES = frozenset({
    "ARS", "USD", "EUR", "BRL", "CLP", "UYU", "PYG", "BOB", "PEN", "COP",
    "MXN", "CAD", "GBP", "CHF", "JPY", "CNY", "AUD", "NZD", "CRC", "DOP",
    "GTQ", "HNL", "NIO", "PAB", "SVC",
})
_UNSUPPORTED_MODULES = {
    "ventas_over_time": "sales_timeseries_not_supported",
    "top_productos": "product_sales_not_supported",
    "ventas_por_region": "regional_sales_not_supported",
}


def _unavailable(reason):
    return {"state": "unavailable", "reason_code": reason}


def business_metrics_actor_can_read(actor, tenant, *, explicit_selection=False):
    """Local read boundary; a shared owner never overrides exact membership."""
    if (actor is None or tenant is None or getattr(tenant, "is_active", False) is not True
            or normalize_tenant_type(tenant.tipo, default="") != "pyme"):
        return False
    actor_role = canonical_role(getattr(actor, "rol", None))
    if actor_role == ROLE_SUPERADMIN:
        return is_authorized_superadmin_user(actor) and explicit_selection
    # Existing tenant-scoped role assignments are explicit entitlements.
    assigned_roles = (UserRole.query.join(Role, UserRole.role_id == Role.id)
                      .filter(UserRole.user_id == actor.id, UserRole.tenant_id == tenant.id).all())
    if any(canonical_role(assignment.role.name) in {ROLE_TENANT_ADMIN, ROLE_ANALYTICS_VIEWER}
           for assignment in assigned_roles):
        return True
    if actor_role not in {ROLE_TENANT_ADMIN, ROLE_ANALYTICS_VIEWER}:
        return False
    exact_id = getattr(actor, "tenant_id", None)
    actor_slug = str(getattr(actor, "tenant_slug", None) or "").strip().lower()
    if exact_id is not None or actor_slug:
        owner_refs = {value for value in (getattr(actor, "pyme_id", None),
                                        getattr(actor, "empresa_id", None)) if value is not None}
        return bool((exact_id is None or exact_id == tenant.id)
                    and (not actor_slug or actor_slug == str(tenant.slug).lower())
                    and not getattr(actor, "municipio_id", None)
                    and (not owner_refs or owner_refs == {tenant.pyme_id}))
    legacy = resolve_consistent_user_tenant(actor)
    return legacy is not None and legacy.id == tenant.id


class MetricsScopeError(ValueError):
    """The requested business scope could not be established safely."""


class MetricasService:
    def __init__(self, pyme_id, *, tenant_id=None):
        self.pyme_id = pyme_id
        self.tenant = self._resolve_tenant(tenant_id)
        if self.tenant is None:
            raise MetricsScopeError("metrics_tenant_scope_unavailable")
        self._summary = None

    def _resolve_tenant(self, tenant_id):
        if type(self.pyme_id) is not int or self.pyme_id <= 0:
            return None
        if tenant_id is not None:
            if type(tenant_id) is not int or tenant_id <= 0:
                return None
            tenant = db.session.get(TenantProfile, tenant_id)
        else:
            resolution = resolve_unique_tenant_for_owner(self.pyme_id)
            tenant = resolution.tenant if resolution.status == "unique" else None
        if (tenant is None or tenant.pyme_id != self.pyme_id
                or normalize_tenant_type(tenant.tipo, default="") != "pyme"
                or getattr(tenant, "is_active", True) is not True):
            return None
        return tenant

    def _order_query(self):
        # Share the existing analytics policy, including legacy quarantine.
        return PymePedido.query.filter(_pyme_entity_scope_filter(
            self.tenant, tenant_column=PymePedido.tenant_id,
            owner_column=PymePedido.pyme_id,
        ))

    def get_summary(self):
        """Describe recorded order amounts; these are not collected revenue."""
        if self._summary is None:
            query = self._order_query()
            total, customers, amounts_present, amount_sum, minimum, maximum = query.with_entities(
                db.func.count(PymePedido.id),
                db.func.count(db.func.distinct(PymePedido.user_id)),
                db.func.count(PymePedido.monto_total),
                db.func.sum(PymePedido.monto_total),
                db.func.min(PymePedido.monto_total),
                db.func.max(PymePedido.monto_total),
            ).one()
            # SQL defines the canonical values before DISTINCT/LIMIT.
            # Preserve them exactly: a broader Python strip could collapse
            # ARS and ARS+tab/NBSP, hiding a third distinct currency.
            # Such unsupported padding stays unknown, never inferred as ARS.
            currencies = {
                row[0] if isinstance(row[0], str) else ""
                for row in query.with_entities(db.func.upper(db.func.trim(PymePedido.moneda)))
                .distinct().limit(2).all()
            }
            currency = next(iter(currencies)) if len(currencies) == 1 else None
            currency = currency if currency in _KNOWN_CURRENCIES else None
            amount_reason = None
            if amounts_present != total:
                amount_reason = "missing_order_amount"
            elif total and currency is None:
                amount_reason = "mixed_or_unknown_currency"
            elif any(value is not None and not isfinite(value)
                     for value in (amount_sum, minimum, maximum)):
                amount_reason = "invalid_order_amount"
            elif any(value is not None and abs(value) > 9007199254740991
                     for value in (amount_sum, minimum, maximum)):
                amount_reason = "unsafe_order_amount"
            total_sales = None if amount_reason else (amount_sum if total else 0)
            avg = total_sales / total if total and total_sales is not None else None
            amount_availability = (
                _unavailable(amount_reason) if amount_reason
                else {"state": "available", "definition": "sum_order_amount"}
            )
            avg_availability = (
                _unavailable(amount_reason or "no_orders") if avg is None
                else {"state": "available", "definition": "sum_order_amount_divided_by_order_count"}
            )
            self._summary = {
                "contract_version": BUSINESS_METRICS_CONTRACT,
                "scope": {"tenant_id": self.tenant.id, "source": "pyme_pedido",
                          "period": "all_time", "order_states": "all_recorded_states"},
                "total_sales": total_sales,
                "total_orders": total,
                "unique_customers": customers,
                # Compatibility alias: distinct recorded user IDs, not new registrations.
                "new_customers": customers,
                "avg_order_value": avg,
                "currency": currency,
                "conversion_rate": None,
                "trend": None,
                "availability": {
                    "total_sales": amount_availability,
                    "total_orders": {"state": "available", "definition": "count_order_records"},
                    "unique_customers": {"state": "available", "definition": "distinct_order_user_ids"},
                    "new_customers": {"state": "available", "definition": "alias_unique_customers"},
                    "avg_order_value": avg_availability,
                    "conversion_rate": _unavailable("missing_conversion_denominator"),
                    "trend": _unavailable("missing_comparison_period"),
                },
            }
        return deepcopy(self._summary)

    def get_total_ingresos(self):
        """Compatibility accessor for recorded order amounts, not paid revenue."""
        return self.get_summary()["total_sales"]

    def get_total_pedidos(self):
        return self.get_summary()["total_orders"]

    def get_new_customers(self):
        return self.get_summary()["unique_customers"]

    def get_conversion_rate(self):
        return None

    def get_kpis(self):
        payload = self.get_summary()
        payload.update({
            "sales_growth": None, "customer_acquisition_cost": None,
            "customer_lifetime_value": None,
            "average_order_value": payload["avg_order_value"],
        })
        payload["availability"].update({
            "sales_growth": _unavailable("missing_comparison_period"),
            "customer_acquisition_cost": _unavailable("missing_acquisition_cost_source"),
            "customer_lifetime_value": _unavailable("missing_lifetime_value_source"),
            "average_order_value": deepcopy(payload["availability"]["avg_order_value"]),
        })
        return payload

    @staticmethod
    def get_module_availability():
        return {
            "kpis": {"state": "partial", "reason_code": "some_metrics_unavailable"},
            **{key: _unavailable(reason) for key, reason in _UNSUPPORTED_MODULES.items()},
        }

    def get_top_productos_vendidos(self):
        return []

    def get_sales_over_time(self):
        return []

    def get_top_products(self):
        return []

    def get_sales_by_region(self):
        return []
