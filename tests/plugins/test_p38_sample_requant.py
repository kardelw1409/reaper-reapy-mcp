"""P38 defect fix: the plugin's OWN requantization must not fail a landed sample write.

Measured 2026-10-17 on the live instance, and the reason this file exists:

* three recorded applies wrote a 44100-sample payload whose read-back matched the written
  one EXACTLY apart from the measured 4-sample reload delay (residual 0);
* a fourth apply (`…193527Z…`) moved **1060 of 44096 samples by exactly ±1 LSB** - the same
  ~2.5 % band measured on the plugin's own re-serialisation in Stage B.

The old verdict accepted the delay but required the residual to stay under 0.5 % of the
payload, so a sample the plugin itself re-encodes was reported as a FAILED write: the whole
preset apply rolled back over a difference the plugin's own save/load produces identically.
A difference is the channel's own requantization when it is one step and inside the measured
band; anything else is still a real mismatch, and these tests pin both sides.
"""

from __future__ import annotations

import base64
import struct
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parents[2] / "src" / "reaper_reapy_mcp" / "plugins"
if str(PLUGINS.parents[1]) not in sys.path:
    sys.path.insert(0, str(PLUGINS.parents[1]))

from reaper_reapy_mcp.plugins.providers.vital import codec as VC  # noqa: E402


def pcm(values: list[int]) -> str:
    return base64.b64encode(struct.pack("<%dh" % len(values), *values)).decode("ascii")


def sample_block(payload: str, *, length: int | None = None) -> dict:
    n = len(base64.b64decode(payload)) // 2
    return {
        "name": "White Noise",
        "length": length if length is not None else n,
        "sample_rate": 44100,
        "samples": payload,
    }


def requantized(values: list[int], *, stride: int) -> list[int]:
    """The plugin's truncating requantization: every `stride`-th sample drops one step."""
    return [value - 1 if index % stride == 0 else value for index, value in enumerate(values)]


def delayed(values: list[int], delay: int = VC.SAMPLE_RELOAD_DELAY) -> list[int]:
    """The measured reload delay: the read-back carries the written payload later.

    The reported shift is NEGATIVE for this case (`a = left[max(0, shift):]` against
    `b = right[max(0, -shift):]`): the recorded applies all report `shift: -4` for a
    payload that came back four samples late, and this fixture reproduces it exactly.
    """
    return [0] * delay + values[:-delay]


def row_for(written: list[int], read_back: list[int]) -> dict:
    return VC._sample_row(
        VC.SAMPLE_PATH,
        sample_block(pcm(written)),
        sample_block(pcm(read_back)),
        1e-5,
    )


# ---------------------------------------------------------------------------
# What the channel does: accepted, and named
# ---------------------------------------------------------------------------


def test_an_exactly_returned_payload_is_verified_as_exact():
    values = [(index % 501) - 250 for index in range(4096)]
    row = row_for(values, delayed(values))
    assert row["agrees"] is True
    assert row["payload_compared"]["shift"] == -VC.SAMPLE_RELOAD_DELAY
    assert row["payload_compared"]["residual_differing"] == 0
    assert "unverifiable" in row, "a shifted payload is never silently called byte-exact"


def test_a_payload_the_plugin_requantized_is_verified_and_says_so():
    """The measured case: 2.4 % of the samples come back one step lower."""
    values = [(index * 7919 % 60000) - 30000 for index in range(44100)]
    read_back = delayed(requantized(values, stride=42))  # ~2.4 % of 44100
    row = row_for(values, read_back)
    compared = row["payload_compared"]
    assert compared["shift"] == -VC.SAMPLE_RELOAD_DELAY
    assert compared["residual_max_abs"] == 1
    assert compared["residual_differing"] > 1000, "the fixture must reproduce the measured band"
    assert row["agrees"] is True
    note = row["unverifiable"]["reason"]
    assert "requantization" in note and "delayed" in note


def test_the_band_and_the_step_are_the_measured_ones():
    values = [(index % 977) - 488 for index in range(8000)]
    # inside the band (5 %) and one step: accepted
    inside = delayed(requantized(values, stride=25))
    assert row_for(values, inside)["agrees"] is True
    # twice the band: the payload is not the same material any more
    outside = delayed(requantized(values, stride=10))
    assert row_for(values, outside)["agrees"] is False


def test_a_moved_by_more_than_one_step_is_refused():
    """The step bound is what keeps the tolerance from accepting a different buffer."""
    values = [(index * 131 % 40000) - 20000 for index in range(4000)]
    bumped = [value + 3 if index % 40 == 0 else value for index, value in enumerate(values)]
    row = row_for(values, delayed(bumped))
    assert row["payload_compared"]["residual_max_abs"] == 3
    assert row["agrees"] is False


def test_a_payload_shifted_past_the_measured_delay_is_refused():
    values = [(index % 401) - 200 for index in range(4096)]
    shifted = [0] * (VC.SAMPLE_RELOAD_DELAY + 20) + values[: -(VC.SAMPLE_RELOAD_DELAY + 20)]
    row = row_for(values, shifted)
    assert row["agrees"] is False, "the delay defence must still be a defence"


def test_a_different_buffer_of_the_same_length_is_refused():
    written = [(index % 301) - 150 for index in range(4096)]
    other = [int(value * 0.5) for value in written]
    row = row_for(written, delayed(other))
    assert row["agrees"] is False


def test_the_non_payload_fields_are_still_compared_exactly():
    """The tolerance is about the payload's requantization, not about the block's fields."""
    values = [index % 128 for index in range(1024)]
    written = sample_block(pcm(values))
    read_back = sample_block(pcm(delayed(values)), length=44100)
    row = VC._sample_row(VC.SAMPLE_PATH, written, read_back, 1e-5)
    assert row["differing_fields"], "a differing length is a differing field"
    assert row["agrees"] is False


def test_a_payload_length_mismatch_is_refused():
    written = sample_block(pcm([index % 100 for index in range(2048)]))
    shorter = sample_block(pcm([index % 100 for index in range(1024)]))
    row = VC._sample_row(VC.SAMPLE_PATH, written, shorter, 1e-5)
    assert row["agrees"] is False
    assert row["payload_compared"]["reason"] == "payload_length_mismatch"
