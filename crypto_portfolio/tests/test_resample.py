"""Resampling history onto a regular grid so the chart's X axis is proportional to time."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.resample import MAX_BUCKETS, TooManyBuckets, bucket_start, next_bucket, resample

H, D = 3600, 86400


def ts(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


def pt(seconds: int, total: float) -> dict:
    return {"time": seconds, "timestamp": "", "total_usd": total, "binance_usd": total, "onchain_usd": 0.0,
            "is_backfilled": False}


def test_bucket_edges():
    t = ts(2026, 11, 25, 13, 47)                          # a Wednesday
    assert bucket_start(t, "1h") == ts(2026, 11, 25, 13)
    assert bucket_start(t, "4h") == ts(2026, 11, 25, 12)
    assert bucket_start(t, "1d") == ts(2026, 11, 25)
    assert bucket_start(t, "1w") == ts(2026, 11, 23)      # Monday
    assert bucket_start(t, "1mo") == ts(2026, 11, 1)
    assert next_bucket(ts(2026, 12, 1), "1mo") == ts(2027, 1, 1)
    assert next_bucket(ts(2026, 2, 1), "1mo") == ts(2026, 3, 1)


def test_bucket_edges_follow_the_viewer_clock():
    seven = 7 * 60                                        # UTC+7: local midnight is 17:00 UTC
    t = ts(2026, 11, 25, 18)                              # 01:00 on the 26th, local
    assert bucket_start(t, "1d", seven * 60) == ts(2026, 11, 25, 17)
    assert [p["time"] for p in resample([pt(t - 2 * H, 1), pt(t, 2)], "1d", seven)] == [t - 2 * H, t]


def test_one_point_per_bucket_keeps_first_and_last_snapshot():
    start = ts(2026, 11, 25)
    hourly = [pt(start + i * 600, float(i)) for i in range(18)]   # every 10 minutes for 3 hours
    out = resample(hourly, "1h")
    assert [p["total_usd"] for p in out] == [0.0, 11.0, 17.0]      # first bucket opens, others close
    assert not any(p["filled"] for p in out)


def test_gaps_are_filled_by_time_interpolation_so_spacing_is_proportional():
    # a monthly checkpoint, then hourly snapshots three days later: the 1Y-chart case
    jan, feb = ts(2026, 1, 5), ts(2026, 2, 2)
    hours = [pt(feb + 9 * D + i * H, 300.0 + i) for i in range(48)]
    out = resample([pt(jan, 100.0), pt(feb, 200.0), *hours], "1w")
    starts = [bucket_start(p["time"], "1w") for p in out]
    assert all(b - a == 7 * D for a, b in zip(starts, starts[1:]))   # one point per week, no hole
    assert [p["filled"] for p in out] == [False, True, True, True, False, False]
    third = out[2]                                         # Jan 19: 14 of 28 days from 100 to 200
    assert third["time"] == ts(2026, 1, 19) and third["total_usd"] == pytest.approx(150.0)
    assert out[-1]["total_usd"] == 347.0                   # ends on the newest snapshot
    times = [p["time"] for p in out]
    assert times == sorted(times) and len(set(times)) == len(times)


def test_monthly_cadence_handles_uneven_months():
    points = [pt(ts(2025, 12, 1), 1.0), pt(ts(2026, 4, 1), 5.0)]
    out = resample(points, "1mo")
    assert [p["time"] for p in out] == [ts(2025, 12, 1), ts(2026, 1, 1), ts(2026, 2, 1), ts(2026, 3, 1),
                                        ts(2026, 4, 1)]


def test_empty_and_bad_input():
    assert resample([], "1d") == []
    with pytest.raises(ValueError):
        resample([pt(0, 1)], "2d")
    with pytest.raises(TooManyBuckets):
        resample([pt(0, 1), pt((MAX_BUCKETS + 5) * H, 2)], "1h")
