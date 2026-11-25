"""Dark-themed Matplotlib charts (headless Agg) plus a terminal text report.

Colours come from a validated categorical palette: the eight-slot order passes the
lightness-band, chroma, CVD-separation, normal-vision and contrast checks against the
`#1a1a19` surface, so series stay distinguishable for colour-blind readers. Hues are
assigned in fixed slot order and never cycled - a 7th asset folds into "Others" in
neutral grey rather than inventing a colour.

Every chart degrades instead of crashing: no snapshots renders an empty-state card and a
single snapshot renders a hero figure, because a one-point line chart is not a chart.
"""
from __future__ import annotations

import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # must precede pyplot: no display on a cron box

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

from models import SOURCE_LABELS, SOURCES  # noqa: E402
from services.db import SOURCE_COLUMNS  # noqa: E402

# -- theme ---------------------------------------------------------------------
SURFACE = "#1a1a19"      # chart surface
PLANE = "#0d0d0d"        # page plane behind the surface
INK = "#ffffff"          # primary ink
INK_2 = "#c3c2b7"        # secondary ink
MUTED = "#898781"        # axis / tick labels
GRID = "#2c2c2a"         # hairline gridline
BASELINE = "#383835"     # axis line
GOOD = "#0ca30c"
CRITICAL = "#d03b3b"

# Fixed categorical order, stepped for the dark surface. Never reorder, never cycle.
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
OTHER = MUTED            # "Others" is not an entity - it gets neutral grey, not a hue
SOURCE_COLORS = dict(zip(SOURCES, SERIES))

MAX_SLICES = 6           # part-to-whole stays readable to ~6 segments; the rest is "Others"
SPARSE = 10              # at or below this many points, show every marker

RC = {
    "figure.facecolor": PLANE,
    "savefig.facecolor": PLANE,
    "axes.facecolor": SURFACE,
    "axes.edgecolor": BASELINE,
    "axes.labelcolor": INK_2,
    "axes.titlecolor": INK,
    "axes.titlesize": 15,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "axes.titlepad": 14,
    "axes.grid": True,
    "axes.axisbelow": True,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "text.color": INK,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans", "Segoe UI", "Helvetica", "Arial"],
    "font.size": 10,
    "legend.frameon": False,
    "legend.labelcolor": INK_2,
    "legend.fontsize": 9,
    "figure.dpi": 130,
}


def money(value: float, decimals: int = 2) -> str:
    return f"${value:,.{decimals}f}"


def compact(value: float) -> str:
    """Axis-friendly money: $1.2k, $3.4M."""
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if magnitude >= 1_000:
        return f"${value / 1_000:.1f}k"
    return f"${value:,.0f}"


def qty(value: float) -> str:
    """Quantities span 9 orders of magnitude between BTC and a memecoin - scale the precision."""
    magnitude = abs(value)
    if magnitude == 0:
        return "0"
    if magnitude >= 1000:
        return f"{value:,.2f}"
    if magnitude >= 1:
        return f"{value:,.4f}"
    if magnitude >= 0.0001:
        return f"{value:,.6f}"
    return f"{value:.3e}"


def _figure(title: str, subtitle: str = "", size=(11, 6)):
    figure, axes = plt.subplots(figsize=size)
    # the title sits above the subtitle, so its pad has to clear the subtitle's own line
    axes.set_title(title, pad=34 if subtitle else 14)
    if subtitle:
        axes.text(0, 1.012, subtitle, transform=axes.transAxes, color=MUTED, fontsize=10,
                  va="bottom", parse_math=False)
    for spine in ("top", "right"):
        axes.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        axes.spines[spine].set_color(BASELINE)
    return figure, axes


def _card(title: str, headline: str, note: str, path: Path, accent: str = INK) -> Path:
    """Empty state / hero figure: one number (or one message), no axes to mislead anyone."""
    figure = plt.figure(figsize=(11, 6))
    figure.text(0.06, 0.80, title, color=INK, fontsize=15, fontweight="bold", va="top")
    figure.text(0.06, 0.52, headline, color=accent, fontsize=46, fontweight="bold",
                va="center", parse_math=False)
    figure.text(0.06, 0.33, note, color=MUTED, fontsize=11, va="center", wrap=True)
    _save(figure, path)
    return path


def _save(figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, bbox_inches="tight", pad_inches=0.35)
    plt.close(figure)


def _top_slices(rows, limit: int = MAX_SLICES):
    """Top `limit - 1` assets by value plus an aggregated "Others"; never more than `limit`."""
    priced = [r for r in rows if r.value_usd > 0]
    priced.sort(key=lambda r: -r.value_usd)
    if len(priced) <= limit:
        return [(r.symbol, r.value_usd) for r in priced]
    head = [(r.symbol, r.value_usd) for r in priced[:limit - 1]]
    head.append(("Others", sum(r.value_usd for r in priced[limit - 1:])))
    return head


# -- charts --------------------------------------------------------------------
def history_trend(snapshots, path: Path) -> Path:
    """Net worth over time as a stacked area by source; parts sum to the total, baseline 0.

    0 snapshots -> empty state. 1 snapshot -> hero figure. Everything else -> the chart.
    """
    with plt.rc_context(RC):
        if not snapshots:
            return _card(
                "Portfolio history",
                "No data yet",
                "Run  python main.py record  (ideally from cron) to start building the time series.",
                path, accent=MUTED,
            )
        dates = [s.dt for s in snapshots]
        if len(set(dates)) == 1:
            # one snapshot, or several recorded within the same second: a zero-width time
            # axis is not a trend, and matplotlib would warn about identical x limits
            only = snapshots[-1]
            return _card(
                "Portfolio history",
                money(only.total_portfolio_usd),
                f"{'One snapshot' if len(snapshots) == 1 else f'{len(snapshots)} snapshots'} "
                f"so far, taken {only.dt:%Y-%m-%d %H:%M UTC}. "
                "A trend needs at least two distinct times - record again later.",
                path,
            )

        series = {
            # getattr default: a snapshot read before schema v5 has no manual_usd
            source: [getattr(snapshot, SOURCE_COLUMNS[source], 0.0) or 0.0 for snapshot in snapshots]
            for source in SOURCES
        }
        active = {src: values for src, values in series.items() if any(v > 0 for v in values)}
        if not active:  # every snapshot is zero - still worth showing the flat line
            active = {SOURCES[0]: [0.0] * len(snapshots)}

        totals = [s.total_portfolio_usd for s in snapshots]
        span = (dates[-1] - dates[0]).days
        figure, axes = _figure(
            "Portfolio net worth",
            f"{len(snapshots)} snapshots over {span} day{'s' if span != 1 else ''} · "
            f"{dates[0]:%Y-%m-%d} to {dates[-1]:%Y-%m-%d} (UTC)",
        )

        axes.stackplot(
            dates, *active.values(),
            labels=[SOURCE_LABELS[s] for s in active],
            colors=[SOURCE_COLORS[s] for s in active],
            alpha=0.82, edgecolor=SURFACE, linewidth=2,  # 2px surface gap between bands
        )
        axes.plot(dates, totals, color=INK, linewidth=2, zorder=5)
        if len(snapshots) <= SPARSE:  # sparse series: show that these are discrete samples
            axes.plot(dates, totals, "o", color=INK, markersize=5,
                      markeredgecolor=SURFACE, markeredgewidth=2, zorder=6)

        axes.set_ylim(0, max(max(totals), 1.0) * 1.18)
        axes.set_xlim(dates[0], dates[-1])
        axes.yaxis.set_major_formatter(lambda v, _pos: compact(v))
        axes.grid(axis="x", visible=False)
        _date_axis(axes, dates, len(snapshots))

        # direct-label the latest value instead of numbering every point
        change = totals[-1] - totals[0]
        axes.annotate(
            f"{money(totals[-1], 0)}   {'+' if change >= 0 else '-'}{money(abs(change), 0)}",
            xy=(dates[-1], totals[-1]), xytext=(-6, 14), textcoords="offset points",
            color=GOOD if change >= 0 else CRITICAL, fontsize=12, fontweight="bold", ha="right",
            parse_math=False,
        )
        if len(active) > 1:
            axes.legend(loc="upper left", ncols=min(len(active), 4), bbox_to_anchor=(0, -0.10))
        _save(figure, path)
    return path


def _date_axis(axes, dates: list, points: int) -> None:
    """Readable ticks whether the history spans thirty seconds or three years.

    The format has to resolve the span: `%b %d` on a one-minute range prints the same
    label six times over.
    """
    span = (dates[-1] - dates[0]).total_seconds()
    # maxticks below ~4 makes AutoDateLocator give up and warn, so keep a floor
    if span < 3600:
        locator, fmt = mdates.AutoDateLocator(minticks=2, maxticks=5), "%H:%M:%S"
    elif span < 2 * 86400:
        locator, fmt = mdates.AutoDateLocator(minticks=2, maxticks=6), "%m-%d %H:%M"
    elif span < 90 * 86400:
        locator, fmt = mdates.AutoDateLocator(minticks=2, maxticks=min(8, max(4, points))), "%b %d"
    else:
        locator, fmt = mdates.AutoDateLocator(minticks=3, maxticks=8), "%b %Y"
    axes.xaxis.set_major_locator(locator)
    axes.xaxis.set_major_formatter(mdates.DateFormatter(fmt))


def token_allocation(portfolio, path: Path) -> Path:
    """Donut of the top assets by share of USD value, with an aggregated "Others"."""
    with plt.rc_context(RC):
        slices = _top_slices(portfolio.rows)
        if not slices:
            return _card(
                "Token allocation",
                "Nothing to show",
                "No priced holdings above the dust filter. Check your API key and wallet addresses.",
                path, accent=MUTED,
            )

        labels = [name for name, _ in slices]
        values = [value for _, value in slices]
        total = sum(values)
        colors = [OTHER if name == "Others" else SERIES[i % len(SERIES)]
                  for i, name in enumerate(labels)]

        figure, axes = plt.subplots(figsize=(10, 6.6))
        axes.set_title("Token allocation", pad=18)
        wedges, _texts, autotexts = axes.pie(
            values, colors=colors, startangle=90, counterclock=False,
            autopct=lambda pct: f"{pct:.0f}%" if pct >= 6 else "",
            pctdistance=0.78,
            wedgeprops={"width": 0.42, "edgecolor": SURFACE, "linewidth": 2},
            textprops={"color": INK, "fontsize": 10, "fontweight": "bold"},
        )
        for text in autotexts:  # keep labels legible on every hue, incl. the light yellow
            text.set_color(SURFACE if text.get_text() else INK)
        axes.text(0, 0.08, money(total, 0), ha="center", va="center", color=INK,
                  fontsize=24, fontweight="bold", parse_math=False)
        axes.text(0, -0.14, f"{len(portfolio.rows)} assets", ha="center", va="center",
                  color=MUTED, fontsize=11)
        axes.set_aspect("equal")
        # identity is never colour-alone: every wedge is also named in the legend
        legend = axes.legend(
            wedges,
            [f"{name}   {money(value, 0)}  ({value / total * 100:.1f}%)" for name, value in slices],
            loc="center left", bbox_to_anchor=(1.0, 0.5), labelspacing=0.9,
        )
        for text in legend.get_texts():
            text.set_parse_math(False)
        _save(figure, path)
    return path


def source_distribution(portfolio, path: Path) -> Path:
    """Where the money sits. A bar, not a donut: these values are compared, not apportioned."""
    with plt.rc_context(RC):
        entries = [(SOURCE_LABELS[s], portfolio.totals.get(s, 0.0), SOURCE_COLORS[s])
                   for s in SOURCES if portfolio.totals.get(s, 0.0) > 0]
        if not entries:
            return _card(
                "Where your funds sit",
                "Nothing to show",
                "No source reported a positive balance. Check your API key and wallet addresses.",
                path, accent=MUTED,
            )

        entries.sort(key=lambda e: e[1])  # largest on top in a horizontal bar
        total = sum(value for _, value, _ in entries)
        figure, axes = _figure(
            "Where your funds sit", f"Total {money(total, 0)} across {len(entries)} sources",
            size=(11, 0.8 * len(entries) + 2.2),
        )

        top = max(value for _, value, _ in entries)
        rounding = top * 0.012  # rounded data-end, ~4px at the default figure width
        for index, (label, value, color) in enumerate(entries):
            # draw from -rounding so only the data-end reads as rounded; xlim clips the rest
            axes.add_patch(FancyBboxPatch(
                (-rounding, index - 0.22), value + rounding, 0.44,
                boxstyle=f"round,pad=0,rounding_size={rounding}",
                facecolor=color, edgecolor="none", mutation_aspect=0.0001, zorder=3,
            ))
            axes.text(value + top * 0.02, index, f"{money(value, 0)}   {value / total * 100:.1f}%",
                      va="center", ha="left", color=INK, fontsize=11, fontweight="bold", zorder=4,
                      parse_math=False)

        axes.set_yticks(range(len(entries)), [label for label, _, _ in entries], color=INK_2, fontsize=11)
        axes.set_ylim(-0.7, len(entries) - 0.3)
        axes.set_xlim(0, top * 1.24)
        axes.xaxis.set_major_formatter(lambda v, _pos: compact(v))
        axes.grid(axis="y", visible=False)
        axes.spines["left"].set_visible(False)
        axes.tick_params(axis="y", length=0)
        _save(figure, path)
    return path


def generate(portfolio, snapshots, output_dir: str | Path = "./charts", kinds=("all",)) -> list[Path]:
    """Render the requested charts and return the paths actually written."""
    output_dir = Path(output_dir)
    kinds = set(kinds)
    wanted = {"history", "allocation", "sources"} if "all" in kinds else kinds
    written = []
    if "history" in wanted:
        written.append(history_trend(snapshots, output_dir / "history_trend.png"))
    if "allocation" in wanted:
        written.append(token_allocation(portfolio, output_dir / "token_allocation.png"))
    if "sources" in wanted:
        written.append(source_distribution(portfolio, output_dir / "source_distribution.png"))
    return written


# -- text mode -----------------------------------------------------------------
BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 48) -> str:
    """Unicode sparkline, downsampled to `width` buckets."""
    values = [v for v in values if isinstance(v, (int, float)) and not math.isnan(v)]
    if not values:
        return ""
    if len(values) > width:  # bucket-average so a long history still fits on one line
        step = len(values) / width
        buckets = []
        for index in range(width):
            chunk = values[int(index * step):int((index + 1) * step)] or [values[int(index * step)]]
            buckets.append(sum(chunk) / len(chunk))
        values = buckets
    low, high = min(values), max(values)
    if high - low < 1e-12:
        return BLOCKS[3] * len(values)
    return "".join(BLOCKS[min(int((v - low) / (high - low) * (len(BLOCKS) - 1)), len(BLOCKS) - 1)]
                   for v in values)


def bar(fraction: float, width: int = 24) -> str:
    """Horizontal ASCII bar for a 0..1 fraction."""
    fraction = min(max(fraction, 0.0), 1.0)
    filled = int(round(fraction * width))
    return "█" * filled + "·" * (width - filled)


def text_report(portfolio, snapshots=None) -> str:
    """The charts as text, for terminals, logs and `--type` runs without a display."""
    lines = ["", "  ALLOCATION".ljust(40) + "  (top assets by USD value)", "  " + "─" * 62]
    slices = _top_slices(portfolio.rows, limit=8)
    total = sum(value for _, value in slices) or 1.0
    for name, value in slices:
        lines.append(f"  {name:<10} {bar(value / total)}  {value / total * 100:5.1f}%  {money(value, 0):>12}")
    if not slices:
        lines.append("  (no priced holdings)")

    lines += ["", "  SOURCES".ljust(40) + "  (where the money sits)", "  " + "─" * 62]
    source_total = portfolio.total_usd or 1.0
    shown = [s for s in SOURCES if portfolio.totals.get(s, 0.0) > 0]
    for source in shown:
        value = portfolio.totals[source]
        lines.append(
            f"  {SOURCE_LABELS[source]:<17} {bar(value / source_total, 20)}  "
            f"{value / source_total * 100:5.1f}%  {money(value, 0):>12}"
        )
    if not shown:
        lines.append("  (no source reported a balance)")

    if snapshots:
        lines += ["", "  HISTORY".ljust(40) + f"  ({len(snapshots)} snapshots)", "  " + "─" * 62]
        totals = [s.total_portfolio_usd for s in snapshots]
        if len(snapshots) == 1:
            lines.append(f"  {money(totals[0])} at {snapshots[0].dt:%Y-%m-%d %H:%M UTC} "
                         "(one snapshot - no trend yet)")
        else:
            change = totals[-1] - totals[0]
            pct = (change / totals[0] * 100) if totals[0] else 0.0
            lines.append(f"  {sparkline(totals)}")
            lines.append(
                f"  {snapshots[0].dt:%Y-%m-%d} {money(totals[0], 0)}  →  "
                f"{snapshots[-1].dt:%Y-%m-%d} {money(totals[-1], 0)}   "
                f"{'+' if change >= 0 else '-'}{money(abs(change), 0)} ({pct:+.1f}%)"
            )
    return "\n".join(lines) + "\n"
