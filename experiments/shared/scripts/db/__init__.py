"""Ground-truth DB queries over the `events` table.

Three views of one event-sourced hierarchy:

  - `fetch_run_events(boss)`        →  full subtree (boss + every descendant)
  - `get_agent_trajectory(agent)`   →  one agent's events + parent's outcome tail
  - `get_parent_trace(node)`        →  full event trace along boss → ... → node

Pure transforms live in `transforms.py`; SQL/connection live in
`event_queries.py`. Tests in `tests/` exercise both.
"""

from experiments.shared.scripts.db.event_queries import (
    fetch_agent_events,
    fetch_lineage_events,
    fetch_parent_outcome_events,
    fetch_run_events,
    get_agent_trajectory,
    get_parent_trace,
    open_connection,
)
from experiments.shared.scripts.db.models import EventRow
from experiments.shared.scripts.db.transforms import (
    build_agent_trajectory,
    build_parent_trace,
    prettify_trajectory,
)


__all__ = [
    "EventRow",
    "build_agent_trajectory",
    "build_parent_trace",
    "fetch_agent_events",
    "fetch_lineage_events",
    "fetch_parent_outcome_events",
    "fetch_run_events",
    "get_agent_trajectory",
    "get_parent_trace",
    "open_connection",
    "prettify_trajectory",
]
