"""
Tests for the encode -> mux timestamp pipeline.

These tests pin down the relationship between three timelines when video
frames may be reordered (B-frames) and delayed (encoder lookahead) on the
way from raw frames to a muxed container:

- input frame time: the ``pts``/``time_base`` the user sets on each
  :class:`~av.video.frame.VideoFrame`;
- encoder time: ``CodecContext.time_base``, derived from the frame rate,
  which frames are rebased into during :meth:`~av.stream.Stream.encode`;
- container playback time: ``Stream.time_base``, which the muxer is free
  to choose only when the header is written (i.e. after encoding has
  already started), and which packets are rebased into during
  :meth:`~av.container.output.OutputContainer.mux`.

Everything is asserted in seconds so that a missing or duplicated unit
conversion anywhere in the chain shows up as a large error, while the
half-tick rounding introduced by each time base remains tolerated.
"""

from __future__ import annotations

import io
import itertools
from fractions import Fraction
from typing import Any

import pytest

import av
from av import Packet, VideoFrame
from av.rational import AVRational

WIDTH = 64
HEIGHT = 64


def solid_frame(pts: int, time_base: Fraction) -> VideoFrame:
    frame = VideoFrame(WIDTH, HEIGHT, "yuv420p")
    frame.pts = pts
    frame.time_base = time_base
    return frame


def seconds(pts: int, time_base: AVRational | Fraction) -> float:
    return pts * float(time_base)


def packet_timestamps(packet: Packet[Any]) -> tuple[int, int, AVRational]:
    """(pts, dts, time_base) of a packet, rejecting timestamp-less packets."""
    pts = packet.pts
    dts = packet.dts
    assert pts is not None and dts is not None
    return pts, dts, packet.time_base


def assert_timeline_seconds(
    actual: list[float], expected: list[float], tolerance: float
) -> None:
    """Assert two timelines match, timestamp by timestamp.

    The length check comes first: an encoder flush that fails to release
    the delayed tail frames (or a conversion that duplicates entries)
    must fail here rather than be silently truncated by ``zip``.

    Each timestamp is then compared in seconds with a tolerance, so a
    forgotten or repeated time base conversion (which errs by orders of
    magnitude) is caught, while time base quantization (bounded by half
    a tick per conversion step) is allowed.
    """
    assert len(actual) == len(expected), (
        f"timeline has {len(actual)} entries, expected {len(expected)}; "
        "tail frames lost in flush or entries duplicated?"
    )
    for i, (a, e) in enumerate(zip(actual, expected)):
        assert abs(a - e) <= tolerance, (
            f"timestamp {i}: got {a:.6f}s, expected {e:.6f}s "
            f"(tolerance {tolerance:.6f}s); "
            "error far exceeds time base quantization — "
            "missing or repeated unit conversion?"
        )


def decoded_frame_times(buffer: io.BytesIO) -> list[float]:
    """Presentation times of every frame in the file, in decode (display)
    order."""
    buffer.seek(0)
    times = []
    with av.open(buffer, "r") as input_:
        for frame in input_.decode(video=0):
            time = frame.time
            assert time is not None
            times.append(time)
    return times


class TestTimeBaseChain:
    """Frames, encoder and container all use different time bases."""

    def test_playback_timeline_matches_input_timeline(self) -> None:
        rate = 24
        frame_count = 12
        # 1/48000 is a typical acquisition time base; 2000 ticks = 1/24s.
        frame_time_base = Fraction(1, 48000)

        buffer = io.BytesIO()
        output = av.open(buffer, "w", format="mp4")
        stream = output.add_stream("mpeg4", rate)
        stream.width = WIDTH
        stream.height = HEIGHT
        stream.pix_fmt = "yuv420p"

        # The stream has no time base until the muxer writes the header.
        assert not stream.time_base

        encoded = []
        for i in range(frame_count):
            frame = solid_frame(i * 2000, frame_time_base)
            for packet in stream.encode(frame):
                # Record now: mux() rebases the packet to the stream time
                # base in place, so the encoder-side view must be captured
                # first.
                encoded.append(packet_timestamps(packet))
                output.mux(packet)

        # Writing the header let the muxer choose the stream time base;
        # it differs from both the frame and the encoder time bases.
        encoder_time_base = Fraction(1, rate)
        muxer_time_base = stream.time_base
        assert muxer_time_base
        assert muxer_time_base != frame_time_base
        assert muxer_time_base != encoder_time_base

        for packet in stream.encode(None):
            encoded.append(packet_timestamps(packet))
            output.mux(packet)
        output.close()

        # Encoder side: frames were rebased into the encoder time base
        # (1/rate); 2000/48000 = 1/24 exactly, so pts are exact integers.
        assert len(encoded) == frame_count
        for i, (pts, dts, time_base) in enumerate(encoded):
            assert time_base == encoder_time_base
            assert pts == i
            assert dts == i  # no B-frames: decode order == display order

        # Container side: only the unit changed. In seconds the packets
        # must sit exactly on the original 1/24s grid.
        buffer.seek(0)
        with av.open(buffer, "r") as input_:
            in_stream = input_.streams.video[0]
            assert in_stream.time_base == muxer_time_base
            demuxed = [
                packet_timestamps(p)
                for p in input_.demux(in_stream)
                if p.pts is not None
            ]

        tolerance = 0.5 * float(encoder_time_base) + 0.5 * float(muxer_time_base)
        assert_timeline_seconds(
            [seconds(pts, time_base) for pts, _, time_base in demuxed],
            [i / rate for i in range(frame_count)],
            tolerance,
        )

        # Packet durations undergo the same pure unit conversion.
        buffer.seek(0)
        with av.open(buffer, "r") as input_:
            durations = []
            for packet in input_.demux(video=0):
                duration = packet.duration
                if duration is None or packet.pts is None:
                    continue
                durations.append(seconds(duration, packet.time_base))
        assert_timeline_seconds(durations, [1 / rate] * frame_count, tolerance)

        # Decoded frames play at the original times.
        assert_timeline_seconds(
            decoded_frame_times(buffer),
            [i / rate for i in range(frame_count)],
            tolerance,
        )

    def test_time_base_quantization_error_stays_bounded(self) -> None:
        # 30fps expressed in a millisecond frame time base: 1000/30 is not
        # an integer, so rebasing into the encoder time base must round.
        rate = 30
        frame_count = 10
        frame_time_base = Fraction(1, 1000)
        frame_pts = [round(i * 1000 / rate) for i in range(frame_count)]
        # The scenario is only meaningful if rounding actually occurs.
        assert any(pts / 1000 != i / rate for i, pts in enumerate(frame_pts))

        buffer = io.BytesIO()
        output = av.open(buffer, "w", format="mp4")
        stream = output.add_stream("h264", rate)
        stream.width = WIDTH
        stream.height = HEIGHT
        stream.pix_fmt = "yuv420p"
        stream.cctx.max_b_frames = 0

        encoded = []
        for pts in frame_pts:
            for packet in stream.encode(solid_frame(pts, frame_time_base)):
                encoded.append(packet_timestamps(packet))
                output.mux(packet)
        for packet in stream.encode(None):
            encoded.append(packet_timestamps(packet))
            output.mux(packet)
        muxer_time_base = stream.time_base
        output.close()

        # Quantization is bounded by half a tick per rebasing step; a
        # missing or repeated conversion errs by orders of magnitude more.
        tolerance = 0.5 / rate + 0.5 * float(muxer_time_base) + 1e-9
        expected = [pts / 1000 for pts in frame_pts]

        assert_timeline_seconds(
            [seconds(pts, time_base) for pts, _, time_base in encoded],
            expected,
            tolerance,
        )
        assert_timeline_seconds(decoded_frame_times(buffer), expected, tolerance)


class TestReorderedFrames:
    """B-frames and encoder delay: display order != decode order."""

    def test_decode_order_and_display_order_are_consistent(self) -> None:
        rate = 24
        frame_count = 16
        frame_time_base = Fraction(1, 48000)

        buffer = io.BytesIO()
        output = av.open(buffer, "w", format="mp4")
        stream = output.add_stream("h264", rate)
        stream.width = WIDTH
        stream.height = HEIGHT
        stream.pix_fmt = "yuv420p"
        stream.cctx.max_b_frames = 2

        encoded = []
        for i in range(frame_count):
            for packet in stream.encode(solid_frame(i * 2000, frame_time_base)):
                encoded.append(packet_timestamps(packet))
                output.mux(packet)
        tail = []
        for packet in stream.encode(None):
            tail.append(packet_timestamps(packet))
            output.mux(packet)
        output.close()

        packets = encoded + tail

        # Flushing released the delayed tail frames: every input frame
        # produced exactly one packet.
        assert len(tail) > 0, "encoder held no delayed frames to flush"
        assert len(packets) == frame_count, (
            f"{frame_count} frames encoded but {len(packets)} packets came out; "
            "tail frames lost in flush?"
        )

        pts_sequence = [pts for pts, _, _ in packets]
        dts_sequence = [dts for _, dts, _ in packets]

        # The scenario must actually contain reordering, otherwise the
        # decode-order assertions below are vacuous.
        assert pts_sequence != sorted(pts_sequence), (
            "encoder did not reorder frames; test is not exercising B-frames"
        )

        # Decode order (the order packets are emitted and stored): dts is
        # strictly increasing. The leading dts may legitimately be negative
        # once B-frame reordering shifts the decode timeline before zero.
        assert all(
            earlier < later for earlier, later in itertools.pairwise(dts_sequence)
        ), "dts must be monotonic in decode order"
        assert dts_sequence[0] <= 0

        # Display order: the pts values are exactly the input timeline,
        # permuted. Sorting them must restore every input frame's slot —
        # no gaps, no duplicates, no missing tail frames. Note we do not
        # require pts to be monotonic in packet order: with B-frames it
        # must not be, and monotonic pts is not a substitute for correct
        # dts.
        assert sorted(pts_sequence) == list(range(frame_count))

        # A frame cannot be displayed before it is decoded.
        for pts, dts, _ in packets:
            assert pts >= dts

        # The muxed file preserves both orders and the playback timeline.
        buffer.seek(0)
        with av.open(buffer, "r") as input_:
            in_stream = input_.streams.video[0]
            in_stream_time_base = in_stream.time_base
            demuxed = [
                packet_timestamps(p)
                for p in input_.demux(in_stream)
                if p.pts is not None
            ]

        assert len(demuxed) == frame_count

        demux_dts = [dts for _, dts, _ in demuxed]
        assert all(
            earlier < later for earlier, later in itertools.pairwise(demux_dts)
        ), "demuxed dts must be monotonic in storage order"
        # Negative leading dts in the container is legal (mp4 shifts the
        # presentation timeline with an edit list); do not demand dts >= 0.
        assert demux_dts[0] <= 0
        for pts, dts, _ in demuxed:
            assert pts >= dts

        tolerance = 0.5 / rate + 0.5 * float(in_stream_time_base) + 1e-9
        assert_timeline_seconds(
            sorted(seconds(pts, time_base) for pts, _, time_base in demuxed),
            [i / rate for i in range(frame_count)],
            tolerance,
        )

        # Packet durations: a pure unit conversion of 1/rate seconds.
        buffer.seek(0)
        durations = []
        with av.open(buffer, "r") as input_:
            for packet in input_.demux(video=0):
                duration = packet.duration
                if duration is None or packet.pts is None:
                    continue
                durations.append(seconds(duration, packet.time_base))
        assert_timeline_seconds(durations, [1 / rate] * frame_count, tolerance)

        # Decoding yields every frame exactly once, in display order, at
        # the original playback times.
        assert_timeline_seconds(
            decoded_frame_times(buffer),
            [i / rate for i in range(frame_count)],
            tolerance,
        )


class TestTimelineAssertion:
    """The assertion helper itself: strict enough to catch conversion and
    flush bugs, lenient enough for time base quantization."""

    def test_detects_double_conversion(self) -> None:
        expected = [i / 24 for i in range(8)]
        tolerance = 0.5 / 24

        # Timestamps scaled by an extra (or missing) factor land far
        # outside the quantization tolerance.
        with pytest.raises(AssertionError, match="unit conversion"):
            assert_timeline_seconds([t * 2 for t in expected], expected, tolerance)
        with pytest.raises(AssertionError, match="unit conversion"):
            assert_timeline_seconds([t / 2 for t in expected], expected, tolerance)

    def test_detects_missing_tail_frames(self) -> None:
        expected = [i / 24 for i in range(8)]
        with pytest.raises(AssertionError, match="tail frames"):
            assert_timeline_seconds(expected[:-1], expected, 0.5 / 24)
        with pytest.raises(AssertionError, match="tail frames"):
            assert_timeline_seconds(expected + [expected[-1]], expected, 0.5 / 24)

    def test_accepts_quantization_noise(self) -> None:
        expected = [i / 24 for i in range(8)]
        tolerance = 0.5 / 24
        # Errors of up to half a tick pass...
        quantized = [t + 0.49 * tolerance for t in expected]
        assert_timeline_seconds(quantized, expected, tolerance)
        # ...and just beyond the bound they fail.
        quantized = [t + 1.01 * tolerance for t in expected]
        with pytest.raises(AssertionError):
            assert_timeline_seconds(quantized, expected, tolerance)
