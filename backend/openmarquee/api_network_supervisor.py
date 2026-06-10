"""P1.2-A: network supervisor API (observability surface).

GET /api/network-supervisor/state — returns the supervisor's
current state, current STA freq / AP channel, fallback flag, and
the diagnostics ring buffer (last 5 minutes). Read-only; no
mutations from this surface in P1.2-A.

P1.2-B+ will add POST endpoints for operator-driven setup-mode
re-entry, fallback-flag flip, etc.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from openmarquee.dependencies import get_network_supervisor
from openmarquee.network_supervisor import NetworkSupervisor, supervisor_to_dict

router = APIRouter(prefix="/api/network-supervisor", tags=["network-supervisor"])

# Module-level Annotated alias — matches the house style used by
# api_auth / api_flock / api_live / api_backgrounds so ruff's B008
# stays happy (avoid Depends() in argument defaults).
SupervisorDep = Annotated[NetworkSupervisor, Depends(get_network_supervisor)]


@router.get("/state")
async def get_state(supervisor: SupervisorDep) -> dict:
    """Return the supervisor's current state + diagnostics for QA
    observability during the P1.2-A observe-only soak.

    Response shape (stable):
        {
            "state": "SETUP" | "CONNECTING" | "LINGER" | "ONLINE" | "DEGRADED",
            "current_sta_freq_mhz": int | null,
            "current_ap_channel": int | null,
            "fallback_mutex_mode": bool,
            "diagnostics": [
                {"seconds_ago": float, "source": str,
                 "severity": str, "message": str},
                ...
            ]
        }
    """
    return supervisor_to_dict(supervisor)
