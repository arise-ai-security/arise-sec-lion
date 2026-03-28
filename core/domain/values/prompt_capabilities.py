"""Prompt-facing capability descriptors derived from runtime tool config."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PromptToolDescriptor:
    """Compact tool metadata safe to inject into prompts."""

    name: str
    description: str
    parameters: tuple[str, ...] = ()
    required_parameters: tuple[str, ...] = ()

    @property
    def signature(self) -> str:
        """Return a prompt-friendly function signature."""
        if not self.parameters:
            return f"{self.name}()"

        rendered_params = [
            param if param in self.required_parameters else f"{param}?"
            for param in self.parameters
        ]
        return f"{self.name}({', '.join(rendered_params)})"

    @classmethod
    def from_tool_definition(
        cls,
        tool_definition: dict[str, Any],
    ) -> "PromptToolDescriptor | None":
        """Build a prompt descriptor from an OpenAI function-calling definition."""
        function = tool_definition.get("function", {})
        name = function.get("name")
        description = function.get("description")
        parameters_block = function.get("parameters", {})
        properties = parameters_block.get("properties", {})
        required = parameters_block.get("required", ())

        if not isinstance(name, str) or not name:
            return None
        if not isinstance(description, str) or not description:
            return None
        if not isinstance(properties, dict):
            properties = {}
        if not isinstance(required, (list, tuple)):
            required = ()

        return cls(
            name=name,
            description=description,
            parameters=tuple(str(param_name) for param_name in properties),
            required_parameters=tuple(str(param_name) for param_name in required),
        )


@dataclass(frozen=True, slots=True)
class PromptCapabilities:
    """Runtime capabilities exposed to prompt rendering."""

    available_tools: tuple[PromptToolDescriptor, ...] = ()

    @property
    def has_tools(self) -> bool:
        """Whether any runtime tools should be mentioned in the prompt."""
        return bool(self.available_tools)
