"""Numeric diffing rules - the part that must never invent a number."""

from __future__ import annotations

import datetime as dt

import pytest

from aios.services.diff_engine import (
    CORRECTION,
    DATA_CHANGE,
    NEW,
    UNCHANGED,
    compare_metric,
    compare_reports,
    compare_structured_data,
    format_delta,
    parse_number,
)


class TestParseNumber:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (85_000_000, 85_000_000.0),
            (24.3, 24.3),
            ("87200000", 87_200_000.0),
            ("87,200,000", 87_200_000.0),
            ("24.3%", 24.3),
            ("-5", -5.0),
        ],
    )
    def test_parses_numbers(self, value, expected):
        assert parse_number(value) == expected

    @pytest.mark.parametrize(
        "value", ["8720万", "多家厂商", "Q3", "", None, True, False, [1], {"a": 1}]
    )
    def test_rejects_non_numbers(self, value):
        """Anything ambiguous must return None rather than a guess."""
        assert parse_number(value) is None


class TestCompareMetric:
    def test_spec_example(self):
        """85,000,000 -> 87,200,000 gives +2,200,000 and +2.59%."""
        change = compare_metric(
            "device_count",
            {"value": 85_000_000, "display": "8500万", "unit": "devices"},
            {"value": 87_200_000, "display": "8720万", "unit": "devices"},
        )
        assert change.comparable is True
        assert change.absolute_change == 2_200_000
        assert change.percentage_change == pytest.approx(2.588235, rel=1e-5)
        assert change.percentage_display == "+2.59%"
        assert change.direction == "up"
        assert change.old_display == "8500万"
        assert change.new_display == "8720万"

    def test_zero_baseline_has_no_percentage(self):
        """Division by zero must not produce a percentage."""
        change = compare_metric("count", {"value": 0}, {"value": 500})
        assert change.comparable is True
        assert change.absolute_change == 500
        assert change.percentage_change is None
        assert change.percentage_display == ""

    def test_non_numeric_does_no_arithmetic(self):
        change = compare_metric(
            "status", {"display": "测试阶段"}, {"display": "商用阶段"}
        )
        assert change.comparable is False
        assert change.absolute_change is None
        assert change.percentage_change is None
        assert change.old_display == "测试阶段"
        assert change.new_display == "商用阶段"

    def test_decrease_is_negative(self):
        change = compare_metric("share", {"value": 25.0}, {"value": 24.3})
        assert change.absolute_change == pytest.approx(-0.7)
        assert change.direction == "down"
        assert change.percentage_display.startswith("-2.8")

    def test_display_only_values_still_compare(self):
        """A metric with no `value` key falls back to parsing `display`."""
        change = compare_metric("n", {"display": "100"}, {"display": "150"})
        assert change.comparable is True
        assert change.absolute_change == 50


class TestFormatDelta:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (2_200_000, "+220万"),
            (-2_200_000, "-220万"),
            (500, "+500"),
            (0, "0"),
            (250_000_000, "+2.50亿"),
        ],
    )
    def test_human_sizes(self, value, expected):
        assert format_delta(value) == expected


class TestCompareStructuredData:
    def test_only_shared_keys_are_compared(self):
        changes = compare_structured_data(
            {"a": {"value": 1}, "gone": {"value": 9}},
            {"a": {"value": 2}, "brand_new": {"value": 7}},
        )
        assert [c.key for c in changes] == ["a"]

    def test_identical_values_are_not_reported(self):
        changes = compare_structured_data(
            {"a": {"value": 5, "display": "5"}}, {"a": {"value": 5, "display": "5"}}
        )
        assert changes == []


class TestCompareReports:
    def _build_report(self, session, report_date, entries):
        """Create a report with one section and the given (event, obs) pairs."""
        from aios.models import Report, ReportItem, ReportSection

        report = Report(report_date=report_date, title=f"R {report_date}", model="test")
        session.add(report)
        session.flush()
        section = ReportSection(
            report_id=report.id, module_name="移动智能终端侧", module_key="mobile", status="new"
        )
        session.add(section)
        session.flush()
        for order, (event, observation) in enumerate(entries):
            session.add(
                ReportItem(
                    report_section_id=section.id,
                    event_id=event.id,
                    observation_id=observation.id,
                    title=observation.title,
                    fact_summary=observation.fact_summary,
                    assessment=observation.assessment,
                    confidence=observation.confidence,
                    sort_order=order,
                )
            )
        session.flush()
        return report

    def test_new_updated_and_data_change(self, session, make_event):
        from aios.repositories import events as events_repo
        from aios.repositories import reports as reports_repo

        day1 = dt.date(2026, 9, 14)
        day2 = dt.date(2026, 9, 15)

        event, obs1 = make_event(
            session,
            title="HarmonyOS 7 生态发展",
            date=day1,
            metrics={"device_count": {"value": 85_000_000, "display": "8500万"}},
        )
        report_a = self._build_report(session, day1, [(event, obs1)])

        obs2 = events_repo.add_observation(
            session,
            event_id=event.id,
            observation_date=day2,
            title="HarmonyOS 7 生态发展",
            fact_summary="设备数量增长至 8720 万。",
            assessment="判断：增速平稳。",
            confidence="high",
            structured_data_json={"device_count": {"value": 87_200_000, "display": "8720万"}},
        )
        brand_new, new_obs = make_event(session, title="全新事件", date=day2)
        report_b = self._build_report(session, day2, [(event, obs2), (brand_new, new_obs)])
        session.commit()

        result = compare_reports(
            session,
            reports_repo.get_report(session, report_a.id),
            reports_repo.get_report(session, report_b.id),
        )

        by_event = {d.event_id: d for d in result.diffs}
        assert by_event[event.id].change_type == DATA_CHANGE
        assert by_event[event.id].metric_changes[0].absolute_change == 2_200_000
        assert by_event[brand_new.id].change_type == NEW
        assert result.summary[NEW] == 1
        assert result.summary[DATA_CHANGE] == 1

    def test_correction_wins_over_data_change(self, session, make_event):
        from aios.repositories import events as events_repo
        from aios.repositories import reports as reports_repo

        day1, day2 = dt.date(2026, 9, 14), dt.date(2026, 9, 15)
        event, obs1 = make_event(
            session, title="数据披露", date=day1, metrics={"n": {"value": 100}}
        )
        report_a = self._build_report(session, day1, [(event, obs1)])
        obs2 = events_repo.add_observation(
            session,
            event_id=event.id,
            observation_date=day2,
            title="数据披露",
            fact_summary="官方更正为 90。",
            is_correction=True,
            structured_data_json={"n": {"value": 90}},
        )
        report_b = self._build_report(session, day2, [(event, obs2)])
        session.commit()

        result = compare_reports(
            session,
            reports_repo.get_report(session, report_a.id),
            reports_repo.get_report(session, report_b.id),
        )
        assert result.diffs[0].change_type == CORRECTION

    def test_same_observation_is_unchanged(self, session, make_event):
        from aios.repositories import reports as reports_repo

        day1, day2 = dt.date(2026, 9, 14), dt.date(2026, 9, 15)
        event, obs = make_event(session, title="静止事件", date=day1)
        report_a = self._build_report(session, day1, [(event, obs)])
        report_b = self._build_report(session, day2, [(event, obs)])
        session.commit()

        result = compare_reports(
            session,
            reports_repo.get_report(session, report_a.id),
            reports_repo.get_report(session, report_b.id),
        )
        assert result.diffs[0].change_type == UNCHANGED

    def test_argument_order_is_normalised(self, session, make_event):
        """Passing the newer report first must not invert the diff."""
        from aios.repositories import reports as reports_repo

        day1, day2 = dt.date(2026, 9, 14), dt.date(2026, 9, 15)
        old_event, old_obs = make_event(session, title="旧事件", date=day1)
        new_event, new_obs = make_event(session, title="新事件", date=day2)
        report_a = self._build_report(session, day1, [(old_event, old_obs)])
        report_b = self._build_report(session, day2, [(new_event, new_obs)])
        session.commit()

        result = compare_reports(
            session,
            reports_repo.get_report(session, report_b.id),
            reports_repo.get_report(session, report_a.id),
        )
        assert result.date_a == day1 and result.date_b == day2
        by_event = {d.event_id: d.change_type for d in result.diffs}
        assert by_event[new_event.id] == NEW

    def test_missing_report_returns_error(self, session):
        result = compare_reports(session, None, None)
        assert result.error
        assert result.diffs == []
