"""Database initialisation, seeding and the no-overwrite guarantee."""

from __future__ import annotations

from aios.database import init_db, session_scope
from aios.repositories import modules as modules_repo
from aios.repositories import topics as topics_repo
from aios.services.seed import SEED_MODULES, is_empty, seed_summary
from aios.services.settings_service import all_settings, get_int, set_value


def test_init_creates_every_table(db):
    from sqlalchemy import inspect

    from aios.database import get_engine

    tables = set(inspect(get_engine()).get_table_names())
    expected = {
        "monitor_modules", "topics", "search_queries", "preferred_sources",
        "excluded_keywords", "raw_articles", "intelligence_events",
        "event_observations", "observation_sources", "reports", "report_sections",
        "report_items", "monitoring_runs", "module_runs", "run_logs", "llm_usage",
        "app_settings",
    }
    assert expected <= tables


def test_seed_creates_the_eight_original_tracks(session):
    modules = modules_repo.list_modules(session)
    assert len(modules) == 8
    assert [m.key for m in modules] == [
        "mobile", "pc", "server", "supernode", "iot", "uav", "robotics", "space",
    ]


def test_seed_counts_match_summary(session):
    summary = seed_summary()
    assert summary["modules"] == 8
    assert topics_repo.count_topics(session) == summary["topics"]
    assert topics_repo.count_queries(session) == summary["queries"]


def test_original_track_queries_are_preserved(session):
    """Every query string from the old TRACKS constant must still exist."""
    legacy_queries = {
        'HarmonyOS OR 鸿蒙 OR "Android 17" OR iOS "operating system" AI agent',
        '"mobile operating system" AI agent smartphone',
        'Windows agentic OS OR "agent-ready" PC',
        'HarmonyOS PC OR 鸿蒙电脑 OR "AI PC" operating system',
        "openEuler OR Anolis OS OR 龙蜥 operating system server",
        '"server operating system" AI Linux China',
        "SuperPoD OR 超节点 AI cluster interconnect",
        "Atlas 950 SuperPoD OR AI supernode",
        "OpenHarmony IoT OR 开源鸿蒙 物联网",
        "RISC-V OpenHarmony industrial IoT",
        "drone operating system AI flight controller RT-Thread",
        "无人机 操作系统 AI 飞控",
        "robot operating system embodied AI ROS 2",
        "M-Robots OS OpenHarmony robot",
        "space computing operating system satellite AI",
        "太空算力 太空操作系统 卫星",
    }
    seeded = {
        query
        for module in SEED_MODULES
        for topic in module["topics"]
        for query in topic["queries"]
    }
    assert legacy_queries <= seeded


def test_every_seeded_module_is_runnable(session):
    """A module with no enabled topic+query would be silently skipped."""
    for module in modules_repo.list_enabled_modules(session):
        assert module.active_topics, f"{module.key} has no active topic"
        for topic in module.active_topics:
            assert topic.active_queries, f"{module.key}/{topic.name} has no query"


def test_seed_does_not_run_twice(db, session):
    assert is_empty(session) is False
    module = modules_repo.get_module_by_key(session, "mobile")
    module.name = "我改过的名字"
    module.enabled = False
    session.commit()

    init_db(db)  # simulates restarting the application

    with session_scope() as fresh:
        again = modules_repo.get_module_by_key(fresh, "mobile")
        assert again.name == "我改过的名字"
        assert again.enabled is False
        assert modules_repo.count_modules(fresh) == 8


def test_default_settings_are_present(session):
    values = all_settings(session)
    assert values["deepseek_model"] == "deepseek-flash"
    assert values["deepseek_base_url"] == "https://api.deepseek.com"
    assert values["scheduler_enabled"] == "false"


def test_user_settings_survive_restart(db, session):
    set_value(session, "default_lookback_days", 9)
    session.commit()
    init_db(db)
    with session_scope() as fresh:
        assert get_int(fresh, "default_lookback_days") == 9
