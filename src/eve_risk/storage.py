from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from eve_risk.domain import CharacterIdentity, Killmail, ShipTypeInfo


class Base(DeclarativeBase):
    pass


class CharacterRecord(Base):
    __tablename__ = "characters"

    character_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(128), index=True)
    corporation_id: Mapped[int] = mapped_column(BigInteger)
    corporation_name: Mapped[str] = mapped_column(String(255))
    alliance_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    alliance_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ShipTypeRecord(Base):
    __tablename__ = "ship_types"

    type_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(255))
    group_id: Mapped[int] = mapped_column(BigInteger)
    group_name: Mapped[str] = mapped_column(String(255))
    category_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    role: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class KillmailRecord(Base):
    __tablename__ = "killmails"

    killmail_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    killmail_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    solar_system_id: Mapped[int] = mapped_column(BigInteger, index=True)
    solo: Mapped[bool] = mapped_column(Boolean, default=False)
    total_value: Mapped[float | None] = mapped_column(Float, nullable=True)


class ParticipantRecord(Base):
    __tablename__ = "killmail_participants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    killmail_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("killmails.killmail_id", ondelete="CASCADE"), index=True
    )
    character_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    corporation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    alliance_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ship_type_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    is_victim: Mapped[bool] = mapped_column(Boolean, default=False)
    final_blow: Mapped[bool] = mapped_column(Boolean, default=False)


class FetchStateRecord(Base):
    __tablename__ = "fetch_states"

    character_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    direction: Mapped[str] = mapped_column(String(16), primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    item_count: Mapped[int] = mapped_column(Integer)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)


class JobRecord(Base):
    __tablename__ = "analysis_jobs"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    requested_count: Mapped[int] = mapped_column(Integer)
    resolved_count: Mapped[int] = mapped_column(Integer, default=0)
    data_events: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


def create_session_factory(database_url: str) -> tuple[object, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


class Repository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def save_analysis_data(
        self,
        identities: list[CharacterIdentity],
        ship_types: dict[int, ShipTypeInfo],
        killmails: list[Killmail],
        now: datetime,
        fetch_states: list[tuple[int, str, int, bool]] | None = None,
    ) -> None:
        async with self.sessions() as session:
            for identity in identities:
                await session.merge(
                    CharacterRecord(
                        character_id=identity.character_id,
                        name=identity.name,
                        corporation_id=identity.corporation_id,
                        corporation_name=identity.corporation_name,
                        alliance_id=identity.alliance_id,
                        alliance_name=identity.alliance_name,
                        updated_at=now,
                    )
                )
            for ship_type in ship_types.values():
                await session.merge(
                    ShipTypeRecord(
                        type_id=ship_type.type_id,
                        name=ship_type.name,
                        group_id=ship_type.group_id,
                        group_name=ship_type.group_name,
                        category_id=ship_type.category_id,
                        role=ship_type.role.value,
                        updated_at=now,
                    )
                )
            for killmail in killmails:
                await session.merge(
                    KillmailRecord(
                        killmail_id=killmail.killmail_id,
                        killmail_time=killmail.killmail_time,
                        solar_system_id=killmail.solar_system_id,
                        solo=killmail.solo,
                        total_value=killmail.total_value,
                    )
                )
                await session.execute(
                    delete(ParticipantRecord).where(
                        ParticipantRecord.killmail_id == killmail.killmail_id
                    )
                )
                session.add_all(
                    ParticipantRecord(
                        killmail_id=killmail.killmail_id,
                        character_id=participant.character_id,
                        corporation_id=participant.corporation_id,
                        alliance_id=participant.alliance_id,
                        ship_type_id=participant.ship_type_id,
                        is_victim=participant.is_victim,
                        final_blow=participant.final_blow,
                    )
                    for participant in killmail.participants
                )
            for character_id, direction, item_count, truncated in fetch_states or []:
                await session.merge(
                    FetchStateRecord(
                        character_id=character_id,
                        direction=direction,
                        fetched_at=now,
                        item_count=item_count,
                        truncated=truncated,
                    )
                )
            await session.commit()

    async def record_job_started(
        self, request_id: str, requested_count: int, created_at: datetime
    ) -> None:
        async with self.sessions() as session:
            await session.merge(
                JobRecord(
                    request_id=request_id,
                    status="running",
                    requested_count=requested_count,
                    resolved_count=0,
                    data_events=0,
                    created_at=created_at,
                )
            )
            await session.commit()

    async def record_job_finished(
        self,
        request_id: str,
        *,
        status: str,
        resolved_count: int,
        data_events: int,
        completed_at: datetime,
        error_code: str | None = None,
    ) -> None:
        async with self.sessions() as session:
            record = await session.get(JobRecord, request_id)
            if record is None:
                record = JobRecord(
                    request_id=request_id,
                    status=status,
                    requested_count=0,
                    resolved_count=resolved_count,
                    data_events=data_events,
                    created_at=completed_at,
                    completed_at=completed_at,
                    error_code=error_code,
                )
                session.add(record)
            else:
                record.status = status
                record.resolved_count = resolved_count
                record.data_events = data_events
                record.completed_at = completed_at
                record.error_code = error_code
            await session.commit()
