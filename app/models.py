"""Request / response schemas."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from . import config

# Non-negative word index (index-range checks against the actual word count
# live in the segmenter, so both endpoints report them uniformly as 422).
WordIndex = Annotated[int, Field(ge=0)]

# Non-negative millisecond timestamp on the original video timeline.
Ms = Annotated[int, Field(ge=0)]


class Word(BaseModel):
    text: str = Field(min_length=1, description="token text")
    start_ms: int = Field(ge=0, description="word start time in milliseconds")
    end_ms: int = Field(gt=0, description="word end time in milliseconds")

    @model_validator(mode="after")
    def _check_times(self) -> "Word":
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class BaseSegmentRequest(BaseModel):
    """Fields shared by the plain and the cut-aware segmentation requests."""

    words: list[Word] = Field(
        min_length=1,
        description="time-ordered, non-overlapping word-level transcript",
    )
    max_chars: int = Field(
        default=config.DEFAULT_MAX_CHARS,
        ge=1,
        description="maximum number of characters per subtitle",
    )
    min_duration_ms: int = Field(
        default=config.DEFAULT_MIN_DURATION_MS,
        ge=0,
        description="minimum subtitle duration in milliseconds",
    )
    max_duration_ms: int = Field(
        default=config.DEFAULT_MAX_DURATION_MS,
        gt=0,
        description="maximum subtitle duration in milliseconds",
    )
    target_duration_ms: int = Field(
        default=config.DEFAULT_TARGET_DURATION_MS,
        ge=0,
        description="ideal subtitle duration in milliseconds",
    )
    pause_threshold_ms: int = Field(
        default=config.DEFAULT_PAUSE_THRESHOLD_MS,
        ge=0,
        description="gap (ms) between words that counts as a long pause",
    )
    max_line_chars: int | None = Field(
        default=None,
        ge=1,
        description=(
            "optional line width for at-most-two-line typesetting: when set, "
            "each subtitle is laid out on one or two lines that break only "
            "between words, with every line no longer than this many "
            "characters (joining spaces included); omitted to keep plain "
            "single-line output"
        ),
    )

    @model_validator(mode="after")
    def _check_limits(self) -> "BaseSegmentRequest":
        if self.max_duration_ms < self.min_duration_ms:
            raise ValueError("max_duration_ms must be >= min_duration_ms")
        return self


class SegmentRequest(BaseSegmentRequest):
    required_break_after: list[WordIndex] | None = Field(
        default=None,
        description=(
            "optional zero-based word indices after which a subtitle break is "
            "mandatory; each value k (0 <= k <= number of words - 2) forces a "
            "segment edge between word k and word k+1. Duplicates are "
            "ignored."
        ),
    )
    keep_together: list[tuple[WordIndex, WordIndex]] | None = Field(
        default=None,
        description=(
            "optional closed zero-based word-index intervals [lo, hi], each "
            "containing at least two words (lo < hi), inside which no subtitle "
            "edge may fall; intervals must be pairwise disjoint (touching at "
            "an endpoint is allowed), and a required_break_after index may "
            "not equal lo or lie inside [lo, hi] (it may equal hi, the "
            "interval's right edge)"
        ),
    )


class CutRequest(BaseSegmentRequest):
    deletions: list[tuple[Ms, Ms]] = Field(
        min_length=1,
        description=(
            "deletion intervals [start_ms, end_ms] on the original video "
            "timeline, in milliseconds; they must be sorted by start time, "
            "pairwise non-overlapping (touching at an endpoint is allowed) "
            "and each must satisfy start_ms < end_ms. Words fully covered "
            "by an interval are dropped; a word that intersects an interval "
            "boundary without being fully covered is rejected (422). The "
            "surviving words are shifted earlier by the total deleted "
            "duration before them, and words on opposite sides of a "
            "deletion never share a subtitle"
        ),
    )


class Occlusion(BaseModel):
    """A timeline interval during which one screen area is unavailable."""

    start_ms: int = Field(ge=0, description="occlusion start in milliseconds")
    end_ms: int = Field(gt=0, description="occlusion end in milliseconds")
    region: Literal["top", "bottom"] = Field(
        description="blocked vertical area; a subtitle with a positive-length "
        "time intersection with the interval may not be placed there"
    )

    @model_validator(mode="after")
    def _check_times(self) -> "Occlusion":
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be greater than start_ms")
        return self


class PlaceRequest(SegmentRequest):
    occlusions: list[Occlusion] = Field(
        min_length=1,
        description=(
            "timeline intervals [start_ms, end_ms) on the video timeline, in "
            "milliseconds, during which one vertical area (region = \"top\" "
            "or \"bottom\") is unavailable to subtitles. Intervals of the "
            "same region must be sorted by start time and pairwise "
            "non-overlapping (touching at an endpoint is allowed); top and "
            "bottom intervals are independent. Every subtitle must be "
            "placeable in one region that stays clear for its whole "
            "duration; segmentation and placement are optimised jointly"
        ),
    )


class SegmentOut(BaseModel):
    index: int
    text: str
    start_ms: int
    end_ms: int
    duration_ms: int
    word_start: int
    word_end: int
    crossed_boundaries: int


class PlacedSegmentOut(SegmentOut):
    position: Literal["top", "bottom"]


class SegmentResponse(BaseModel):
    segment_count: int
    total_cost: float
    segments: list[SegmentOut]


class PlaceResponse(BaseModel):
    segment_count: int
    total_cost: float
    segments: list[PlacedSegmentOut]
