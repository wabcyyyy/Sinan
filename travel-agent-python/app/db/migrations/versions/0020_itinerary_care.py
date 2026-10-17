"""V20: itinerary care task state; SQL remains the source."""

from alembic import op

from app.db.schema_source import statements_between

revision = "0020_itinerary_care"
down_revision = "0019_trial_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = statements_between(20, 20)
    if not statements:
        raise RuntimeError("V20 SQL is missing")
    for _filename, statement in statements:
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("Restore a backup to an isolated database; migrations are append-only.")
