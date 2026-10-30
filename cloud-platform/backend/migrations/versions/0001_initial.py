"""Initial cloud tables. Immutable initial migration."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
revision = '0001'
down_revision = None
branch_labels = None
depends_on = None
J = sa.JSON().with_variant(JSONB(), 'postgresql')
D = sa.DateTime(timezone=True)

def upgrade():
    op.create_table('devices', sa.Column('device_id', sa.String(128), primary_key=True),
        sa.Column('created_at', D, nullable=False), sa.Column('last_seen', D), sa.Column('last_sync', D))
    op.create_table('events', sa.Column('event_id', sa.String(36), primary_key=True),
        sa.Column('device_id', sa.String(128), sa.ForeignKey('devices.device_id'), nullable=False),
        sa.Column('timestamp', D, nullable=False), sa.Column('received_at', D, nullable=False),
        sa.Column('event_type', sa.String(40), nullable=False), sa.Column('data', J, nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False))
    op.create_index('ix_events_device_id', 'events', ['device_id'])
    op.create_index('ix_events_time_id', 'events', ['timestamp', 'event_id'])
    op.create_index('ix_events_device_type_time', 'events', ['device_id', 'event_type', 'timestamp'])
    op.create_index('ix_events_received', 'events', ['received_at'])
    op.create_table('analysis_tasks', sa.Column('task_id', sa.String(36), primary_key=True),
        sa.Column('device_id', sa.String(128), sa.ForeignKey('devices.device_id'), nullable=False),
        sa.Column('identity', sa.String(128), nullable=False), sa.Column('idempotency_key', sa.String(128), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False), sa.Column('request', J, nullable=False),
        sa.Column('status', sa.String(16), nullable=False), sa.Column('created_at', D, nullable=False),
        sa.Column('updated_at', D, nullable=False), sa.Column('result_event_id', sa.String(36), sa.ForeignKey('events.event_id')),
        sa.Column('error', J), sa.UniqueConstraint('identity', 'idempotency_key', name='uq_task_identity_key'))
    op.create_index('ix_analysis_tasks_device_id', 'analysis_tasks', ['device_id'])
    op.create_index('ix_analysis_tasks_status', 'analysis_tasks', ['status'])
    op.create_table('analysis_results', sa.Column('task_id', sa.String(36), sa.ForeignKey('analysis_tasks.task_id'), primary_key=True),
        sa.Column('event_id', sa.String(36), sa.ForeignKey('events.event_id'), nullable=False, unique=True),
        sa.Column('created_at', D, nullable=False), sa.Column('result', J, nullable=False))

def downgrade():
    op.drop_table('analysis_results')
    op.drop_table('analysis_tasks')
    op.drop_table('events')
    op.drop_table('devices')
