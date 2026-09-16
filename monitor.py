#!/usr/bin/env python3
"""Read-only KORAIL seat monitor for one fixed trip window.

The script never reserves, pays for, or cancels a ticket.  It only searches
for one adult general-class seat and writes a GitHub-ready notification when a
seat becomes newly available.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


SEOUL_TZ = ZoneInfo("Asia/Seoul")
WINDOW_START = datetime(2026, 9, 23, 18, 30, tzinfo=SEOUL_TZ)
WINDOW_END = datetime(2026, 9, 24, 19, 0, tzinfo=SEOUL_TZ)
BOOKING_URL = "https://www.korail.com/ticket/search"
STATE_PATH = Path(os.environ.get("MONITOR_STATE_PATH", "monitor_state.json"))
NOTIFICATION_PATH = Path(".monitor/notification.md")
OUTAGE_THRESHOLD = 3
MAX_PAGES_PER_DATE = 20


@dataclass(frozen=True)
class AvailableTrain:
    key: str
    train_type: str
    train_no: str
    departure: datetime
    arrival: datetime


def _default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "available_keys": [],
        "failure_count": 0,
        "outage_notified": False,
        "last_error_type": None,
    }


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    state = _default_state()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return state
    if not isinstance(loaded, dict):
        return state
    keys = loaded.get("available_keys")
    if isinstance(keys, list) and all(isinstance(item, str) for item in keys):
        state["available_keys"] = sorted(set(keys))
    count = loaded.get("failure_count")
    if isinstance(count, int) and count >= 0:
        state["failure_count"] = count
    state["outage_notified"] = bool(loaded.get("outage_notified", False))
    error_type = loaded.get("last_error_type")
    if isinstance(error_type, str) or error_type is None:
        state["last_error_type"] = error_type
    return state


def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _digits(value: Any, length: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text.isdigit() or len(text) > length:
        return None
    return text.zfill(length)


def _parse_train_datetimes(train: Any) -> tuple[datetime, datetime] | None:
    raw = getattr(train, "raw", {})
    if not isinstance(raw, dict):
        raw = {}

    departure_date = _digits(
        getattr(train, "departure_date", None)
        or getattr(train, "run_date", None),
        8,
    )
    departure_time = _digits(getattr(train, "departure_time", None), 6)
    arrival_time = _digits(getattr(train, "arrival_time", None), 6)
    if not departure_date or not departure_time or not arrival_time:
        return None

    try:
        departure = datetime.strptime(
            departure_date + departure_time, "%Y%m%d%H%M%S"
        ).replace(tzinfo=SEOUL_TZ)
    except ValueError:
        return None

    arrival_date = _digits(raw.get("h_arv_dt") or raw.get("arvDt"), 8)
    if arrival_date:
        try:
            arrival = datetime.strptime(
                arrival_date + arrival_time, "%Y%m%d%H%M%S"
            ).replace(tzinfo=SEOUL_TZ)
        except ValueError:
            return None
    else:
        try:
            arrival = datetime.strptime(
                departure_date + arrival_time, "%Y%m%d%H%M%S"
            ).replace(tzinfo=SEOUL_TZ)
        except ValueError:
            return None
        if arrival < departure:
            arrival += timedelta(days=1)

    return departure, arrival


def _train_type(train: Any) -> str:
    for value in (
        getattr(train, "train_class_name", None),
        getattr(train, "train_group_name", None),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "열차"


def filter_available_trains(
    trains: Iterable[Any], now: datetime
) -> list[AvailableTrain]:
    """Keep only future Seoul→Daejeon trains with a general seat."""
    available: dict[str, AvailableTrain] = {}
    for train in trains:
        # The mobile app's evidenced code for an immediately reservable general
        # seat is 11.  Standing/free/special seats are deliberately excluded.
        if getattr(train, "general_reservation_code", None) != "11":
            continue

        departure_name = getattr(train, "departure_station_name", None)
        arrival_name = getattr(train, "arrival_station_name", None)
        if departure_name and departure_name != "서울":
            continue
        if arrival_name and arrival_name != "대전":
            continue

        datetimes = _parse_train_datetimes(train)
        if datetimes is None:
            continue
        departure, arrival = datetimes
        if departure < WINDOW_START or arrival > WINDOW_END:
            continue
        if departure <= now:
            continue

        train_no = str(getattr(train, "train_no", "")).strip()
        if not train_no:
            continue
        key = (
            f"{departure.isoformat()}|{arrival.isoformat()}|"
            f"{train_no}|general"
        )
        available[key] = AvailableTrain(
            key=key,
            train_type=_train_type(train),
            train_no=train_no,
            departure=departure,
            arrival=arrival,
        )
    return sorted(available.values(), key=lambda item: item.departure)


def _search_dates(now: datetime) -> list[tuple[str, str]]:
    searches = [
        ("20260923", "183000"),
        ("20260924", "000000"),
    ]
    result: list[tuple[str, str]] = []
    today = now.strftime("%Y%m%d")
    for date_value, minimum_time in searches:
        if date_value < today:
            continue
        if date_value == today:
            minimum_time = max(minimum_time, now.strftime("%H%M%S"))
        result.append((date_value, minimum_time))
    return result


def query_korail(member_id: str, password: str, now: datetime) -> list[Any]:
    """Query KORAIL's mobile service without performing any mutation."""
    from korail_mobile_api import (
        KorailClient,
        KorailNoResultsError,
        TrainSearchQuery,
    )

    client = KorailClient()
    trains: list[Any] = []
    try:
        client.login(member_id, password)
        for date_value, time_value in _search_dates(now):
            query = TrainSearchQuery(
                "서울",
                "대전",
                date_value,
                departure_time=time_value,
                passengers=1,
                include_srt=False,
            )
            continuation = None
            seen_cursors: set[tuple[str, str, str, str]] = set()
            for _ in range(MAX_PAGES_PER_DATE):
                try:
                    page = client.search_trains(query, continuation=continuation)
                except KorailNoResultsError:
                    break
                trains.extend(page.trains)
                continuation = page.next_page()
                if continuation is None:
                    break
                cursor = (
                    continuation.query_station_no,
                    continuation.query_train_no,
                    continuation.page_count,
                    continuation.query_train_no2,
                )
                if cursor in seen_cursors:
                    break
                seen_cursors.add(cursor)
    finally:
        try:
            client.logout()
        except Exception:
            pass
        client.close()
    return trains


def _fmt_time(value: datetime) -> str:
    return value.strftime("%m/%d %H:%M")


def build_seat_notification(
    trains: list[AvailableTrain], owner: str
) -> str:
    lines = [
        f"@{owner} **서울역 → 대전역 일반실 취소표가 새로 확인됐습니다.**",
        "",
    ]
    for train in trains:
        lines.append(
            f"- **{train.train_type} {train.train_no}** · "
            f"{_fmt_time(train.departure)} 출발 → "
            f"{_fmt_time(train.arrival)} 도착"
        )
    lines.extend(
        [
            "",
            f"[코레일 공식 예매 화면 열기]({BOOKING_URL})",
            "",
            "> 좌석을 확보하거나 예약한 것이 아닙니다. 취소표는 빠르게 사라질 수 있으니 바로 확인하세요.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_outage_notification(owner: str, failure_count: int) -> str:
    return (
        f"@{owner} 코레일 조회가 **{failure_count}회 연속 실패**했습니다.\n\n"
        "좌석이 있다고 추정하거나 알림을 보내지 않았습니다. "
        "저장소의 최근 Actions 실행 기록을 확인해 주세요.\n"
    )


def _write_notification(body: str | None) -> None:
    NOTIFICATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTIFICATION_PATH.write_text(body or "", encoding="utf-8")


def _set_outputs(**values: str | bool) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as output:
        for key, value in values.items():
            text = str(value).lower() if isinstance(value, bool) else str(value)
            text = text.replace("\r", " ").replace("\n", " ")
            output.write(f"{key}={text}\n")


def main() -> int:
    now_override = os.environ.get("MONITOR_NOW")
    if now_override:
        now = datetime.fromisoformat(now_override).astimezone(SEOUL_TZ)
    else:
        now = datetime.now(SEOUL_TZ)

    before = load_state()
    state = dict(before)
    _write_notification(None)

    if now >= WINDOW_END:
        state["available_keys"] = []
        save_state(state)
        _set_outputs(
            status="expired",
            expired=True,
            has_notification=False,
            state_changed=state != before,
        )
        print("Monitoring window has ended; the workflow will disable itself.")
        return 0

    member_id = os.environ.get("KORAIL_MEMBER_ID", "").strip()
    password = os.environ.get("KORAIL_PASSWORD", "")
    if not member_id or not password:
        _set_outputs(
            status="configuration_required",
            expired=False,
            has_notification=False,
            state_changed=False,
        )
        print("::warning::KORAIL_MEMBER_ID and KORAIL_PASSWORD secrets are required.")
        return 0

    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "snu-js")
    try:
        rows = query_korail(member_id, password, now)
        available = filter_available_trains(rows, now)
    except Exception as error:
        failure_count = int(state.get("failure_count", 0)) + 1
        state["failure_count"] = failure_count
        state["last_error_type"] = type(error).__name__
        should_notify = (
            failure_count >= OUTAGE_THRESHOLD
            and not bool(state.get("outage_notified", False))
        )
        if should_notify:
            state["outage_notified"] = True
            _write_notification(build_outage_notification(owner, failure_count))
        save_state(state)
        _set_outputs(
            status="query_failed",
            expired=False,
            has_notification=should_notify,
            notification_kind="outage" if should_notify else "none",
            notification_title="[조회 장애] 코레일 취소표 모니터",
            state_changed=state != before,
        )
        print(f"KORAIL query failed: {type(error).__name__}")
        return 0

    current_keys = {train.key for train in available}
    previous_keys = set(state.get("available_keys", []))
    newly_available = [
        train for train in available if train.key not in previous_keys
    ]
    state.update(
        {
            "available_keys": sorted(current_keys),
            "failure_count": 0,
            "outage_notified": False,
            "last_error_type": None,
        }
    )
    if newly_available:
        _write_notification(build_seat_notification(newly_available, owner))
    save_state(state)
    _set_outputs(
        status="seat_found" if newly_available else "no_change",
        expired=False,
        has_notification=bool(newly_available),
        notification_kind="seat" if newly_available else "none",
        notification_title="[취소표 발견] 서울→대전 일반실",
        state_changed=state != before,
    )
    print(
        f"Checked {len(rows)} rows; {len(available)} available; "
        f"{len(newly_available)} newly available."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
