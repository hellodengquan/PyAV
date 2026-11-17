"""Tests for video reformatting that changes both pixel values and color metadata.

Every frame used here is generated in memory from deterministic pixel data, so
the suite needs no downloaded or online media. The focus is on the four things
swscale touches at once during a pixel-format conversion:

* the YUV/RGB colorspace matrix,
* the signal range (limited/MPEG vs full/JPEG),
* the chroma sample location used for chroma upsampling,
* and the color metadata tags (unspecified versus explicitly set) carried on
  the resulting :class:`~av.VideoFrame`.

Odd dimensions (17x11) are used throughout because subsampled planes then need
padding and rounding, which is where metadata-driven conversions most often
break silently.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

import av
from av import VideoFrame
from av.video.reformatter import (
    ChromaLocation,
    ColorPrimaries,
    ColorRange,
    Colorspace,
    ColorTrc,
)

ODD_WIDTH, ODD_HEIGHT = 17, 11
EVEN_WIDTH, EVEN_HEIGHT = 16, 16


def _solid_yuv420p(
    width: int,
    height: int,
    y: int,
    u: int = 128,
    v: int = 128,
) -> VideoFrame:
    """Build a yuv420p frame filled with one YUV triplet.

    Plane buffers are filled through :meth:`Buffer.update`, so alignment
    padding on odd dimensions is handled by FFmpeg rather than assumed away.
    """
    frame = VideoFrame(width, height, "yuv420p")
    frame.planes[0].update(bytes([y]) * frame.planes[0].buffer_size)
    frame.planes[1].update(bytes([u]) * frame.planes[1].buffer_size)
    frame.planes[2].update(bytes([v]) * frame.planes[2].buffer_size)
    return frame


def _chroma_edge_yuv420p(
    width: int,
    height: int,
    chroma_location: ChromaLocation,
) -> VideoFrame:
    """Build a frame with flat luma and a hard horizontal chroma edge.

    The left half of each chroma plane holds a dark value and the right half a
    bright one, so chroma upsampling has a real edge to place. A neutral luma
    plane keeps the difference entirely attributable to the chroma position.
    """
    frame = _solid_yuv420p(width, height, y=128)
    chroma_plane = frame.planes[1]
    line_size = chroma_plane.line_size
    chroma_width = chroma_plane.width
    row = np.zeros(line_size, dtype=np.uint8)
    row[: chroma_width // 2] = 16
    row[chroma_width // 2 : chroma_width] = 240
    payload = np.repeat(row[np.newaxis, :], chroma_plane.height, axis=0)
    chroma_plane.update(payload.tobytes())
    frame.chroma_location = chroma_location
    return frame


def _vertical_chroma_edge_yuv420p(
    width: int,
    height: int,
    chroma_location: ChromaLocation,
) -> VideoFrame:
    """Build a frame whose chroma changes halfway down (a vertical edge).

    Used to exercise the vertical component of the chroma sample position.
    """
    frame = _solid_yuv420p(width, height, y=128)
    chroma_plane = frame.planes[1]
    payload = np.zeros((chroma_plane.height, chroma_plane.line_size), np.uint8)
    split = chroma_plane.height // 2
    payload[:split, : chroma_plane.width] = 16
    payload[split:, : chroma_plane.width] = 240
    chroma_plane.update(payload.tobytes())
    frame.planes[2].update(bytes([128]) * frame.planes[2].buffer_size)
    frame.chroma_location = chroma_location
    return frame


@pytest.mark.parametrize(
    ("width", "height"),
    [(EVEN_WIDTH, EVEN_HEIGHT), (ODD_WIDTH, ODD_HEIGHT)],
)
def test_reformat_color_range_changes_pixels_and_metadata(
    width: int, height: int
) -> None:
    # Y=235 with neutral chroma is the top of the limited (MPEG) range but only
    # a mid-gray when interpreted as full (JPEG) range.
    frame = _solid_yuv420p(width, height, y=235)

    limited = frame.reformat(
        format="rgb24",
        src_color_range=ColorRange.MPEG,
        dst_color_range=ColorRange.MPEG,
    )
    full = frame.reformat(
        format="rgb24",
        src_color_range=ColorRange.JPEG,
        dst_color_range=ColorRange.JPEG,
    )

    limited_rgb = limited.to_ndarray().astype(int)
    full_rgb = full.to_ndarray().astype(int)

    assert limited.color_range == ColorRange.MPEG
    assert full.color_range == ColorRange.JPEG

    # The same coded Y value renders as peak white under limited range but as
    # a 235/255 gray under full range, on both even and odd dimensions.
    assert np.all(limited_rgb == 255)
    assert np.all(full_rgb == 235)
    assert not np.array_equal(limited_rgb, full_rgb)


def test_reformat_without_range_keeps_metadata_unspecified() -> None:
    frame = _solid_yuv420p(ODD_WIDTH, ODD_HEIGHT, y=100)
    assert frame.color_range == ColorRange.UNSPECIFIED

    # No range argument: the output tag must remain UNSPECIFIED rather than
    # swscale guessing one.
    rgb = frame.reformat(format="rgb24")
    assert rgb.color_range == ColorRange.UNSPECIFIED

    yuv = frame.reformat(format="yuv444p")
    assert yuv.color_range == ColorRange.UNSPECIFIED


def test_frame_tagged_range_matches_explicit_arguments() -> None:
    frame = _solid_yuv420p(ODD_WIDTH, ODD_HEIGHT, y=200)
    frame.color_range = ColorRange.MPEG

    tagged = frame.reformat(format="rgb24").to_ndarray()
    explicit = (
        _solid_yuv420p(ODD_WIDTH, ODD_HEIGHT, y=200)
        .reformat(
            format="rgb24",
            src_color_range=ColorRange.MPEG,
            dst_color_range=ColorRange.MPEG,
        )
        .to_ndarray()
    )

    np.testing.assert_array_equal(tagged, explicit)


def test_full_range_rgb_roundtrip_is_lossless_on_odd_dimensions() -> None:
    rgb = VideoFrame.solid(ODD_WIDTH, ODD_HEIGHT, (200, 100, 50), "rgb24")

    yuv = rgb.reformat(format="yuv420p", dst_color_range=ColorRange.JPEG)
    assert yuv.format.name == "yuv420p"
    assert (yuv.width, yuv.height) == (ODD_WIDTH, ODD_HEIGHT)
    assert yuv.color_range == ColorRange.JPEG

    back = yuv.reformat(
        format="rgb24",
        src_color_range=ColorRange.JPEG,
        dst_color_range=ColorRange.JPEG,
    )
    assert back.color_range == ColorRange.JPEG

    # Full range has no headroom/footroom, so an RGB->YUV->RGB trip is exact;
    # limited range would quantize the same values instead.
    np.testing.assert_array_equal(back.to_ndarray(), rgb.to_ndarray())


@pytest.mark.parametrize(
    ("width", "height"),
    [(EVEN_WIDTH, EVEN_HEIGHT), (ODD_WIDTH, ODD_HEIGHT)],
)
def test_reformat_colorspace_matrix_changes_pixels(width: int, height: int) -> None:
    # Non-neutral chroma so the YUV->RGB matrix actually matters.
    frame = _solid_yuv420p(width, height, y=100, u=200, v=50)

    bt709 = frame.reformat(
        format="rgb24",
        src_colorspace=Colorspace.ITU709,
        dst_colorspace=Colorspace.ITU709,
    )
    bt601 = frame.reformat(
        format="rgb24",
        src_colorspace=Colorspace.ITU601,
        dst_colorspace=Colorspace.ITU601,
    )

    assert not np.array_equal(bt709.to_ndarray(), bt601.to_ndarray())

    # SWS_CS_ITU709 maps straight through to AVCOL_SPC_BT709.
    assert bt709.colorspace == Colorspace.ITU709
    # SWS_CS_ITU601 (5) is translated to AVCOL_SPC_SMPTE170M (6).
    assert bt601.colorspace == 6


def test_frame_tagged_colorspace_matches_explicit_arguments() -> None:
    frame = _solid_yuv420p(EVEN_WIDTH, EVEN_HEIGHT, y=100, u=200, v=50)
    frame.colorspace = int(Colorspace.ITU709)

    tagged = frame.reformat(format="rgb24").to_ndarray()
    explicit = (
        _solid_yuv420p(EVEN_WIDTH, EVEN_HEIGHT, y=100, u=200, v=50)
        .reformat(
            format="rgb24",
            src_colorspace=Colorspace.ITU709,
            dst_colorspace=Colorspace.ITU709,
        )
        .to_ndarray()
    )

    np.testing.assert_array_equal(tagged, explicit)


def test_reformat_unspecified_colorspace_is_preserved() -> None:
    # A freshly allocated frame carries AVCOL_SPC_UNSPECIFIED (2). Note this is
    # distinct from swscale's SWS_CS_DEFAULT (5): with no explicit colorspace
    # the conversion still runs on a default matrix, while the output keeps the
    # source's unspecified tag rather than inventing one.
    frame = VideoFrame(EVEN_WIDTH, EVEN_HEIGHT, "yuv420p")
    assert frame.colorspace == 2  # AVCOL_SPC_UNSPECIFIED

    rgb = frame.reformat(format="rgb24")
    assert rgb.colorspace == 2


@pytest.mark.parametrize(
    ("width", "height"),
    [(EVEN_WIDTH, EVEN_HEIGHT), (ODD_WIDTH, ODD_HEIGHT)],
)
def test_chroma_location_tag_propagates_through_conversion(
    width: int, height: int
) -> None:
    # Metadata propagation must work regardless of whether the grid is even or
    # odd, and regardless of the destination pixel format.
    for dst_format in ("yuv422p", "nv12", "rgb24"):
        frame = _chroma_edge_yuv420p(width, height, ChromaLocation.TOPLEFT)
        out = frame.reformat(format=dst_format)
        assert (out.width, out.height) == (width, height)
        assert out.chroma_location == ChromaLocation.TOPLEFT


def test_chroma_location_changes_upsampled_pixels_on_odd_dimensions() -> None:
    # On an even grid a 2x chroma upscale places LEFT and CENTER samples at the
    # same luma columns, so the two are equivalent there. An odd width leaves an
    # unpaired luma column where the half-sample offset is real, which makes the
    # interpolation diverge.
    left = _chroma_edge_yuv420p(ODD_WIDTH, ODD_HEIGHT, ChromaLocation.LEFT)
    center = _chroma_edge_yuv420p(ODD_WIDTH, ODD_HEIGHT, ChromaLocation.CENTER)

    assert not np.array_equal(
        left.reformat(format="rgb24").to_ndarray(),
        center.reformat(format="rgb24").to_ndarray(),
    )


def test_chroma_locations_are_equivalent_on_even_grid() -> None:
    # Guards the inverse of the odd-size case: on a clean 16x16 4:2:0 grid the
    # half-sample distinction has no luma column to land on, so LEFT and CENTER
    # produce bit-identical upsampled pixels even though the tags differ.
    left = _chroma_edge_yuv420p(EVEN_WIDTH, EVEN_HEIGHT, ChromaLocation.LEFT)
    center = _chroma_edge_yuv420p(EVEN_WIDTH, EVEN_HEIGHT, ChromaLocation.CENTER)
    np.testing.assert_array_equal(
        left.reformat(format="rgb24").to_ndarray(),
        center.reformat(format="rgb24").to_ndarray(),
    )


def test_vertical_chroma_location_changes_pixels_on_odd_height() -> None:
    # Same half-sample effect in the vertical direction: an odd height (16x17)
    # makes the top-row placement of TOPLEFT differ from LEFT.
    top_left = _vertical_chroma_edge_yuv420p(
        EVEN_WIDTH, ODD_HEIGHT, ChromaLocation.TOPLEFT
    )
    left = _vertical_chroma_edge_yuv420p(EVEN_WIDTH, ODD_HEIGHT, ChromaLocation.LEFT)
    assert not np.array_equal(
        top_left.reformat(format="rgb24").to_ndarray(),
        left.reformat(format="rgb24").to_ndarray(),
    )


def test_chroma_location_unspecified_matches_center() -> None:
    # swscale treats an unspecified location like centered chroma for
    # progressive 4:2:0 content; LEFT (the H.264 default) differs.
    center = (
        _chroma_edge_yuv420p(ODD_WIDTH, ODD_HEIGHT, ChromaLocation.CENTER)
        .reformat(format="rgb24")
        .to_ndarray()
    )
    unspecified = (
        _chroma_edge_yuv420p(ODD_WIDTH, ODD_HEIGHT, ChromaLocation.UNSPECIFIED)
        .reformat(format="rgb24")
        .to_ndarray()
    )
    left = (
        _chroma_edge_yuv420p(ODD_WIDTH, ODD_HEIGHT, ChromaLocation.LEFT)
        .reformat(format="rgb24")
        .to_ndarray()
    )

    np.testing.assert_array_equal(unspecified, center)
    assert not np.array_equal(unspecified, left)


def test_reformat_preserves_unspecified_trc_and_primaries() -> None:
    frame = _solid_yuv420p(EVEN_WIDTH, EVEN_HEIGHT, y=128)
    assert frame.color_trc == ColorTrc.UNSPECIFIED
    assert frame.color_primaries == ColorPrimaries.UNSPECIFIED

    rgb = frame.reformat(format="rgb24")
    assert rgb.color_trc == ColorTrc.UNSPECIFIED
    assert rgb.color_primaries == ColorPrimaries.UNSPECIFIED


def test_reformat_preserves_explicit_trc_and_primaries_by_default() -> None:
    frame = _solid_yuv420p(EVEN_WIDTH, EVEN_HEIGHT, y=128)
    frame.color_trc = ColorTrc.BT709
    frame.color_primaries = ColorPrimaries.BT470M

    rgb = frame.reformat(format="rgb24")
    assert rgb.color_trc == ColorTrc.BT709
    assert rgb.color_primaries == ColorPrimaries.BT470M


def test_reformat_explicit_trc_and_primaries_override_source() -> None:
    frame = _solid_yuv420p(ODD_WIDTH, ODD_HEIGHT, y=128)
    frame.color_trc = ColorTrc.BT709
    frame.color_primaries = ColorPrimaries.BT709

    rgb = frame.reformat(
        format="rgb24",
        dst_color_trc=ColorTrc.SMPTE2084,
        dst_color_primaries=ColorPrimaries.BT2020,
    )
    assert rgb.color_trc == ColorTrc.SMPTE2084
    assert rgb.color_primaries == ColorPrimaries.BT2020

    # Explicitly asking for a conversion must not retroactively retag the
    # source frame; its metadata is restored after the scale.
    assert frame.color_trc == ColorTrc.BT709
    assert frame.color_primaries == ColorPrimaries.BT709


def test_reformat_does_not_mutate_source_metadata() -> None:
    frame = _chroma_edge_yuv420p(ODD_WIDTH, ODD_HEIGHT, ChromaLocation.LEFT)
    frame.color_range = ColorRange.MPEG
    frame.colorspace = int(Colorspace.ITU709)
    frame.color_trc = ColorTrc.BT709
    frame.color_primaries = ColorPrimaries.BT709

    original = (
        frame.color_range,
        frame.colorspace,
        frame.chroma_location,
        frame.color_trc,
        frame.color_primaries,
    )

    frame.reformat(
        format="rgb24",
        src_color_range=ColorRange.JPEG,
        src_colorspace=Colorspace.ITU601,
    )

    assert (
        frame.color_range,
        frame.colorspace,
        frame.chroma_location,
        frame.color_trc,
        frame.color_primaries,
    ) == original


def test_identity_reformat_returns_same_frame_but_range_change_does_not() -> None:
    frame = _solid_yuv420p(EVEN_WIDTH, EVEN_HEIGHT, y=128)
    frame.color_range = ColorRange.MPEG

    # Nothing asked for: sws_is_noop short-circuits and hands the frame back.
    assert frame.reformat(format="yuv420p") is frame

    # A metadata-only range change is still a real conversion.
    changed = frame.reformat(format="yuv420p", dst_color_range=ColorRange.JPEG)
    assert changed is not frame
    assert changed.color_range == ColorRange.JPEG


def test_color_metadata_roundtrip_through_local_encoder(
    tmp_path: Path,
) -> None:
    """End-to-end check with a tiny locally generated clip.

    FFV1 in Matroska stores range, matrix, transfer, primaries and chroma
    location verbatim, so an encode/decode cycle must preserve every tag
    together with the odd frame dimensions. The tags must be set on the codec
    context (the muxer reads them from there), with matching frame tags. No
    external media is involved.
    """
    path = tmp_path / "color-metadata.mkv"
    width, height = ODD_WIDTH, ODD_HEIGHT

    frame = _chroma_edge_yuv420p(width, height, ChromaLocation.LEFT)
    frame.color_range = ColorRange.MPEG
    frame.colorspace = int(Colorspace.ITU709)
    frame.color_trc = ColorTrc.BT709
    frame.color_primaries = ColorPrimaries.BT709
    frame.pts = 0
    frame.time_base = Fraction(1, 24)

    with av.open(str(path), "w", format="matroska") as output:
        stream = output.add_stream("ffv1", rate=24)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        stream.cctx.color_range = ColorRange.MPEG
        stream.cctx.colorspace = int(Colorspace.ITU709)
        stream.cctx.color_trc = ColorTrc.BT709
        stream.cctx.color_primaries = ColorPrimaries.BT709
        stream.cctx.chroma_sample_location = ChromaLocation.LEFT
        output.mux(stream.encode(frame))
        output.mux(stream.encode(None))

    with av.open(path) as container:
        stream = container.streams.video[0]
        assert stream.cctx.color_range == ColorRange.MPEG
        assert stream.chroma_sample_location == ChromaLocation.LEFT

        decoded = next(container.decode(stream))

    assert (decoded.width, decoded.height) == (width, height)
    assert decoded.color_range == ColorRange.MPEG
    assert decoded.colorspace == Colorspace.ITU709
    assert decoded.color_trc == ColorTrc.BT709
    assert decoded.color_primaries == ColorPrimaries.BT709
    assert decoded.chroma_location == ChromaLocation.LEFT


def test_reformat_accepts_string_enum_arguments() -> None:
    frame = _solid_yuv420p(ODD_WIDTH, ODD_HEIGHT, y=235)
    out = frame.reformat(
        format="rgb24",
        src_color_range="MPEG",
        dst_color_range="MPEG",
        src_colorspace="itu709",
        dst_colorspace="itu709",
    )
    assert out.color_range == ColorRange.MPEG
    assert out.colorspace == Colorspace.ITU709
    np.testing.assert_array_equal(
        out.to_ndarray(), np.full((ODD_HEIGHT, ODD_WIDTH, 3), 255)
    )


def test_invalid_enum_argument_raises() -> None:
    frame = _solid_yuv420p(EVEN_WIDTH, EVEN_HEIGHT, y=128)
    with pytest.raises(KeyError):
        frame.reformat(format="rgb24", dst_color_range="not-a-range")
