"""Initial ledger schema."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "person",
        sa.Column("person_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("slack_user_id", sa.Text(), nullable=True),
        sa.Column("jira_account_id", sa.Text(), nullable=True),
        sa.Column("github_login", sa.Text(), nullable=True),
        sa.Column("manager_id", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.ForeignKeyConstraint(["manager_id"], ["person.person_id"]),
        sa.PrimaryKeyConstraint("person_id"),
        sa.UniqueConstraint("slack_user_id"),
        sa.UniqueConstraint("jira_account_id"),
    )

    op.create_table(
        "epic",
        sa.Column("epic_key", sa.Text(), nullable=False),
        sa.Column("current_name", sa.Text(), nullable=False),
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("jira_status", sa.Text(), nullable=True),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("epic_key"),
    )

    op.create_table(
        "status_entry",
        sa.Column(
            "entry_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("week_ending", sa.Date(), nullable=False),
        sa.Column("person_id", sa.Text(), nullable=False),
        sa.Column("epic_key", sa.Text(), nullable=True),
        sa.Column("epic_name_snapshot", sa.Text(), nullable=True),
        sa.Column("project", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("blocker", sa.Text(), nullable=True),
        sa.Column("ask", sa.Text(), nullable=True),
        sa.Column("draft_outcome", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Text(), nullable=True),
        sa.Column("needs_human", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("extra", postgresql.JSONB(), server_default="{}", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("supersedes_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_current", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("drafted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("length(btrim(outcome)) > 0", name="outcome_not_blank"),
        sa.CheckConstraint("jsonb_typeof(evidence) = 'array'", name="evidence_is_array"),
        sa.CheckConstraint(
            "extract(isodow from week_ending) = 5",
            name="week_ending_is_friday",
        ),
        sa.ForeignKeyConstraint(["confirmed_by"], ["person.person_id"]),
        sa.ForeignKeyConstraint(["epic_key"], ["epic.epic_key"]),
        sa.ForeignKeyConstraint(["person_id"], ["person.person_id"]),
        sa.ForeignKeyConstraint(["supersedes_entry_id"], ["status_entry.entry_id"]),
        sa.PrimaryKeyConstraint("entry_id"),
    )
    op.create_index(
        "status_entry_current_uniq",
        "status_entry",
        ["person_id", "week_ending", sa.text("coalesce(epic_key, entry_id::text)")],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_index(
        "status_entry_week_idx",
        "status_entry",
        ["week_ending"],
        postgresql_where=sa.text("is_current"),
    )
    op.create_index(
        "status_entry_epic_idx",
        "status_entry",
        ["epic_key", "week_ending"],
        postgresql_where=sa.text("is_current"),
    )
    op.create_index(
        "status_entry_review_idx",
        "status_entry",
        ["week_ending"],
        postgresql_where=sa.text("is_current AND needs_human"),
    )
    op.create_index(
        "status_entry_evidence_gin",
        "status_entry",
        ["evidence"],
        postgresql_using="gin",
    )

    op.create_table(
        "participation",
        sa.Column("person_id", sa.Text(), nullable=False),
        sa.Column("week_ending", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("draft_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminder_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("regenerated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("regenerate_reason", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["person_id"], ["person.person_id"]),
        sa.PrimaryKeyConstraint("person_id", "week_ending"),
    )

    op.create_table(
        "report_run",
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("week_ending", sa.Date(), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("output_uri", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded", sa.Boolean(), server_default="false", nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("report_run_week_idx", "report_run", ["week_ending", "generated_at"])

    op.create_table(
        "report_entry",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entry_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("section", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["entry_id"], ["status_entry.entry_id"]),
        sa.ForeignKeyConstraint(["run_id"], ["report_run.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "entry_id"),
    )

    op.create_table(
        "flag",
        sa.Column(
            "flag_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("week_ending", sa.Date(), nullable=False),
        sa.Column("person_id", sa.Text(), nullable=True),
        sa.Column("epic_key", sa.Text(), nullable=True),
        sa.Column("flag_type", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("acknowledged", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["epic_key"], ["epic.epic_key"]),
        sa.ForeignKeyConstraint(["person_id"], ["person.person_id"]),
        sa.PrimaryKeyConstraint("flag_id"),
    )
    op.create_index(
        "flag_week_idx",
        "flag",
        ["week_ending"],
        postgresql_where=sa.text("NOT acknowledged"),
    )


def downgrade() -> None:
    op.drop_index("flag_week_idx", table_name="flag")
    op.drop_table("flag")
    op.drop_table("report_entry")
    op.drop_index("report_run_week_idx", table_name="report_run")
    op.drop_table("report_run")
    op.drop_table("participation")
    op.drop_index("status_entry_evidence_gin", table_name="status_entry")
    op.drop_index("status_entry_review_idx", table_name="status_entry")
    op.drop_index("status_entry_epic_idx", table_name="status_entry")
    op.drop_index("status_entry_week_idx", table_name="status_entry")
    op.drop_index("status_entry_current_uniq", table_name="status_entry")
    op.drop_table("status_entry")
    op.drop_table("epic")
    op.drop_table("person")
