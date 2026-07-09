"""Tests for bounded worker error descriptions."""

import traceback
from collections.abc import Callable


try:
    from infrastructure.adapters.worker.shared.errors import describe_error
except ModuleNotFoundError as import_error:
    _IMPORT_ERROR = import_error

    def describe_error(error: BaseException, limit: int = 1500) -> str:
        raise AssertionError("describe_error helper is missing") from _IMPORT_ERROR


def _capture_error(function: Callable[[], None]) -> BaseException:
    try:
        function()
    except Exception as error:
        return error
    raise AssertionError("expected function to raise")


def _raise_example_error() -> None:
    raise ValueError("broken worker")


def _raise_short_error() -> None:
    raise RuntimeError("short failure")


def _raise_long_traceback() -> None:
    error: BaseException | None = None
    for index in range(40):
        try:
            if error is None:
                raise ValueError("root failure")
            raise RuntimeError(f"middle layer {index}") from error
        except Exception as caught:
            error = caught
    raise RuntimeError("outer failure") from error


def test_describe_error_includes_repr_and_raising_frame() -> None:
    """Error description includes repr and traceback frame context."""
    # Given: An exception captured with its traceback
    error = _capture_error(_raise_example_error)

    # When: Describe the error
    result = describe_error(error)

    # Then: The repr line is present
    assert "ValueError('broken worker')" in result

    # And: The traceback includes the raising frame
    assert "_raise_example_error" in result


def test_describe_error_truncates_from_front_and_preserves_innermost_frame() -> None:
    """Long tracebacks are bounded while retaining the final frame."""
    # Given: A long chained traceback and a tight limit
    error = _capture_error(_raise_long_traceback)
    limit = 350

    # When: Describe the error
    result = describe_error(error, limit=limit)

    # Then: The bounded result stays near the configured limit
    assert len(result) <= limit + 30

    # And: The exception repr remains intact as the first line
    assert result.splitlines()[0] == "RuntimeError('outer failure')"

    # And: The omitted prefix is marked explicitly
    assert "[..." in result
    assert "chars omitted...]" in result

    # And: The innermost frame survives truncation
    assert "_raise_long_traceback" in result
    assert "RuntimeError: outer failure" in result


def test_describe_error_returns_short_traceback_unmodified() -> None:
    """Short tracebacks are returned without truncation markers."""
    # Given: An exception whose full description fits the limit
    error = _capture_error(_raise_short_error)
    expected = f"{error!r}\n{''.join(traceback.format_exception(error))}"

    # When: Describe the error with room for the full traceback
    result = describe_error(error, limit=len(expected) + 1)

    # Then: The full description is unchanged
    assert result == expected

    # And: No omission marker is added
    assert "[..." not in result
