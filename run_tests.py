import pytest
import sys
import eventlet

def main():
    """
    Runs the tests.
    """
    # Apply monkey patch before running tests
    eventlet.monkey_patch()

    # Simply run pytest. The test fixtures should handle app creation and context.
    result = pytest.main(['-v', 'tests'])
    sys.exit(result)

if __name__ == '__main__':
    main()
