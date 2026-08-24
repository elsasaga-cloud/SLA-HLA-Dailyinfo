#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从腾讯证券单一数据源更新海澜之家、海尔智家的前复权日 K 数据。"""

from __future__ import annotations

import argparse
import csv
import json
import random
import time
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

STOCKS = {
    "sh600398": "海澜之家",
    "sh600690": "海尔智家",
}
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data" / "tencent_qfq"
TENCENT_ENDPOINT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def default_end_date() -> date:
    """盘中不写入未收盘 K 线；周末/休市日由腾讯自动回退到最近交易日。"""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if now.weekday() < 5 and now.time() < dt_time(16, 0):
        return now.date() - timedelta(days=1)
    return now.date()


def build_url(symbol: str, end_date: date, count: int) -> str:
    param = f"{symbol},day,,{end_date.isoformat()},{count},qfq"
    return f"{TENCENT_ENDPOINT}?param={quote(param, safe=',')}"


def http_get_json(url: str, retries: int = 3, timeout: int = 20) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": "https://gu.qq.com/",
                    "Accept": "application/json,text/plain,*/*",
                    "Connection": "close",
                },
            )
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8-sig"))
            if payload.get("code") != 0:
                raise RuntimeError(f"腾讯接口返回错误: {payload!r}")
            return payload
        except Exception as exc:  # 网络错误按同一来源重试，不切换数据源
            last_error = exc
            if attempt + 1 < retries:
                time.sleep((2**attempt) + random.uniform(0.2, 0.8))
    raise RuntimeError(f"腾讯接口连续 {retries} 次请求失败: {last_error}")


def parse_rows(payload: dict[str, Any], symbol: str, count: int) -> list[list[str]]:
    stock_data = (payload.get("data") or {}).get(symbol) or {}
    source_rows = stock_data.get("qfqday") or []
    rows: list[list[str]] = []

    for source_row in source_rows:
        if not isinstance(source_row, list) or len(source_row) < 6:
            continue
        # 腾讯字段顺序：日期、开盘、收盘、最高、最低、成交量（手）。
        day, open_, close, high, low, volume_lots = source_row[:6]
        rows.append(
            [
                str(day),
                f"{float(open_):.3f}",
                f"{float(close):.3f}",
                f"{float(high):.3f}",
                f"{float(low):.3f}",
                str(int(float(volume_lots))),
            ]
        )

    rows = rows[-count:]
    if len(rows) != count:
        raise ValueError(f"{symbol} 期望 {count} 条，腾讯仅返回 {len(rows)} 条")
    if rows != sorted(rows, key=lambda row: row[0]):
        raise ValueError(f"{symbol} 日期不是严格升序")
    if len({row[0] for row in rows}) != count:
        raise ValueError(f"{symbol} 存在重复交易日")

    for day, open_, close, high, low, volume_lots in rows:
        prices = [float(open_), float(close), float(high), float(low)]
        if float(high) < max(prices) or float(low) > min(prices):
            raise ValueError(f"{symbol} {day} OHLC 范围无效")
        if int(volume_lots) < 0:
            raise ValueError(f"{symbol} {day} 成交量为负数")
    return rows


def write_csv(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(["date", "open", "close", "high", "low", "volume_lots"])
        writer.writerows(rows)
    temp_path.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="仅使用腾讯证券源抓取海澜之家和海尔智家的前复权日 K 数据"
    )
    parser.add_argument("--count", type=int, default=90, help="每只股票的交易日数量（默认 90）")
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=None,
        metavar="YYYY-MM-DD",
        help="截止日期；默认自动避开尚未收盘的当日 K 线",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"CSV 输出目录（默认 {DEFAULT_OUTPUT_DIR}）",
    )
    args = parser.parse_args()

    if args.count <= 0:
        parser.error("--count 必须大于 0")
    end_date = args.end_date or default_end_date()

    for symbol, stock_name in STOCKS.items():
        url = build_url(symbol, end_date, args.count)
        payload = http_get_json(url)
        rows = parse_rows(payload, symbol, args.count)
        stock_code = symbol[2:]
        output_path = args.output_dir / f"{stock_code}_{stock_name}_{args.count}d.csv"
        write_csv(output_path, rows)
        print(
            f"{stock_code} {stock_name}: {len(rows)} 条，"
            f"{rows[0][0]} 至 {rows[-1][0]} -> {output_path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
