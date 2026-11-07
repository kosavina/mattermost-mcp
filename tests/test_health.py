from mattermost_mcp import health


def test_health_returns_ok() -> None:
    assert health() == {"status": "ok"}


def test_health_status_value() -> None:
    assert health() == {"status": "ok"}
