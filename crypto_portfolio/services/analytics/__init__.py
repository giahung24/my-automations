"""Read-only analytics computed from the snapshot history (ROADMAP_LEDGER Epic F1, F3).

Every engine here is a pure function over plain data; the loaders are the only code that
touches SQLite, so the maths can be tested without a database.
"""
from services.analytics.attribution import (
    BUCKETS,
    GROUP_BY,
    METHODS,
    Attribution,
    Position,
    SnapshotState,
    attribute,
    attribution_report,
    load_states,
    parse_window,
    position_key,
    window_snapshot_ids,
)
from services.analytics.pnl import Flow, PnlResult, flow_adjusted_pnl, pnl_report, window_pnl
from services.analytics.quant import (
    Close,
    Observation,
    annualized_volatility,
    daily_closes,
    daily_returns,
    drawdown,
    quant_report,
    rebase,
    time_weighted_return,
    twr_index,
)

__all__ = [
    "BUCKETS",
    "GROUP_BY",
    "METHODS",
    "Attribution",
    "Position",
    "SnapshotState",
    "attribute",
    "attribution_report",
    "load_states",
    "parse_window",
    "position_key",
    "window_snapshot_ids",
    "Flow",
    "PnlResult",
    "flow_adjusted_pnl",
    "pnl_report",
    "window_pnl",
    "Close",
    "Observation",
    "annualized_volatility",
    "daily_closes",
    "daily_returns",
    "drawdown",
    "quant_report",
    "rebase",
    "time_weighted_return",
    "twr_index",
]
