"""Replace email login with passkeys and a hashed recovery code."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261121_09"
down_revision: str | None = "20261116_08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_login_tokens_expires_at", table_name="login_tokens")
    op.drop_index("ix_login_tokens_email", table_name="login_tokens")
    op.drop_table("login_tokens")

    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.drop_column("users", "email_verified_at")
    op.drop_column("users", "email")
    op.drop_column("user_sessions", "user_agent")

    op.create_table(
        "passkey_credentials",
        sa.Column("credential_id", sa.LargeBinary(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_passkey_credentials_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("credential_id", name="pk_passkey_credentials"),
    )
    op.create_index("ix_passkey_credentials_user_id", "passkey_credentials", ["user_id"])

    op.create_table(
        "auth_challenges",
        sa.Column("challenge", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("challenge", name="pk_auth_challenges"),
    )
    op.create_index("ix_auth_challenges_expires_at", "auth_challenges", ["expires_at"])

    op.create_table(
        "recovery_codes",
        sa.Column("lookup", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("salt", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_recovery_codes_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("lookup", name="pk_recovery_codes"),
    )
    op.create_index("ix_recovery_codes_user_id", "recovery_codes", ["user_id"])


def downgrade() -> None:
    """Best-effort rollback; email addresses cannot be reconstructed."""

    op.drop_index("ix_recovery_codes_user_id", table_name="recovery_codes")
    op.drop_table("recovery_codes")
    op.drop_index("ix_auth_challenges_expires_at", table_name="auth_challenges")
    op.drop_table("auth_challenges")
    op.drop_index("ix_passkey_credentials_user_id", table_name="passkey_credentials")
    op.drop_table("passkey_credentials")

    op.add_column("user_sessions", sa.Column("user_agent", sa.String(length=500), nullable=True))
    op.add_column(
        "users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("users", sa.Column("email", sa.String(length=320), nullable=True))

    op.create_table(
        "login_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("code_salt", sa.String(length=32), nullable=False),
        sa.Column("link_token_hash", sa.String(length=64), nullable=False),
        sa.Column("attempts_left", sa.Integer(), nullable=False),
        sa.Column("request_ip", sa.String(length=64), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_login_tokens"),
        sa.UniqueConstraint("link_token_hash", name="uq_login_tokens_link_token_hash"),
    )
    op.create_index("ix_login_tokens_email", "login_tokens", ["email"])
    op.create_index("ix_login_tokens_expires_at", "login_tokens", ["expires_at"])
