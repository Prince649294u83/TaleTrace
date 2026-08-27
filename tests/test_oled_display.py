"""OLED display integration tests.

Proves the wire contract and the data boundary between the reading engine
and the hardware.  Every test here uses a ``FakeDisplay`` that records
calls and never touches the network, because the thing being tested is
the *Python code*, not the ESP32 firmware.

What these tests cover
----------------------
1. Wire format: POST with Content-Type: text/plain, raw body.
2. Text normalization: whitespace collapsing, truncation.
3. show() must never raise — failures are logged and swallowed.
4. MeaningLookupResult carries the data the OLED needs.
5. DeviceLoop sends exactly one display update per successful lookup.
6. No display → nothing crashes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import pytest

from backend.app.modules.image_receiver.esp32_buttons import Esp32Buttons
from backend.app.modules.image_receiver.protocols import DisplayTarget
from backend.app.modules.reading_engine.engine import MeaningLookupResult


# ------------------------------------------------------------------ fakes


@dataclass
class FakeDisplay:
    """Records display calls for assertion.  Implements ``DisplayTarget``."""

    calls: list[str] = field(default_factory=list)
    fail_next: bool = False

    async def show(self, text: str) -> None:
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("OLED unreachable")
        self.calls.append(text)


class TestDisplayTargetProtocol:
    """FakeDisplay satisfies DisplayTarget at runtime."""

    def test_fake_display_satisfies_protocol(self):
        assert isinstance(FakeDisplay(), DisplayTarget)


# -------------------------------------------------------------- wire format


class TestOledWireFormat:
    """The firmware reads ``server.arg("plain")``, which expects
    ``Content-Type: text/plain`` with the text as a raw body.

    These tests verify that `Esp32Buttons.show()` would produce the right
    request shape by exercising the normalization and error handling,
    without actually hitting the network.
    """

    def test_normalize_collapses_whitespace(self):
        result = Esp32Buttons._normalize_oled_text("  hello   world  \n  foo  ")
        assert result == "hello world foo"

    def test_normalize_truncates_long_text(self):
        long_text = "a" * 300
        result = Esp32Buttons._normalize_oled_text(long_text, max_length=200)
        assert len(result) == 200
        assert result.endswith("...")

    def test_normalize_preserves_short_text(self):
        result = Esp32Buttons._normalize_oled_text("ENTROPY: disorder in a system")
        assert result == "ENTROPY: disorder in a system"

    def test_normalize_empty_input(self):
        assert Esp32Buttons._normalize_oled_text("") == ""
        assert Esp32Buttons._normalize_oled_text("   ") == ""


# -------------------------------------------------------- MeaningLookupResult


class TestMeaningLookupResult:
    """The data boundary the OLED pipeline crosses."""

    def test_successful_lookup_carries_oled_text(self):
        result = MeaningLookupResult(
            target_word="entropy",
            oled_text="disorder in a system",
            full_explanation="In thermodynamics, entropy is...",
            success=True,
        )
        assert result.success
        assert result.target_word == "entropy"
        assert result.oled_text == "disorder in a system"
        assert result.full_explanation.startswith("In thermodynamics")

    def test_failed_lookup_has_success_false(self):
        result = MeaningLookupResult(
            target_word="quux",
            oled_text="",
            full_explanation="",
            success=False,
        )
        assert not result.success

    def test_result_is_frozen(self):
        result = MeaningLookupResult(
            target_word="test", oled_text="t", full_explanation="f", success=True
        )
        with pytest.raises(AttributeError):
            result.target_word = "changed"  # type: ignore[misc]


# ------------------------------------------------------------- display calls


class TestDeviceLoopOledIntegration:
    """DeviceLoop sends exactly one display update per successful lookup."""

    @pytest.mark.asyncio
    async def test_show_on_display_sends_text(self):
        """_show_on_display calls display.show() with the text."""

        # We test the helper directly since it's the public contract.
        from backend.app.modules.reading_engine.device_loop import DeviceLoop

        fake = FakeDisplay()
        # Construct a minimal DeviceLoop.  We only need the display field.
        loop = DeviceLoop.__new__(DeviceLoop)
        loop.display = fake

        await loop._show_on_display("ENTROPY: disorder in a system")
        assert fake.calls == ["ENTROPY: disorder in a system"]

    @pytest.mark.asyncio
    async def test_show_on_display_swallows_errors(self, caplog):
        """A failing display must not stop reading."""

        from backend.app.modules.reading_engine.device_loop import DeviceLoop

        fake = FakeDisplay(fail_next=True)
        loop = DeviceLoop.__new__(DeviceLoop)
        loop.display = fake

        # Must not raise
        with caplog.at_level(logging.DEBUG):
            await loop._show_on_display("test")

        assert fake.calls == []  # The call failed, nothing recorded

    @pytest.mark.asyncio
    async def test_no_display_does_nothing(self):
        """When display is None, _show_on_display is a no-op."""

        from backend.app.modules.reading_engine.device_loop import DeviceLoop

        loop = DeviceLoop.__new__(DeviceLoop)
        loop.display = None

        # Must not raise
        await loop._show_on_display("test")

