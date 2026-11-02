"""Target composition loads only selected settings and owns no remote resources."""

from contextlib import ExitStack
from unittest.mock import Mock

import pytest
from test_celery_dispatcher import target as demo_target

from agentgate.domain import TargetSnapshot
from agentgate.integrations.targets import execution_factory as factory


def target_snapshot(adapter_type, adapter_version="1"):
    values = demo_target().model_dump(exclude={"content_sha256"})
    values.update(
        adapter_type=adapter_type,
        adapter_version=adapter_version,
        invocation_config=(
            {"branch_id": "branch-review"}
            if adapter_type == "inbank_yunxia"
            else {"arrange_type": "base"}
        ),
    )
    return TargetSnapshot(**values)


def configure_inbank(monkeypatch, adapter_type):
    monkeypatch.setenv("AGENTGATE_INBANK_CREATE_BASE_URL", "http://bank.invalid")
    monkeypatch.setenv("AGENTGATE_TRACE_SERVER_URL", "http://trace.invalid")
    monkeypatch.setenv("AGENTGATE_TRACE_SERVER_SOURCE_TIMEZONE", "Asia/Shanghai")
    prefix = "CHATABC" if adapter_type == "inbank_chatabc" else "YUNXIA"
    for name in ("HEALTH", "POD_API", "DELETE"):
        monkeypatch.setenv(f"AGENTGATE_INBANK_{prefix}_{name}_BASE_URL", "http://bank.invalid")
    if prefix == "YUNXIA":
        monkeypatch.setenv("AGENTGATE_INBANK_YUNXIA_AGENT_NAMESPACE", "test")
        monkeypatch.setenv("AGENTGATE_INBANK_YUNXIA_POST_HEALTH_SLEEP_SECONDS", "0")


@pytest.mark.parametrize(
    "adapter_class,resolver",
    [
        (factory.DemoLoanTargetAdapter, None),
        (factory.LocalBankAdapter, factory.resolve_local_bank_trace),
        (factory.InbankChatABCTargetAdapter, factory.resolve_chatabc_trace),
        (factory.InbankYunxiaTargetAdapter, factory.resolve_yunxia_trace),
    ],
)
def test_create_selects_components_without_starting_target(monkeypatch, adapter_class, resolver):
    for name in ("load_chatabc_settings", "load_yunxia_settings"):
        loader = getattr(factory, name)
        monkeypatch.setattr(factory, name, Mock(wraps=loader))
    if adapter_class.adapter_type.startswith("inbank_"):
        configure_inbank(monkeypatch, adapter_class.adapter_type)
    with ExitStack() as resources:
        adapter, actual_resolver = factory.TargetExecutionFactory.create(
            target_snapshot(adapter_class.adapter_type), resources,
            trace_token="bank-task-token" if adapter_class.adapter_type.startswith("inbank_") else None,
        )
        assert isinstance(adapter, adapter_class)
        if resolver is not None:
            assert actual_resolver is resolver
        else:
            assert actual_resolver == adapter.capture.resolve
        if adapter_class.adapter_type.startswith("inbank_"):
            assert adapter.pod_lifecycle["create"] == "not_attempted"
    assert factory.load_chatabc_settings.call_count == (
        adapter_class.adapter_type == "inbank_chatabc"
    )
    assert factory.load_yunxia_settings.call_count == (
        adapter_class.adapter_type == "inbank_yunxia"
    )


@pytest.mark.parametrize("adapter_type", ["inbank_chatabc", "inbank_yunxia"])
@pytest.mark.parametrize("invalid_version", [False, True])
def test_caller_scope_cleans_adapter_even_on_version_error(
    monkeypatch, adapter_type, invalid_version
):
    configure_inbank(monkeypatch, adapter_type)
    adapter_class = (
        factory.InbankChatABCTargetAdapter
        if adapter_type == "inbank_chatabc"
        else factory.InbankYunxiaTargetAdapter
    )
    close = Mock()
    monkeypatch.setattr(adapter_class, "close", close)
    target = target_snapshot(adapter_type, "unsupported" if invalid_version else "1")
    if invalid_version:
        with pytest.raises(ValueError, match="adapter_version"), ExitStack() as resources:
            factory.TargetExecutionFactory.create(target, resources, trace_token="bank-task-token")
    else:
        with ExitStack() as resources:
            first, _ = factory.TargetExecutionFactory.create(target, resources, trace_token="first-task")
            close.assert_not_called()
        with ExitStack() as resources:
            second, _ = factory.TargetExecutionFactory.create(target, resources, trace_token="second-task")
            assert second is not first
    assert close.call_count == (1 if invalid_version else 2)


def test_unknown_adapter_does_not_fall_back_to_demo():
    with pytest.raises(ValueError, match="does not support"), ExitStack() as resources:
        factory.TargetExecutionFactory.create(target_snapshot("unknown"), resources)


@pytest.mark.parametrize("adapter_type", ["inbank_chatabc", "inbank_yunxia"])
@pytest.mark.parametrize("timezone", [None, "", "Asia/Shanghai", "UTC"])
def test_factory_creates_task_scoped_bank_trace_client(monkeypatch, adapter_type, timezone):
    configure_inbank(monkeypatch, adapter_type)
    if timezone is None:
        monkeypatch.delenv("AGENTGATE_TRACE_SERVER_SOURCE_TIMEZONE")
    else:
        monkeypatch.setenv("AGENTGATE_TRACE_SERVER_SOURCE_TIMEZONE", timezone)
    monkeypatch.setenv("AGENTGATE_TRACE_SERVER_TOKEN", "must-not-be-used")
    constructor = Mock(wraps=factory.TraceServerClient)
    monkeypatch.setattr(factory, "TraceServerClient", constructor)
    for token in ("task-one", "task-two"):
        with ExitStack() as resources:
            factory.TargetExecutionFactory.create(target_snapshot(adapter_type), resources, trace_token=token)
        constructor.assert_called_with("http://trace.invalid", mode="inbank", token=token,
                                       source_timezone=timezone or None)
    assert constructor.call_count == 2


@pytest.mark.parametrize("adapter_type", ["inbank_chatabc", "inbank_yunxia"])
@pytest.mark.parametrize("failure", ["token", "origin", "timezone"])
def test_bad_bank_trace_configuration_is_rejected_before_adapter_creation(monkeypatch, adapter_type, failure):
    configure_inbank(monkeypatch, adapter_type)
    monkeypatch.setenv("AGENTGATE_TRACE_SERVER_TOKEN", "must-not-be-used")
    if failure == "origin":
        monkeypatch.delenv("AGENTGATE_TRACE_SERVER_URL")
    elif failure == "timezone":
        monkeypatch.setenv("AGENTGATE_TRACE_SERVER_SOURCE_TIMEZONE", "unknown/zone")
    loader_name = "load_chatabc_settings" if adapter_type == "inbank_chatabc" else "load_yunxia_settings"
    loader = Mock(side_effect=AssertionError("must fail before constructing adapter"))
    monkeypatch.setattr(factory, loader_name, loader)
    with pytest.raises(ValueError), ExitStack() as resources:
        factory.TargetExecutionFactory.create(target_snapshot(adapter_type), resources,
                                             trace_token=None if failure == "token" else "task-token")
    loader.assert_not_called()


@pytest.mark.parametrize("adapter_type", ["demo_loan", "local_bank"])
def test_non_bank_factory_does_not_load_trace_auth_configuration(monkeypatch, adapter_type):
    # Use the demo's actual public adapter type, not its module name.
    if adapter_type == "demo_loan":
        adapter_type = factory.DemoLoanTargetAdapter.adapter_type
    monkeypatch.delenv("AGENTGATE_TRACE_SERVER_URL", raising=False)
    monkeypatch.setenv("AGENTGATE_TRACE_SERVER_SOURCE_TIMEZONE", "unknown/zone")
    constructor = Mock(side_effect=AssertionError("not an in-bank execution"))
    monkeypatch.setattr(factory, "TraceServerClient", constructor)
    with ExitStack() as resources:
        factory.TargetExecutionFactory.create(target_snapshot(adapter_type), resources)
    constructor.assert_not_called()
