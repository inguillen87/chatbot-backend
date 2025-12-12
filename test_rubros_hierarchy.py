
import json
from app import app

def test_rubros_hierarchy():
    print("Testing /api/rubros hierarchy (padre_id field)...")

    with app.test_client() as client:
        # Test /api/rubros/
        resp = client.get('/api/rubros/')
        print(f"Status: {resp.status_code}")

        if resp.status_code != 200:
            print("Failed to fetch rubros.")
            return

        data = resp.get_json()
        if not data:
            print("No data received.")
            return

        print(f"Received {len(data)} items.")

        # Check for padre_id
        missing_padre_id = [item for item in data if "padre_id" not in item]

        if missing_padre_id:
            print(f"❌ FAILURE: {len(missing_padre_id)} items missing 'padre_id' field.")
            print("Example missing:", missing_padre_id[0])
        else:
            print("✅ SUCCESS: All items have 'padre_id' field.")

        # Optional: Print hierarchy sample
        roots = [item for item in data if item['padre_id'] is None]
        children = [item for item in data if item['padre_id'] is not None]
        print(f"Roots: {len(roots)}, Children: {len(children)}")

if __name__ == "__main__":
    test_rubros_hierarchy()
