from pathlib import Path

from agentgate.integrations.targets import (
    ChatABCSettings,
    DemoLoanTargetAdapter,
    InbankChatABCTargetAdapter,
    InbankYunxiaTargetAdapter,
    YunxiaSettings,
)


def test_removed_monolithic_modules_are_not_referenced():
    root = Path(__file__).parents[1] / "src" / "agentgate"
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in root.rglob("*.py")
    )
    assert "agentgate.contracts" not in sources
    assert "from agentgate.case " not in sources
    assert "from agentgate.case." not in sources
    assert "import agentgate.case" not in sources
    assert "agentgate.evaluator.core" not in sources


def test_target_package_exports_existing_and_inbank_adapters():
    assert DemoLoanTargetAdapter.adapter_type == "demo_loan"
    assert InbankChatABCTargetAdapter.adapter_type == "inbank_chatabc"
    assert InbankYunxiaTargetAdapter.adapter_type == "inbank_yunxia"
    assert ChatABCSettings.__name__ == "ChatABCSettings"
    assert YunxiaSettings.__name__ == "YunxiaSettings"
