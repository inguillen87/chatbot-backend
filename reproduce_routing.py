
from flask import Flask, Blueprint
from app import app

def test_routes():
    print("Testing routes...")
    with app.test_client() as client:
        # Test /api/rubros/
        resp = client.get('/api/rubros/')
        print(f"/api/rubros/ status: {resp.status_code}")

        # Test /api/rubros (no slash)
        resp = client.get('/api/rubros')
        print(f"/api/rubros status: {resp.status_code}")

        # Test /rubros/
        resp = client.get('/rubros/')
        print(f"/rubros/ status: {resp.status_code}")

if __name__ == "__main__":
    test_routes()
