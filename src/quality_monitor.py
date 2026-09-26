"""Evaluate objective learning events against configurable quality gates.

The module uses only the Python standard library. All timestamps are UTC and
all thresholds come from config/quality_gates.json.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG = logging.getLogger("quality_monitor")
EVENT_TYPES = {
    "assignment_published",
    "submission_created",
    "autotest_completed",
    "activity_recorded",
    "homework_reviewed",
    "support_ticket_opened",
    "support_ticket_answered",
}
STATUS_ORDER = {"GREEN": 0, "YELLOW": 1, "RED": 2}


def parse_time(value: str) -> datetime:
    """Accept timezone-aware ISO-8601; normalize to UTC."""
    if not value:
        raise ValueError("occurred_at is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must have a timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    required = {"review_hours", "first_attempt_pass_pct", "activity_gap_hours", "support_hours"}
    if not isinstance(config.get("gates"), dict) or set(config["gates"]) != required:
        raise ValueError("config must define exactly four quality gates")
    if not isinstance(config.get("window_days"), int) or config["window_days"] < 1:
        raise ValueError("window_days must be a positive integer")
    for name, low_key, high_key in (
        ("review_hours", "target_lt", "critical_gt"),
        ("support_hours", "target_lt", "critical_gt"),
        ("first_attempt_pass_pct", "critical_lt", "target_gt"),
        ("activity_gap_hours", "target_max", "critical_min"),
    ):
        gate = config["gates"][name]
        if not isinstance(gate, dict) or low_key not in gate or high_key not in gate:
            raise ValueError(f"missing limits for {name}")
        lo, hi = gate[low_key], gate[high_key]
        if isinstance(lo, bool) or isinstance(hi, bool) or not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)) or lo < 0 or hi <= lo:
            raise ValueError(f"invalid limits for {name}")
    return config


def load_events(path: Path, now: datetime) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Validate CSV and de-duplicate by immutable event_id.

    Identical duplicate rows are ignored. Conflicting rows sharing event_id are
    rejected so that corrupted input cannot quietly change a gate decision.
    """
    seen: dict[str, dict[str, Any]] = {}
    stats = {"read": 0, "duplicates_ignored": 0, "future_ignored": 0}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        required = {"event_id", "event_type", "occurred_at", "student_id", "group_id", "module_id", "entity_id", "attempt_no", "passed", "activity_kind"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError(f"CSV must contain: {', '.join(sorted(required))}")
        for row_number, raw in enumerate(reader, start=2):
            stats["read"] += 1
            row = {key: (raw.get(key) or "").strip() for key in required}
            for field in ("event_id", "event_type", "student_id", "group_id", "module_id", "entity_id"):
                if not row[field]:
                    raise ValueError(f"row {row_number}: {field} is required")
            if row["event_type"] not in EVENT_TYPES:
                raise ValueError(f"row {row_number}: unknown event_type {row['event_type']!r}")
            at = parse_time(row["occurred_at"])
            row["occurred_at"] = at.isoformat()
            if row["event_type"] == "autotest_completed":
                try:
                    attempt = int(row["attempt_no"])
                except ValueError as exc:
                    raise ValueError(f"row {row_number}: attempt_no must be an integer") from exc
                if attempt < 1 or row["passed"].lower() not in {"true", "false"}:
                    raise ValueError(f"row {row_number}: autotest requires attempt_no >= 1 and passed true/false")
                row["attempt_no"] = str(attempt)
                row["passed"] = row["passed"].lower()
            if row["event_type"] == "activity_recorded" and row["activity_kind"] not in {"commit", "view"}:
                raise ValueError(f"row {row_number}: activity_kind must be commit or view")
            old = seen.get(row["event_id"])
            if old is not None:
                if old != row:
                    raise ValueError(f"row {row_number}: conflicting duplicate event_id {row['event_id']!r}")
                stats["duplicates_ignored"] += 1
                continue
            seen[row["event_id"]] = row
            if at > now:
                stats["future_ignored"] += 1
                LOG.warning("future event ignored: %s", row["event_id"])
    return [row for row in seen.values() if parse_time(row["occurred_at"]) <= now], stats


def score_upper(value: float | None, target_lt: float, critical_gt: float) -> str:
    if value is None:
        return "YELLOW"
    if value < target_lt:
        return "GREEN"
    if value > critical_gt:
        return "RED"
    return "YELLOW"


def score_lower(value: float | None, target_gt: float, critical_lt: float) -> str:
    if value is None:
        return "YELLOW"
    if value > target_gt:
        return "GREEN"
    if value < critical_lt:
        return "RED"
    return "YELLOW"


def score_activity(gap_hours: float | None, target_max: float, critical_min: float) -> str:
    if gap_hours is None:
        return "YELLOW"
    if gap_hours <= target_max:
        return "GREEN"
    if gap_hours >= critical_min:
        return "RED"
    return "YELLOW"


def worst_status(statuses: list[str]) -> str:
    return max(statuses, key=lambda status: STATUS_ORDER[status], default="YELLOW")


def _hours(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 3600


def _gate(status: str, value: float | None, unit: str, sample_size: int, reason: str) -> dict[str, Any]:
    return {"status": status, "value": round(value, 2) if value is not None else None,
            "unit": unit, "sample_size": sample_size, "reason": reason}


def analyze_scope(events: list[dict[str, Any]], now: datetime, config: dict[str, Any]) -> dict[str, Any]:
    """Aggregate one group/module or one student within that group/module."""
    from datetime import timedelta

    start = now - timedelta(days=config["window_days"])
    recent = [e for e in events if parse_time(e["occurred_at"]) >= start]
    gates = config["gates"]

    # Each entity_id identifies one submission, autotest or support ticket.
    submissions = {(e["student_id"], e["entity_id"]): e for e in recent if e["event_type"] == "submission_created"}
    reviews = [e for e in recent if e["event_type"] == "homework_reviewed"]
    review_hours = []
    for review in reviews:
        submitted = submissions.get((review["student_id"], review["entity_id"]))
        if submitted:
            duration = _hours(parse_time(review["occurred_at"]), parse_time(submitted["occurred_at"]))
            if duration >= 0:
                review_hours.append(duration)
    review_mean = sum(review_hours) / len(review_hours) if review_hours else None
    review_gate = _gate(
        score_upper(review_mean, gates["review_hours"]["target_lt"], gates["review_hours"]["critical_gt"]),
        review_mean, "h", len(review_hours),
        "Среднее время проверки ДЗ" if review_mean is not None else "Нет завершённых проверок ДЗ в окне наблюдения",
    )

    first_attempts: dict[tuple[str, str], dict[str, Any]] = {}
    for event in sorted(recent, key=lambda e: e["occurred_at"]):
        if event["event_type"] == "autotest_completed" and event["attempt_no"] == "1":
            first_attempts.setdefault((event["student_id"], event["entity_id"]), event)
    passed = sum(e["passed"] == "true" for e in first_attempts.values())
    pass_rate = passed / len(first_attempts) * 100 if first_attempts else None
    pass_gate = _gate(
        score_lower(pass_rate, gates["first_attempt_pass_pct"]["target_gt"], gates["first_attempt_pass_pct"]["critical_lt"]),
        pass_rate, "%", len(first_attempts),
        "Доля успешных первых попыток авто-теста" if pass_rate is not None else "Нет первых попыток авто-теста в окне наблюдения",
    )

    # Student roster is derived from objective events, preferably assignments.
    students = sorted({e["student_id"] for e in events})
    gaps: dict[str, float | None] = {}
    for student in students:
        owned = [e for e in events if e["student_id"] == student]
        published = [parse_time(e["occurred_at"]) for e in owned if e["event_type"] == "assignment_published"]
        course_start = min(published) if published else None
        actions = [parse_time(e["occurred_at"]) for e in owned
                   if e["event_type"] == "activity_recorded"
                   and (course_start is None or parse_time(e["occurred_at"]) >= course_start)]
        if actions:
            gaps[student] = _hours(now, max(actions))
        elif course_start:
            gaps[student] = _hours(now, course_start)
        else:
            gaps[student] = None
    known_gaps = [v for v in gaps.values() if v is not None]
    max_gap = max(known_gaps) if known_gaps else None
    activity_status = worst_status([score_activity(v, gates["activity_gap_hours"]["target_max"], gates["activity_gap_hours"]["critical_min"]) for v in gaps.values()])
    activity_gate = _gate(activity_status, max_gap, "h", len(students),
                          "Максимальная пауза активности среди студентов" if students else "Нет студентов или событий")
    activity_gate["student_gap_hours"] = {k: round(v, 2) if v is not None else None for k, v in gaps.items()}

    opened = {(e["student_id"], e["entity_id"]): e for e in recent if e["event_type"] == "support_ticket_opened"}
    answered = {(e["student_id"], e["entity_id"]): e for e in recent if e["event_type"] == "support_ticket_answered"}
    response_hours = []
    overdue_open = []
    waiting_open = []
    for ticket_key, opened_event in opened.items():
        answer = answered.get(ticket_key)
        if answer:
            duration = _hours(parse_time(answer["occurred_at"]), parse_time(opened_event["occurred_at"]))
            if duration >= 0:
                response_hours.append(duration)
                continue
        age = _hours(now, parse_time(opened_event["occurred_at"]))
        if age > gates["support_hours"]["critical_gt"]:
            overdue_open.append(ticket_key[1])
        else:
            waiting_open.append(ticket_key[1])
    response_mean = sum(response_hours) / len(response_hours) if response_hours else None
    support_status = score_upper(response_mean, gates["support_hours"]["target_lt"], gates["support_hours"]["critical_gt"])
    if overdue_open:
        support_status = "RED"
    elif waiting_open:
        support_status = worst_status([support_status, "YELLOW"])
    support_gate = _gate(support_status, response_mean, "h", len(response_hours),
                         "Среднее время ответа и открытые обращения" if opened else "Нет обращений в поддержку в окне наблюдения")
    support_gate["overdue_ticket_ids"] = sorted(overdue_open)
    support_gate["waiting_ticket_ids"] = sorted(waiting_open)

    metrics = {"review_hours": review_gate, "first_attempt_pass_pct": pass_gate,
               "activity_gap_hours": activity_gate, "support_hours": support_gate}
    for name, data in metrics.items():
        data["action"] = gates[name]["red_action"] if data["status"] == "RED" else None
    violations = [f"{name}: {data['reason']} = {data['value']} {data['unit']} (RED)"
                  for name, data in metrics.items() if data["status"] == "RED"]
    if overdue_open:
        violations.append(f"support_hours: обращения без ответа > {gates['support_hours']['critical_gt']} ч: {', '.join(sorted(overdue_open))}")
    return {"status": worst_status([m["status"] for m in metrics.values()]),
            "metrics": metrics, "violations": violations}


def evaluate(events: list[dict[str, Any]], now: datetime, config: dict[str, Any],
             group_id: str | None = None, module_id: str | None = None) -> dict[str, Any]:
    selected = [e for e in events if (group_id is None or e["group_id"] == group_id)
                and (module_id is None or e["module_id"] == module_id)]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in selected:
        grouped[(event["group_id"], event["module_id"])].append(event)
    results = []
    for (group, module), scope_events in sorted(grouped.items()):
        summary = analyze_scope(scope_events, now, config)
        students = {}
        for student in sorted({e["student_id"] for e in scope_events}):
            students[student] = analyze_scope([e for e in scope_events if e["student_id"] == student], now, config)
        record = {"group_id": group, "module_id": module, **summary, "students": students}
        for violation in summary["violations"]:
            LOG.warning("%s/%s: %s", group, module, violation)
        results.append(record)
    modules: dict[str, dict[str, Any]] = {}
    for result in results:
        entry = modules.setdefault(result["module_id"], {"groups": [], "status": "GREEN"})
        entry["groups"].append(result["group_id"])
        entry["status"] = worst_status([entry["status"], result["status"]])
    return {"as_of_utc": now.isoformat(), "window_days": config["window_days"],
            "status": worst_status([r["status"] for r in results]),
            "groups": results, "modules": modules}


def display(report: dict[str, Any]) -> str:
    if not report["groups"]:
        return "Нет данных для выбранной группы/модуля; статус YELLOW."
    names = {"review_hours": "Проверка ДЗ", "first_attempt_pass_pct": "Тест с 1-й попытки",
             "activity_gap_hours": "Пауза активности", "support_hours": "Ответ в поддержке"}
    lines = [f"Оценка на {report['as_of_utc']} | окно {report['window_days']} дн."]
    for result in report["groups"]:
        lines.append(f"\nГруппа {result['group_id']} | модуль {result['module_id']} | {result['status']}")
        for key, gate in result["metrics"].items():
            value = "нет данных" if gate["value"] is None else f"{gate['value']} {gate['unit']}"
            lines.append(f"  {names[key]:22} {value:13} {gate['status']} (n={gate['sample_size']})")
        lines.append("  Студенты: " + ", ".join(f"{sid}={data['status']}" for sid, data in result["students"].items()))
        for violation in result["violations"]:
            lines.append("  Причина RED: " + violation)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Контроль качества учебных модулей по событиям CSV")
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--now", help="ISO-8601 со смещением; по умолчанию текущее время UTC")
    parser.add_argument("--group")
    parser.add_argument("--module")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--json", action="store_true", help="вывести JSON вместо таблицы")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
    config = load_config(args.config)
    events, input_stats = load_events(args.events, now)
    report = evaluate(events, now, config, args.group, args.module)
    report["input_stats"] = input_stats
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded + "\n", encoding="utf-8")
    print(encoded if args.json else display(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
