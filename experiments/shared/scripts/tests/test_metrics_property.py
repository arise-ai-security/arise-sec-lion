"""Property-style round-trip test for every concrete DomainEvent subclass.

Audit §7.5.7 minimum bar: typed_pipeline ≡ jsonl_pipeline for every concrete
DomainEvent subclass. Pre-fix, 25 of 32 subclasses fell through to "Unknown"
in the JSONL reader because the projector wrote no `event_type`
discriminator (audit N-5). Adding any new subclass that the writer doesn't
emit-with-discriminator would silently regress this property.

The test parametrizes over every concrete subclass of `DomainEvent` and
constructs a minimal instance via `model_construct` (which bypasses
validation) with default field values where Pydantic provides them. The
typed pipeline (`metrics_from_events`) and the JSONL pipeline
(`metrics_from_events_jsonl` driven by `project_events._atomic_write_events_jsonl`)
must agree.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from uuid import uuid4

import pytest

from core.domain.events.events import DomainEvent
from experiments.shared.scripts.project_events import _atomic_write_events_jsonl
from experiments.shared.scripts.run_metrics import (
    metrics_from_events,
    metrics_from_events_jsonl,
)


logger = logging.getLogger(__name__)


def _all_concrete_domain_event_subclasses() -> list[type[DomainEvent]]:
    seen: set[type[DomainEvent]] = set()
    stack: list[type[DomainEvent]] = list(DomainEvent.__subclasses__())
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        stack.extend(cls.__subclasses__())
    return sorted(seen, key=lambda c: c.__name__)


_CONCRETE = _all_concrete_domain_event_subclasses()


def _build_minimal_instance(cls: type[DomainEvent]) -> DomainEvent:
    """Construct a minimal instance bypassing validators.

    The metrics pipeline only reads a small handful of fields per event type;
    we don't need fully-validated instances. `model_construct` skips required-
    field checks and uses defaults when present.
    """
    return cls.model_construct(aggregate_id=uuid4(), sequence_number=0)


@pytest.mark.parametrize("event_cls", _CONCRETE, ids=lambda c: c.__name__)
def test_jsonl_roundtrip_matches_typed_pipeline(
    event_cls: type[DomainEvent], tmp_path: Path
) -> None:
    # Given: one minimal instance of this concrete DomainEvent subclass
    event = _build_minimal_instance(event_cls)

    # When: route it through both pipelines
    typed_metrics = metrics_from_events([event])
    jsonl_path = tmp_path / "events.jsonl"
    _atomic_write_events_jsonl(jsonl_path, [event])
    jsonl_metrics = metrics_from_events_jsonl(jsonl_path)

    # Then: the two pipelines agree on event classification + counters
    # (`tool_calls_by_type` order can vary between dict iterations across
    # Python versions; the metric itself is already sort-stable via
    # `dict(sorted(...))` in metrics_from_events).
    assert typed_metrics == jsonl_metrics, (
        f"divergence for {event_cls.__name__}: "
        f"typed={typed_metrics} jsonl={jsonl_metrics}"
    )


def test_every_concrete_subclass_is_discoverable() -> None:
    # Audit §7.5.7: the parametrize fixture must enumerate every concrete
    # subclass of DomainEvent. The current source has 32 concrete classes.
    # If this assertion fails, a refactor has either removed events or
    # changed inheritance shape — review the audit's coverage claim.
    assert len(_CONCRETE) >= 30
    for cls in _CONCRETE:
        # Sanity: each class must be concrete (no abstract methods left over).
        assert not inspect.isabstract(cls)
