"""Instructor-provided assignment context: notes and uploaded documents.

Revision ID: 0008
Revises: 0007
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("assignment", sa.Column("context_notes", sa.Text(), nullable=True))
    op.create_table(
        "assignment_context_file",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["assignment_id"], ["assignment.id"],
            name=op.f("fk_assignment_context_file_assignment_id_assignment"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assignment_context_file")),
    )
    op.create_index(
        op.f("ix_assignment_context_file_assignment_id"), "assignment_context_file", ["assignment_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_assignment_context_file_assignment_id"), table_name="assignment_context_file")
    op.drop_table("assignment_context_file")
    op.drop_column("assignment", "context_notes")
