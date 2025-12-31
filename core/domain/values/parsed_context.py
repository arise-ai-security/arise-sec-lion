from dataclasses import dataclass, asdict
from typing import Any, Self
from xml.etree import ElementTree as ET

from core.domain.services.context_update_parser import _text


@dataclass(frozen=True, slots=True)
class ParsedDecision:
    """A decision extracted from worker output."""

    key: str
    value: str
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(**{k: v for k, v in data.items() if k in cls.__slots__})

    @classmethod
    def from_element(cls, elem: ET.Element) -> Self | None:
        """Parse from XML element, returns None if invalid."""
        if not (key := elem.get("key")):
            return None
        if (value_elem := elem.find("value")) is None:
            return None
        return cls(
            key=key,
            value=_text(value_elem),
            rationale=_text(elem.find("rationale")),
        )


@dataclass(frozen=True, slots=True)
class ParsedOutput:
    """An output/artifact extracted from worker output."""

    key: str
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(**{k: v for k, v in data.items() if k in cls.__slots__})

    @classmethod
    def from_element(cls, elem: ET.Element) -> Self | None:
        """Parse from XML element, returns None if invalid."""
        if not (key := elem.get("key")):
            return None
        return cls(key=key, description=_text(elem))


@dataclass(frozen=True, slots=True)
class ParsedContextUpdate:
    """Parsed context update containing decisions and outputs."""

    decisions: tuple[ParsedDecision, ...] = ()
    outputs: tuple[ParsedOutput, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "decisions": [d.to_dict() for d in self.decisions],
            "outputs": [o.to_dict() for o in self.outputs],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            decisions=tuple(ParsedDecision.from_dict(d) for d in data.get("decisions", [])),
            outputs=tuple(ParsedOutput.from_dict(o) for o in data.get("outputs", [])),
        )

    def __bool__(self) -> bool:
        """True if any decisions or outputs were parsed."""
        return bool(self.decisions or self.outputs)
