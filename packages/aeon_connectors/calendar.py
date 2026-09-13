"""Read-only Google Calendar event adapter."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple
import urllib.parse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ._google import perform_request, require_secret
from .transport import ConnectorError, HttpTransport


_PROVIDER = "google_calendar"
_OVERRUN_ASSUMPTION = "uncalibrated overrun, zero prior"


class GoogleCalendarClient:
    def __init__(self, access_token: str, transport: Any = None) -> None:
        self._access_token = require_secret(access_token, "access_token")
        self._transport = transport if transport is not None else HttpTransport()

    def list_events(
        self,
        calendar_id: str,
        *,
        time_min: Optional[str] = None,
        time_max: Optional[str] = None,
        sync_token: Optional[str] = None,
        page_token: Optional[str] = None,
        timezone: str = "Europe/Paris",
        authorized_flexible_ids: Iterable[str] = (),
    ) -> dict:
        if not isinstance(calendar_id, str) or not calendar_id:
            raise ConnectorError(_PROVIDER, "invalid_arguments", False, "calendar_id is required")
        zone = _timezone(timezone)
        horizon_start = _boundary(time_min, "time_min")
        horizon_end = _boundary(time_max, "time_max")
        if horizon_start is not None and horizon_end is not None and horizon_end <= horizon_start:
            raise ConnectorError(
                _PROVIDER, "invalid_arguments", False, "time_max must be after time_min"
            )
        if sync_token is not None and (time_min is not None or time_max is not None):
            raise ConnectorError(
                _PROVIDER,
                "invalid_arguments",
                False,
                "sync_token cannot be combined with time bounds",
            )
        if sync_token is not None and (not isinstance(sync_token, str) or not sync_token):
            raise ConnectorError(_PROVIDER, "invalid_arguments", False, "sync_token is invalid")
        if page_token is not None and (not isinstance(page_token, str) or not page_token):
            raise ConnectorError(_PROVIDER, "invalid_arguments", False, "page_token is invalid")
        flexible_ids = _flexible_ids(authorized_flexible_ids)

        params = {"singleEvents": "true", "showDeleted": "true", "timeZone": timezone}
        if sync_token is not None:
            params["syncToken"] = sync_token
        else:
            params["orderBy"] = "startTime"
            if time_min is not None:
                params["timeMin"] = time_min
            if time_max is not None:
                params["timeMax"] = time_max
        if page_token is not None:
            params["pageToken"] = page_token
        encoded_calendar = urllib.parse.quote(calendar_id, safe="")
        url = (
            f"https://www.googleapis.com/calendar/v3/calendars/{encoded_calendar}/events?"
            + urllib.parse.urlencode(params)
        )
        response = perform_request(
            _PROVIDER,
            self._transport,
            "GET",
            url,
            {"Authorization": f"Bearer {self._access_token}"},
        )
        items = response.body.get("items", [])
        if not isinstance(items, list):
            raise ConnectorError(_PROVIDER, "invalid_response", False, "Calendar items are invalid")

        events = []
        deleted = []
        excluded = []
        for index, raw in enumerate(items):
            fallback_id = f"unknown:{index}"
            if not isinstance(raw, dict):
                excluded.append({"id": fallback_id, "reason": "malformed_event"})
                continue
            event_id = raw.get("id")
            if not isinstance(event_id, str) or not event_id:
                excluded.append({"id": fallback_id, "reason": "malformed_event"})
                continue
            if raw.get("status") == "cancelled":
                deleted.append(event_id)
                continue
            if _is_all_day(raw):
                excluded.append({"id": event_id, "reason": "all_day_requires_policy"})
                continue
            try:
                event = _canonical_event(
                    raw,
                    event_id,
                    calendar_id,
                    zone,
                    flexible_ids,
                    horizon_start,
                    horizon_end,
                )
            except ValueError as exc:
                excluded.append({"id": event_id, "reason": str(exc)})
                continue
            events.append(event)

        return {
            "events": events,
            "deleted_event_ids": deleted,
            "excluded_events": excluded,
            "next_page_token": _optional_token(response.body.get("nextPageToken")),
            "next_sync_token": _optional_token(response.body.get("nextSyncToken")),
        }


def _timezone(value: Any) -> ZoneInfo:
    if not isinstance(value, str) or not value:
        raise ConnectorError(_PROVIDER, "invalid_arguments", False, "timezone is invalid")
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError:
        raise ConnectorError(_PROVIDER, "invalid_arguments", False, "timezone is invalid") from None


def _boundary(value: Any, name: str) -> Optional[datetime]:
    if value is None:
        return None
    try:
        instant = _parse_datetime(value, None)
    except ValueError:
        raise ConnectorError(_PROVIDER, "invalid_arguments", False, f"{name} is invalid") from None
    if instant.tzinfo is None:
        raise ConnectorError(_PROVIDER, "invalid_arguments", False, f"{name} needs an offset")
    return instant


def _flexible_ids(value: Iterable[str]) -> frozenset:
    if isinstance(value, (str, bytes)):
        raise ConnectorError(
            _PROVIDER, "invalid_arguments", False, "authorized_flexible_ids must be an iterable"
        )
    try:
        result = frozenset(value)
    except TypeError:
        raise ConnectorError(
            _PROVIDER, "invalid_arguments", False, "authorized_flexible_ids is invalid"
        ) from None
    if any(not isinstance(item, str) or not item for item in result):
        raise ConnectorError(
            _PROVIDER, "invalid_arguments", False, "authorized_flexible_ids is invalid"
        )
    return result


def _is_all_day(raw: Dict[str, Any]) -> bool:
    start = raw.get("start")
    end = raw.get("end")
    return (
        isinstance(start, dict)
        and "date" in start
        and "dateTime" not in start
    ) or (
        isinstance(end, dict)
        and "date" in end
        and "dateTime" not in end
    )


def _canonical_event(
    raw: Dict[str, Any],
    event_id: str,
    calendar_id: str,
    default_zone: ZoneInfo,
    flexible_ids: frozenset,
    horizon_start: Optional[datetime],
    horizon_end: Optional[datetime],
) -> dict:
    start = _event_datetime(raw.get("start"), default_zone)
    end = _event_datetime(raw.get("end"), default_zone)
    if end <= start:
        raise ValueError("invalid_event_time")
    if (horizon_start is not None and start < horizon_start) or (
        horizon_end is not None and end > horizon_end
    ):
        raise ValueError("event_outside_horizon")
    title = raw.get("summary", "")
    if not isinstance(title, str):
        raise ValueError("malformed_event")
    organizer = raw.get("organizer", {})
    organizer_self = isinstance(organizer, dict) and organizer.get("self") is True
    attendees = raw.get("attendees", [])
    if not isinstance(attendees, list) or any(not isinstance(item, dict) for item in attendees):
        raise ValueError("malformed_event")
    attendees_omitted = raw.get("attendeesOmitted") is True
    has_third_party = any(item.get("self") is not True for item in attendees)
    controlled_without_third_party = (
        organizer_self and not attendees_omitted and not has_third_party
    )
    classification = (
        "flexible"
        if event_id in flexible_ids and controlled_without_third_party
        else "fixed"
    )
    return {
        "id": event_id,
        "title": title,
        "planned_start": start.isoformat(),
        "planned_end": end.isoformat(),
        "classification": classification,
        "private": controlled_without_third_party,
        "overrun_minutes": {"min": 0, "mode": 0, "max": 0},
        "source": {
            "provider": _PROVIDER,
            "reference": f"google_calendar:{calendar_id}:{event_id}",
            "synthetic": False,
            "assumption": _OVERRUN_ASSUMPTION,
        },
        "calendar_id": calendar_id,
        "etag": raw.get("etag"),
        "updated": raw.get("updated"),
        "organizer_self": organizer_self,
        "attendees_count": len(attendees),
        "attendees_omitted": attendees_omitted,
        "recurring_event_id": raw.get("recurringEventId"),
        "visibility": raw.get("visibility"),
    }


def _event_datetime(value: Any, default_zone: ZoneInfo) -> datetime:
    if not isinstance(value, dict) or not isinstance(value.get("dateTime"), str):
        raise ValueError("malformed_event")
    zone = default_zone
    if "timeZone" in value:
        try:
            zone = ZoneInfo(value["timeZone"])
        except (TypeError, ZoneInfoNotFoundError) as exc:
            raise ValueError("malformed_event") from exc
    return _parse_datetime(value["dateTime"], zone)


def _parse_datetime(value: Any, fallback_zone: Optional[ZoneInfo]) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid datetime")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("invalid datetime") from exc
    if result.tzinfo is None:
        if fallback_zone is None:
            raise ValueError("missing timezone")
        result = result.replace(tzinfo=fallback_zone)
    return result


def _optional_token(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None
