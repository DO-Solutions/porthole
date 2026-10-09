"""HTTP routes of the head, one module per area; ROUTERS is the list main.py includes in order.

Pages and public reads need nothing; every mutating route depends on security.captain."""
from __future__ import annotations

from porthole.routes import api_config, api_fleet, api_insights, api_trace, api_traces, events, health, pages

ROUTERS = [health.router, pages.router, api_config.router, api_fleet.router, api_insights.router,
           api_trace.router, api_traces.router, events.router]
