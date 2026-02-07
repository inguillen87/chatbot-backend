from app import create_app, db
from models import TenantProfile, PymePedido, Order, User

app = create_app()

with app.app_context():
    print("--- Reproducing Issue with 'Ferretería Demo' ---")
    tenant = TenantProfile.query.filter_by(slug="ferreteria").first()

    if not tenant:
        print("Tenant 'ferreteria' not found. Creating or picking first available.")
        tenant = TenantProfile.query.first()
        if not tenant:
            print("No tenants found.")
            exit(1)
        print(f"Using Tenant: {tenant.nombre} ({tenant.slug})")
    else:
        print(f"Tenant Found: ID={tenant.id}, Slug='{tenant.slug}', pyme_id={tenant.pyme_id}")

    # Create a 'problematic' order: Has pyme_id but NO tenant_id
    # Assuming the pyme_id is valid.
    pyme_user_id = tenant.pyme_id
    if not pyme_user_id:
        print("Tenant has no pyme_id. Assigning one if possible.")
        # Create a dummy user for pyme if needed
        if not tenant.pyme_id:
             u = User(name="Dueño Ferreteria", email="ferreteria@test.com", password_hash="hash")
             db.session.add(u)
             db.session.commit()
             tenant.pyme_id = u.id
             db.session.commit()
             pyme_user_id = u.id
             print(f"Assigned pyme_id={pyme_user_id}")

    print(f"Creating Order with pyme_id={pyme_user_id}, tenant_id=None")
    pyme_order = PymePedido(
        pyme_id=pyme_user_id,
        asunto="Pedido Prueba Missing Tenant ID",
        detalles='[]',
        monto_total=100.0,
        nombre_cliente="Cliente Test",
        tenant_id=None # Explicitly None
    )
    db.session.add(pyme_order)
    db.session.commit()
    print(f"Created PymePedido ID={pyme_order.id}, Nro='{pyme_order.nro_pedido}'")

    # Now verify if list_tenant_orders query finds it
    print("--- Simulating Admin Query ---")
    legacy_query = PymePedido.query.filter(
        (PymePedido.tenant_id == tenant.id) | (PymePedido.pyme_id == tenant.pyme_id)
    )
    found_orders = legacy_query.all()
    found_ids = [o.id for o in found_orders]

    if pyme_order.id in found_ids:
        print("✅ Order FOUND by Admin Query (even with tenant_id=None).")
    else:
        print("❌ Order NOT FOUND by Admin Query.")
        print(f"tenant.id={tenant.id}, tenant.pyme_id={tenant.pyme_id}")
        print(f"order.tenant_id={pyme_order.tenant_id}, order.pyme_id={pyme_order.pyme_id}")

    # Cleanup
    db.session.delete(pyme_order)
    db.session.commit()
