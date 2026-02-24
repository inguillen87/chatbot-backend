from utils import user_query


def test_user_query_inspection_failure_is_memoized(monkeypatch, app):
    monkeypatch.setattr(user_query, "_ES_EMPLEADO_COLUMN_EXISTS", None)
    monkeypatch.setattr(user_query, "_ES_EMPLEADO_INSPECTION_LOGGED_FAILURE", False)

    class Boom(Exception):
        pass

    def _raise(*args, **kwargs):
        raise Boom("db down")

    monkeypatch.setattr(user_query, "inspect", _raise)

    with app.app_context():
        first = user_query._user_table_has_es_empleado_column()
        second = user_query._user_table_has_es_empleado_column()

    assert first is True
    assert second is True
    assert user_query._ES_EMPLEADO_COLUMN_EXISTS is True
