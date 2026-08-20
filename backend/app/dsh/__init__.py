"""DeepSeek Harness (DSH) integration package.

The official DSH SDK spawns the bundled agent runtime per research project.
The runtime loads the QuantLab domain-tools plugin (backend/dsh_runtime) which
talks to FastAPI over the HTTP bridge (app.dsh.bridge). The engine
(app.dsh.engine) drives turns, maps SDK events, and persists them.
"""

from .bridge import router as bridge_router
from .engine import (
    cancel_turn,
    get_live_session_events,
    get_session_events,
    get_status,
    run_llm_connectivity_test,
    run_turn,
    shutdown_all,
)

__all__ = [
    "bridge_router",
    "cancel_turn",
    "get_live_session_events",
    "get_session_events",
    "get_status",
    "run_llm_connectivity_test",
    "run_turn",
    "shutdown_all",
]
