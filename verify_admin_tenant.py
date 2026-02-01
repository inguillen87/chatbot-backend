
import sys
import os

# Add root directory to path so we can import routes
sys.path.insert(0, os.path.abspath(os.getcwd()))

try:
    from routes.admin_tenant import admin_tenant_bp
    print("Import successful. Blueprint name:", admin_tenant_bp.name)
except ImportError as e:
    print(f"ImportError: {e}")
    sys.exit(1)
except Exception as e:
    print(f"Error: {e}")
    sys.exit(1)
