"""simple mode: research topics, agent runs and durable event identity

Everything here is **additive**. v2.2 introduces a second way to *acquire*
information (a Research Agent) but deliberately reuses the v2.1 intelligence
core, so no existing table is restructured and no existing row is rewritten:

* ``research_topics`` / ``research_topic_revisions`` - the user-facing 研究主题
  concept behind 简易版, plus the snapshots that keep an older report readable
  against the brief that actually produced it.
* ``monitoring_runs.engine`` / ``.research_topic_id`` / ``.coverage_status`` /
  ``.total_sources_examined`` - a run now records *how* it acquired its
  information and what the agent said about coverage. ``engine`` defaults to
  ``classic`` so every pre-v2.2 run keeps reading correctly.
* ``reports.engine`` / ``.research_topic_id`` / ``.coverage_status`` - the same
  facts mirrored on the publication, so a report can still answer "was this
  complete?" long after its run row is pruned.
* ``intelligence_events`` identity columns - a research agent words the same
  real-world event differently on different days, so the durable identity
  (organisation, product, event type, entities, canonical URLs) is stored and
  reused by the matcher instead of relying on title equality.
* ``event_observations.entities_json`` / ``.event_type`` / ``.event_date`` -
  the per-day wording, and when the event actually happened as distinct from
  when we learned about it.

Classic events leave every new identity column empty and therefore match
exactly as they did in v2.1.

Application mode (简易版 / 本地专业版), the selected research agent and the
Simple-mode schedule are rows in the existing ``app_settings`` key/value table,
so they need no schema change and an older database picks up their defaults on
first launch.

Revision ID: b8d51c0a4f72
Revises: 7c1a4b2d9e05
Create Date: 2026-09-20
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8d51c0a4f72"
down_revision: Union[str, None] = "7c1a4b2d9e05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "research_topics",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False, server_default=""),
        sa.Column("scope", sa.Text(), nullable=False, server_default=""),
        sa.Column("focus_areas_json", sa.JSON(), nullable=True),
        sa.Column("exclusions_json", sa.JSON(), nullable=True),
        sa.Column("regions", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("keywords_json", sa.JSON(), nullable=True),
        sa.Column("window_hours", sa.Integer(), nullable=False, server_default="72"),
        sa.Column("depth", sa.String(length=16), nullable=False, server_default="standard"),
        sa.Column("schedule_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("schedule_time", sa.String(length=5), nullable=False, server_default="08:00"),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_research_topic_name"),
    )
    op.create_index("ix_research_topics_name", "research_topics", ["name"])
    op.create_index("ix_research_topics_archived", "research_topics", ["archived"])
    op.create_index("ix_research_topics_last_run_at", "research_topics", ["last_run_at"])

    op.create_table(
        "research_topic_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("topic_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("brief", sa.Text(), nullable=False, server_default=""),
        sa.Column("scope", sa.Text(), nullable=False, server_default=""),
        sa.Column("focus_areas_json", sa.JSON(), nullable=True),
        sa.Column("exclusions_json", sa.JSON(), nullable=True),
        sa.Column("keywords_json", sa.JSON(), nullable=True),
        sa.Column("regions", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("window_hours", sa.Integer(), nullable=False, server_default="72"),
        sa.Column("change_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["topic_id"], ["research_topics.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("topic_id", "version", name="uq_research_revision_version"),
    )
    op.create_index(
        "ix_research_topic_revisions_topic_id", "research_topic_revisions", ["topic_id"]
    )

    with op.batch_alter_table("monitoring_runs") as batch:
        batch.add_column(
            sa.Column("engine", sa.String(length=16), nullable=False, server_default="classic")
        )
        batch.add_column(sa.Column("research_topic_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("coverage_status", sa.String(length=16), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("total_sources_examined", sa.Integer(), nullable=False, server_default="0")
        )
    op.create_index("ix_monitoring_runs_engine", "monitoring_runs", ["engine"])
    op.create_index(
        "ix_monitoring_runs_research_topic_id", "monitoring_runs", ["research_topic_id"]
    )

    with op.batch_alter_table("reports") as batch:
        batch.add_column(sa.Column("research_topic_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("engine", sa.String(length=16), nullable=False, server_default="classic")
        )
        batch.add_column(
            sa.Column("coverage_status", sa.String(length=16), nullable=False, server_default="")
        )
    op.create_index("ix_reports_engine", "reports", ["engine"])
    op.create_index("ix_reports_research_topic_id", "reports", ["research_topic_id"])

    with op.batch_alter_table("intelligence_events") as batch:
        batch.add_column(sa.Column("research_topic_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("organization", sa.String(length=200), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column(
                "product_or_project", sa.String(length=200), nullable=False, server_default=""
            )
        )
        batch.add_column(
            sa.Column("event_type", sa.String(length=64), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("entities_json", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("canonical_urls_json", sa.JSON(), nullable=True))
    op.create_index(
        "ix_intelligence_events_research_topic_id", "intelligence_events", ["research_topic_id"]
    )
    op.create_index(
        "ix_intelligence_events_organization", "intelligence_events", ["organization"]
    )
    op.create_index(
        "ix_intelligence_events_product", "intelligence_events", ["product_or_project"]
    )

    with op.batch_alter_table("event_observations") as batch:
        batch.add_column(sa.Column("entities_json", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column("event_type", sa.String(length=64), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("event_date", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("event_observations") as batch:
        batch.drop_column("event_date")
        batch.drop_column("event_type")
        batch.drop_column("entities_json")

    op.drop_index("ix_intelligence_events_product", table_name="intelligence_events")
    op.drop_index("ix_intelligence_events_organization", table_name="intelligence_events")
    op.drop_index(
        "ix_intelligence_events_research_topic_id", table_name="intelligence_events"
    )
    with op.batch_alter_table("intelligence_events") as batch:
        batch.drop_column("canonical_urls_json")
        batch.drop_column("entities_json")
        batch.drop_column("event_type")
        batch.drop_column("product_or_project")
        batch.drop_column("organization")
        batch.drop_column("research_topic_id")

    op.drop_index("ix_reports_research_topic_id", table_name="reports")
    op.drop_index("ix_reports_engine", table_name="reports")
    with op.batch_alter_table("reports") as batch:
        batch.drop_column("coverage_status")
        batch.drop_column("engine")
        batch.drop_column("research_topic_id")

    op.drop_index("ix_monitoring_runs_research_topic_id", table_name="monitoring_runs")
    op.drop_index("ix_monitoring_runs_engine", table_name="monitoring_runs")
    with op.batch_alter_table("monitoring_runs") as batch:
        batch.drop_column("total_sources_examined")
        batch.drop_column("coverage_status")
        batch.drop_column("research_topic_id")
        batch.drop_column("engine")

    op.drop_index("ix_research_topic_revisions_topic_id", table_name="research_topic_revisions")
    op.drop_table("research_topic_revisions")
    op.drop_index("ix_research_topics_last_run_at", table_name="research_topics")
    op.drop_index("ix_research_topics_archived", table_name="research_topics")
    op.drop_index("ix_research_topics_name", table_name="research_topics")
    op.drop_table("research_topics")
