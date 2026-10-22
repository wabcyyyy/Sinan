"""V21: durable planning dispatch and shared SSE replay."""

from alembic import op

from app.db.schema_source import statements_between

revision = "0021_planning_dispatch"
down_revision = "0020_itinerary_care"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = statements_between(21, 21)
    if not statements:
        raise RuntimeError("V21 SQL is missing")
    for _filename, statement in statements:
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("Restore an isolated backup; migrations are append-only.")
