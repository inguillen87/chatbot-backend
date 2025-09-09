from utils.time_utils import get_local_now


def test_get_local_now_timezone():
    now = get_local_now()
    assert now.utcoffset().total_seconds() == -3 * 3600
