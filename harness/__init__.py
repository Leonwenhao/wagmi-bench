# SPDX-License-Identifier: Apache-2.0
"""Agent adapters used by the WAGMI Bench episode loop."""

from harness.http import (
    HTTPAgent,
    HTTPAgentConfigurationError,
    HTTPAgentError,
)
from harness.protocol import (
    AgentReply,
    DecisionTimeout,
    HarnessEvent,
    HarnessEventSource,
    InProcessAgent,
)
from harness.scripted import MomentumAgent, ScriptedFixtureAgent

__all__ = [
    "AgentReply",
    "DecisionTimeout",
    "HTTPAgent",
    "HTTPAgentConfigurationError",
    "HTTPAgentError",
    "HarnessEvent",
    "HarnessEventSource",
    "InProcessAgent",
    "MomentumAgent",
    "ScriptedFixtureAgent",
]
