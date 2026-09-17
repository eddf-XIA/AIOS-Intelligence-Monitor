"""Importing reports produced by the original ``aios_daily.py``."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from aios.repositories import articles as articles_repo
from aios.repositories import events as events_repo
from aios.repositories import reports as reports_repo
from aios.services.importer import detect_format, import_file, import_legacy_payload


LEGACY = {
    "date": "2026-09-15",
    "sections": [
        {
            "section": "移动智能终端侧",
            "status": "new",
            "items": [
                {
                    "tag": "操作系统AI化",
                    "title": "华为发布 HarmonyOS 6",
                    "summary": "官方发布会宣布预置 80 个 AI 智能体。",
                    "assessment": "判断：系统级 AI 进入新阶段。",
                    "importance": 1,
                    "confidence": "high",
                    "source_ids": [0, 1],
                }
            ],
            "metrics": [{"value": "80", "label": "预置智能体数量", "source_ids": [0]}],
            "_evidence": [
                {
                    "id": 0,
                    "title": "Huawei Launches HarmonyOS 6",
                    "source": "Yicai Global",
                    "published_at": "Thu, 23 Oct 2025 07:00:00 GMT",
                    "url": "https://www.yicaiglobal.com/news/harmonyos-6",
                    "text": "Huawei launched HarmonyOS 6 with over 80 AI agents.",
                },
                {
                    "id": 1,
                    "title": "鸿蒙 6 发布",
                    "source": "Huawei",
                    "published_at": "20251023T070000Z",
                    "url": "https://www.huawei.com/cn/news/harmonyos-6",
                    "text": "华为正式发布 HarmonyOS 6。",
                },
            ],
        },
        {
            "section": "太空智算侧",
            "status": "watch",
            "items": [],
            "metrics": [],
            "_evidence": [],
        },
    ],
    "overview": {
        "headline": {"title": "鸿蒙 6 发布", "body": "摘要。", "section": "移动智能终端侧"},
        "trends": [{"title": "趋势一", "body": "研判。", "confidence": "high"}],
        "metrics": [{"value": "80", "label": "智能体", "section": "移动智能终端侧"}],
    },
}


class TestFormatDetection:
    def test_detects_legacy(self):
        assert detect_format(LEGACY) == "legacy"

    def test_detects_v2(self):
        assert detect_format({"schema_version": 2, "report_items": []}) == "v2"

    def test_rejects_unknown(self):
        assert detect_format({"hello": "world"}) == "unknown"


class TestImport:
    def test_imports_report_sections_items_and_sources(self, session):
        result = import_legacy_payload(session, LEGACY, source_name="test.json")
        assert result.ok
        assert result.report_date == dt.date(2026, 9, 15)
        assert result.sections == 2
        assert result.items == 1
        assert result.articles == 2

        report = reports_repo.get_report(session, result.report_id)
        assert report.legacy_import is True
        assert report.headline_json["title"] == "鸿蒙 6 发布"
        assert len(report.trends_json) == 1

        section = report.sections[0]
        assert section.module_name == "移动智能终端侧"
        assert section.module_key == "mobile", "should bind to the seeded module"
        assert len(section.items) == 1

        item = section.items[0]
        assert item.title == "华为发布 HarmonyOS 6"
        assert item.fact_summary.startswith("官方发布会")
        assert item.assessment.startswith("判断")

    def test_evidence_chain_survives_the_import(self, session):
        result = import_legacy_payload(session, LEGACY)
        report = reports_repo.get_report(session, result.report_id)
        observation = report.sections[0].items[0].observation

        assert observation is not None
        urls = {link.article.url for link in observation.sources}
        assert urls == {
            "https://www.yicaiglobal.com/news/harmonyos-6",
            "https://www.huawei.com/cn/news/harmonyos-6",
        }

    def test_legacy_rows_are_flagged(self, session):
        """Old data has no cross-day history; that must be visible, not faked."""
        result = import_legacy_payload(session, LEGACY)
        events = events_repo.list_events(session)
        assert len(events) == 1
        assert events[0].legacy_import is True
        assert events[0].observation_count == 1
        assert events[0].observations[0].legacy_import is True

    def test_publication_dates_are_parsed(self, session):
        """Both RFC822 and GDELT timestamp shapes must be understood."""
        import_legacy_payload(session, LEGACY)
        articles = {a.source: a for a in articles_repo.recent(session, days=1)}
        assert articles["Yicai Global"].published_at == dt.datetime(2025, 10, 23, 7, 0)
        assert articles["Huawei"].published_at == dt.datetime(2025, 10, 23, 7, 0)

    def test_second_import_is_skipped_by_default(self, session):
        first = import_legacy_payload(session, LEGACY)
        second = import_legacy_payload(session, LEGACY)
        assert second.skipped is True
        assert second.report_id == first.report_id
        assert reports_repo.count_reports(session) == 1

    def test_overwrite_replaces_the_report(self, session):
        import_legacy_payload(session, LEGACY)
        second = import_legacy_payload(session, LEGACY, overwrite=True)
        assert second.skipped is False
        assert reports_repo.count_reports(session) == 1

    def test_missing_date_is_reported(self, session):
        result = import_legacy_payload(session, {"sections": [], "overview": {}})
        assert not result.ok
        assert "date" in result.summary()

    def test_bad_date_is_reported(self, session):
        result = import_legacy_payload(session, {"date": "15/09/2026", "sections": []})
        assert not result.ok

    def test_items_without_titles_are_skipped(self, session):
        payload = json.loads(json.dumps(LEGACY))
        payload["sections"][0]["items"].append({"summary": "no title"})
        result = import_legacy_payload(session, payload)
        assert result.items == 1

    def test_unknown_module_name_still_imports(self, session):
        payload = json.loads(json.dumps(LEGACY))
        payload["sections"][0]["section"] = "某个已被删除的领域"
        result = import_legacy_payload(session, payload)
        assert result.ok
        report = reports_repo.get_report(session, result.report_id)
        assert report.sections[0].module_id is None
        assert report.sections[0].module_name == "某个已被删除的领域"


class TestImportFile:
    def test_import_from_disk_and_export(self, session, db, tmp_path):
        path = tmp_path / "legacy.json"
        path.write_text(json.dumps(LEGACY, ensure_ascii=False), encoding="utf-8")

        result = import_file(session, path)
        assert result.ok
        assert (db.reports_dir / "2026-09-15.html").exists()

    def test_missing_file(self, session, tmp_path):
        result = import_file(session, tmp_path / "nope.json")
        assert not result.ok
        assert "not found" in result.summary().lower()

    def test_invalid_json(self, session, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        result = import_file(session, path)
        assert not result.ok

    def test_unknown_format(self, session, tmp_path):
        path = tmp_path / "other.json"
        path.write_text(json.dumps({"a": 1}), encoding="utf-8")
        result = import_file(session, path)
        assert not result.ok
        assert "Unrecognised" in result.summary()
