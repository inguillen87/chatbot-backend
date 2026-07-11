from utils.safe_logging import describe_database_uri


def test_describe_database_uri_redacts_password_and_query_values():
    raw = (
        "postgresql+psycopg://chatboc_user:database-secret@db.example:5432/chatboc"
        "?sslmode=require&access_token=query-secret"
    )

    rendered = describe_database_uri(raw)

    assert "database-secret" not in rendered
    assert "query-secret" not in rendered
    assert "chatboc_user" not in rendered
    assert ":***@db.example:5432/chatboc" in rendered
    assert rendered.endswith("?access_token&sslmode")


def test_describe_database_uri_handles_missing_and_malformed_values():
    assert describe_database_uri(None) == "<unset>"
    assert describe_database_uri("not a valid uri") == "<configured>"
