"""建立续期审议所需的全部业务表。"""

from alembic import op

from skill_engine.models import metadata

revision = "002_renewal_review"
down_revision = "001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    for table in reversed(metadata.sorted_tables):
        op.drop_table(table.name)
