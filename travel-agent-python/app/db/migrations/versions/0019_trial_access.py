"""V19: invitation-only trial and CNY budget ledger; SQL remains the source."""

from alembic import op

from app.db.schema_source import statements_between

revision = "0019_trial_access"
down_revision = "0018_seed_city_consumption"
branch_labels = None
depends_on = None


def upgrade() -> None:
    statements = statements_between(19, 19)
    if not statements:
        raise RuntimeError("V19 SQL is missing")
    for _filename, statement in statements:
        op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    raise NotImplementedError("Restore a backup to an isolated database; migrations are append-only.")
