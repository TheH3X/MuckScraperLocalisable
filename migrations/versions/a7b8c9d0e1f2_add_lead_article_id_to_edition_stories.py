"""add lead_article_id to edition_stories

Revision ID: a7b8c9d0e1f2
Revises: 0ee4dbd42d1b
Create Date: 2026-07-14 00:40:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7b8c9d0e1f2'
down_revision = '0ee4dbd42d1b'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('edition_stories', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lead_article_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_edition_stories_lead_article_id',
            'articles',
            ['lead_article_id'],
            ['id'],
        )


def downgrade():
    with op.batch_alter_table('edition_stories', schema=None) as batch_op:
        batch_op.drop_constraint('fk_edition_stories_lead_article_id', type_='foreignkey')
        batch_op.drop_column('lead_article_id')
