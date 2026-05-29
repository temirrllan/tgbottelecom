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
from bot.services.tz import LOCAL_TZ, local_now, normalize_for_db

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
    now = local_now()
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

# Алиас для обратной совместимости внутри модуля
_normalize_dt = normalize_for_db


async def create_ticket(
    user_id: int,
    data: TicketIn,
    created_by_id: Optional[int] = None,
) -> int:
    """
    Создаёт заявку с материалами и фото в одной транзакции, возвращает внутренний id.
    user_id — исполнитель (монтёр), created_by_id — кто создал (КРОСС или None).
    Личный номер заявки (user_ticket_number) берётся из счётчика монтёра.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            visit = normalize_for_db(data.visit_date) or local_now()

            # Атомарно инкрементим счётчик исполнителя
            user_number = await conn.fetchval(
                """
                INSERT INTO user_ticket_counters (user_id, last_number)
                VALUES ($1, 1)
                ON CONFLICT (user_id) DO UPDATE
                    SET last_number = user_ticket_counters.last_number + 1
                RETURNING last_number
                """,
                user_id,
            )

            ticket_id = await conn.fetchval(
                """
                INSERT INTO tickets (
                    user_id, created_by_id, user_ticket_number,
                    address, problem_description, work_done,
                    visit_date, is_repeat_visit, act_number,
                    customer_name, customer_phone, crm_ticket_number, license_account
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                RETURNING id
                """,
                user_id,
                created_by_id,
                int(user_number),
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
            value = normalize_for_db(value)
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


async def get_ticket_by_number(
    user_id: int, user_ticket_number: int,
) -> Optional[Ticket]:
    """Получает заявку по личному номеру монтёра (#1, #2, …)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM tickets
            WHERE user_id = $1 AND user_ticket_number = $2
            """,
            user_id, user_ticket_number,
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


async def get_ticket(user_id: int, ticket_id: int) -> Optional[Ticket]:
    """Получает одну заявку по внутреннему id с материалами и фото."""
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


async def find_ticket_by_crm(user_id: int, crm_number: str) -> Optional[Ticket]:
    """Поиск заявки по номеру CRM (для упоминаний типа «по 2368874...»)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM tickets
            WHERE user_id = $1 AND crm_ticket_number = $2
            ORDER BY created_at DESC
            LIMIT 1
            """,
            user_id, crm_number,
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


async def get_last_ticket_today(user_id: int) -> Optional[Ticket]:
    """Последняя заявка монтёра, созданная сегодня (по локальной TZ)."""
    pool = _get_pool()
    today = local_now().date()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM tickets
            WHERE user_id = $1
              AND (created_at AT TIME ZONE $2)::date = $3
            ORDER BY created_at DESC
            LIMIT 1
            """,
            user_id, str(LOCAL_TZ), today,
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


async def list_open_tickets(user_id: int, limit: int = 50) -> list[Ticket]:
    """
    Заявки без проставленного work_done — то, что монтёру ещё предстоит сделать.
    Сортировка по visit_date по возрастанию (старые сверху).
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM tickets
            WHERE user_id = $1
              AND (work_done IS NULL OR work_done = '')
            ORDER BY visit_date
            LIMIT $2
            """,
            user_id, limit,
        )
        if not rows:
            return []
        ticket_ids = [r["id"] for r in rows]
        materials = await _fetch_materials(conn, ticket_ids)
        photos = await _fetch_photos(conn, ticket_ids)
    return [
        _row_to_ticket(r, materials.get(r["id"], []), photos.get(r["id"], []))
        for r in rows
    ]


# --- Пользователи: списки и нагрузка ---------------------------------------

async def list_users_except(exclude_ids: set[int]) -> list[dict]:
    """
    Все зарегистрированные пользователи, кроме переданных id.
    Используется КРОСС для выбора монтёра.
    """
    pool = _get_pool()
    if exclude_ids:
        rows = await pool.fetch(
            """
            SELECT id, username, full_name FROM users
            WHERE id <> ALL($1::bigint[])
            ORDER BY full_name NULLS LAST, id
            """,
            list(exclude_ids),
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, username, full_name FROM users
            ORDER BY full_name NULLS LAST, id
            """,
        )
    return [
        {
            "id": int(r["id"]),
            "username": r["username"],
            "full_name": r["full_name"] or f"id{r['id']}",
        }
        for r in rows
    ]


async def get_user(user_id: int) -> Optional[dict]:
    """Полная инфа о пользователе по id."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT id, username, full_name FROM users WHERE id = $1",
        user_id,
    )
    if not row:
        return None
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "full_name": row["full_name"] or f"id{row['id']}",
    }


async def count_open_tickets_for(user_id: int) -> int:
    """Сколько у монтёра открытых (не закрытых) заявок."""
    pool = _get_pool()
    val = await pool.fetchval(
        """
        SELECT COUNT(*) FROM tickets
        WHERE user_id = $1
          AND (work_done IS NULL OR work_done = '')
        """,
        user_id,
    )
    return int(val or 0)


async def get_ticket_for_dispatcher(
    ticket_id: int, dispatcher_id: int,
) -> Optional[Ticket]:
    """
    Получает заявку без проверки исполнителя, но проверяя,
    что она создана этим КРОСС. Нужно для переназначения и удаления.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM tickets WHERE id = $1 AND created_by_id = $2",
            ticket_id, dispatcher_id,
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


async def reassign_ticket(
    ticket_id: int,
    new_user_id: int,
    dispatcher_id: int,
) -> tuple[bool, Optional[int], Optional[int]]:
    """
    Переназначает заявку другому монтёру.
    Возвращает (success, old_user_id, new_user_ticket_number).
    success=False если: заявка не существует / не принадлежит КРОСС /
    закрыта / уже у этого же монтёра.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, user_id, work_done FROM tickets
                WHERE id = $1 AND created_by_id = $2
                FOR UPDATE
                """,
                ticket_id, dispatcher_id,
            )
            if not row:
                return (False, None, None)
            if row["work_done"]:
                return (False, None, None)
            old_user_id = int(row["user_id"])
            if old_user_id == new_user_id:
                return (False, old_user_id, None)

            new_number = await conn.fetchval(
                """
                INSERT INTO user_ticket_counters (user_id, last_number)
                VALUES ($1, 1)
                ON CONFLICT (user_id) DO UPDATE
                    SET last_number = user_ticket_counters.last_number + 1
                RETURNING last_number
                """,
                new_user_id,
            )

            await conn.execute(
                """
                UPDATE tickets SET
                    user_id = $1,
                    user_ticket_number = $2,
                    updated_at = NOW()
                WHERE id = $3
                """,
                new_user_id, int(new_number), ticket_id,
            )
    return (True, old_user_id, int(new_number))


async def delete_ticket(ticket_id: int, dispatcher_id: int) -> tuple[bool, Optional[int], Optional[int]]:
    """
    Удаляет заявку (с CASCADE для материалов и фото).
    Возвращает (success, executor_user_id, user_ticket_number) для уведомления.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT user_id, user_ticket_number FROM tickets
                WHERE id = $1 AND created_by_id = $2
                """,
                ticket_id, dispatcher_id,
            )
            if not row:
                return (False, None, None)
            executor_id = int(row["user_id"])
            number = row["user_ticket_number"]

            await conn.execute(
                "DELETE FROM tickets WHERE id = $1 AND created_by_id = $2",
                ticket_id, dispatcher_id,
            )
    return (True, executor_id, int(number) if number else None)


async def list_dispatcher_inbox(
    dispatcher_id: int, limit: int = 30,
) -> list[Ticket]:
    """Заявки, созданные данным КРОСС-ом (любому монтёру)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM tickets
            WHERE created_by_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            dispatcher_id, limit,
        )
        if not rows:
            return []
        ticket_ids = [r["id"] for r in rows]
        materials = await _fetch_materials(conn, ticket_ids)
        photos = await _fetch_photos(conn, ticket_ids)
    return [
        _row_to_ticket(r, materials.get(r["id"], []), photos.get(r["id"], []))
        for r in rows
    ]


# --- Кэш геокодинга --------------------------------------------------------

async def get_cached_geocode(address: str) -> Optional[dict]:
    """Возвращает кэшированные координаты адреса или None."""
    pool = _get_pool()
    row = await pool.fetchrow(
        "SELECT lat, lng, display_name FROM address_geocache WHERE address = $1",
        address,
    )
    if not row:
        return None
    return {
        "lat": float(row["lat"]),
        "lng": float(row["lng"]),
        "display_name": row["display_name"] or "",
    }


async def save_cached_geocode(
    address: str,
    lat: float,
    lng: float,
    display_name: str = "",
) -> None:
    """Сохраняет координаты адреса в кэш."""
    pool = _get_pool()
    await pool.execute(
        """
        INSERT INTO address_geocache (address, lat, lng, display_name)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (address) DO UPDATE SET
            lat = EXCLUDED.lat,
            lng = EXCLUDED.lng,
            display_name = EXCLUDED.display_name,
            geocoded_at = NOW()
        """,
        address, lat, lng, display_name,
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
        where.append(f"(visit_date AT TIME ZONE ${idx})::date = ${idx + 1}")
        args.append(str(LOCAL_TZ))
        args.append(local_now().date())
        idx += 2
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


async def count_tickets_since(user_id: int, since: datetime) -> int:
    """Количество заявок за период [since, now]."""
    pool = _get_pool()
    val = await pool.fetchval(
        "SELECT COUNT(*) FROM tickets WHERE user_id = $1 AND visit_date >= $2",
        user_id, since,
    )
    return int(val or 0)


async def count_tickets_between(
    user_id: int, since: datetime, until: datetime,
) -> int:
    """Количество заявок в интервале [since, until)."""
    pool = _get_pool()
    val = await pool.fetchval(
        """
        SELECT COUNT(*) FROM tickets
        WHERE user_id = $1
          AND visit_date >= $2
          AND visit_date < $3
        """,
        user_id, since, until,
    )
    return int(val or 0)


async def count_repeats_since(user_id: int, since: datetime) -> int:
    """Сколько заявок было отмечено как повторные."""
    pool = _get_pool()
    val = await pool.fetchval(
        """
        SELECT COUNT(*) FROM tickets
        WHERE user_id = $1
          AND visit_date >= $2
          AND is_repeat_visit
        """,
        user_id, since,
    )
    return int(val or 0)


async def count_with_photos_since(user_id: int, since: datetime) -> int:
    """Заявок с фотографиями за период."""
    pool = _get_pool()
    val = await pool.fetchval(
        """
        SELECT COUNT(DISTINCT t.id) FROM tickets t
        WHERE t.user_id = $1
          AND t.visit_date >= $2
          AND EXISTS (SELECT 1 FROM ticket_photos p WHERE p.ticket_id = t.id)
        """,
        user_id, since,
    )
    return int(val or 0)


async def count_with_act_since(user_id: int, since: datetime) -> int:
    """Заявок с проставленным номером акта."""
    pool = _get_pool()
    val = await pool.fetchval(
        """
        SELECT COUNT(*) FROM tickets
        WHERE user_id = $1
          AND visit_date >= $2
          AND act_number IS NOT NULL
          AND act_number != ''
        """,
        user_id, since,
    )
    return int(val or 0)


async def hour_distribution_since(
    user_id: int, since: datetime,
) -> list[dict]:
    """Распределение заявок по часам суток."""
    pool = _get_pool()
    rows = await pool.fetch(
        """
        SELECT EXTRACT(HOUR FROM visit_date)::int AS hour,
               COUNT(*) AS cnt
        FROM tickets
        WHERE user_id = $1 AND visit_date >= $2
        GROUP BY hour
        ORDER BY hour
        """,
        user_id, since,
    )
    return [{"hour": int(r["hour"]), "count": int(r["cnt"])} for r in rows]


async def top_addresses_since(
    user_id: int, since: datetime, limit: int = 5,
) -> list[dict]:
    """Самые частые адреса за период."""
    pool = _get_pool()
    rows = await pool.fetch(
        """
        SELECT address, COUNT(*) AS cnt
        FROM tickets
        WHERE user_id = $1 AND visit_date >= $2
        GROUP BY address
        HAVING COUNT(*) > 0
        ORDER BY cnt DESC, address
        LIMIT $3
        """,
        user_id, since, limit,
    )
    return [{"address": r["address"], "count": int(r["cnt"])} for r in rows]


async def top_materials_since(
    user_id: int, since: datetime, limit: int = 10,
) -> list[dict]:
    """Топ материалов за период."""
    pool = _get_pool()
    rows = await pool.fetch(
        """
        SELECT m.name, m.unit, SUM(m.quantity) AS total
        FROM materials m
        JOIN tickets t ON t.id = m.ticket_id
        WHERE t.user_id = $1 AND t.visit_date >= $2
        GROUP BY m.name, m.unit
        ORDER BY total DESC
        LIMIT $3
        """,
        user_id, since, limit,
    )
    return [
        {"name": r["name"], "unit": r["unit"], "total": r["total"]}
        for r in rows
    ]


async def count_similar_tickets_in_area(
    address: str,
    days: int = 14,
    exclude_ticket_id: Optional[int] = None,
) -> int:
    """
    Считает заявки на ту же улицу/район за последние N дней — по ВСЕЙ бригаде.
    Нужно для подсказки «на этом узле уже N жалоб за период».
    """
    if not address or not address.strip():
        return 0
    key = _address_key(address)
    pool = _get_pool()
    query = """
        SELECT COUNT(*) FROM tickets
        WHERE address ILIKE $1
          AND visit_date >= NOW() - $2::interval
    """
    args: list = [f"%{key}%", timedelta(days=days)]
    if exclude_ticket_id is not None:
        query += " AND id <> $3"
        args.append(exclude_ticket_id)
    val = await pool.fetchval(query, *args)
    return int(val or 0)


async def count_recent_visits_at_address(
    user_id: int,
    address: str,
    days: int = 30,
    exclude_ticket_id: Optional[int] = None,
) -> int:
    """
    Сколько раз монтёр уже выезжал по похожему адресу за последние N дней.
    Сравнение нечувствительно к регистру; ищем по ключевой части адреса.
    """
    pool = _get_pool()
    if not address or not address.strip():
        return 0

    key = _address_key(address)
    query = """
        SELECT COUNT(*) FROM tickets
        WHERE user_id = $1
          AND address ILIKE $2
          AND visit_date >= NOW() - $3::interval
    """
    args: list = [user_id, f"%{key}%", timedelta(days=days)]
    if exclude_ticket_id is not None:
        query += " AND id <> $4"
        args.append(exclude_ticket_id)
    val = await pool.fetchval(query, *args)
    return int(val or 0)


def _address_key(address: str) -> str:
    """
    Достаёт «опознавательную» часть адреса:
    самое длинное буквенное слово (4+ символа) + первое число после него.
    Например, «ул. Абая 45 кв 12» → «Абая 45».
    """
    import re
    matches = list(re.finditer(r"[А-ЯЁA-Zа-яёa-z\-]{4,}", address))
    if not matches:
        return address.strip()[:20]
    # Берём самое длинное слово
    word_match = max(matches, key=lambda m: len(m.group()))
    word = word_match.group()
    # Ищем первое число после этого слова
    tail = address[word_match.end():]
    num_match = re.search(r"\d+", tail)
    if num_match:
        return f"{word} {num_match.group()}"
    return word


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
        user_ticket_number=row["user_ticket_number"],
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
        created_by_id=row["created_by_id"],
        departed_at=row["departed_at"],
        arrived_at=row["arrived_at"],
        finishing_at=row["finishing_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        materials=materials,
        photos=photos or [],
    )


_STATUS_FIELDS = {"departed_at", "arrived_at", "finishing_at"}


async def set_ticket_status(
    user_id: int,
    ticket_id: int,
    field: str,
) -> bool:
    """
    Проставляет timestamp статуса (departed_at/arrived_at/finishing_at).
    Только для незакрытых заявок (без work_done) и только если ещё не проставлено.
    """
    if field not in _STATUS_FIELDS:
        return False
    pool = _get_pool()
    result = await pool.execute(
        f"""
        UPDATE tickets
        SET {field} = NOW(), updated_at = NOW()
        WHERE id = $1 AND user_id = $2
          AND {field} IS NULL
          AND (work_done IS NULL OR work_done = '')
        """,
        ticket_id, user_id,
    )
    return not result.endswith(" 0")


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
