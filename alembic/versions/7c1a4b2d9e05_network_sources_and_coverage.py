"""network modes, feed sources, per-run source health and report coverage

Adds only what genuinely needs to survive a restart:

* ``feed_sources`` - user-configured RSS/Atom feeds (configuration).
* ``run_source_stats`` - per-run collector counters behind the 数据源状态 panel
  and the live run view (history worth keeping).
* ``reports.coverage_json`` / ``report_sections.coverage_state`` - so a report
  can say "coverage was degraded" long after the run that produced it.
* ``module_runs.collection_status`` - "the module finished" and "the sources
  answered" are separate facts and need separate columns.

Transient state (open circuits, the short-term query cache, live source health)
deliberately has no table: persisting it would mean showing a verdict about a
network that no longer exists.

Network mode, collector enable/disable and collection tuning are stored as rows
in the existing ``app_settings`` key/value table, so they need no schema change
and an older database picks up their defaults on first launch.

Revision ID: 7c1a4b2d9e05
Revises: 03f23e4329f9
Create Date: 2026-09-16
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7c1a4b2d9e05"
down_revision: Union[str, None] = "03f23e4329f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "feed_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("domain", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("module_id", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_status", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("last_item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["module_id"], ["monitor_modules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url", name="uq_feed_source_url"),
    )
    op.create_index("ix_feed_sources_domain", "feed_sources", ["domain"])
    op.create_index("ix_feed_sources_enabled", "feed_sources", ["enabled"])
    op.create_index("ix_feed_sources_module_id", "feed_sources", ["module_id"])

    op.create_table(
        "run_source_stats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("collector", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("requests_attempted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("requests_succeeded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("zero_result_responses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidates_returned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("timeouts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rate_limited", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("network_errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parse_errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("http_errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("circuit_activations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("circuit_open_skips", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_hits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("extra_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["monitoring_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "collector", name="uq_run_source_collector"),
    )
    op.create_index("ix_run_source_stats_run_id", "run_source_stats", ["run_id"])
    op.create_index("ix_run_source_stats_collector", "run_source_stats", ["collector"])
    op.create_index("ix_run_source_stats_state", "run_source_stats", ["state"])

    # Existing rows get "" / NULL, which reads as "coverage was not recorded" -
    # deliberately not as "coverage was complete".
    with op.batch_alter_table("reports") as batch:
        batch.add_column(sa.Column("coverage_json", sa.JSON(), nullable=True))
    with op.batch_alter_table("report_sections") as batch:
        batch.add_column(
            sa.Column("coverage_state", sa.String(length=32), nullable=False, server_default="")
        )
    with op.batch_alter_table("module_runs") as batch:
        batch.add_column(
            sa.Column("collection_status", sa.String(length=32), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("module_runs") as batch:
        batch.drop_column("collection_status")
    with op.batch_alter_table("report_sections") as batch:
        batch.drop_column("coverage_state")
    with op.batch_alter_table("reports") as batch:
        batch.drop_column("coverage_json")

    op.drop_index("ix_run_source_stats_state", table_name="run_source_stats")
    op.drop_index("ix_run_source_stats_collector", table_name="run_source_stats")
    op.drop_index("ix_run_source_stats_run_id", table_name="run_source_stats")
    op.drop_table("run_source_stats")

    op.drop_index("ix_feed_sources_module_id", table_name="feed_sources")
    op.drop_index("ix_feed_sources_enabled", table_name="feed_sources")
    op.drop_index("ix_feed_sources_domain", table_name="feed_sources")
    op.drop_table("feed_sources")
