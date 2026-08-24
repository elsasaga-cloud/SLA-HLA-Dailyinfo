#!/usr/bin/env python3
"""Fetch source K-lines for the requested stocks and rebuild outputs.

The fetch uses one EastMoney endpoint and the same fields/method for all
stocks.  It retrieves 299 sessions: 209 warm-up sessions plus the requested
90 sessions.  Standard-library Python only.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "eastmoney"
ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
STOCKS = (
    ("000858", "五粮液", "0"),
    ("600398", "海澜之家", "1"),
    ("600690", "海尔智家", "1"),
)
FIELDS = (
    "date",
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
)


def fetch(code: str, market: str, end_date: str) -> list[list[str]]:
    query = urllib.parse.urlencode(
        {
            "secid": f"{market}.{code}",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": "101",
            "fqt": "0",
            "end": end_date,
            "lmt": "299",
        }
    )
    request = urllib.request.Request(
        f"{ENDPOINT}?{query}", headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("rc") != 0 or not payload.get("data"):
        raise RuntimeError(f"EastMoney returned no data for {code}: {payload!r}")
    rows = [line.split(",") for line in payload["data"]["klines"]]
    if len(rows) != 299 or any(len(row) != len(FIELDS) for row in rows):
        raise RuntimeError(f"expected 299 complete sessions for {code}, got {len(rows)}")
    return rows


def write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row[field] for field in fieldnames} for row in rows)


def store(code: str, name: str, values: list[list[str]]) -> None:
    rows = [dict(zip(FIELDS, row, strict=True)) for row in values]
    warmup, requested = rows[:209], rows[209:]
    if len(warmup) != 209 or len(requested) != 90:
        raise AssertionError("unexpected split")
    stem = f"{code}_{name}"
    write_csv(
        DATA / f"{stem}_chip_warmup_209d.csv",
        ("date", "open", "close", "high", "low", "turnover_rate_pct"),
        warmup,
    )
    write_csv(DATA / f"{stem}_kline_90d.csv", FIELDS, requested)
    write_csv(
        DATA / f"{stem}_volume_warmup_5d.csv",
        ("date", "volume_lots"),
        warmup[-5:],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--end-date",
        default="20260821",
        help="last completed session as YYYYMMDD (default: %(default)s)",
    )
    args = parser.parse_args()
    if len(args.end_date) != 8 or not args.end_date.isdigit():
        parser.error("--end-date must be YYYYMMDD")

    for code, name, market in STOCKS:
        store(code, name, fetch(code, market, args.end_date))
        print(f"fetched {code} {name}")

    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_daily_text.py")],
        cwd=ROOT,
        check=True,
    )


if __name__ == "__main__":
    main()
