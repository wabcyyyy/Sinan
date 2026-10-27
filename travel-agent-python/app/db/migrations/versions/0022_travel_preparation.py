"""V22: private travel preparation."""

from alembic import op

from app.db.schema_source import statements_between

revision = "0022_travel_preparation"
down_revision = "0021_planning_dispatch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for _filename, statement in statements_between(22, 22):
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("Restore an isolated backup; migrations are append-only.")
