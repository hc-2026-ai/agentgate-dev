from __future__ import annotations

from agentgate.integrations.targets.inbank.diagnostics import (
    business_summary,
    mapping_shape,
    safe_url,
)


def test_safe_url_removes_credentials_query_values_and_fragment() -> None:
    rendered = safe_url(
        "https://user:password@example.test:8443/path?taskId=secret&token=private#x"
    )

    assert rendered == (
        "https://example.test:8443/path?taskId=<redacted>&token=<redacted>"
    )
    assert "password" not in rendered
    assert "secret" not in rendered
    assert "private" not in rendered


def test_mapping_diagnostics_show_shape_and_status_without_values() -> None:
    value = {
        "code": "0000",
        "token": "private-token",
        "data": {"status": "ok", "answer": "private-answer"},
    }

    shape = mapping_shape(value)
    summary = business_summary(value)

    assert "private-token" not in shape
    assert "private-answer" not in shape
    assert "private-token" not in summary
    assert "private-answer" not in summary
    assert "code='0000'" in summary
    assert "data.status='ok'" in summary
