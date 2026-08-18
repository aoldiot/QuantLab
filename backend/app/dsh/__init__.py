"""DeepSeek Harness (DSH) Multi-Agent Orchestration Package.

Provides agent roles (Quant Lead, Researcher, Developer, Reviewer) connected in a star topology,
session management, and QuantLab deterministic tool calling bindings.
"""

from .orchestrator import DSHOrchestrator
from .runtime import AgentEvent, DSHRuntimeManager, dsh_runtime
from .tools import DSH_TOOL_DEFINITIONS, dispatch_dsh_tool_call

__all__ = [
    "DSH_TOOL_DEFINITIONS",
    "AgentEvent",
    "DSHOrchestrator",
    "DSHRuntimeManager",
    "dispatch_dsh_tool_call",
    "dsh_runtime",
]
