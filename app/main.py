"""FastAPI application exposing the subtitle segmentation service."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from . import config
from .cut import segment_with_deletions
from .models import (
    CutRequest,
    PlaceRequest,
    PlaceResponse,
    PlacedSegmentOut,
    SegmentOut,
    SegmentRequest,
    SegmentResponse,
)
from .place import segment_and_place
from .segmenter import SegmentationError, SegmentationResult, segment_words
from .srt import to_srt
from .vtt import to_vtt

app = FastAPI(
    title="Subtitle Segmenter",
    version="1.0.0",
    description=(
        "Split a word-level transcript into subtitles with a dynamic-programming "
        "optimiser. Total cost = sum of squared deviations of subtitle durations "
        "(seconds) from the target duration, plus a 4-point penalty for every "
        "crossed sentence-ending-punctuation or long-pause boundary; when "
        "max_line_chars is given it also includes a two-line typesetting cost "
        "(squared line-length difference divided by the squared line width; "
        "single-line subtitles cost 0). Ties are broken in favour of fewer "
        "subtitles. Optional required_break_after and keep_together fields "
        "add hard, human-edited segment boundaries that the optimiser must "
        "respect. The /segment/cut endpoints additionally accept deletion "
        "intervals on the original video timeline: covered words are dropped, "
        "the survivors are shifted earlier by the deleted duration, and every "
        "continuous fragment is re-optimised separately. The /segment/place "
        "endpoints additionally accept timeline occlusions tagged with a "
        "screen area (top/bottom): segmentation and per-subtitle vertical "
        "placement are decided jointly, with a 1-point cost for every pair of "
        "adjacent subtitles placed in different areas, ties broken by fewer "
        "subtitles and then by the area sequence (bottom preferred); every "
        "subtitle must keep one area fully clear for its whole duration."
    ),
)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


@app.get("/", tags=["meta"])
def root() -> dict:
    return {
        "service": "subtitle-segmenter",
        "docs": "/docs",
        "endpoints": {
            "segment_json": "POST /segment",
            "segment_srt": "POST /segment/srt",
            "segment_cut_json": "POST /segment/cut",
            "segment_cut_srt": "POST /segment/cut/srt",
            "segment_place_json": "POST /segment/place",
            "segment_place_vtt": "POST /segment/place/vtt",
            "health": "GET /health",
        },
        "defaults": {
            "max_chars": config.DEFAULT_MAX_CHARS,
            "min_duration_ms": config.DEFAULT_MIN_DURATION_MS,
            "max_duration_ms": config.DEFAULT_MAX_DURATION_MS,
            "target_duration_ms": config.DEFAULT_TARGET_DURATION_MS,
            "pause_threshold_ms": config.DEFAULT_PAUSE_THRESHOLD_MS,
        },
    }


def _run(req: SegmentRequest) -> SegmentationResult:
    try:
        return segment_words(
            req.words,
            max_chars=req.max_chars,
            min_duration_ms=req.min_duration_ms,
            max_duration_ms=req.max_duration_ms,
            target_duration_ms=req.target_duration_ms,
            pause_threshold_ms=req.pause_threshold_ms,
            max_line_chars=req.max_line_chars,
            required_break_after=req.required_break_after,
            keep_together=req.keep_together,
        )
    except SegmentationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/segment", response_model=SegmentResponse, tags=["segment"])
def segment(req: SegmentRequest) -> SegmentResponse:
    """Segment the transcript and return the subtitles as JSON."""
    result = _run(req)
    return SegmentResponse(
        segment_count=len(result.segments),
        total_cost=round(result.total_cost, 6),
        segments=[SegmentOut(**vars(seg)) for seg in result.segments],
    )


@app.post(
    "/segment/srt",
    response_class=PlainTextResponse,
    tags=["segment"],
)
def segment_srt(req: SegmentRequest) -> PlainTextResponse:
    """Segment the transcript and return the subtitles as an SRT document."""
    result = _run(req)
    return PlainTextResponse(to_srt(result.segments))


def _run_cut(req: CutRequest) -> SegmentationResult:
    try:
        return segment_with_deletions(
            req.words,
            req.deletions,
            max_chars=req.max_chars,
            min_duration_ms=req.min_duration_ms,
            max_duration_ms=req.max_duration_ms,
            target_duration_ms=req.target_duration_ms,
            pause_threshold_ms=req.pause_threshold_ms,
            max_line_chars=req.max_line_chars,
        )
    except SegmentationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/segment/cut", response_model=SegmentResponse, tags=["segment"])
def segment_cut(req: CutRequest) -> SegmentResponse:
    """Drop the deleted ranges, re-time the survivors and re-segment.

    Returns the subtitles of the edited video as JSON; ``word_start`` /
    ``word_end`` are indices into the original request's ``words``.
    """
    result = _run_cut(req)
    return SegmentResponse(
        segment_count=len(result.segments),
        total_cost=round(result.total_cost, 6),
        segments=[SegmentOut(**vars(seg)) for seg in result.segments],
    )


@app.post(
    "/segment/cut/srt",
    response_class=PlainTextResponse,
    tags=["segment"],
)
def segment_cut_srt(req: CutRequest) -> PlainTextResponse:
    """Same re-segmentation as /segment/cut, exported as an SRT document."""
    result = _run_cut(req)
    return PlainTextResponse(to_srt(result.segments))


def _run_place(req: PlaceRequest):
    try:
        return segment_and_place(
            req.words,
            [(o.start_ms, o.end_ms, o.region) for o in req.occlusions],
            max_chars=req.max_chars,
            min_duration_ms=req.min_duration_ms,
            max_duration_ms=req.max_duration_ms,
            target_duration_ms=req.target_duration_ms,
            pause_threshold_ms=req.pause_threshold_ms,
            max_line_chars=req.max_line_chars,
            required_break_after=req.required_break_after,
            keep_together=req.keep_together,
        )
    except SegmentationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/segment/place", response_model=PlaceResponse, tags=["segment"])
def segment_place(req: PlaceRequest) -> PlaceResponse:
    """Segment and place each subtitle in a clear top/bottom area (JSON)."""
    result = _run_place(req)
    return PlaceResponse(
        segment_count=len(result.segments),
        total_cost=round(result.total_cost, 6),
        segments=[PlacedSegmentOut(**vars(seg)) for seg in result.segments],
    )


@app.post(
    "/segment/place/vtt",
    response_class=PlainTextResponse,
    tags=["segment"],
)
def segment_place_vtt(req: PlaceRequest) -> PlainTextResponse:
    """Same joint segmentation/placement as /segment/place, as WebVTT.

    The chosen area is rendered on each cue with a WebVTT line setting
    (``line:0`` for top, ``line:-1`` for bottom).
    """
    result = _run_place(req)
    return PlainTextResponse(
        to_vtt(result.segments), media_type="text/vtt; charset=utf-8"
    )
