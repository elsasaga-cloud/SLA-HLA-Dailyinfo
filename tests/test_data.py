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
            self.assertEqual("2026-04-14", target[0]["date"])
            self.assertEqual("2026-08-21", target[-1]["date"])
            self.assertEqual("2025-06-04", warmup[0]["date"])
            self.assertEqual("2026-04-13", warmup[-1]["date"])
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
        stock = GENERATOR.STOCKS[0]
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

    def test_text_outputs_have_90_complete_blocks(self) -> None:
        required = (
            "【价格行情】",
            "【成交与估值】",
            "【筹码分布】",
            "【5 日表现】",
            "【移动平均线】",
            "获利比例：",
            "90% 成本区间：",
            "MA90：",
        )
        for stock in GENERATOR.STOCKS:
            text = (
                GENERATOR.OUTPUT / f"{stock.stem}_90d.txt"
            ).read_text(encoding="utf-8")
            self.assertEqual(90, text.count("\n日期："))
            self.assertEqual(90, text.count("股票代码：" + stock.code))
            for marker in required:
                self.assertEqual(90, text.count(marker), marker)
            self.assertNotIn("nan", text.lower())
            self.assertNotIn("none", text.lower())

    def test_metadata_hashes(self) -> None:
        metadata = json.loads(
            (GENERATOR.DATA / "metadata.json").read_text(encoding="utf-8")
        )
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
