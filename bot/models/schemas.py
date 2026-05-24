"""Pydantic-модели для проекта."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field


# --- Материалы ---------------------------------------------------------------

class MaterialIn(BaseModel):
    """Материал, извлечённый ИИ из текста монтёра."""
    name: str
    quantity: Decimal = Field(default=Decimal("0"))
    unit: str = "шт"


class Material(MaterialIn):
    """Материал из БД (с id и ticket_id)."""
    id: int
    ticket_id: int


# --- Заявки ------------------------------------------------------------------

class TicketIn(BaseModel):
    """Данные новой заявки, извлечённые ИИ из текста или скриншота CRM."""
    address: str
    problem_description: Optional[str] = None
    work_done: Optional[str] = None
    visit_date: Optional[datetime] = None
    is_repeat_visit: bool = False
    act_number: Optional[str] = None
    # Поля абонента и CRM, извлекаются из скриншота заявки
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    crm_ticket_number: Optional[str] = None
    license_account: Optional[str] = None
    materials: list[MaterialIn] = Field(default_factory=list)
    # Telegram file_id фотографий, прикреплённых к заявке
    photos: list[str] = Field(default_factory=list)


class TicketUpdate(BaseModel):
    """Поля, которые можно изменить при редактировании заявки."""
    address: Optional[str] = None
    problem_description: Optional[str] = None
    work_done: Optional[str] = None
    visit_date: Optional[datetime] = None
    is_repeat_visit: Optional[bool] = None
    act_number: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    crm_ticket_number: Optional[str] = None
    license_account: Optional[str] = None
    materials: Optional[list[MaterialIn]] = None
    photos: Optional[list[str]] = None


class Ticket(BaseModel):
    """Заявка из БД."""
    id: int                              # внутренний PK, для FK и связок
    user_ticket_number: Optional[int] = None  # личный номер у этого монтёра (#1, #2, …)
    user_id: int                         # исполнитель (монтёр)
    created_by_id: Optional[int] = None  # кто создал (если null — монтёр сам)
    address: str
    problem_description: Optional[str] = None
    work_done: Optional[str] = None
    visit_date: datetime
    is_repeat_visit: bool = False
    act_number: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    crm_ticket_number: Optional[str] = None
    license_account: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    materials: list[Material] = Field(default_factory=list)
    photos: list[str] = Field(default_factory=list)


# --- Ответ ИИ ----------------------------------------------------------------

ActionType = Literal["SAVE_TICKET", "QUERY", "EDIT_TICKET", "CHAT"]


class AIResponse(BaseModel):
    """JSON-ответ от Claude после разбора сообщения."""
    action: ActionType
    data: dict = Field(default_factory=dict)
    reply: str = ""
