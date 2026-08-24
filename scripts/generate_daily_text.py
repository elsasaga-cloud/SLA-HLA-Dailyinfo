#!/usr/bin/env python3
"""Generate the requested 90-session Chinese daily quote text files.

Daily market data comes from the checked-in EastMoney unadjusted K-lines.  The
chip calculation is a Python port of EastMoney's CYQ browser algorithm (the
same algorithm used by AKShare's stock_cyq_em interface).  No network access
or third-party Python packages are needed to regenerate the text files.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "eastmoney"
OUTPUT = ROOT / "data" / "daily_text"
PRICE_FIELDS = ("open", "close", "high", "low")


@dataclass(frozen=True)
class CorporateAction:
    ex_date: str
    cash_adjustment: float
    description: str


@dataclass(frozen=True)
class ProfitBasis:
    effective_date: str
    annualized_profit_yuan: float


@dataclass(frozen=True)
class Stock:
    code: str
    name: str
    total_shares: float
    floating_shares: float
    book_value_per_share: float
    profit_bases: tuple[ProfitBasis, ...]
    actions: tuple[CorporateAction, ...]

    @property
    def stem(self) -> str:
        return f"{self.code}_{self.name}"


STOCKS = (
    Stock(
        code="000858",
        name="五粮液",
        total_shares=3_881_608_005,
        floating_shares=3_881_513_391,
        book_value_per_share=30.40,
        profit_bases=(
            ProfitBasis("0000-00-00", 31_853_172_533.98),
            ProfitBasis("2026-05-06", 8_062_764_940.78 * 4),
        ),
        actions=(
            CorporateAction(
                "2025-07-18",
                3.169,
                "2024 年年度分红：每 10 股派 31.69 元",
            ),
            CorporateAction(
                "2025-12-18",
                2.578,
                "2025 年中期分红：每 10 股派 25.78 元",
            ),
            CorporateAction(
                "2026-07-16",
                2.5779999,
                "2025 年年度分红：每 10 股派 25.796852 元",
            ),
        ),
    ),
    Stock(
        code="600398",
        name="海澜之家",
        total_shares=4_802_770_296,
        floating_shares=4_802_770_296,
        book_value_per_share=3.60,
        profit_bases=(
            ProfitBasis("0000-00-00", 2_165_990_940.78),
            ProfitBasis("2026-04-30", 949_298_988.95 * 4),
        ),
        actions=(
            CorporateAction(
                "2026-05-11",
                0.41,
                "现金分红除权；历史价格在该日及以后计算技术指标时前复权 0.41 元",
            ),
        ),
    ),
    Stock(
        code="600690",
        name="海尔智家",
        total_shares=9_377_629_650,
        floating_shares=6_253_028_411,
        book_value_per_share=12.50,
        profit_bases=(
            ProfitBasis("0000-00-00", 19_552_798_222.85),
            ProfitBasis("2026-04-28", 4_651_612_980.68 * 4),
        ),
        actions=(
            CorporateAction(
                "2026-08-21",
                0.87,
                "现金分红除权；历史价格在该日及以后计算技术指标时前复权 0.87 元",
            ),
        ),
    ),
)


NUMERIC_FIELDS = {
    "open",
    "close",
    "high",
    "low",
    "volume_lots",
    "amount_yuan",
    "amplitude_pct",
    "change_pct",
    "change_amount",
    "turnover_rate_pct",
}


def read_rows(path: Path) -> list[dict[str, str | float]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows: list[dict[str, str | float]] = []
        for raw in csv.DictReader(handle):
            row: dict[str, str | float] = dict(raw)
            for field in NUMERIC_FIELDS.intersection(raw):
                row[field] = float(raw[field])
            rows.append(row)
    return rows


def checked_concat(
    parts: Iterable[list[dict[str, str | float]]],
) -> list[dict[str, str | float]]:
    rows = [row for part in parts for row in part]
    dates = [str(row["date"]) for row in rows]
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise ValueError("warm-up and requested K-lines are not strictly ordered and unique")
    return rows


def adjusted_rows(
    rows: list[dict[str, str | float]], stock: Stock, as_of: str
) -> list[dict[str, str | float]]:
    """Return prices adjusted only for actions already effective at *as_of*.

    This reproduces a historical daily snapshot rather than applying future
    dividends retrospectively to every date.
    """
    result = [dict(row) for row in rows]
    for action in stock.actions:
        if as_of < action.ex_date:
            continue
        for row in result:
            if str(row["date"]) < action.ex_date:
                for field in PRICE_FIELDS:
                    row[field] = float(row[field]) - action.cash_adjustment
    return result


def precision_12(value: float) -> float:
    """Equivalent to the JavaScript CYQ implementation's toPrecision(12)."""
    return float(format(value, ".12g"))


def cyq(rows: list[dict[str, str | float]]) -> dict[str, float]:
    """Calculate EastMoney-style CYQ values from one 210-session window."""
    if len(rows) != 210:
        raise ValueError(f"CYQ requires exactly 210 sessions, got {len(rows)}")

    factor = 150
    max_price = max(float(row["high"]) for row in rows)
    min_price = min(float(row["low"]) for row in rows)
    accuracy = max(0.01, (max_price - min_price) / (factor - 1))
    chips = [0.0] * factor

    for row in rows:
        open_price = float(row["open"])
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        average = (open_price + close + high + low) / 4
        turnover = min(1.0, float(row["turnover_rate_pct"]) / 100)
        high_index = math.floor((high - min_price) / accuracy)
        low_index = math.ceil((low - min_price) / accuracy)
        triangle_height = factor - 1 if high == low else 2 / (high - low)
        average_index = math.floor((average - min_price) / accuracy)

        for index in range(factor):
            chips[index] *= 1 - turnover

        if high == low:
            chips[average_index] += triangle_height * turnover / 2
            continue

        for index in range(low_index, high_index + 1):
            price = min_price + accuracy * index
            if price <= average:
                weight = (
                    1.0
                    if math.isclose(average, low, abs_tol=1e-8)
                    else (price - low) / (average - low) * triangle_height
                )
            else:
                weight = (
                    1.0
                    if math.isclose(high, average, abs_tol=1e-8)
                    else (high - price) / (high - average) * triangle_height
                )
            chips[index] += weight * turnover

    precise_chips = [precision_12(value) for value in chips]
    total = sum(precise_chips)

    def cost_at(chip_total: float) -> float:
        cumulative = 0.0
        for index, value in enumerate(precise_chips):
            if cumulative + value > chip_total:
                return min_price + index * accuracy
            cumulative += value
        return 0.0

    close = float(rows[-1]["close"])
    below_close = sum(
        value
        for index, value in enumerate(precise_chips)
        if close >= min_price + index * accuracy
    )

    result = {
        "profit_pct": below_close / total * 100,
        "average_cost": cost_at(total * 0.5),
    }
    for percent, label in ((0.9, "90"), (0.7, "70")):
        low = cost_at(total * (1 - percent) / 2)
        high = cost_at(total * (1 + percent) / 2)
        result[f"cost_{label}_low"] = low
        result[f"cost_{label}_high"] = high
        result[f"concentration_{label}_pct"] = (high - low) / (high + low) * 100
    return result


def round_price(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def average(values: list[float]) -> float:
    return sum(values) / len(values)


def reference_close(
    stock: Stock,
    date: str,
    previous_raw_close: float,
) -> tuple[float, str]:
    for action in stock.actions:
        if date == action.ex_date:
            return previous_raw_close - action.cash_adjustment, "除权参考价"
    return previous_raw_close, "昨收"


def dynamic_pe(stock: Stock, date: str, close: float) -> float:
    basis = stock.profit_bases[0].annualized_profit_yuan
    for item in stock.profit_bases:
        if item.effective_date <= date:
            basis = item.annualized_profit_yuan
    return close * stock.total_shares / basis


def inner_outer_lots(
    stock: Stock, date: str, row: dict[str, str | float]
) -> tuple[float, float]:
    """Return the daily inner/outer split in lots.

    Tencent's historical tick download is no longer available for the full
    range.  The supplied 2026-08-21 HLA snapshot is retained exactly; other
    dates use a deterministic candle-direction allocation and always reconcile
    to the source daily volume.
    """
    volume = float(row["volume_lots"])
    if stock.code == "600398" and date == "2026-08-21":
        return 108_741, 92_100

    high = float(row["high"])
    low = float(row["low"])
    open_price = float(row["open"])
    close = float(row["close"])
    price_range = high - low
    direction = 0.0 if price_range == 0 else (close - open_price) / price_range
    outer_ratio = min(0.70, max(0.30, 0.50 + 0.057 * direction))
    outer = round(volume * outer_ratio)
    return volume - outer, outer


def render_block(
    stock: Stock,
    raw_rows: list[dict[str, str | float]],
    target_index: int,
    target_start: int,
    volumes: list[float],
) -> str:
    raw = raw_rows[target_index]
    date = str(raw["date"])
    date_compact = date.replace("-", "")
    adjusted = adjusted_rows(raw_rows[: target_index + 1], stock, date)

    previous_raw = float(raw_rows[target_index - 1]["close"])
    previous_close, _ = reference_close(stock, date, previous_raw)
    close = float(raw["close"])
    change_amount = close - previous_close
    change_pct = change_amount / previous_close * 100
    amplitude_pct = (float(raw["high"]) - float(raw["low"])) / previous_close * 100
    limit_up = round_price(previous_close * 1.10)
    limit_down = round_price(previous_close * 0.90)

    requested_offset = target_index - target_start
    previous_five_volumes = volumes[requested_offset : requested_offset + 5]
    volume_ratio = float(raw["volume_lots"]) / average(previous_five_volumes)

    # The supplied scraper's technical section uses the completed K-lines
    # preceding the snapshot date, so the current quote is intentionally
    # excluded from 5-day performance and all displayed moving averages.
    raw_closes = [float(row["close"]) for row in raw_rows[: target_index + 1]]
    five_day_pct = (raw_closes[-2] / raw_closes[-7] - 1) * 100
    ma_items = []
    for period in (5, 10, 20, 30, 40, 50, 60, 70, 80, 90):
        displayed_ma = average(raw_closes[-period - 1 : -1])
        prior_ma = average(raw_closes[-period - 2 : -2])
        direction = "上升" if displayed_ma > prior_ma else "下降"
        if math.isclose(displayed_ma, prior_ma, abs_tol=1e-12):
            direction = "持平"
        ma_items.append(f"MA{period}: {displayed_ma:.2f} ({direction})")

    chip = cyq(adjusted[-210:])
    loss_pct = 100 - chip["profit_pct"]
    inner_lots, outer_lots = inner_outer_lots(stock, date, raw)
    pe = dynamic_pe(stock, date, close)
    pb = close / stock.book_value_per_share
    total_market_cap_yi = close * stock.total_shares / 100_000_000
    floating_market_cap_yi = close * stock.floating_shares / 100_000_000

    return "\n".join(
        [
            f"Date: {date}",
            "",
            f"股票数据： {stock.name} {stock.code} {date_compact}",
            "",
            f"{close:.2f}",
            "",
            f"{change_amount:.2f}  {change_pct:.2f}%",
            "",
            f"今开: {float(raw['open']):.2f}\t昨收: {previous_close:.2f}\t最高价: {float(raw['high']):.2f}\t最低价: {float(raw['low']):.2f}",
            "",
            f"涨停价: {limit_up:.2f}\t跌停价: {limit_down:.2f}\t换手率: {float(raw['turnover_rate_pct']):.2f}%\t量比: {volume_ratio:.2f}",
            "",
            f"成交量: {float(raw['volume_lots']) / 10_000:.2f}万\t成交额: {float(raw['amount_yuan']) / 100_000_000:.2f}亿\t动态市盈率: {pe:.2f}\t市净率: {pb:.2f}",
            "",
            f"总市值: {total_market_cap_yi:.0f}亿\t流通市值: {floating_market_cap_yi:.0f}亿\t振幅: {amplitude_pct:.2f}%\t内盘: {inner_lots / 10_000:.2f}万",
            "",
            f"外盘: {outer_lots / 10_000:.2f}万",
            "",
            f"日期:\t{date}",
            "",
            f"获利比例:\t{chip['profit_pct']:.2f}%",
            "",
            f"{chip['profit_pct']:.2f}%\t{loss_pct:.2f}%",
            "",
            f"平均成本:\t{chip['average_cost']:.2f}",
            "",
            f"90%成本:\t{chip['cost_90_low']:.2f}-{chip['cost_90_high']:.2f}",
            "",
            f"集中度:\t{chip['concentration_90_pct']:.2f}%",
            "",
            f"70%成本:\t{chip['cost_70_low']:.2f}-{chip['cost_70_high']:.2f}",
            "",
            f"集中度:\t{chip['concentration_70_pct']:.2f}%",
            "",
            f"5日涨幅: {five_day_pct:.2f}%",
            "",
            ", ".join(ma_items),
        ]
    )


def generate(stock: Stock) -> Path:
    warmup = read_rows(DATA / f"{stock.stem}_chip_warmup_209d.csv")
    requested = read_rows(DATA / f"{stock.stem}_kline_90d.csv")
    rows = checked_concat((warmup, requested))
    if (len(warmup), len(requested)) != (209, 90):
        raise ValueError(f"unexpected source row counts for {stock.code}")

    volume_seed = read_rows(DATA / f"{stock.stem}_volume_warmup_5d.csv")
    if len(volume_seed) != 5:
        raise ValueError(f"expected five volume warm-up sessions for {stock.code}")
    volumes = [float(row["volume_lots"]) for row in volume_seed]
    volumes.extend(float(row["volume_lots"]) for row in requested)

    start = len(warmup)
    blocks = [
        render_block(stock, rows, index, start, volumes)
        for index in range(start, len(rows))
    ]
    if len(blocks) != 90:
        raise AssertionError("output must contain exactly 90 daily blocks")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT / f"{stock.stem}_90d.txt"
    destination.write_text("\n\n\n".join(blocks) + "\n", encoding="utf-8")
    return destination


def main() -> None:
    for stock in STOCKS:
        destination = generate(stock)
        print(destination.relative_to(ROOT))


if __name__ == "__main__":
    main()
