"""Shared JSON-safe value aliases used across events and APIs."""

type JsonPrimitive = str | int | float | bool | None
type JsonObject = dict[str, JsonPrimitive]
