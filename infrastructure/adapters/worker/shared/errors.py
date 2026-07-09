"""Bounded error descriptions for worker failure events."""

import traceback


def describe_error(error: BaseException, limit: int = 1500) -> str:
    repr_line = f"{error!r}"
    traceback_text = "".join(traceback.format_exception(error))
    combined = f"{repr_line}\n{traceback_text}"
    if len(combined) <= limit:
        return combined

    prefix = f"{repr_line}\n"
    marker = "[...0 chars omitted...]\n"
    available = max(0, limit - len(prefix) - len(marker))
    while True:
        omitted = max(0, len(traceback_text) - available)
        marker = f"[...{omitted} chars omitted...]\n"
        next_available = max(0, limit - len(prefix) - len(marker))
        if next_available == available:
            break
        available = next_available

    traceback_tail = traceback_text[-available:] if available else ""
    return f"{prefix}{marker}{traceback_tail}"
