import re
import xml.etree.ElementTree as ET

from core.domain.values.parsed_context import ParsedArtifact, ParsedDecision, ParsedUpdate

# Regex pattern for extracting context-update block
_CONTEXT_UPDATE_PATTERN = re.compile(
    r"<context-update>(.*?)</context-update>",
    re.DOTALL | re.IGNORECASE,
)


def _text(elem: ET.Element | None) -> str:
    """Extract stripped text from element, or empty string if None."""
    return (elem.text or "").strip() if elem is not None else ""


def parse_context_update(result: str) -> ParsedUpdate | None:
    """Parse <context-update> XML section from worker result.

    Returns ParsedUpdate if valid content found, None otherwise.
    """
    if not (match := _CONTEXT_UPDATE_PATTERN.search(result)):
        return None

    try:
        root = ET.fromstring(f"<root>{match.group(1)}</root>")
    except ET.ParseError:
        return None

    decisions = tuple(filter(None, (ParsedDecision.from_element(e) for e in root.findall("decision"))))
    artifacts = tuple(filter(None, (ParsedArtifact.from_element(e) for e in root.findall("output"))))

    return ParsedUpdate(decisions=decisions, artifacts=artifacts) or None
