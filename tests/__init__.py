import eventlet
eventlet.monkey_patch()

import os
import sys

# Add the project root to the Python path
# This allows tests to import modules from the 'services', 'routes', etc. directories
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)
    print(f"✅ tests/__init__.py executed. Project root '{project_root}' added to sys.path.")
