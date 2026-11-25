"""Resample history points onto a regular time grid for the value-over-time chart.

Lightweight Charts spaces points by their index in the data array, not by their time. Raw
history mixes monthly backfilled checkpoints with hourly live snapshots, so the last few days
would take most of the chart's width. Putting exactly one point in every bucket of a fixed
cadence (hour, 4 hours, day, week, calendar month) makes the distance between two points on
the X axis proportional to the time between them.

- A bucket with snapshots keeps its last one (the closing value). The first bucket keeps its
  first one instead, so the chart still starts at the oldest snapshot in the window and the
  change over the range is exact at both ends.
- A bucket with no snapshot gets a filler point at the bucket start, linearly interpolated in
  time between its real neighbours and flagged `filled: true`. That straight line is what a
  time-proportional chart draws between two distant points anyway.
- Bucket edges follow the viewer's clock (`tz_offset_minutes` east of UTC), so "one point per
  day" means the snapshot closest to the viewer's midnight, not UTC's.
"""

from __future__ import annotations

from datetime import datetime, timezone

INTERVALS = ("1h", "4h", "1d", "1w", "1mo")
MAX_BUCKETS = 10_000   # e.g. hourly over 5 years would be 43,800: too many for a phone chart

_FIXED = {"1h": 3600, "4h": 4 * 3600, "1d": 86400}
_NUMERIC = ("total_usd", "binance_usd", "onchain_usd", "manual_usd",
            "binance_spot_usd", "binance_earn_usd", "binance_funding_usd")


class TooManyBuckets(ValueError):
    """The window is too long for the requested cadence."""


def bucket_start(seconds: int, interval: str, offset: int = 0) -> int:
    """UTC epoch second at which the bucket holding `seconds` starts (`offset` in seconds)."""
    local = seconds + offset
    if interval in _FIXED:
        size = _FIXED[interval]
        return local - local % size - offset
    if interval == "1w":   # weeks start on Monday; 1970-01-01 was a Thursday
        day = local // 86400
        return (day - (day + 3) % 7) * 86400 - offset
    if interval == "1mo":
        d = datetime.fromtimestamp(local, timezone.utc)
        return int(datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp()) - offset
    raise ValueError(f"unknown interval {interval!r}")


def next_bucket(start: int, interval: str, offset: int = 0) -> int:
    """Start of the bucket right after the one starting at `start`."""
    if interval in _FIXED:
        return start + _FIXED[interval]
    if interval == "1w":
        return start + 7 * 86400
    if interval == "1mo":
        d = datetime.fromtimestamp(start + offset, timezone.utc)
        year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp()) - offset
    raise ValueError(f"unknown interval {interval!r}")


def _filler(start: int, before: dict, after: dict) -> dict:
    span = after["time"] - before["time"]
    weight = (start - before["time"]) / span if span else 0.0
    point = {
        "time": start,
        "timestamp": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "is_backfilled": False,
        "filled": True,
    }
    for key in _NUMERIC:
        if key in before and key in after:
            point[key] = round(before[key] + (after[key] - before[key]) * weight, 2)
    return point


def resample(points: list[dict], interval: str, tz_offset_minutes: int = 0) -> list[dict]:
    """One point per `interval` bucket from the first point's bucket to the last one's.

    `points` must be sorted oldest-first with integer epoch seconds in `time`.
    """
    if interval not in INTERVALS:
        raise ValueError(f"unknown interval {interval!r}")
    if not points:
        return []
    offset = tz_offset_minutes * 60

    first = bucket_start(points[0]["time"], interval, offset)
    chosen: dict[int, dict] = {}
    for point in points:
        start = bucket_start(point["time"], interval, offset)
        if start == first and start in chosen:
            continue   # the first bucket keeps its first point, every other bucket its last
        chosen[start] = point
    real = sorted(chosen.items())

    out: list[dict] = []
    previous = real[0][1]
    index = 0
    start = first
    while index < len(real):
        if len(out) >= MAX_BUCKETS:
            raise TooManyBuckets(f"more than {MAX_BUCKETS} {interval} buckets; use a coarser interval")
        if real[index][0] == start:
            previous = real[index][1]
            out.append({**previous, "filled": False})
            index += 1
        else:
            out.append(_filler(start, previous, real[index][1]))
        start = next_bucket(start, interval, offset)
    return out
