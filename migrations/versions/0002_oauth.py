"""Add encrypted OAuth authorization state."""

import sqlalchemy as sa
from alembic import op

revision = "0002_oauth"
down_revision = "0001_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oauth_states",
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("principal_json", sa.Text(), nullable=False),
        sa.Column("delivery_target_json", sa.Text(), nullable=False),
        sa.Column("required_scopes_json", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("state_hash"),
    )


def downgrade() -> None:
    raise RuntimeError("song-agent schema migrations are forward-only")
