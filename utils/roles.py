"""Role definitions and permission mappings for RBAC."""

# Role Constants
ROLE_SUPERADMIN = "superadmin"
ROLE_TENANT_ADMIN = "admin"  # Maps to legacy 'admin' which is per-tenant
ROLE_EMPLEADO = "empleado"
ROLE_CLIENTE = "usuario"     # Maps to legacy 'usuario' (end user)
ROLE_LEAD = "lead"

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

def has_permission(user_role: str, permission: str) -> bool:
    """Check if the given role has the requested permission."""
    # Superadmin has implicit access to everything (or define explicitly)
    if user_role == ROLE_SUPERADMIN:
        return True

    perms = ROLE_PERMISSIONS.get(user_role, set())
    return permission in perms
