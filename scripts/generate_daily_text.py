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
class Stock:
    code: str
    name: str
    actions: tuple[CorporateAction, ...]

    @property
    def stem(self) -> str:
        return f"{self.code}_{self.name}"


STOCKS = (
    Stock(
        "600398",
        "海澜之家",
        (
            CorporateAction(
                "2026-05-11",
                0.41,
                "现金分红除权；历史价格在该日及以后计算技术指标时前复权 0.41 元",
            ),
        ),
    ),
    Stock(
        "600690",
        "海尔智家",
        (
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


def signed(value: float, suffix: str = "") -> str:
    return f"{value:+.2f}{suffix}"


def money_text(yuan: float) -> str:
    if yuan >= 100_000_000:
        return f"{yuan / 100_000_000:.2f} 亿元（{yuan:.0f} 元）"
    return f"{yuan / 10_000:.2f} 万元（{yuan:.0f} 元）"


def trend(current: float, previous: float) -> str:
    if current > previous + 0.000_000_1:
        return "向上"
    if current < previous - 0.000_000_1:
        return "向下"
    return "持平"


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


def render_block(
    stock: Stock,
    raw_rows: list[dict[str, str | float]],
    target_index: int,
    target_start: int,
    volumes: list[float],
) -> str:
    raw = raw_rows[target_index]
    date = str(raw["date"])
    adjusted = adjusted_rows(raw_rows[: target_index + 1], stock, date)

    previous_raw = float(raw_rows[target_index - 1]["close"])
    previous_close, previous_label = reference_close(stock, date, previous_raw)
    close = float(raw["close"])
    change_amount = close - previous_close
    change_pct = change_amount / previous_close * 100
    amplitude_pct = (float(raw["high"]) - float(raw["low"])) / previous_close * 100
    limit_up = round_price(previous_close * 1.10)
    limit_down = round_price(previous_close * 0.90)

    # volumes starts with five seed sessions, so offset the requested-day index.
    requested_offset = target_index - target_start
    previous_five_volumes = volumes[requested_offset : requested_offset + 5]
    volume_ratio = float(raw["volume_lots"]) / average(previous_five_volumes)

    closes = [float(row["close"]) for row in adjusted]
    five_day_change = close - closes[-6]
    five_day_pct = five_day_change / closes[-6] * 100

    ma_lines = []
    for period in (5, 10, 20, 30, 60, 90):
        current_ma = average(closes[-period:])
        previous_ma = average(closes[-period - 1 : -1])
        ma_lines.append(f"  MA{period}：{current_ma:.2f}（{trend(current_ma, previous_ma)}）")

    chip_window = adjusted[-210:]
    chip = cyq(chip_window)

    unavailable = "N/A（历史 K 线不提供，未以当前值回填）"
    action_note = "无"
    for action in stock.actions:
        if date == action.ex_date:
            action_note = action.description

    return "\n".join(
        [
            "=" * 64,
            f"日期：{date}",
            f"股票名称：{stock.name}",
            f"股票代码：{stock.code}",
            "",
            "【价格行情】",
            f"最新价/收盘价：{close:.2f} 元",
            f"涨跌额：{signed(change_amount, ' 元')}",
            f"涨跌幅：{signed(change_pct, '%')}",
            f"开盘价：{float(raw['open']):.2f} 元",
            f"{previous_label}：{previous_close:.2f} 元",
            f"最高价：{float(raw['high']):.2f} 元",
            f"最低价：{float(raw['low']):.2f} 元",
            f"涨停价：{limit_up:.2f} 元",
            f"跌停价：{limit_down:.2f} 元",
            f"振幅：{amplitude_pct:.2f}%",
            f"除权说明：{action_note}",
            "",
            "【成交与估值】",
            f"换手率：{float(raw['turnover_rate_pct']):.2f}%",
            f"量比：{volume_ratio:.2f}",
            f"成交量：{float(raw['volume_lots']) / 10_000:.2f} 万手（{float(raw['volume_lots']):.0f} 手）",
            f"成交额：{money_text(float(raw['amount_yuan']))}",
            f"市盈率（PE）：{unavailable}",
            f"市净率（PB）：{unavailable}",
            f"总市值：{unavailable}",
            f"流通市值：{unavailable}",
            f"内盘：{unavailable}",
            f"外盘：{unavailable}",
            "",
            "【筹码分布】",
            f"获利比例：{chip['profit_pct']:.2f}%",
            f"平均成本：{chip['average_cost']:.2f} 元",
            f"90% 成本区间：{chip['cost_90_low']:.2f}–{chip['cost_90_high']:.2f} 元",
            f"90% 集中度：{chip['concentration_90_pct']:.2f}%",
            f"70% 成本区间：{chip['cost_70_low']:.2f}–{chip['cost_70_high']:.2f} 元",
            f"70% 集中度：{chip['concentration_70_pct']:.2f}%",
            "",
            "【5 日表现】",
            f"5 日涨跌额：{signed(five_day_change, ' 元')}",
            f"5 日涨跌幅：{signed(five_day_pct, '%')}",
            "",
            "【移动平均线】",
            *ma_lines,
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

    header = "\n".join(
        [
            f"{stock.name}（{stock.code}）90 个交易日逐日行情",
            "数据源：东方财富历史日 K 线；筹码分布由同源 OHLC/换手率按 CYQ 算法计算",
            f"日期范围：{requested[0]['date']} 至 {requested[-1]['date']}",
            "说明：无法从历史 K 线可靠取得的快照字段明确标为 N/A，不使用当前值冒充历史值。",
            "",
        ]
    )
    OUTPUT.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT / f"{stock.stem}_90d.txt"
    destination.write_text(header + "\n\n".join(blocks) + "\n", encoding="utf-8")
    return destination


def main() -> None:
    for stock in STOCKS:
        destination = generate(stock)
        print(destination.relative_to(ROOT))


if __name__ == "__main__":
    main()
