"""Timing checks for the resample -> FIFO -> fixed-frame encoder chain.

The tests protect the *effective audio duration* of the chain:

* sample rate conversion must not shorten or lengthen the signal beyond the
  rounding of ``samples_in * out_rate / in_rate``; the number of samples does
  not depend on how the input happens to be chunked;
* a fixed-frame-size encoder is fed through :class:`av.AudioFifo`, so the final
  short frame survives as a partial frame with a truthful duration rather than
  being padded into the audio (encoder padding is accounted separately);
* each draining stage (resampler flush, encoder flush) is counted once.

All assertions are deterministic: the material is a fixed prefix of the local
test asset plus a generated ramp, and no wall clock or random state is used.
"""

from __future__ import annotations

import io
import itertools
import math
from collections.abc import Iterator, Sequence
from fractions import Fraction
from typing import Any

import numpy as np
import pytest

import av
from av import AudioFrame, AudioResampler
from av.audio.stream import AudioStream
from av.codec.codec import UnknownCodecError
from av.container import OutputContainer

from .common import fate_suite

# Prefix length (per channel) taken from the local FATE asset. Big enough for
# several 1024 sample frames, small enough to keep the tests quick.
MATERIAL_SAMPLES = 12345
MATERIAL_RATE = 44100

# Chunkings of the same material. They share the total number of samples but
# differ in where the input frame boundaries fall, including 1 sample frames.
CHUNK_PATTERNS: tuple[tuple[int, ...], ...] = (
    (4096,),
    (1000, 1, 2047, 3),
    (7, 13, 997, 5, 1023, 2051),
)

# A small, frame sized FIFO chunk used to feed fixed-frame-size encoders.
FIFO_FRAME_SIZE = 1024


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _require_encoder(name: str) -> None:
    try:
        av.codec.Codec(name, "w")
    except UnknownCodecError:
        pytest.skip(f"{name} encoder is not available")


def _require_format(name: str) -> None:
    if name not in av.formats_available:
        pytest.skip(f"{name} muxer is not available")


def _ramp(size: int) -> np.ndarray:
    """A deterministic signal whose value depends only on the sample index."""
    return (((np.arange(size, dtype=np.int64) * 37) % 2001) - 1000).astype(np.int16)


def _plane_samples(frame: AudioFrame) -> np.ndarray:
    """All samples of a packed frame, in interleaved order.

    ``to_ndarray`` is used instead of reading the plane buffer directly because
    frames coming out of a filter graph may have padding past ``nb_samples``.
    The tests stick to packed ``s16`` frames, so the rows hold the interleaved
    channels.
    """
    return np.ascontiguousarray(frame.to_ndarray().reshape(-1))


def _iter_frames(
    signal: np.ndarray,
    rate: int,
    layout: str,
    channels: int,
    pattern: Sequence[int],
    *,
    with_pts: bool = True,
) -> Iterator[AudioFrame]:
    """Split an interleaved ``signal`` into frames according to ``pattern``."""
    total = signal.size // channels
    position = 0
    index = 0
    while position < total:
        samples = min(pattern[index % len(pattern)], total - position)
        index += 1

        frame = AudioFrame(format="s16", layout=layout, samples=samples)
        frame.sample_rate = rate
        frame.time_base = Fraction(1, rate)
        if with_pts:
            frame.pts = position
        frame.planes[0].update(
            np.ascontiguousarray(
                signal[position * channels : (position + samples) * channels]
            ).tobytes()
        )
        yield frame
        position += samples


def _resample(
    signal: np.ndarray,
    in_rate: int,
    out_rate: int,
    layout: str = "mono",
    channels: int = 1,
    frame_size: int = 0,
    pattern: Sequence[int] = (4096,),
    *,
    with_pts: bool = True,
) -> tuple[list[AudioFrame], list[AudioFrame], list[AudioFrame]]:
    """Return ``(streamed, flushed, second_flush)`` output frames."""
    resampler = AudioResampler(
        "s16", layout, out_rate, frame_size if frame_size else None
    )
    streamed: list[AudioFrame] = []
    for frame in _iter_frames(
        signal, in_rate, layout, channels, pattern, with_pts=with_pts
    ):
        streamed.extend(resampler.resample(frame))

    flushed = resampler.resample(None)
    # A second flush must be empty; otherwise the tail would be emitted twice.
    second_flush = resampler.resample(None)
    return streamed, flushed, second_flush


def _total(frames: Sequence[AudioFrame]) -> int:
    return sum(frame.samples for frame in frames)


def _assert_contiguous(frames: Sequence[AudioFrame]) -> None:
    for previous, current in itertools.pairwise(frames):
        assert previous.pts is not None
        assert current.pts is not None
        assert previous.pts + previous.samples == current.pts


def _configure_encoder(
    output: OutputContainer, codec: str, rate: int, layout: str, format: str
) -> AudioStream:
    stream = output.add_stream(codec, rate=rate, layout=layout)
    assert isinstance(stream, AudioStream)
    stream.cctx.format = av.AudioFormat(format)
    return stream


def _mux_frames(
    stream: AudioStream, frames: Sequence[AudioFrame], output: OutputContainer
) -> list[av.Packet[AudioStream]]:
    packets: list[av.Packet[AudioStream]] = []
    for frame in frames:
        for packet in stream.encode(frame):
            output.mux(packet)
            packets.append(packet)
    return packets


def _data_packets(
    packets: Sequence[av.Packet[Any]],
) -> list[av.Packet[Any]]:
    """Packets carrying samples, dropping end-of-stream marker packets."""
    return [packet for packet in packets if packet.duration]


def _packet_span(packets: Sequence[av.Packet[Any]]) -> int:
    """Samples in ``[first presentation time, end of last packet)``."""
    data = _data_packets(packets)
    assert data
    assert data[0].pts is not None
    assert data[-1].pts is not None
    return data[-1].pts + (data[-1].duration or 0) - data[0].pts


def _assert_packets_contiguous(packets: Sequence[av.Packet[Any]]) -> None:
    """Consecutive packets abut, so no sample is covered twice or skipped."""
    data = _data_packets(packets)
    for previous, current in itertools.pairwise(data):
        assert previous.pts is not None
        assert current.pts is not None
        assert previous.pts + (previous.duration or 0) == current.pts


def _load_local_asset() -> tuple[np.ndarray, int, str, int]:
    """Load a fixed prefix of the local WAV, keeping its channel layout."""
    path = fate_suite("audio-reference/chorusnoise_2ch_44kHz_s16.wav")
    with av.open(path) as container:
        stream = container.streams.audio[0]
        rate = stream.sample_rate
        layout = stream.cctx.layout.name
        channels = stream.cctx.layout.nb_channels
        chunks = [frame.to_ndarray() for frame in container.decode(stream)]

    packed = np.concatenate(chunks, axis=1)
    signal = np.ascontiguousarray(packed[:, : MATERIAL_SAMPLES * channels].reshape(-1))
    return signal, rate, layout, channels


# --------------------------------------------------------------------------
# Sample rate conversion: duration and continuity
# --------------------------------------------------------------------------


class TestResamplerDuration:
    @pytest.mark.parametrize("out_rate", [48000, 22050, 8000])
    @pytest.mark.parametrize("frame_size", [0, FIFO_FRAME_SIZE])
    def test_duration_is_independent_of_chunking(
        self, out_rate: int, frame_size: int
    ) -> None:
        """Chunking changes frame boundaries, not the amount of audio.

        ``libswresample`` rounds the resampled position of every output sample
        independently of the input frame boundaries, so the final count is the
        rounding of ``samples_in * out_rate / in_rate`` and does not move with
        the chunking. The content may differ in the last place from filter
        rounding, so it is compared with a 1 LSB tolerance; the lengths must be
        identical.
        """
        exact = Fraction(MATERIAL_SAMPLES * out_rate, MATERIAL_RATE)
        signal = _ramp(MATERIAL_SAMPLES)

        totals: set[int] = set()
        rendered: list[np.ndarray] = []
        for pattern in CHUNK_PATTERNS:
            streamed, flushed, again = _resample(
                signal,
                MATERIAL_RATE,
                out_rate,
                frame_size=frame_size,
                pattern=pattern,
            )
            frames = streamed + flushed

            assert again == []
            total = _total(frames)
            totals.add(total)
            assert math.floor(exact) <= total <= math.ceil(exact), (
                f"{total} samples for {MATERIAL_SAMPLES} input samples "
                f"({float(exact):.3f} expected)"
            )

            # The output timeline is contiguous and starts at zero: the
            # converter neither duplicates nor drops a sample boundary.
            assert frames[0].pts == 0
            assert frames[0].time_base == Fraction(1, out_rate)
            _assert_contiguous(frames)

            if frame_size:
                assert all(frame.samples == frame_size for frame in streamed)
            rendered.append(np.concatenate([_plane_samples(frame) for frame in frames]))

        assert len(totals) == 1, f"chunking changed the duration: {totals}"

        reference = rendered[0]
        for other in rendered[1:]:
            assert other.shape == reference.shape
            difference = other.astype(np.int32) - reference.astype(np.int32)
            assert np.abs(difference).max() <= 1

    @pytest.mark.parametrize("out_rate", [48000, 8000])
    def test_flush_releases_the_resampler_tail_once(self, out_rate: int) -> None:
        """The converter holds samples back until it is flushed.

        With a fixed frame size the withheld tail is exactly the remainder of
        the full output divided into frames. Without one, the tail is the
        filter's own look-ahead, which is only a few samples. In both cases the
        flush releases it once, so the total equals the unframed total and no
        sample is emitted twice.
        """
        signal = _ramp(MATERIAL_SAMPLES)

        streamed, flushed, again = _resample(
            signal,
            MATERIAL_RATE,
            out_rate,
            frame_size=FIFO_FRAME_SIZE,
            pattern=(1000, 1, 2047, 3),
        )
        assert streamed, "the converter should emit full frames before the flush"
        assert all(frame.samples == FIFO_FRAME_SIZE for frame in streamed)
        total = _total(streamed) + _total(flushed)
        # The framed output is the full duration cut into fixed frames, so the
        # tail is the remainder and nothing else.
        assert _total(flushed) == total % FIFO_FRAME_SIZE
        # Flushing twice must not duplicate the tail.
        assert again == []

        # Without a requested frame size only the filter delay is withheld; it
        # is much shorter than a frame and does not depend on chunking.
        streamed_unframed, flushed_unframed, again_unframed = _resample(
            signal,
            MATERIAL_RATE,
            out_rate,
            pattern=(7, 13, 997, 5, 1023, 2051),
        )
        assert again_unframed == []
        assert 0 < _total(flushed_unframed) < FIFO_FRAME_SIZE
        assert _total(streamed_unframed) + _total(flushed_unframed) == total

    def test_pts_are_contiguous_after_rate_change(self) -> None:
        signal = _ramp(MATERIAL_SAMPLES)
        streamed, flushed, _ = _resample(
            signal, MATERIAL_RATE, 48000, frame_size=FIFO_FRAME_SIZE
        )

        frames = streamed + flushed
        assert frames[0].pts == 0
        _assert_contiguous(frames)
        for frame in frames:
            assert frame.sample_rate == 48000
            assert frame.time_base == Fraction(1, 48000)

    def test_pts_gap_is_mapped_not_repaired(self) -> None:
        """A known gap in the input is preserved by the converter.

        The converter does not invent samples to fill the gap; the gap merely
        stretches the output timeline. Rejecting such input is the FIFO's job
        (see :class:`TestFifoContinuity`).
        """
        # Five 1024 sample frames; the third one starts 7951 samples late.
        plan = ((0, 1024), (1024, 1024), (9999, 1024), (11023, 1024), (12047, 1024))
        contiguous_plan = (
            (0, 1024),
            (1024, 1024),
            (2048, 1024),
            (3072, 1024),
            (4096, 1024),
        )

        def render(points: Sequence[tuple[int, int]]) -> list[AudioFrame]:
            resampler = AudioResampler("s16", "mono", 44100)
            frames: list[AudioFrame] = []
            for pts, samples in points:
                frame = AudioFrame(format="s16", layout="mono", samples=samples)
                frame.sample_rate = 48000
                frame.time_base = Fraction(1, 48000)
                frame.pts = pts
                frame.planes[0].update(bytes(2 * samples))
                frames.extend(resampler.resample(frame))
            frames.extend(resampler.resample(None))
            return frames

        gapped = render(plan)
        contiguous = render(contiguous_plan)

        assert _total(gapped) == _total(contiguous)
        assert gapped[-1].pts is not None and gapped[0].pts is not None
        assert contiguous[-1].pts is not None and contiguous[0].pts is not None
        gap = 9999 - 2048
        expected_gap = round(gap * 44100 / 48000)
        gap_span = gapped[-1].pts + gapped[-1].samples - gapped[0].pts
        contiguous_span = (
            contiguous[-1].pts + contiguous[-1].samples - contiguous[0].pts
        )
        assert gap_span - contiguous_span == expected_gap

    def test_missing_pts_produces_unknown_pts(self) -> None:
        """Without input timestamps the converter makes none up while streaming."""
        signal = _ramp(MATERIAL_SAMPLES)
        streamed, flushed, _ = _resample(
            signal,
            MATERIAL_RATE,
            48000,
            frame_size=FIFO_FRAME_SIZE,
            pattern=(1000, 1, 2047, 3),
            with_pts=False,
        )
        assert streamed
        assert all(frame.pts is None for frame in streamed)

        # The drain does not invent a timestamp either.
        assert all(frame.pts is None for frame in flushed)

        with_pts_signal_streamed, with_pts_flushed, _ = _resample(
            signal,
            MATERIAL_RATE,
            48000,
            frame_size=FIFO_FRAME_SIZE,
            pattern=(1000, 1, 2047, 3),
        )
        assert _total(streamed + flushed) == _total(
            with_pts_signal_streamed + with_pts_flushed
        )


# --------------------------------------------------------------------------
# FIFO: gap rejection and queryable state
# --------------------------------------------------------------------------


def _fifo_frame(samples: int, pts: int | None, rate: int = 48000) -> AudioFrame:
    frame = AudioFrame(format="s16", layout="mono", samples=samples)
    frame.sample_rate = rate
    frame.time_base = Fraction(1, rate)
    frame.pts = pts
    frame.planes[0].update(bytes(2 * samples))
    return frame


class TestFifoContinuity:
    def test_discontinuous_pts_is_rejected_and_state_survives(self) -> None:
        fifo = av.AudioFifo()
        fifo.write(_fifo_frame(1024, 0))
        first = fifo.read(400)
        assert first is not None
        assert first.pts == 0

        assert fifo.samples == 624
        assert fifo.samples_written == 1024
        assert fifo.samples_read == 400

        with pytest.raises(ValueError, match=r"Frame\.pts"):
            fifo.write(_fifo_frame(1024, 9999))

        # The rejected frame left no trace: the accepted audio is still fully
        # described and can be read or extended.
        assert fifo.samples == 624
        assert fifo.samples_written == 1024
        assert fifo.samples_read == 400

        fifo.write(_fifo_frame(1024, 1024))
        assert fifo.samples == 1648
        assert fifo.samples_written == 2048

        remainder = fifo.read(624)
        assert remainder is not None
        assert remainder.pts == 400
        assert remainder.samples == 624

    def test_missing_pts_is_not_a_continuity_error(self) -> None:
        """Unknown timestamps use a different criterion: nothing to compare."""
        fifo = av.AudioFifo()
        fifo.write(_fifo_frame(1024, 0))
        fifo.write(_fifo_frame(1024, None))
        # The timestamp check is skipped for the unknown frame, but its samples
        # still advance the expected position, so the next known timestamp must
        # account for them.
        with pytest.raises(ValueError, match=r"Frame\.pts"):
            fifo.write(_fifo_frame(1024, 1024))
        fifo.write(_fifo_frame(1024, 2048))

        assert fifo.samples == 3072
        assert fifo.samples_written == 3072
        assert fifo.samples_read == 0

        # Output timestamps are derived from the FIFO's own sample counters.
        expected_pts = 0
        while (frame := fifo.read(512)) is not None:
            assert frame.pts == expected_pts
            expected_pts += frame.samples

    def test_missing_timing_metadata_disables_the_check(self) -> None:
        # Without a sample rate the pts per sample cannot be computed, so
        # arbitrary timestamps are accepted and no timestamps are produced.
        no_rate = av.AudioFifo()
        frame = _fifo_frame(1024, 0)
        frame.sample_rate = 0
        no_rate.write(frame)
        assert no_rate.pts_per_sample == 0
        gap_frame = _fifo_frame(1024, 9999)
        gap_frame.sample_rate = 0
        no_rate.write(gap_frame)
        assert no_rate.samples == 2048
        out = no_rate.read(512)
        assert out is not None
        assert out.pts is None

        # Without a time base the same applies, even if sample_rate and pts are
        # present: timestamps cannot be compared without a common time base.
        # ``time_base`` cannot be unset once created, so the zero rational is
        # used; it is what an unset frame exposes.
        no_time_base = av.AudioFifo()
        frame = _fifo_frame(1024, 0)
        frame.time_base = Fraction(0, 1)
        no_time_base.write(frame)
        assert no_time_base.pts_per_sample == 0
        no_time_base.write(_fifo_frame(1024, 9999))
        assert no_time_base.samples == 2048
        out = no_time_base.read(512)
        assert out is not None
        assert out.pts is None
        assert not out.time_base

    def test_resampler_gap_is_rejected_downstream(self) -> None:
        """A gap that the converter propagates is rejected by the FIFO.

        The converter accepts discontinuous input because it only maps the
        timeline; a FIFO that promises contiguous audio refuses it, and its
        state stays readable.
        """
        resampler = AudioResampler("s16", "mono", 44100)
        fifo = av.AudioFifo()

        def push(pts: int) -> None:
            for converted in resampler.resample(_fifo_frame(1024, pts, rate=48000)):
                fifo.write(converted)

        push(0)
        push(1024)
        accepted = fifo.samples_written
        assert accepted > 0
        assert fifo.samples == accepted

        # The gap now reaches the FIFO.
        with pytest.raises(ValueError, match=r"Frame\.pts"):
            push(9999)

        # The audio accepted before the gap is untouched and its timeline can
        # still be read back: the samples and their timestamps agree with what
        # was written, and the gap did not shift them.
        assert fifo.samples == accepted
        assert fifo.samples_written == accepted
        assert fifo.samples_read == 0

        drained: list[AudioFrame] = []
        while (frame := fifo.read(partial=True)) is not None:
            drained.append(frame)
        assert _total(drained) == accepted
        assert fifo.samples == 0
        assert fifo.samples_read == accepted
        assert drained[0].pts == 0
        _assert_contiguous(drained)


# --------------------------------------------------------------------------
# FIFO reframing and fixed-frame encoder draining
# --------------------------------------------------------------------------


def _resampled_signal_frames() -> tuple[list[AudioFrame], int]:
    """Resample the deterministic ramp and reframe it into FIFO sized frames."""
    signal = _ramp(MATERIAL_SAMPLES)
    streamed, flushed, _ = _resample(
        signal,
        MATERIAL_RATE,
        48000,
        frame_size=FIFO_FRAME_SIZE,
        pattern=(1000, 1, 2047, 3),
    )
    converted = streamed + flushed
    total = _total(converted)

    fifo = av.AudioFifo()
    for frame in converted:
        fifo.write(frame)

    frames = fifo.read_many(FIFO_FRAME_SIZE)
    tail = fifo.read(partial=True)
    if tail is not None:
        frames.append(tail)
    assert fifo.samples == 0
    return frames, total


class TestFixedFrameEncoderChain:
    def test_fifo_reframing_keeps_every_sample(self) -> None:
        frames, total = _resampled_signal_frames()

        assert _total(frames) == total
        assert all(frame.samples == FIFO_FRAME_SIZE for frame in frames[:-1])
        assert 0 < frames[-1].samples <= FIFO_FRAME_SIZE
        if total % FIFO_FRAME_SIZE:
            assert frames[-1].samples == total % FIFO_FRAME_SIZE
        _assert_contiguous(frames)

    def test_flac_encodes_the_partial_tail_as_real_audio(self) -> None:
        _require_encoder("flac")
        _require_format("flac")

        frames, total = _resampled_signal_frames()
        buffer = io.BytesIO()
        with av.open(buffer, "w", format="flac") as output:
            stream = _configure_encoder(output, "flac", 48000, "mono", "s16")
            packets = _mux_frames(stream, frames, output)
            for packet in stream.encode(None):
                output.mux(packet)
                packets.append(packet)
            frame_size = stream.cctx.frame_size
            initial_padding = stream.cctx.initial_padding

        data = _data_packets(packets)
        assert initial_padding == 0
        assert data[0].pts == 0
        # The last packet reports the real remainder; it is not a padded frame.
        assert data[-1].pts is not None
        assert data[-1].pts + (data[-1].duration or 0) == total
        assert (data[-1].duration or 0) == total - data[-1].pts
        if total % frame_size:
            assert (data[-1].duration or 0) == total % frame_size

        # No sample is counted twice and none is lost.
        assert _packet_span(packets) == total
        assert sum(packet.duration or 0 for packet in packets) == total
        _assert_packets_contiguous(packets)

        # The lossless round trip is sample exact, including the tail.
        with av.open(io.BytesIO(buffer.getvalue()), "r") as container:
            decoded = list(container.decode(container.streams.audio[0]))
        assert _total(decoded) == total
        expected = np.concatenate([_plane_samples(frame) for frame in frames])
        actual = np.concatenate([_plane_samples(frame) for frame in decoded])
        assert actual.shape == expected.shape
        assert np.array_equal(actual, expected)

    def test_flac_drain_is_required_to_reach_the_end(self) -> None:
        """Without the encoder flush the buffered remainder stays missing.

        This is the encoder's own drain, distinct from the resampler drain; the
        shortfall is bounded by one encoder frame plus the FIFO tail.
        """
        _require_encoder("flac")
        _require_format("flac")

        frames, total = _resampled_signal_frames()
        buffer = io.BytesIO()
        with av.open(buffer, "w", format="flac") as output:
            stream = _configure_encoder(output, "flac", 48000, "mono", "s16")
            packets = _mux_frames(stream, frames, output)
            frame_size = stream.cctx.frame_size

        shortfall = total - _packet_span(packets)
        assert 0 < shortfall <= 2 * frame_size

    def test_aac_padding_is_separate_from_the_audio_tail(self) -> None:
        """Priming padding and the final partial frame are accounted separately."""
        _require_encoder("aac")
        _require_format("mp4")

        frames, total = _resampled_signal_frames()
        buffer = io.BytesIO()
        with av.open(buffer, "w", format="mp4") as output:
            stream = _configure_encoder(output, "aac", 48000, "mono", "fltp")
            packets = _mux_frames(stream, frames, output)
            for packet in stream.encode(None):
                output.mux(packet)
                packets.append(packet)
            initial_padding = stream.cctx.initial_padding
            frame_size = stream.cctx.frame_size

        data = _data_packets(packets)
        assert initial_padding > 0, "AAC priming should be reported"
        # The encoder reports its priming delay as a negative first timestamp,
        # and that packet covers exactly the region before the real start.
        assert data[0].pts == -initial_padding
        assert data[0].duration is not None
        assert data[0].pts + data[0].duration == 0
        # Padding is prepended by the codec; subtract it to recover the real
        # duration, then the final packet still ends at the real end.
        assert sum(packet.duration or 0 for packet in data) - initial_padding == total
        assert data[-1].pts is not None
        assert data[-1].pts + (data[-1].duration or 0) == total
        if total % frame_size:
            assert (data[-1].duration or 0) == total % frame_size
        # Packets abut after the priming packet, and the last one carries the
        # partial tail: padding and audio are counted separately, never twice.
        _assert_packets_contiguous(packets)

        # The container trims the priming padding on decode: the decoded
        # duration equals the resampled duration, with no tail lost.
        with av.open(io.BytesIO(buffer.getvalue()), "r") as container:
            stream_in = container.streams.audio[0]
            decoded = list(container.decode(stream_in))
            duration = stream_in.duration
            rate = stream_in.sample_rate
        assert rate == 48000
        assert _total(decoded) == total
        assert duration is not None
        assert duration * Fraction(1, rate) == Fraction(total, 48000)

        # Without the flush earlier the last partial frame is not encoded.
        buffer_no_flush = io.BytesIO()
        with av.open(buffer_no_flush, "w", format="mp4") as output:
            stream = _configure_encoder(output, "aac", 48000, "mono", "fltp")
            partial_packets = _mux_frames(stream, frames, output)
            frame_size = stream.cctx.frame_size
        assert _packet_span(partial_packets) < total
        assert total - _packet_span(partial_packets) <= 2 * frame_size

    @pytest.mark.parametrize(
        ("codec", "format", "muxer"),
        [
            ("flac", "s16", "flac"),
            ("aac", "fltp", "mp4"),
        ],
    )
    def test_encoder_flush_happens_exactly_once(
        self, codec: str, format: str, muxer: str
    ) -> None:
        _require_encoder(codec)
        _require_format(muxer)

        frames, _ = _resampled_signal_frames()
        buffer = io.BytesIO()
        with av.open(buffer, "w", format=muxer) as output:
            stream = _configure_encoder(output, codec, 48000, "mono", format)
            _mux_frames(stream, frames, output)
            first = list(stream.encode(None))
            for packet in first:
                output.mux(packet)
            with pytest.raises(av.EOFError):
                stream.encode(None)


# --------------------------------------------------------------------------
# End to end with the local asset
# --------------------------------------------------------------------------


class TestLocalAssetChain:
    def test_resample_fifo_encode_preserves_duration(self) -> None:
        _require_encoder("flac")
        _require_format("flac")

        signal, in_rate, layout, channels = _load_local_asset()
        in_samples = signal.size // channels
        assert in_samples == MATERIAL_SAMPLES

        streamed, flushed, again = _resample(
            signal,
            in_rate,
            48000,
            layout=layout,
            channels=channels,
            frame_size=FIFO_FRAME_SIZE,
            pattern=(1000, 1, 2047, 3),
        )
        assert again == []
        converted = streamed + flushed
        total = _total(converted)
        exact = Fraction(in_samples * 48000, in_rate)
        assert math.floor(exact) <= total <= math.ceil(exact)
        _assert_contiguous(converted)

        fifo = av.AudioFifo()
        for frame in converted:
            fifo.write(frame)
        frames = fifo.read_many(FIFO_FRAME_SIZE)
        tail = fifo.read(partial=True)
        if tail is not None:
            frames.append(tail)
        assert _total(frames) == total
        assert fifo.samples == 0

        buffer = io.BytesIO()
        with av.open(buffer, "w", format="flac") as output:
            stream = _configure_encoder(output, "flac", 48000, layout, "s16")
            packets = _mux_frames(stream, frames, output)
            for packet in stream.encode(None):
                output.mux(packet)
                packets.append(packet)
            frame_size = stream.cctx.frame_size

        assert _packet_span(packets) == total
        assert sum(packet.duration or 0 for packet in packets) == total
        _assert_packets_contiguous(packets)

        with av.open(io.BytesIO(buffer.getvalue()), "r") as container:
            decoded = list(container.decode(container.streams.audio[0]))
        assert _total(decoded) == total
        expected = np.concatenate([_plane_samples(frame) for frame in frames])
        actual = np.concatenate([_plane_samples(frame) for frame in decoded])
        assert np.array_equal(actual, expected)

        # The tail is a real partial packet, trimmed from a full encoder frame.
        data = _data_packets(packets)
        assert data[-1].pts is not None
        assert data[-1].pts + (data[-1].duration or 0) == total
        assert 0 < (data[-1].duration or 0) <= frame_size
