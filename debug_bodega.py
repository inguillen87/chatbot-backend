from app import create_app
from models import TenantProfile, PymePedido, Order, User

app = create_app()

with app.app_context():
    # Find Tenant
    print("--- Searching for Tenant 'Bodega Cuatro Fincas' ---")
    tenant = TenantProfile.query.filter(TenantProfile.nombre.ilike("%Bodega Cuatro Fincas%")).first()

    if not tenant:
        print("Tenant not found by name. Listing all tenants:")
        for t in TenantProfile.query.all():
            print(f"  {t.id}: {t.nombre} ({t.slug})")
    else:
        print(f"Tenant Found: ID={tenant.id}, Slug='{tenant.slug}', Type='{tenant.tipo}'")
        print(f"  pyme_id={tenant.pyme_id}, municipio_id={tenant.municipio_id}")

        # Check Owner
        if tenant.pyme_id:
            owner = User.query.get(tenant.pyme_id)
            print(f"  Owner (Pyme): ID={owner.id}, Name='{owner.name}', Email='{owner.email}'")

        # Check PymePedido (Legacy)
        print("\n--- Checking PymePedido (Legacy) ---")
        # Query matching the one in admin_tenant.py
        legacy_query = PymePedido.query.filter(
            (PymePedido.tenant_id == tenant.id) | (PymePedido.pyme_id == tenant.pyme_id)
        )
        orders = legacy_query.all()
        print(f"  Query found {len(orders)} orders.")

        for o in orders:
            print(f"    - ID={o.id}, Nro='{o.nro_pedido}', TenantID={o.tenant_id}, PymeID={o.pyme_id}, Status='{o.estado}'")

        # Check Order (New)
        print("\n--- Checking Order (New) ---")
        new_orders = Order.query.filter(Order.tenant_id == tenant.id).all()
        print(f"  Query found {len(new_orders)} new orders.")
        for o in new_orders:
            print(f"    - ID={o.id}, Status='{o.status}'")

        # Check if there are orphaned PymePedidos for this Pyme User but not linked to Tenant?
        if tenant.pyme_id:
            print("\n--- Checking potential orphans (PymeID match, but not caught by query?) ---")
            orphans = PymePedido.query.filter(PymePedido.pyme_id == tenant.pyme_id).all()
            print(f"  Total orders with pyme_id={tenant.pyme_id}: {len(orphans)}")
