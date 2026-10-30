from datetime import datetime, timezone
from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.types import TypeDecorator


def now():
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError('Naive datetime forbidden')
            return value.astimezone(timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return value


class Base(DeclarativeBase):
    pass


json_type = JSON().with_variant(JSONB(), 'postgresql')


class Device(Base):
    __tablename__ = 'devices'
    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    last_seen: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_sync: Mapped[datetime | None] = mapped_column(UTCDateTime())


class EventRow(Base):
    __tablename__ = 'events'
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey('devices.device_id'), index=True)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime())
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    event_type: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(json_type)
    content_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (Index('ix_events_time_id', 'timestamp', 'event_id'),
                      Index('ix_events_device_type_time', 'device_id', 'event_type', 'timestamp'),
                      Index('ix_events_received', 'received_at'))


class TaskRow(Base):
    __tablename__ = 'analysis_tasks'
    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey('devices.device_id'), index=True)
    identity: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(json_type)
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    result_event_id: Mapped[str | None] = mapped_column(ForeignKey('events.event_id'))
    error: Mapped[dict | None] = mapped_column(json_type)
    __table_args__ = (UniqueConstraint('identity', 'idempotency_key', name='uq_task_identity_key'),)


class AnalysisResult(Base):
    __tablename__ = 'analysis_results'
    task_id: Mapped[str] = mapped_column(ForeignKey('analysis_tasks.task_id'), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey('events.event_id'), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    result: Mapped[dict] = mapped_column(json_type)


def database(url):
    options = {'connect_args': {'check_same_thread': False}} if url.startswith('sqlite') else {}
    engine = create_engine(url, pool_pre_ping=True, **options)
    return engine, sessionmaker(engine, expire_on_commit=False)
