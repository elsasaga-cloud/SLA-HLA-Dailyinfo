#!/usr/bin/env python3
"""Fetch source K-lines for the requested stocks and rebuild outputs.

The fetch uses one EastMoney endpoint and the same fields/method for all
stocks.  It retrieves 459 sessions per stock (209 warm-up sessions plus the
requested 250 sessions) through four lmt-bounded windows anchored at the
same end date, keeping every single request small enough to stay reliable:

- A1: 11 fields, lmt=160 (most recent 160 requested sessions)
- A2: 11 fields, lmt=90  (earliest 90 requested sessions)
- B1: 7 fields,  lmt=160 (most recent 160 warm-up sessions)
- B2: 7 fields,  lmt=49  (earliest 49 warm-up sessions)

The windows of a common end date are nested, so the segments concatenate
without overlap.  The 7-field responses and the 11-field responses must
agree on the shared date windows, which is verified before storing.
Standard-library Python only.
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
    ("601600", "中国铝业", "1"),
    ("601988", "中国银行", "1"),
    ("600863", "华能蒙电", "1"),
    ("601138", "工业富联", "1"),
    ("000651", "格力电器", "0"),
    ("300124", "汇川技术", "0"),
    ("600362", "江西铜业", "1"),
    ("601899", "紫金矿业", "1"),
    ("300760", "迈瑞医疗", "0"),
    ("002352", "顺丰控股", "0"),
    ("000807", "云铝股份", "0"),
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
FIELDS7 = (
    "date",
    "open",
    "close",
    "high",
    "low",
    "volume_lots",
    "turnover_rate_pct",
)
FIELDS2_11 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
FIELDS2_7 = "f51,f52,f53,f54,f55,f56,f61"


def fetch_window(
    code: str, market: str, end_date: str, lmt: int, fields2: str
) -> list[list[str]]:
    query = urllib.parse.urlencode(
        {
            "secid": f"{market}.{code}",
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": fields2,
            "klt": "101",
            "fqt": "0",
            "end": end_date,
            "lmt": str(lmt),
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
    if len(rows) != lmt or any(len(row) != len(fields2.split(",")) for row in rows):
        raise RuntimeError(f"expected {lmt} complete sessions for {code}, got {len(rows)}")
    return rows


def write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row[field] for field in fieldnames} for row in rows)


def store(code: str, name: str, requested: list[dict[str, str]], warmup: list[dict[str, str]]) -> None:
    if len(requested) != 250 or len(warmup) != 209:
        raise AssertionError("unexpected split")
    stem = f"{code}_{name}"
    write_csv(
        DATA / f"{stem}_chip_warmup_209d.csv",
        ("date", "open", "close", "high", "low", "turnover_rate_pct"),
        warmup,
    )
    write_csv(DATA / f"{stem}_kline_250d.csv", FIELDS, requested)
    write_csv(
        DATA / f"{stem}_volume_warmup_5d.csv",
        ("date", "volume_lots"),
        warmup[-5:],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--end-date",
        default="20260929",
        help="last completed session as YYYYMMDD (default: %(default)s)",
    )
    args = parser.parse_args()
    if len(args.end_date) != 8 or not args.end_date.isdigit():
        parser.error("--end-date must be YYYYMMDD")

    for code, name, market in STOCKS:
        # Four lmt-bounded windows anchored at the same end date.  Windows
        # with a common end date are nested: the lmt=160 responses hold the
        # most recent 160 sessions and the smaller lmt responses hold the
        # earliest ones, so each pair concatenates without overlap.
        a1 = fetch_window(code, market, args.end_date, 160, FIELDS2_11)
        a2 = fetch_window(code, market, args.end_date, 90, FIELDS2_11)
        b1 = fetch_window(code, market, args.end_date, 160, FIELDS2_7)
        b2 = fetch_window(code, market, args.end_date, 49, FIELDS2_7)
        if [row[0] for row in a1] != [row[0] for row in b1]:
            raise RuntimeError(f"date mismatch between 11-field and 7-field windows for {code}")
        requested = [dict(zip(FIELDS, row, strict=True)) for row in a2 + a1]
        warmup_rows = b2 + b1
        warmup = [dict(zip(FIELDS7, row, strict=True)) for row in warmup_rows]
        store(code, name, requested, warmup)
        print(f"fetched {code} {name}")

    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_daily_text.py")],
        cwd=ROOT,
        check=True,
    )


if __name__ == "__main__":
    main()
