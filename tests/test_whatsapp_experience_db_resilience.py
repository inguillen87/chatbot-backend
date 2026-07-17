from contextlib import nullcontext

from services.whatsapp_experience import _safe_all, _safe_count


class _FakeSavepoint:
    def __init__(self, session):
        self.session = session
        self.is_active = True

    def commit(self):
        self.is_active = False
        self.session.commits += 1

    def rollback(self):
        self.is_active = False
        self.session.aborted = False
        self.session.rollbacks += 1


class _FakeConnection:
    def __init__(self, session):
        self.session = session

    def begin_nested(self):
        self.session.savepoints += 1
        return _FakeSavepoint(self.session)


class _FakeSession:
    def __init__(self):
        self.aborted = False
        self.commits = 0
        self.rollbacks = 0
        self.savepoints = 0
        self.no_autoflush = nullcontext()

    def connection(self):
        return _FakeConnection(self)


class _CountQuery:
    def __init__(self, session, *, value=0, fail=False):
        self.session = session
        self.value = value
        self.fail = fail

    def count(self):
        if self.fail:
            self.session.aborted = True
            raise RuntimeError("optional schema unavailable")
        if self.session.aborted:
            raise RuntimeError("transaction still aborted")
        return self.value


class _AllQuery:
    def __init__(self, session, *, rows=None, fail=False):
        self.session = session
        self.rows = rows or []
        self.fail = fail

    def all(self):
        if self.fail:
            self.session.aborted = True
            raise RuntimeError("optional schema unavailable")
        if self.session.aborted:
            raise RuntimeError("transaction still aborted")
        return self.rows


def test_safe_count_rolls_back_only_its_savepoint_after_optional_schema_error():
    session = _FakeSession()

    assert _safe_count(_CountQuery(session, fail=True)) == 0
    assert session.rollbacks == 1
    assert session.aborted is False

    assert _safe_count(_CountQuery(session, value=7)) == 7
    assert session.commits == 1
    assert session.savepoints == 2


def test_safe_all_recovers_session_for_following_template_registry_query():
    session = _FakeSession()

    assert _safe_all(_AllQuery(session, fail=True)) == []
    assert session.rollbacks == 1

    rows = [{"name": "survey_vote"}]
    assert _safe_all(_AllQuery(session, rows=rows)) == rows
    assert session.commits == 1
    assert session.savepoints == 2
