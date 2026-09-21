"""Target adapters used by AgentGate execution."""

from .demo_loan import DemoLoanTargetAdapter
from .inbank import (
    ChatABCSettings,
    InbankChatABCTargetAdapter,
    InbankYunxiaTargetAdapter,
    YunxiaSettings,
)

__all__ = [
    "ChatABCSettings",
    "DemoLoanTargetAdapter",
    "InbankChatABCTargetAdapter",
    "InbankYunxiaTargetAdapter",
    "YunxiaSettings",
]
