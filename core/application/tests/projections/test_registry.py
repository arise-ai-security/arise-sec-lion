"""Tests for the projection registry system."""

import pytest

from core.application.projections.registry import (
    ProjectionRegistry,
    RegistryError,
    register_filter,
    register_formatter,
    register_projection,
    register_sink,
)


class TestProjectionRegistry:
    """Tests for ProjectionRegistry."""

    def setup_method(self) -> None:
        """Save registry state before each test."""
        # Save current state
        self._saved_filters = dict(ProjectionRegistry._filters)
        self._saved_projections = dict(ProjectionRegistry._projections)
        self._saved_formatters = dict(ProjectionRegistry._formatters)
        self._saved_sinks = dict(ProjectionRegistry._sinks)
        # Clear for isolated testing
        ProjectionRegistry.clear()

    def teardown_method(self) -> None:
        """Restore registry state after each test."""
        ProjectionRegistry._filters = self._saved_filters
        ProjectionRegistry._projections = self._saved_projections
        ProjectionRegistry._formatters = self._saved_formatters
        ProjectionRegistry._sinks = self._saved_sinks

    def test_register_and_get_filter(self) -> None:
        """Should register and retrieve a filter."""

        @ProjectionRegistry.register_filter("test_filter")
        class TestFilter:
            pass

        result = ProjectionRegistry.get_filter("test_filter")
        assert result is TestFilter

    def test_register_and_get_projection(self) -> None:
        """Should register and retrieve a projection."""

        @ProjectionRegistry.register_projection("test_projection")
        class TestProjection:
            pass

        result = ProjectionRegistry.get_projection("test_projection")
        assert result is TestProjection

    def test_register_and_get_formatter(self) -> None:
        """Should register and retrieve a formatter."""

        @ProjectionRegistry.register_formatter("test_formatter")
        class TestFormatter:
            pass

        result = ProjectionRegistry.get_formatter("test_formatter")
        assert result is TestFormatter

    def test_register_and_get_sink(self) -> None:
        """Should register and retrieve a sink."""

        @ProjectionRegistry.register_sink("test_sink")
        class TestSink:
            pass

        result = ProjectionRegistry.get_sink("test_sink")
        assert result is TestSink

    def test_duplicate_registration_raises_error(self) -> None:
        """Should raise RegistryError on duplicate registration."""

        @ProjectionRegistry.register_filter("duplicate")
        class FirstFilter:
            pass

        with pytest.raises(RegistryError) as exc_info:

            @ProjectionRegistry.register_filter("duplicate")
            class SecondFilter:
                pass

        assert "already registered" in str(exc_info.value)

    def test_get_unknown_filter_raises_error(self) -> None:
        """Should raise RegistryError for unknown filter."""
        with pytest.raises(RegistryError) as exc_info:
            ProjectionRegistry.get_filter("nonexistent")

        assert "Unknown filter" in str(exc_info.value)
        assert "nonexistent" in str(exc_info.value)

    def test_get_unknown_projection_raises_error(self) -> None:
        """Should raise RegistryError for unknown projection."""
        with pytest.raises(RegistryError) as exc_info:
            ProjectionRegistry.get_projection("nonexistent")

        assert "Unknown projection" in str(exc_info.value)

    def test_get_unknown_formatter_raises_error(self) -> None:
        """Should raise RegistryError for unknown formatter."""
        with pytest.raises(RegistryError) as exc_info:
            ProjectionRegistry.get_formatter("nonexistent")

        assert "Unknown formatter" in str(exc_info.value)

    def test_get_unknown_sink_raises_error(self) -> None:
        """Should raise RegistryError for unknown sink."""
        with pytest.raises(RegistryError) as exc_info:
            ProjectionRegistry.get_sink("nonexistent")

        assert "Unknown sink" in str(exc_info.value)

    def test_list_filters(self) -> None:
        """Should list all registered filters."""

        @ProjectionRegistry.register_filter("filter_a")
        class FilterA:
            pass

        @ProjectionRegistry.register_filter("filter_b")
        class FilterB:
            pass

        filters = ProjectionRegistry.list_filters()
        assert filters == ["filter_a", "filter_b"]

    def test_list_formatters(self) -> None:
        """Should list all registered formatters."""

        @ProjectionRegistry.register_formatter("fmt_x")
        class FormatX:
            pass

        formatters = ProjectionRegistry.list_formatters()
        assert "fmt_x" in formatters

    def test_clear_removes_all_registrations(self) -> None:
        """Clear should remove all registrations."""

        @ProjectionRegistry.register_filter("to_clear")
        class ToClear:
            pass

        ProjectionRegistry.clear()

        with pytest.raises(RegistryError):
            ProjectionRegistry.get_filter("to_clear")


class TestModuleLevelDecorators:
    """Tests for convenience decorator aliases."""

    def setup_method(self) -> None:
        """Save registry state before each test."""
        self._saved_filters = dict(ProjectionRegistry._filters)
        self._saved_projections = dict(ProjectionRegistry._projections)
        self._saved_formatters = dict(ProjectionRegistry._formatters)
        self._saved_sinks = dict(ProjectionRegistry._sinks)
        ProjectionRegistry.clear()

    def teardown_method(self) -> None:
        """Restore registry state after each test."""
        ProjectionRegistry._filters = self._saved_filters
        ProjectionRegistry._projections = self._saved_projections
        ProjectionRegistry._formatters = self._saved_formatters
        ProjectionRegistry._sinks = self._saved_sinks

    def test_register_filter_decorator(self) -> None:
        """Module-level register_filter should work."""

        @register_filter("mod_filter")
        class ModFilter:
            pass

        result = ProjectionRegistry.get_filter("mod_filter")
        assert result is ModFilter

    def test_register_projection_decorator(self) -> None:
        """Module-level register_projection should work."""

        @register_projection("mod_proj")
        class ModProjection:
            pass

        result = ProjectionRegistry.get_projection("mod_proj")
        assert result is ModProjection

    def test_register_formatter_decorator(self) -> None:
        """Module-level register_formatter should work."""

        @register_formatter("mod_fmt")
        class ModFormatter:
            pass

        result = ProjectionRegistry.get_formatter("mod_fmt")
        assert result is ModFormatter

    def test_register_sink_decorator(self) -> None:
        """Module-level register_sink should work."""

        @register_sink("mod_sink")
        class ModSink:
            pass

        result = ProjectionRegistry.get_sink("mod_sink")
        assert result is ModSink
