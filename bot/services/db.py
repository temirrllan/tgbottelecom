"""Сервис работы с PostgreSQL через asyncpg."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import asyncpg

from bot.models.schemas import (
    Material,
    MaterialIn,
    Ticket,
    TicketIn,
    TicketUpdate,
)

logger = logging.getLogger(__name__)

# Глобальный пул подключений
_pool: Optional[asyncpg.Pool] = None


# --- Инициализация ----------------------------------------------------------

async def init_db() -> None:
    """Создаёт пул и применяет миграции."""
    global _pool
    dsn = os.environ["DATABASE_URL"]
    _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
    logger.info("Пул подключений к БД создан")

    # Применяем схему из init.sql
    sql_path = Path(__file__).resolve().parent.parent.parent / "migrations" / "init.sql"
    if sql_path.exists():
        sql = sql_path.read_text(encoding="utf-8")
        async with _pool.acquire() as conn:
            await conn.execute(sql)
        logger.info("Миграции применены")


async def close_db() -> None:
    """Закрывает пул подключений."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("БД не инициализирована — вызовите init_db()")
    return _pool


# --- Пользователи -----------------------------------------------------------

async def upsert_user(
    user_id: int,
    username: Optional[str],
    full_name: Optional[str],
) -> None:
    """Добавляет нового монтёра или обновляет данные существующего."""
    pool = _get_pool()
    await pool.execute(
        """
        INSERT INTO users (id, username, full_name)
        VALUES ($1, $2, $3)
        ON CONFLICT (id) DO UPDATE
            SET username = EXCLUDED.username,
                full_name = EXCLUDED.full_name,
                last_active_at = NOW()
        """,
        user_id, username, full_name,
    )


async def touch_user(user_id: int) -> None:
    """Обновляет last_active_at — фиксирует, что монтёр пользуется ботом."""
    pool = _get_pool()
    await pool.execute(
        "UPDATE users SET last_active_at = NOW() WHERE id = $1",
        user_id,
    )


async def get_idle_users(
    idle_hours: int = 4,
    work_hour_start: int = 8,
    work_hour_end: int = 18,
) -> list[int]:
    """
    Возвращает id пользователей, не писавших боту больше idle_hours часов.
    Только в рабочее время (08:00–18:00) и в рабочие дни (пн–сб).
    """
    pool = _get_pool()
    now = datetime.now(timezone.utc).astimezone()
    if now.weekday() == 6:  # воскресенье
        return []
    if not (work_hour_start <= now.hour < work_hour_end):
        return []

    rows = await pool.fetch(
        """
        SELECT id FROM users
        WHERE last_active_at < NOW() - $1::interval
        """,
        timedelta(hours=idle_hours),
    )
    return [row["id"] for row in rows]


# --- Заявки -----------------------------------------------------------------

def _normalize_dt(dt: Optional[datetime]) -> Optional[datetime]:
    """Привязывает наивный datetime к локальной таймзоне (для TIMESTAMPTZ)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt


async def create_ticket(user_id: int, data: TicketIn) -> int:
    """Создаёт заявку с материалами и фото в одной транзакции, возвращает id."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            visit = _normalize_dt(data.visit_date) or datetime.now(timezone.utc)
            ticket_id = await conn.fetchval(
                """
                INSERT INTO tickets (
                    user_id, address, problem_description, work_done,
                    visit_date, is_repeat_visit, act_number,
                    customer_name, customer_phone, crm_ticket_number, license_account
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                RETURNING id
                """,
                user_id,
                data.address,
                data.problem_description,
                data.work_done,
                visit,
                data.is_repeat_visit,
                data.act_number,
                data.customer_name,
                data.customer_phone,
                data.crm_ticket_number,
                data.license_account,
            )
            await _insert_materials(conn, ticket_id, data.materials)
            await _insert_photos(conn, ticket_id, data.photos)
    return int(ticket_id)


async def _insert_photos(
    conn: asyncpg.Connection,
    ticket_id: int,
    file_ids: list[str],
) -> None:
    """Вставляет список file_id фотографий для заявки."""
    if not file_ids:
        return
    await conn.executemany(
        """
        INSERT INTO ticket_photos (ticket_id, file_id)
        VALUES ($1, $2)
        """,
        [(ticket_id, fid) for fid in file_ids],
    )


async def _insert_materials(
    conn: asyncpg.Connection,
    ticket_id: int,
    materials: list[MaterialIn],
) -> None:
    """Вставляет список материалов для заявки."""
    if not materials:
        return
    await conn.executemany(
        """
        INSERT INTO materials (ticket_id, name, quantity, unit)
        VALUES ($1, $2, $3, $4)
        """,
        [
            (ticket_id, m.name, m.quantity, m.unit)
            for m in materials
        ],
    )


async def update_ticket(
    user_id: int,
    ticket_id: int,
    upd: TicketUpdate,
) -> bool:
    """
    Обновляет заявку. Возвращает True, если заявка обновлена.
    Принадлежность проверяется по user_id.
    """
    pool = _get_pool()
    fields: list[str] = []
    values: list = []
    idx = 1

    for field, value in upd.model_dump(exclude_unset=True).items():
        if field in ("materials", "photos"):
            continue
        if field == "visit_date":
            value = _normalize_dt(value)
        fields.append(f"{field} = ${idx}")
        values.append(value)
        idx += 1

    async with pool.acquire() as conn:
        async with conn.transaction():
            if fields:
                fields.append("updated_at = NOW()")
                query = (
                    f"UPDATE tickets SET {', '.join(fields)} "
                    f"WHERE id = ${idx} AND user_id = ${idx + 1}"
                )
                values.extend([ticket_id, user_id])
                result = await conn.execute(query, *values)
                if result.endswith(" 0"):
                    return False
            else:
                # Изменения только в материалах — проверим, что заявка принадлежит юзеру
                owns = await conn.fetchval(
                    "SELECT 1 FROM tickets WHERE id = $1 AND user_id = $2",
                    ticket_id, user_id,
                )
                if not owns:
                    return False

            if upd.materials is not None:
                # Заменяем материалы полностью
                await conn.execute(
                    "DELETE FROM materials WHERE ticket_id = $1",
                    ticket_id,
                )
                await _insert_materials(conn, ticket_id, upd.materials)

            if upd.photos is not None:
                # Заменяем фото полностью
                await conn.execute(
                    "DELETE FROM ticket_photos WHERE ticket_id = $1",
                    ticket_id,
                )
                await _insert_photos(conn, ticket_id, upd.photos)

    return True


async def get_ticket(user_id: int, ticket_id: int) -> Optional[Ticket]:
    """Получает одну заявку с материалами и фото (только если принадлежит юзеру)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM tickets WHERE id = $1 AND user_id = $2",
            ticket_id, user_id,
        )
        if not row:
            return None
        materials = await _fetch_materials(conn, [ticket_id])
        photos = await _fetch_photos(conn, [ticket_id])
    return _row_to_ticket(
        row,
        materials.get(ticket_id, []),
        photos.get(ticket_id, []),
    )


async def get_last_ticket_today(user_id: int) -> Optional[Ticket]:
    """Последняя заявка монтёра, созданная сегодня."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM tickets
            WHERE user_id = $1
              AND created_at::date = CURRENT_DATE
            ORDER BY created_at DESC
            LIMIT 1
            """,
            user_id,
        )
        if not row:
            return None
        materials = await _fetch_materials(conn, [row["id"]])
        photos = await _fetch_photos(conn, [row["id"]])
    return _row_to_ticket(
        row,
        materials.get(row["id"], []),
        photos.get(row["id"], []),
    )


async def list_tickets(
    user_id: int,
    period: Optional[str] = None,
    limit: int = 50,
    search_address: Optional[str] = None,
) -> list[Ticket]:
    """
    Получает заявки за период.
    period: 'today' | 'week' | 'month' | None (последние limit штук).
    """
    pool = _get_pool()
    where = ["user_id = $1"]
    args: list = [user_id]
    idx = 2

    if period == "today":
        where.append("visit_date::date = CURRENT_DATE")
    elif period == "week":
        where.append("visit_date >= NOW() - INTERVAL '7 days'")
    elif period == "month":
        where.append("visit_date >= NOW() - INTERVAL '30 days'")

    if search_address:
        where.append(f"address ILIKE ${idx}")
        args.append(f"%{search_address}%")
        idx += 1

    args.append(limit)
    query = f"""
        SELECT * FROM tickets
        WHERE {' AND '.join(where)}
        ORDER BY visit_date DESC
        LIMIT ${idx}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
        if not rows:
            return []
        ticket_ids = [r["id"] for r in rows]
        materials = await _fetch_materials(conn, ticket_ids)
        photos = await _fetch_photos(conn, ticket_ids)

    return [
        _row_to_ticket(r, materials.get(r["id"], []), photos.get(r["id"], []))
        for r in rows
    ]


async def materials_summary(
    user_id: int,
    period: str = "month",
) -> list[dict]:
    """
    Сводка материалов за период.
    Возвращает список: [{name, unit, total_quantity}].
    """
    pool = _get_pool()
    interval = {
        "today": "1 day",
        "week": "7 days",
        "month": "30 days",
    }.get(period, "30 days")

    rows = await pool.fetch(
        f"""
        SELECT m.name, m.unit, SUM(m.quantity) AS total
        FROM materials m
        JOIN tickets t ON t.id = m.ticket_id
        WHERE t.user_id = $1
          AND t.visit_date >= NOW() - INTERVAL '{interval}'
        GROUP BY m.name, m.unit
        ORDER BY total DESC
        """,
        user_id,
    )
    return [
        {"name": r["name"], "unit": r["unit"], "total": r["total"]}
        for r in rows
    ]


# --- Вспомогательные функции -----------------------------------------------

async def _fetch_materials(
    conn: asyncpg.Connection,
    ticket_ids: list[int],
) -> dict[int, list[Material]]:
    """Загружает материалы для нескольких заявок одним запросом."""
    if not ticket_ids:
        return {}
    rows = await conn.fetch(
        "SELECT * FROM materials WHERE ticket_id = ANY($1::bigint[])",
        ticket_ids,
    )
    result: dict[int, list[Material]] = {}
    for row in rows:
        mat = Material(
            id=row["id"],
            ticket_id=row["ticket_id"],
            name=row["name"],
            quantity=Decimal(row["quantity"]),
            unit=row["unit"],
        )
        result.setdefault(row["ticket_id"], []).append(mat)
    return result


async def _fetch_photos(
    conn: asyncpg.Connection,
    ticket_ids: list[int],
) -> dict[int, list[str]]:
    """Загружает file_id фотографий для нескольких заявок одним запросом."""
    if not ticket_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT ticket_id, file_id FROM ticket_photos
        WHERE ticket_id = ANY($1::bigint[])
        ORDER BY id
        """,
        ticket_ids,
    )
    result: dict[int, list[str]] = {}
    for row in rows:
        result.setdefault(row["ticket_id"], []).append(row["file_id"])
    return result


def _row_to_ticket(
    row,
    materials: list[Material],
    photos: Optional[list[str]] = None,
) -> Ticket:
    return Ticket(
        id=row["id"],
        user_id=row["user_id"],
        address=row["address"],
        problem_description=row["problem_description"],
        work_done=row["work_done"],
        visit_date=row["visit_date"],
        is_repeat_visit=row["is_repeat_visit"],
        act_number=row["act_number"],
        customer_name=row["customer_name"],
        customer_phone=row["customer_phone"],
        crm_ticket_number=row["crm_ticket_number"],
        license_account=row["license_account"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        materials=materials,
        photos=photos or [],
    )


# --- История переписки -----------------------------------------------------

async def add_history(user_id: int, role: str, content: str) -> None:
    """Сохраняет одно сообщение в историю переписки."""
    pool = _get_pool()
    await pool.execute(
        """
        INSERT INTO conversation_history (user_id, role, content)
        VALUES ($1, $2, $3)
        """,
        user_id, role, content,
    )


async def get_recent_history(user_id: int, limit: int = 10) -> list[dict]:
    """Возвращает последние сообщения в хронологическом порядке."""
    pool = _get_pool()
    rows = await pool.fetch(
        """
        SELECT role, content FROM conversation_history
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT $2
        """,
        user_id, limit,
    )
    # Разворачиваем — нужен прямой порядок (старые → новые)
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def trim_history(user_id: int, keep: int = 20) -> None:
    """Оставляет только последние `keep` сообщений в истории пользователя."""
    pool = _get_pool()
    await pool.execute(
        """
        DELETE FROM conversation_history
        WHERE user_id = $1
          AND id NOT IN (
              SELECT id FROM conversation_history
              WHERE user_id = $1
              ORDER BY created_at DESC
              LIMIT $2
          )
        """,
        user_id, keep,
    )
