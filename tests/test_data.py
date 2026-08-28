from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "generate_daily_text", ROOT / "scripts" / "generate_daily_text.py"
)
assert SPEC and SPEC.loader
GENERATOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GENERATOR
SPEC.loader.exec_module(GENERATOR)


class DataValidationTests(unittest.TestCase):
    def test_source_counts_dates_and_ohlc(self) -> None:
        for stock in GENERATOR.STOCKS:
            target = GENERATOR.read_rows(
                GENERATOR.DATA / f"{stock.stem}_kline_90d.csv"
            )
            warmup = GENERATOR.read_rows(
                GENERATOR.DATA / f"{stock.stem}_chip_warmup_209d.csv"
            )
            volume_seed = GENERATOR.read_rows(
                GENERATOR.DATA / f"{stock.stem}_volume_warmup_5d.csv"
            )
            self.assertEqual(90, len(target))
            self.assertEqual(209, len(warmup))
            self.assertEqual(5, len(volume_seed))
            self.assertEqual("2026-04-21", target[0]["date"])
            self.assertEqual("2026-08-28", target[-1]["date"])
            self.assertEqual("2025-06-11", warmup[0]["date"])
            self.assertEqual("2026-04-20", warmup[-1]["date"])
            self.assertEqual(
                [row["date"] for row in warmup[-5:]],
                [row["date"] for row in volume_seed],
            )
            all_rows = warmup + target
            dates = [str(row["date"]) for row in all_rows]
            self.assertEqual(sorted(dates), dates)
            self.assertEqual(len(dates), len(set(dates)))
            for row in all_rows:
                self.assertLessEqual(float(row["low"]), float(row["open"]))
                self.assertLessEqual(float(row["low"]), float(row["close"]))
                self.assertGreaterEqual(float(row["high"]), float(row["open"]))
                self.assertGreaterEqual(float(row["high"]), float(row["close"]))
                self.assertGreaterEqual(float(row["turnover_rate_pct"]), 0)
            for row in target:
                self.assertGreater(float(row["volume_lots"]), 0)
                self.assertGreater(float(row["amount_yuan"]), 0)

    def test_hailan_chip_values_match_reference_samples(self) -> None:
        stock = next(stock for stock in GENERATOR.STOCKS if stock.code == "600398")
        rows = GENERATOR.read_rows(
            GENERATOR.DATA / f"{stock.stem}_chip_warmup_209d.csv"
        ) + GENERATOR.read_rows(GENERATOR.DATA / f"{stock.stem}_kline_90d.csv")
        expected = {
            "2026-07-28": (74.28, 5.88, 5.50, 6.21, 5.68, 6.12),
            "2026-08-06": (74.36, 5.90, 5.51, 6.24, 5.69, 6.14),
            "2026-08-07": (73.54, 5.90, 5.51, 6.24, 5.69, 6.14),
            "2026-08-21": (50.45, 5.90, 5.52, 6.24, 5.69, 6.14),
        }
        for date, wanted in expected.items():
            index = next(i for i, row in enumerate(rows) if row["date"] == date)
            adjusted = GENERATOR.adjusted_rows(rows[: index + 1], stock, date)
            result = GENERATOR.cyq(adjusted[-210:])
            actual = (
                result["profit_pct"],
                result["average_cost"],
                result["cost_90_low"],
                result["cost_90_high"],
                result["cost_70_low"],
                result["cost_70_high"],
            )
            self.assertEqual(wanted, tuple(round(value, 2) for value in actual))

    def test_text_outputs_have_90_exactly_formatted_blocks(self) -> None:
        required = (
            "股票数据： ",
            "今开: ",
            "涨停价: ",
            "动态市盈率: ",
            "总市值: ",
            "内盘: ",
            "外盘: ",
            "获利比例:\t",
            "90%成本:\t",
            "70%成本:\t",
            "5日涨幅: ",
            "MA90: ",
        )
        for stock in GENERATOR.STOCKS:
            text = (
                GENERATOR.OUTPUT / f"{stock.stem}_90d.txt"
            ).read_text(encoding="utf-8")
            self.assertEqual(90, text.count("Date: "))
            self.assertEqual(90, text.count(f"股票数据： {stock.name} {stock.code}"))
            for marker in required:
                self.assertEqual(90, text.count(marker), marker)
            for period in (5, 10, 20, 30, 40, 50, 60, 70, 80, 90):
                self.assertEqual(90, text.count(f"MA{period}: "), f"MA{period}")
            self.assertNotIn("N/A", text)
            self.assertNotIn("nan", text.lower())
            self.assertNotIn("none", text.lower())

    def test_supplied_2026_08_21_block(self) -> None:
        text = (GENERATOR.OUTPUT / "600398_海澜之家_90d.txt").read_text(
            encoding="utf-8"
        )
        start = text.index("Date: 2026-08-21")
        end = text.index("\n\n\nDate:", start)
        block = text[start:end].strip()
        expected = """Date: 2026-08-21

股票数据： 海澜之家 600398 20260821

5.90

-0.11  -1.83%

今开: 6.01\t昨收: 6.01\t最高价: 6.02\t最低价: 5.87

涨停价: 6.61\t跌停价: 5.41\t换手率: 0.42%\t量比: 0.76

成交量: 20.08万\t成交额: 1.19亿\t动态市盈率: 7.46\t市净率: 1.64

总市值: 283亿\t流通市值: 283亿\t振幅: 2.50%\t内盘: 10.87万

外盘: 9.21万

日期:\t2026-08-21

获利比例:\t50.45%

50.45%\t49.55%

平均成本:\t5.90

90%成本:\t5.52-6.24

集中度:\t6.12%

70%成本:\t5.69-6.14

集中度:\t3.80%

5日涨幅: 0.33%

MA5: 5.94 (上升), MA10: 6.00 (下降), MA20: 6.07 (上升), MA30: 6.00 (上升), MA40: 5.87 (上升), MA50: 5.83 (上升), MA60: 5.82 (上升), MA70: 5.83 (下降), MA80: 5.91 (下降), MA90: 5.98 (下降)"""
        self.assertEqual(expected, block)

    def test_wuliangye_2026_08_21_core_fields(self) -> None:
        text = (GENERATOR.OUTPUT / "000858_五粮液_90d.txt").read_text(
            encoding="utf-8"
        )
        block = text[text.index("Date: 2026-08-21") :]
        expected_lines = (
            "股票数据： 五粮液 000858 20260821",
            "71.19",
            "-0.90  -1.25%",
            "今开: 71.96\t昨收: 72.09\t最高价: 72.00\t最低价: 71.11",
            "涨停价: 79.30\t跌停价: 64.88\t换手率: 0.51%\t量比: 0.77",
            "成交量: 19.71万\t成交额: 14.05亿\t动态市盈率: 8.57\t市净率: 2.34",
            "总市值: 2763亿\t流通市值: 2763亿\t振幅: 1.23%",
        )
        for line in expected_lines:
            self.assertIn(line, block)

        ex_date_block = text[
            text.index("Date: 2026-07-16") : text.index("Date: 2026-07-17")
        ]
        self.assertIn("0.08  0.11%", ex_date_block)
        self.assertIn("今开: 73.33\t昨收: 73.82", ex_date_block)

    def test_metadata_hashes(self) -> None:
        metadata = json.loads(
            (GENERATOR.DATA / "metadata.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [(stock.code, stock.name) for stock in GENERATOR.STOCKS],
            [(stock["code"], stock["name"]) for stock in metadata["stocks"]],
        )
        expected_paths = {
            path.relative_to(ROOT).as_posix()
            for directory in (GENERATOR.DATA, GENERATOR.OUTPUT)
            for path in directory.glob("*.csv" if directory == GENERATOR.DATA else "*.txt")
        }
        self.assertEqual(expected_paths, {item["path"] for item in metadata["files"]})
        for item in metadata["files"]:
            path = ROOT / item["path"]
            self.assertTrue(path.is_file(), path)
            self.assertEqual(
                item["sha256"], hashlib.sha256(path.read_bytes()).hexdigest(), path
            )
            if path.suffix == ".csv":
                with path.open(encoding="utf-8", newline="") as handle:
                    self.assertEqual(item["rows"], len(list(csv.DictReader(handle))))


if __name__ == "__main__":
    unittest.main()
