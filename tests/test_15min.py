from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "analyze_15min_volume", ROOT / "scripts" / "analyze_15min_volume.py"
)
assert SPEC and SPEC.loader
ANALYZER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ANALYZER
SPEC.loader.exec_module(ANALYZER)


class FifteenMinuteValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.metadata = json.loads(
            (ANALYZER.DEFAULT_OUTPUT / "metadata.json").read_text(encoding="utf-8")
        )

    def test_all_stocks_have_complete_21_session_bars(self) -> None:
        self.assertEqual(14, len(ANALYZER.STOCKS))
        for stock in ANALYZER.STOCKS:
            directory = ANALYZER.DEFAULT_OUTPUT / stock.stem
            raw_path = directory / f"{stock.code}_15min_raw.csv"
            with raw_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(336, len(rows), stock.code)
            counts = Counter(row["date"] for row in rows)
            self.assertEqual(21, len(counts), stock.code)
            self.assertEqual({16}, set(counts.values()), stock.code)
            dates = sorted(counts)
            self.assertEqual(self.metadata["first_session"], dates[0])
            self.assertEqual(self.metadata["latest_session"], dates[-1])
            for date in dates:
                day = [row for row in rows if row["date"] == date]
                self.assertEqual(list(range(16)), [int(row["slot_idx"]) for row in day])
            for row in rows:
                low = float(row["low"])
                high = float(row["high"])
                self.assertLessEqual(low, float(row["open"]))
                self.assertLessEqual(low, float(row["close"]))
                self.assertGreaterEqual(high, float(row["open"]))
                self.assertGreaterEqual(high, float(row["close"]))
                self.assertGreater(float(row["volume"]), 0)
                self.assertGreater(float(row["amount"]), 0)

    def test_summaries_and_reports(self) -> None:
        for stock in ANALYZER.STOCKS:
            directory = ANALYZER.DEFAULT_OUTPUT / stock.stem
            with (directory / f"{stock.code}_day_summary.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                summaries = list(csv.DictReader(handle))
            self.assertEqual(21, len(summaries))
            latest = self.metadata["latest_session"]
            self.assertEqual(latest, summaries[-1]["date"])
            report = (directory / f"{stock.code}_15min_分析报告.txt").read_text(
                encoding="utf-8"
            )
            alerts = (directory / f"{stock.code}_异动预警.txt").read_text(
                encoding="utf-8"
            )
            guide = (directory / "成交量维度_操作指引.txt").read_text(
                encoding="utf-8"
            )
            self.assertIn("[上午 09:30-11:30]", report)
            self.assertIn("[下午 13:00-15:00]", report)
            self.assertIn("[异动预警汇总]", report)
            self.assertIn("异动预警", alerts)
            self.assertIn(f"[最新交易日速报：{latest}]", guide)
            self.assertIn("不构成投资建议", guide)

    def test_metadata_covers_and_hashes_all_outputs(self) -> None:
        metadata = self.metadata
        self.assertEqual(21, metadata["sessions"])
        self.assertLess(metadata["first_session"], metadata["latest_session"])
        self.assertEqual(70, len(metadata["files"]))
        expected = {
            path.relative_to(ROOT).as_posix()
            for path in ANALYZER.DEFAULT_OUTPUT.glob("*/*")
            if path.is_file()
        }
        self.assertEqual(expected, {item["path"] for item in metadata["files"]})
        for item in metadata["files"]:
            path = ROOT / item["path"]
            data = path.read_bytes()
            self.assertEqual(item["bytes"], len(data), path)
            self.assertEqual(item["sha256"], hashlib.sha256(data).hexdigest(), path)


if __name__ == "__main__":
    unittest.main()
