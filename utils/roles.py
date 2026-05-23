"""Role definitions and permission mappings for RBAC."""

import unicodedata

# Role Constants
ROLE_SUPERADMIN = "super_admin"
ROLE_TENANT_ADMIN = "admin"  # Maps to legacy 'admin' which is per-tenant
ROLE_EMPLEADO = "empleado"
ROLE_CLIENTE = "usuario"     # Maps to legacy 'usuario' (end user)
ROLE_LEAD = "lead"

ROLE_ALIASES = {
    "superadmin": ROLE_SUPERADMIN,
    "super-admin": ROLE_SUPERADMIN,
    "super_admin": ROLE_SUPERADMIN,
    "platform_admin": ROLE_SUPERADMIN,
    "admin": ROLE_TENANT_ADMIN,
    "tenant_admin": ROLE_TENANT_ADMIN,
    "tenant-admin": ROLE_TENANT_ADMIN,
    "admin_pyme": ROLE_TENANT_ADMIN,
    "admin_municipio": ROLE_TENANT_ADMIN,
    "admin_colegio": ROLE_TENANT_ADMIN,
    "operador": ROLE_EMPLEADO,
    "operator": ROLE_EMPLEADO,
    "empleado": ROLE_EMPLEADO,
    "empleado_pyme": ROLE_EMPLEADO,
    "empleado_municipio": ROLE_EMPLEADO,
    "empleado_colegio": ROLE_EMPLEADO,
    "usuario": ROLE_CLIENTE,
    "cliente": ROLE_CLIENTE,
    "lead": ROLE_LEAD,
}

GENERIC_TENANT_SLUGS = {
    "",
    "app",
    "admin",
    "superadmin",
    "super-admin",
    "super_admin",
    "dashboard",
    "perfil",
    "profile",
}

TENANT_TYPE_ALIASES = {
    "municipio": "municipio",
    "gobierno": "municipio",
    "government": "municipio",
    "pyme": "pyme",
    "empresa": "pyme",
    "empresas": "pyme",
    "commerce": "pyme",
    "comercio": "pyme",
    "colegio": "colegio",
    "colegios": "colegio",
    "educacion": "colegio",
    "educación": "colegio",
    "school": "colegio",
}

# Permission Constants
PERM_MANAGE_TENANTS = "manage_tenants"
PERM_MANAGE_EMPLOYEES = "manage_employees"
PERM_MANAGE_CATALOG = "manage_catalog"
PERM_VIEW_STATS = "view_stats"
PERM_HANDLE_TICKETS = "handle_tickets"
PERM_CREATE_TICKET = "create_ticket"
PERM_VIEW_OWN_TICKETS = "view_own_tickets"
PERM_MAKE_ORDERS = "make_orders"

# Role -> Permissions Mapping
ROLE_PERMISSIONS = {
    ROLE_SUPERADMIN: {
        PERM_MANAGE_TENANTS,
        PERM_VIEW_STATS,
    },
    ROLE_TENANT_ADMIN: {
        PERM_MANAGE_EMPLOYEES,
        PERM_MANAGE_CATALOG,
        PERM_VIEW_STATS,
        PERM_HANDLE_TICKETS,
    },
    ROLE_EMPLEADO: {
        PERM_HANDLE_TICKETS,
        PERM_VIEW_STATS, # Limited view usually
    },
    ROLE_CLIENTE: {
        PERM_CREATE_TICKET,
        PERM_VIEW_OWN_TICKETS,
        PERM_MAKE_ORDERS,
    },
    ROLE_LEAD: {
        PERM_MAKE_ORDERS, # Maybe?
    }
}


def canonical_role(value: str | None) -> str:
    """Return the canonical role used by access checks."""
    role = str(value or "").strip().lower()
    return ROLE_ALIASES.get(role, role)


def is_super_admin_role(value: str | None) -> bool:
    return canonical_role(value) == ROLE_SUPERADMIN


def normalize_tenant_slug(value: str | None) -> str:
    return str(value or "").strip().lower()


def is_generic_tenant_slug(value: str | None) -> bool:
    return normalize_tenant_slug(value) in GENERIC_TENANT_SLUGS


def first_specific_tenant_slug(*values: str | None) -> str:
    for value in values:
        slug = normalize_tenant_slug(value)
        if slug and not is_generic_tenant_slug(slug):
            return slug
    return ""


def normalize_tenant_type(value: str | None, default: str = "pyme") -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        raw = default
    raw = "".join(
        char for char in unicodedata.normalize("NFKD", raw)
        if not unicodedata.combining(char)
    )
    return TENANT_TYPE_ALIASES.get(raw, default)


def tenant_owner_field_for_tipo(tipo: str | None) -> str:
    normalized = normalize_tenant_type(tipo)
    if normalized == "municipio":
        return "municipio_id"
    return "pyme_id"


def role_for_tenant_type(tipo: str | None) -> str:
    normalized = normalize_tenant_type(tipo)
    if normalized == "municipio":
        return "admin_municipio"
    if normalized == "colegio":
        return "admin_colegio"
    return "admin_pyme"


def has_permission(user_role: str, permission: str) -> bool:
    """Check if the given role has the requested permission."""
    # Superadmin has implicit access to everything (or define explicitly)
    role = canonical_role(user_role)
    if role == ROLE_SUPERADMIN:
        return True

    perms = ROLE_PERMISSIONS.get(role, set())
    return permission in perms
