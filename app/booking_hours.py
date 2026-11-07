"""Workspace time zone and appointment availability rules."""
from datetime import date, datetime, time, timezone
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from app.account import current_user, membership
from app.cloud import api

router = APIRouter(prefix="/api/account/organizations/{oid}", tags=["booking-hours"])
Clock = Annotated[str, Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")]
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
OUTSIDE_HOURS = "That time is outside this business’s booking hours."


class Window(BaseModel):
    start: Clock
    end: Clock

    @model_validator(mode="after")
    def ordered(self):
        if self.start >= self.end:
            raise ValueError("Closing time must be after opening time on the same day.")
        return self


Windows = Annotated[list[Window], Field(max_length=3)]


class Schedule(BaseModel):
    week: dict[str, Windows] = Field(default_factory=dict, max_length=7)
    exceptions: dict[date, Windows] = Field(default_factory=dict, max_length=50)

    @model_validator(mode="after")
    def valid_schedule(self):
        if self.week.keys() - set(WEEKDAYS):
            raise ValueError("Invalid weekday.")
        for windows in (*self.week.values(), *self.exceptions.values()):
            windows.sort(key=lambda window: window.start)
            if any(left.end > right.start for left, right in zip(windows, windows[1:])):
                raise ValueError("Time windows must not overlap.")
        return self


class BookingSettings(BaseModel):
    timezone: str = Field(min_length=1, max_length=100)
    hours: Schedule | None = None

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, name):
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Enter a valid time zone, such as America/New_York.") from None
        return name


def get_settings(oid: str) -> BookingSettings:
    rows = api("organizations", params={"id": f"eq.{oid}", "select": "timezone,booking_hours"})
    if not rows:
        raise HTTPException(404, "Workspace not found.")
    row = rows[0]
    return BookingSettings(timezone=row.get("timezone") or "America/New_York", hours=row.get("booking_hours"))


@router.get("/booking-hours")
def get_booking_hours(oid: UUID, user=Depends(current_user)):
    membership(str(oid), user)
    return get_settings(str(oid))


@router.put("/booking-hours")
def save_booking_hours(oid: UUID, body: BookingSettings, user=Depends(current_user)):
    membership(str(oid), user, ("owner", "admin"))
    api("organizations", "PATCH", params={"id": f"eq.{oid}"},
        data={"timezone": body.timezone, "booking_hours": body.hours.model_dump(mode="json") if body.hours else None},
        prefer="return=minimal")
    return body


def slot_allowed(settings: BookingSettings, start: datetime, end: datetime) -> bool:
    if settings.hours is None:
        return True
    zone = ZoneInfo(settings.timezone)
    local_start, local_end = start.astimezone(zone), end.astimezone(zone)
    day = local_start.date()
    if day != local_end.date():
        return False
    windows = settings.hours.exceptions.get(day, settings.hours.week.get(WEEKDAYS[day.weekday()], []))
    # Compare whole instants. For an ambiguous clock, open on its first
    # occurrence and close on its last. Reject nonexistent clock boundaries.
    def boundary(clock: str, fold: int):
        local = datetime.combine(day, time.fromisoformat(clock), tzinfo=zone).replace(fold=fold)
        utc = local.astimezone(timezone.utc)
        return utc if utc.astimezone(zone).replace(tzinfo=None) == local.replace(tzinfo=None) else None

    start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    for window in windows:
        opening, closing = boundary(window.start, 0), boundary(window.end, 1)
        if opening is not None and closing is not None and opening <= start_utc < end_utc <= closing:
            return True
    return False


def within_booking_hours(oid: str, start: datetime, end: datetime) -> bool:
    return slot_allowed(get_settings(oid), start, end)
