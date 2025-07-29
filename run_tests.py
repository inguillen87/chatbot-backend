import pytest
import sys

def main():
    """
    Runs the tests.
    """
    # Simply run pytest. The test fixtures should handle app creation and context.
    result = pytest.main(['-v', 'tests'])
    sys.exit(result)

if __name__ == '__main__':
    main()
