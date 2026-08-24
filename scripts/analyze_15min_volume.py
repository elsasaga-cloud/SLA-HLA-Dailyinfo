#!/usr/bin/env python3
"""Generate the repository's 15-minute volume-anomaly datasets and reports.

The implementation follows the user-supplied workflow: 20 historical sessions
plus the latest session, Sina 15-minute bars as the primary source, fixed
16-slot analysis, volume anomaly alerts, a latest-session flash report, and a
volume-only operation guide.  Volumes are normalized from shares to lots.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import statistics
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "15min"
HIST_DAYS = 20


@dataclass(frozen=True)
class Stock:
    code: str
    name: str
    market: int

    @property
    def symbol(self) -> str:
        return ("sh" if self.market == 1 else "sz") + self.code

    @property
    def stem(self) -> str:
        return f"{self.code}_{self.name}"


STOCKS = (
    Stock("000651", "格力电器", 0),
    Stock("000807", "云铝股份", 0),
    Stock("000858", "五粮液", 0),
    Stock("002352", "顺丰控股", 0),
    Stock("300124", "汇川技术", 0),
    Stock("300760", "迈瑞医疗", 0),
    Stock("600362", "江西铜业", 1),
    Stock("600398", "海澜之家", 1),
    Stock("600690", "海尔智家", 1),
    Stock("600863", "华能蒙电", 1),
    Stock("601138", "工业富联", 1),
    Stock("601600", "中国铝业", 1),
    Stock("601899", "紫金矿业", 1),
    Stock("601988", "中国银行", 1),
)

CFG = {
    "bar_surge_ratio": 2.0,
    "bar_extreme_ratio": 4.0,
    "bar_shrink_ratio": 0.4,
    "open_surge_ratio": 1.8,
    "open_extreme_ratio": 3.0,
    "open_shrink_ratio": 0.5,
    "day_surge_ratio": 1.6,
    "day_extreme_ratio": 2.5,
    "day_shrink_ratio": 0.6,
    "consecutive_surge_days": 3,
    "pct_change_alert": 50.0,
    "tail_surge_ratio": 2.0,
    "price_vol_diverge_pct": 1.0,
}

SHORT_LABELS = (
    "09:45", "10:00", "10:15", "10:30", "10:45", "11:00", "11:15", "11:30",
    "13:15", "13:30", "13:45", "14:00", "14:15", "14:30", "14:45", "15:00",
)
SLOT_DESCS = (
    "09:30-09:45 开盘1", "09:45-10:00 开盘2", "10:00-10:15", "10:15-10:30",
    "10:30-10:45", "10:45-11:00", "11:00-11:15", "11:15-11:30 午前收",
    "13:00-13:15 午后开1", "13:15-13:30 午后开2", "13:30-13:45", "13:45-14:00",
    "14:00-14:15", "14:15-14:30", "14:30-14:45 尾盘1", "14:45-15:00 尾盘2",
)
SLOT_MAP = {label: index for index, label in enumerate(SHORT_LABELS)}


def fetch_sina(stock: Stock, datalen: int = 1000) -> list[dict[str, object]]:
    callback = f"arena_{stock.symbol}_15"
    query = urllib.parse.urlencode(
        {"symbol": stock.symbol, "scale": "15", "datalen": str(datalen), "ma": "no"}
    )
    url = (
        "https://quotes.sina.com.cn/cn/api/jsonp_v2.php/"
        f"{callback}/CN_MarketDataService.getKLineData?{query}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8")
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise RuntimeError(f"Sina response could not be parsed for {stock.code}")
    return json.loads(match.group())


def normalize_rows(items: list[dict[str, object]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in items:
        stamp = str(item.get("day") or item.get("d") or "").strip()
        try:
            parsed = dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        label = parsed.strftime("%H:%M")
        if label not in SLOT_MAP:
            continue
        modern = "day" in item or "volume" in item or "amount" in item
        raw_volume = float(item.get("volume", item.get("v", 0)) or 0)
        raw_amount = float(item.get("amount", item.get("a", 0)) or 0)
        rows.append(
            {
                "datetime": parsed.strftime("%Y-%m-%d %H:%M:%S"),
                "date": parsed.strftime("%Y-%m-%d"),
                "time_str": label,
                "slot_idx": SLOT_MAP[label],
                "open": float(item.get("open", item.get("o", 0)) or 0),
                "close": float(item.get("close", item.get("c", 0)) or 0),
                "high": float(item.get("high", item.get("h", 0)) or 0),
                "low": float(item.get("low", item.get("l", 0)) or 0),
                "volume": raw_volume / 100.0 if modern else raw_volume,
                "amount": raw_amount if modern or raw_amount >= 1e8 else raw_amount * 10000,
            }
        )
    rows.sort(key=lambda row: str(row["datetime"]))
    deduplicated = {str(row["datetime"]): row for row in rows}
    return [deduplicated[key] for key in sorted(deduplicated)]


def keep_recent(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    dates = sorted({str(row["date"]) for row in rows})
    if len(dates) < HIST_DAYS + 1:
        raise ValueError(f"need {HIST_DAYS + 1} sessions, got {len(dates)}")
    selected = set(dates[-(HIST_DAYS + 1) :])
    result = [row for row in rows if str(row["date"]) in selected]
    counts = defaultdict(int)
    for row in result:
        counts[str(row["date"])] += 1
    if any(counts[date] != 16 for date in selected):
        raise ValueError(f"incomplete 15-minute session: {dict(counts)}")
    return result


def by_date(rows: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        result[str(row["date"])].append(row)
    for values in result.values():
        values.sort(key=lambda row: int(row["slot_idx"]))
    return dict(sorted(result.items()))


def mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def slot_values(day_rows: list[dict[str, object]], field: str) -> dict[int, float]:
    return {int(row["slot_idx"]): float(row[field]) for row in day_rows}


def build_day_summaries(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    summaries = []
    for date, day_rows in by_date(rows).items():
        volumes = slot_values(day_rows, "volume")
        amounts = slot_values(day_rows, "amount")
        record: dict[str, object] = {"date": date}
        for slot, label in enumerate(SHORT_LABELS):
            record[f"vol_{label}"] = round(volumes.get(slot, 0))
            record[f"amt_{label}"] = round(amounts.get(slot, 0) / 10000, 1)
        record.update(
            day_vol=round(sum(volumes.values())),
            day_amt_wan=round(sum(amounts.values()) / 10000, 1),
            open_price=float(day_rows[0]["open"]),
            close_price=float(day_rows[-1]["close"]),
        )
        summaries.append(record)
    return summaries


def calculate(rows: list[dict[str, object]]) -> dict[str, object]:
    days = by_date(rows)
    dates = list(days)
    slot_means = {
        slot: mean([float(row["volume"]) for row in rows if int(row["slot_idx"]) == slot])
        for slot in range(16)
    }
    day_totals = {date: sum(float(row["volume"]) for row in values) for date, values in days.items()}
    day_amounts = {date: sum(float(row["amount"]) for row in values) for date, values in days.items()}
    open_30 = {}
    tail_30 = {}
    for date, values in days.items():
        volumes = slot_values(values, "volume")
        open_30[date] = volumes.get(0, 0) + volumes.get(1, 0)
        tail_30[date] = volumes.get(14, 0) + volumes.get(15, 0)
    return {
        "days": days,
        "dates": dates,
        "slot_means": slot_means,
        "day_totals": day_totals,
        "day_amounts": day_amounts,
        "day_mean": mean(list(day_totals.values())),
        "day_std": statistics.stdev(day_totals.values()) if len(day_totals) > 1 else 0,
        "open_30": open_30,
        "open_mean": mean(list(open_30.values())),
        "tail_30": tail_30,
        "tail_mean": mean(list(tail_30.values())),
    }


def analyze(stock: Stock, stats: dict[str, object]) -> tuple[str, list[dict[str, str]]]:
    days = stats["days"]
    dates = stats["dates"]
    slot_means = stats["slot_means"]
    day_totals = stats["day_totals"]
    day_mean = float(stats["day_mean"])
    open_30 = stats["open_30"]
    open_mean = float(stats["open_mean"])
    tail_30 = stats["tail_30"]
    tail_mean = float(stats["tail_mean"])
    alerts: list[dict[str, str]] = []
    lines = [
        f"{stock.name}({stock.code}) 15分钟K线 Day-by-Day 分析报告",
        f"数据区间: {dates[0]} ~ {dates[-1]}  共{len(dates)}个交易日（历史{HIST_DAYS}日 + 最新日）",
        f"日均量={day_mean:,.0f}手  开盘30min均={open_mean:,.0f}手  尾盘30min均={tail_mean:,.0f}手",
        "",
        "[上午 09:30-11:30]",
        f"{'日期':>12} | " + " | ".join(f"{label:>7}" for label in SHORT_LABELS[:8]) + " | 开30m | 日/均 | 信号",
    ]
    previous: dict[int, float] | None = None
    consecutive = 0
    for date in dates:
        values = days[date]
        volumes = slot_values(values, "volume")
        day_ratio = day_totals[date] / day_mean if day_mean else 1
        open_ratio = open_30[date] / open_mean if open_mean else 1
        signals = []
        cells = []
        for slot in range(8):
            volume = volumes.get(slot, 0)
            ratio = volume / slot_means[slot] if slot_means[slot] else 1
            tag = "*" if ratio >= CFG["bar_extreme_ratio"] else "+" if ratio >= CFG["bar_surge_ratio"] else "-" if ratio <= CFG["bar_shrink_ratio"] else ""
            cells.append(f"{volume:>6,.0f}{tag}")
        if previous:
            for slot in range(16):
                old, current = previous.get(slot, 0), volumes.get(slot, 0)
                if old:
                    change = (current / old - 1) * 100
                    if abs(change) >= CFG["pct_change_alert"]:
                        alerts.append({"date": date, "severity": "高" if abs(change) >= 100 else "中", "type": "跳变", "slot": SHORT_LABELS[slot], "detail": f"同槽较前日{change:+.1f}% ({old:,.0f}->{current:,.0f}手)"})
        if open_ratio >= CFG["open_extreme_ratio"]:
            signals.append(f"开盘极端放量x{open_ratio:.1f}"); consecutive += 1
            alerts.append({"date": date, "severity": "极端", "type": "开盘极端放量", "slot": "09:30", "detail": f"开盘30min为均量{open_ratio:.2f}倍"})
        elif open_ratio >= CFG["open_surge_ratio"]:
            signals.append(f"开盘放量x{open_ratio:.1f}"); consecutive += 1
        else:
            consecutive = 0
            if open_ratio <= CFG["open_shrink_ratio"]: signals.append(f"开盘缩量x{open_ratio:.1f}")
        if consecutive >= CFG["consecutive_surge_days"]:
            signals.append(f"连续{consecutive}日开盘放量")
        if day_ratio >= CFG["day_extreme_ratio"]:
            signals.append(f"日极端放量x{day_ratio:.1f}")
            alerts.append({"date": date, "severity": "极端", "type": "日极端放量", "slot": "全天", "detail": f"日量{day_totals[date]:,.0f}手，为均量{day_ratio:.2f}倍"})
        elif day_ratio >= CFG["day_surge_ratio"]:
            signals.append(f"日放量x{day_ratio:.1f}")
        elif day_ratio <= CFG["day_shrink_ratio"]:
            signals.append(f"日缩量x{day_ratio:.1f}")
        tail_ratio = tail_30[date] / tail_mean if tail_mean else 1
        if tail_ratio >= CFG["tail_surge_ratio"]:
            signals.append(f"尾盘放量x{tail_ratio:.1f}")
            alerts.append({"date": date, "severity": "高" if tail_ratio >= 3 else "中", "type": "尾盘放量", "slot": "14:30", "detail": f"尾盘30min为均量{tail_ratio:.2f}倍"})
        price_change = (float(values[-1]["close"]) / float(values[0]["open"]) - 1) * 100
        if abs(price_change) >= CFG["price_vol_diverge_pct"]:
            if price_change > 0 and day_ratio < 0.9:
                signals.append("量价背离(涨缩)")
                alerts.append({"date": date, "severity": "中", "type": "量价背离", "slot": "全天", "detail": f"价涨{price_change:.1f}%但量比仅{day_ratio:.2f}"})
            elif price_change < 0 and day_ratio > 1.1:
                signals.append("量价背离(跌涨)")
                alerts.append({"date": date, "severity": "高", "type": "量价背离", "slot": "全天", "detail": f"价跌{price_change:.1f}%但量比达{day_ratio:.2f}"})
        first_ratio = volumes[0] / slot_means[0] if slot_means[0] else 1
        if first_ratio >= CFG["bar_extreme_ratio"]:
            signals.append("首根15min极端放量")
            alerts.append({"date": date, "severity": "极端", "type": "首根极端放量", "slot": "09:45", "detail": f"首根为槽均{first_ratio:.2f}倍"})
        lines.append(f"{date:>12} | " + " | ".join(cells) + f" | {open_30[date]:>6,.0f} | {day_ratio:>4.2f}x | {' | '.join(signals)}")
        previous = volumes
    lines += ["", "[下午 13:00-15:00]", f"{'日期':>12} | " + " | ".join(f"{label:>7}" for label in SHORT_LABELS[8:]) + " | 尾30m | 日/均"]
    for date in dates:
        volumes = slot_values(days[date], "volume")
        cells = []
        for slot in range(8, 16):
            ratio = volumes[slot] / slot_means[slot] if slot_means[slot] else 1
            tag = "*" if ratio >= CFG["bar_extreme_ratio"] else "+" if ratio >= CFG["bar_surge_ratio"] else "-" if ratio <= CFG["bar_shrink_ratio"] else ""
            cells.append(f"{volumes[slot]:>6,.0f}{tag}")
        lines.append(f"{date:>12} | " + " | ".join(cells) + f" | {tail_30[date]:>6,.0f} | {day_totals[date]/day_mean:>4.2f}x")
    lines += ["", "标记: *=极端>=4倍  +=放量>=2倍  -=缩量<=0.4倍  量=手", "", f"[异动预警汇总] 共{len(alerts)}条"]
    for alert in sorted(alerts, key=lambda item: item["date"], reverse=True):
        lines.append(f"[{alert['date']}] [{alert['severity']}] {alert['type']:<12} {alert['slot']:>6}  {alert['detail']}")
    return "\n".join(lines) + "\n", alerts


def operation_guide(stock: Stock, stats: dict[str, object], alerts: list[dict[str, str]]) -> str:
    dates = stats["dates"]
    latest = dates[-1]
    day_totals = stats["day_totals"]
    day_mean = float(stats["day_mean"])
    open_30 = stats["open_30"]
    open_mean = float(stats["open_mean"])
    tail_30 = stats["tail_30"]
    tail_mean = float(stats["tail_mean"])
    days = stats["days"]
    recent5 = [day_totals[date] for date in dates[-5:]]
    ratio5 = mean(recent5) / day_mean if day_mean else 1
    first_segment = mean([day_totals[date] for date in dates[:7]])
    last_segment = mean([day_totals[date] for date in dates[-7:]])
    trend = (last_segment / first_segment - 1) * 100 if first_segment else 0
    latest_open_ratio = open_30[latest] / open_mean if open_mean else 1
    latest_tail_ratio = tail_30[latest] / tail_mean if tail_mean else 1
    latest_rows = days[latest]
    price_change = (float(latest_rows[-1]["close"]) / float(latest_rows[0]["open"]) - 1) * 100
    divergences = [alert for alert in alerts if alert["type"] == "量价背离"]
    score, details = 0, []
    if ratio5 > 1.3: score += 2; details.append(f"+2 近5日放量({ratio5:.2f}x)")
    elif ratio5 > 1.1: score += 1; details.append(f"+1 近5日略放量({ratio5:.2f}x)")
    elif ratio5 < 0.7: score -= 2; details.append(f"-2 近5日明显缩量({ratio5:.2f}x)")
    elif ratio5 < 0.9: score -= 1; details.append(f"-1 近5日略缩量({ratio5:.2f}x)")
    else: details.append(f" 0 近5日量能正常({ratio5:.2f}x)")
    if trend > 20: score += 2; details.append(f"+2 分段量能上升({trend:+.1f}%)")
    elif trend > 5: score += 1; details.append(f"+1 分段量能温和上升({trend:+.1f}%)")
    elif trend < -20: score -= 2; details.append(f"-2 分段量能下降({trend:+.1f}%)")
    elif trend < -5: score -= 1; details.append(f"-1 分段量能温和下降({trend:+.1f}%)")
    else: details.append(f" 0 分段量能平稳({trend:+.1f}%)")
    if latest_open_ratio >= CFG["open_extreme_ratio"]: score += 3; details.append(f"+3 最新开盘极端放量({latest_open_ratio:.2f}x)")
    elif latest_open_ratio >= CFG["open_surge_ratio"]: score += 2; details.append(f"+2 最新开盘放量({latest_open_ratio:.2f}x)")
    elif latest_open_ratio <= CFG["open_shrink_ratio"]: score -= 2; details.append(f"-2 最新开盘缩量({latest_open_ratio:.2f}x)")
    else: details.append(f" 0 最新开盘正常({latest_open_ratio:.2f}x)")
    if len(divergences) >= 3: score -= 2; details.append(f"-2 量价背离频繁({len(divergences)}次)")
    elif divergences: score -= 1; details.append(f"-1 存在量价背离({len(divergences)}次)")
    else: score += 1; details.append("+1 无量价背离")
    if score >= 5: rating, action = "强烈看多", "持有；仅在回调缩量且基本面允许时考虑加仓"
    elif score >= 2: rating, action = "温和看多/持有", "维持仓位，等待持续放量确认"
    elif score >= -1: rating, action = "中性/观望", "按兵不动，等待量能方向明朗"
    elif score >= -3: rating, action = "温和看空/谨慎", "轻仓观望，避免追涨"
    else: rating, action = "看空/离场", "控制仓位并执行既定止损纪律"
    slot_means = stats["slot_means"]
    latest_volumes = slot_values(latest_rows, "volume")
    lines = [
        f"{stock.name}({stock.code}) 成交量维度 操作指引",
        f"数据区间: {dates[0]} ~ {latest}  历史{HIST_DAYS}日 + 最新完整交易日",
        "本报告仅基于成交量，不含基本面或消息面，不构成投资建议。",
        "",
        f"综合评级: 【{rating}】",
        f"综合得分: {score:+d}",
        f"一句话操作: {action}",
        "",
        "评分明细:",
        *[f"  {detail}" for detail in details],
        "",
        f"[最新交易日速报：{latest}]",
        f"全天成交量: {day_totals[latest]:,.0f}手，日均比={day_totals[latest]/day_mean:.2f}x",
        f"开盘30min: {open_30[latest]:,.0f}手，开盘均比={latest_open_ratio:.2f}x",
        f"尾盘30min: {tail_30[latest]:,.0f}手，尾盘均比={latest_tail_ratio:.2f}x",
        f"开盘价={float(latest_rows[0]['open']):.2f}，收盘价={float(latest_rows[-1]['close']):.2f}，日内变化={price_change:+.2f}%",
        "",
        "逐槽量能:",
    ]
    for slot, description in enumerate(SLOT_DESCS):
        ratio = latest_volumes[slot] / slot_means[slot] if slot_means[slot] else 1
        label = "极端放量" if ratio >= 4 else "放量" if ratio >= 2 else "缩量" if ratio <= 0.4 else "正常"
        lines.append(f"  {description:>24}  {latest_volumes[slot]:>10,.0f}手  {ratio:>5.2f}x  {label}")
    lines += ["", "关键信号:", "  做多观察: 放量且价格同步上涨；连续开盘放量。", "  风险观察: 放量下跌；量价背离；天量后低开。", "  缩量阶段: 避免仅凭成交量追涨杀跌。", ""]
    return "\n".join(lines)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row[field] for field in fieldnames} for row in rows)


def generate_stock(stock: Stock, items: list[dict[str, object]], output: Path) -> list[Path]:
    rows = keep_recent(normalize_rows(items))
    destination = output / stock.stem
    destination.mkdir(parents=True, exist_ok=True)
    raw_path = destination / f"{stock.code}_15min_raw.csv"
    day_path = destination / f"{stock.code}_day_summary.csv"
    report_path = destination / f"{stock.code}_15min_分析报告.txt"
    alert_path = destination / f"{stock.code}_异动预警.txt"
    guide_path = destination / "成交量维度_操作指引.txt"
    raw_fields = ["datetime", "open", "close", "high", "low", "volume", "amount", "date", "time_str", "slot_idx"]
    write_csv(raw_path, raw_fields, rows)
    summaries = build_day_summaries(rows)
    write_csv(day_path, list(summaries[0]), summaries)
    stats = calculate(rows)
    report, alerts = analyze(stock, stats)
    report_path.write_text(report, encoding="utf-8")
    alert_lines = [f"{stock.name} 异动预警  数据区间 {stats['dates'][0]} ~ {stats['dates'][-1]}  共{len(alerts)}条", ""]
    alert_lines.extend(f"[{a['date']}] [{a['severity']}] {a['type']:<12} {a['slot']:>6}  {a['detail']}" for a in sorted(alerts, key=lambda item: item["date"], reverse=True))
    alert_path.write_text("\n".join(alert_lines) + "\n", encoding="utf-8")
    guide_path.write_text(operation_guide(stock, stats, alerts), encoding="utf-8")
    return [raw_path, day_path, report_path, alert_path, guide_path]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-json-dir", type=Path, help="directory containing cached <code>.json Sina responses")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    generated: list[Path] = []
    session_ranges: list[tuple[str, str, int]] = []
    for stock in STOCKS:
        if args.source_json_dir:
            items = json.loads((args.source_json_dir / f"{stock.code}.json").read_text(encoding="utf-8"))
        else:
            items = fetch_sina(stock)
        paths = generate_stock(stock, items, args.output)
        generated.extend(paths)
        with paths[0].open(encoding="utf-8-sig", newline="") as handle:
            dates = sorted({row["date"] for row in csv.DictReader(handle)})
        session_ranges.append((dates[0], dates[-1], len(dates)))
        print(f"generated {stock.code} {stock.name}: {len(paths)} files")
    if len(set(session_ranges)) != 1:
        raise ValueError(f"stocks do not share one session range: {session_ranges}")
    first_session, latest_session, session_count = session_ranges[0]
    metadata = {
        "as_of": latest_session,
        "source": "Sina CN_MarketDataService.getKLineData",
        "scale_minutes": 15,
        "volume_unit": "lots (Sina shares divided by 100)",
        "amount_unit": "yuan",
        "sessions": session_count,
        "historical_sessions": HIST_DAYS,
        "first_session": first_session,
        "latest_session": latest_session,
        "slot_labels": list(SHORT_LABELS),
        "thresholds": CFG,
        "stocks": [{"code": stock.code, "name": stock.name} for stock in STOCKS],
        "files": [],
    }
    for path in sorted(generated):
        data = path.read_bytes()
        metadata["files"].append({
            "path": path.relative_to(ROOT).as_posix(),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    (args.output / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
