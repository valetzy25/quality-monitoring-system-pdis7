"""Threshold, pipeline and edge-case tests. Run with python3 -m unittest discover -s tests -v."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.quality_monitor import (
    analyze_scope, evaluate, load_config, load_events, parse_time,
    score_activity, score_lower, score_upper,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_config(ROOT / "config" / "quality_gates.json")
NOW = parse_time("2026-09-26T04:00:00Z")
CSV_FIELDS = ["event_id", "event_type", "occurred_at", "student_id", "group_id", "module_id", "entity_id", "attempt_no", "passed", "activity_kind"]


def event(event_id: str, event_type: str, time: str, **kwargs: str) -> dict[str, str]:
    result = {key: "" for key in CSV_FIELDS}
    result.update({"event_id": event_id, "event_type": event_type, "occurred_at": parse_time(time).isoformat(),
                   "student_id": "s1", "group_id": "g1", "module_id": "m1", "entity_id": event_id})
    result.update(kwargs)
    return result


def csv_file(rows: list[dict[str, str]], folder: Path) -> Path:
    path = folder / "events.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path


class ReviewGateTests(unittest.TestCase):
    def test_green_below_24_hours(self):
        self.assertEqual(score_upper(23.99, 24, 48), "GREEN")

    def test_yellow_at_target_and_critical(self):
        self.assertEqual(score_upper(24, 24, 48), "YELLOW")
        self.assertEqual(score_upper(48, 24, 48), "YELLOW")

    def test_red_above_48_hours(self):
        self.assertEqual(score_upper(48.01, 24, 48), "RED")


class FirstAttemptGateTests(unittest.TestCase):
    def test_green_above_70_percent(self):
        self.assertEqual(score_lower(70.01, 70, 40), "GREEN")

    def test_yellow_at_target_and_critical(self):
        self.assertEqual(score_lower(70, 70, 40), "YELLOW")
        self.assertEqual(score_lower(40, 70, 40), "YELLOW")

    def test_red_below_40_percent(self):
        self.assertEqual(score_lower(39.99, 70, 40), "RED")


class ActivityGateTests(unittest.TestCase):
    def test_green_within_daily_window(self):
        self.assertEqual(score_activity(24, 24, 72), "GREEN")

    def test_yellow_after_one_day(self):
        self.assertEqual(score_activity(48, 24, 72), "YELLOW")

    def test_red_at_three_days(self):
        self.assertEqual(score_activity(72, 24, 72), "RED")


class SupportGateTests(unittest.TestCase):
    def test_green_below_two_hours(self):
        self.assertEqual(score_upper(1.99, 2, 6), "GREEN")

    def test_yellow_at_target_and_critical(self):
        self.assertEqual(score_upper(2, 2, 6), "YELLOW")
        self.assertEqual(score_upper(6, 2, 6), "YELLOW")

    def test_red_above_six_hours(self):
        self.assertEqual(score_upper(6.01, 2, 6), "RED")


class PipelineTests(unittest.TestCase):
    def test_example_metrics_and_input_deduplication(self):
        events, stats = load_events(ROOT / "examples" / "learning_events.csv", NOW)
        self.assertEqual(stats["duplicates_ignored"], 1)
        report = evaluate(events, NOW, CONFIG, "group-25", "module-7")
        group = report["groups"][0]
        self.assertEqual(group["status"], "RED")
        self.assertEqual(group["metrics"]["review_hours"]["value"], 51)
        self.assertEqual(group["metrics"]["first_attempt_pass_pct"]["value"], 25)
        self.assertEqual(group["metrics"]["activity_gap_hours"]["status"], "RED")
        self.assertEqual(group["metrics"]["support_hours"]["value"], 4)
        self.assertEqual(report["modules"]["module-7"]["status"], "RED")
        self.assertEqual(set(group["students"]), {"student-a", "student-b", "student-c", "student-d"})

    def test_no_data_is_yellow_not_green(self):
        result = analyze_scope([], NOW, CONFIG)
        self.assertEqual(result["status"], "YELLOW")
        self.assertTrue(all(metric["value"] is None for metric in result["metrics"].values()))

    def test_zero_duration_review_is_valid_green(self):
        rows = [event("a", "submission_created", "2026-09-25T12:00:00Z", entity_id="sub1"),
                event("b", "homework_reviewed", "2026-09-25T12:00:00Z", entity_id="sub1")]
        gate = analyze_scope(rows, NOW, CONFIG)["metrics"]["review_hours"]
        self.assertEqual((gate["value"], gate["status"]), (0, "GREEN"))

    def test_first_attempt_deduplicated_by_business_key(self):
        rows = [event("a", "autotest_completed", "2026-09-25T10:00:00Z", entity_id="test1", attempt_no="1", passed="false"),
                event("b", "autotest_completed", "2026-09-25T10:01:00Z", entity_id="test1", attempt_no="1", passed="true"),
                event("c", "autotest_completed", "2026-09-25T11:00:00Z", entity_id="test1", attempt_no="2", passed="true")]
        gate = analyze_scope(rows, NOW, CONFIG)["metrics"]["first_attempt_pass_pct"]
        self.assertEqual((gate["value"], gate["sample_size"]), (0, 1))

    def test_unanswered_ticket_over_six_hours_is_red(self):
        rows = [event("a", "support_ticket_opened", "2026-09-25T20:00:00Z", entity_id="ticket1")]
        gate = analyze_scope(rows, NOW, CONFIG)["metrics"]["support_hours"]
        self.assertEqual(gate["status"], "RED")
        self.assertEqual(gate["overdue_ticket_ids"], ["ticket1"])

    def test_unanswered_ticket_under_six_hours_is_yellow(self):
        rows = [event("a", "support_ticket_opened", "2026-09-26T00:00:00Z", entity_id="ticket1")]
        gate = analyze_scope(rows, NOW, CONFIG)["metrics"]["support_hours"]
        self.assertEqual(gate["status"], "YELLOW")

    def test_activity_before_assignment_does_not_count(self):
        rows = [event("a", "assignment_published", "2026-09-23T00:00:00Z"),
                event("b", "activity_recorded", "2026-09-22T00:00:00Z", activity_kind="commit")]
        gate = analyze_scope(rows, NOW, CONFIG)["metrics"]["activity_gap_hours"]
        self.assertEqual((gate["value"], gate["status"]), (76, "RED"))

    def test_identical_duplicate_event_ignored(self):
        row = event("a", "activity_recorded", "2026-09-25T10:00:00Z", activity_kind="commit")
        with tempfile.TemporaryDirectory() as tmp:
            events, stats = load_events(csv_file([row, row], Path(tmp)), NOW)
        self.assertEqual((len(events), stats["duplicates_ignored"]), (1, 1))

    def test_conflicting_duplicate_event_rejected(self):
        row = event("a", "activity_recorded", "2026-09-25T10:00:00Z", activity_kind="commit")
        changed = dict(row, activity_kind="view")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
                load_events(csv_file([row, changed], Path(tmp)), NOW)

    def test_future_event_ignored(self):
        row = event("a", "activity_recorded", "2026-09-27T10:00:00Z", activity_kind="commit")
        with tempfile.TemporaryDirectory() as tmp:
            events, stats = load_events(csv_file([row], Path(tmp)), NOW)
        self.assertEqual((len(events), stats["future_ignored"]), (0, 1))

    def test_bad_timezone_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            parse_time("2026-09-26T04:00:00")

    def test_group_module_filter_and_aggregation(self):
        rows = [event("a", "assignment_published", "2026-09-25T10:00:00Z"),
                event("b", "assignment_published", "2026-09-25T10:00:00Z", group_id="g2")]
        report = evaluate(rows, NOW, CONFIG, group_id="g1")
        self.assertEqual(len(report["groups"]), 1)
        self.assertEqual(report["modules"]["m1"]["groups"], ["g1"])

    def test_invalid_threshold_config_rejected(self):
        bad = json.loads((ROOT / "config" / "quality_gates.json").read_text(encoding="utf-8"))
        bad["gates"]["review_hours"]["target_lt"] = 50
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid limits"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
