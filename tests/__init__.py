import sys
import os

# Add project root to sys.path to ensure modules like 'models.py' are findable
# when tests are run with 'python -m unittest discover -s tests'
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

print(f"✅ tests/__init__.py executed. Project root '{project_root}' added to sys.path.")

# The primary purpose of this file is to ensure the project root is in sys.path
# so that modules like 'models.py' and 'app.py' (for create_app) can be found
# when running tests using 'python -m unittest discover -s tests'.

# Experimental pre-imports of Flask submodules have been removed as they can be problematic
# and might hide underlying issues with test setup or app context.
# Flask components should be imported directly where needed, typically within test files
# or the application code itself, relying on a correctly configured Python environment
# and proper app context management within tests.

# You can also add other test-wide initializations here if needed,
# but avoid overly complex logic that might obscure test discovery or setup.

# Experimental: Attempt to pre-import requests.exceptions and twilio.request_validator
# to help with "ModuleNotFoundError" issues that can occur in some environments
# when google cloud libraries or twilio are imported.
try:
    import requests.exceptions
    print("✅ Successfully pre-imported requests.exceptions in tests/__init__.py")
except ImportError as e_req:
    print(f"⚠️ Failed to pre-import requests.exceptions in tests/__init__.py: {e_req}")
except Exception as e_req_gen: # Catch broader exceptions too
    print(f"⚠️ General error during experimental requests.exceptions import: {e_req_gen}")

try:
    import twilio.request_validator
    print("✅ Successfully pre-imported twilio.request_validator in tests/__init__.py")
except ImportError as e_twilio:
    print(f"⚠️ Failed to pre-import twilio.request_validator in tests/__init__.py: {e_twilio}")
except Exception as e_twilio_gen:
    print(f"⚠️ General error during experimental twilio.request_validator import: {e_twilio_gen}")
