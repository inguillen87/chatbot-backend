import sys
import os
import pytest

# Add project root to sys.path
project_root = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, project_root)

if __name__ == '__main__':
    # Run pytest
    sys.exit(pytest.main())
