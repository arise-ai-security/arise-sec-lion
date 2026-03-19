"""Parsed context value objects from worker XML output."""

from typing import Self
from xml.etree import ElementTree as ET

from pydantic import BaseModel


def _text(elem: ET.Element | None) -> str:
    """Extract stripped text from element, or empty string if None."""
    return (elem.text or "").strip() if elem is not None else ""


class ParsedDecision(BaseModel):
    """A decision extracted from worker output."""

    model_config = {"frozen": True}

    key: str
    value: str
    rationale: str = ""

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


class ParsedArtifact(BaseModel):
    """An artifact extracted from worker output."""

    model_config = {"frozen": True}

    key: str
    description: str = ""

    @classmethod
    def from_element(cls, elem: ET.Element) -> Self | None:
        """Parse from XML element, returns None if invalid."""
        if not (key := elem.get("key")):
            return None
        return cls(key=key, description=_text(elem))


class ParsedUpdate(BaseModel):
    """Parsed context update containing decisions and artifacts."""

    model_config = {"frozen": True}

    decisions: tuple[ParsedDecision, ...] = ()
    artifacts: tuple[ParsedArtifact, ...] = ()

    def __bool__(self) -> bool:
        """True if any decisions or artifacts were parsed."""
        return bool(self.decisions or self.artifacts)
