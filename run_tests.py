import sys
import os
import pytest
from app import create_app, db
from config import TestConfig

# Add project root to sys.path
project_root = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, project_root)

if __name__ == '__main__':
    print("sys.path:", sys.path)
    from models import *
    app = create_app(config_class=TestConfig)
    with app.app_context():
        db.create_all()
        print("✅ Database created for testing.")

    # Run pytest
