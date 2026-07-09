"""Head+tail text truncation shared by verification and failure digests."""


def head_tail(text: str, limit: int) -> str:
    """Keep first 2/3 + last 1/3 of text, showing omission count."""
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n\n[...{omitted} chars omitted...]\n\n{text[-tail:]}"
