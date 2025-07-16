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

# Older revisions attempted to pre-import optional dependencies like ``requests``
# or ``twilio`` here to suppress ``ImportError`` warnings during test discovery.
# This approach caused noisy output and did not resolve missing-package issues.
# Tests that rely on those libraries should handle the imports themselves or
# provide mocks as needed.  Keeping this file simple avoids unnecessary
# side effects when running ``python -m unittest``.
