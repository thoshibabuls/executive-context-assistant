"""Phase 3 reminder rules (TECHNICAL_DESIGN.md §15.1-§15.4; IMPLEMENTATION_PLAN.md §0.7 deferred
unit tests): calendar arithmetic, quiet hours, every rule's slots and fire times, keys and
fingerprints, bounded learning from dismissals and snoozes, and snooze limits. Pure functions."""

from __future__ import annotations

import datetime
import uuid

import pytest

from eca.attention.reminder_rules import (
    DEADLINE_THRESHOLD_MAX,
    Calendar,
    ItemFacts,
    Learning,
    MeetingFacts,
    ThreadFacts,
    follow_up_candidate,
    item_candidates,
    material_key,
    meeting_candidate,
    snooze_until,
)

CAL = Calendar.of("America/Los_Angeles", {})
UTC = datetime.UTC
# Wednesday 30 Sep 2026, 10:00 in Los Angeles.
NOW = datetime.datetime(2026, 9, 30, 17, 0, tzinfo=UTC)
PERSON = uuid.UUID(int=7)


def _la(day: int, hour: int, month: int = 9) -> datetime.datetime:
    return datetime.datetime(2026, month, day, hour, tzinfo=CAL.tz).astimezone(UTC)


def _item(**kw: object) -> ItemFacts:
    base: dict[str, object] = {
        "id": uuid.UUID(int=1),
        "direction": "waiting_for",
        "due_at": None,
        "due_precision": "day",
        "due_kind": "by",
        "lifecycle_status": "open",
        "verification_status": "confirmed",
        "confidence_band": "high",
        "priority": 50.0,
        "last_activity_at": NOW - datetime.timedelta(hours=1),
        "created_at": NOW - datetime.timedelta(days=1),
        "archived": False,
        "person_id": PERSON,
    }
    base.update(kw)
    return ItemFacts(**base)  # type: ignore[arg-type]


def _kinds(
    item: ItemFacts, now: datetime.datetime = NOW, learning: Learning | None = None
) -> dict[tuple[str, str], datetime.datetime]:
    return {
        (c.reminder_type, c.slot): c.fire_at for c in item_candidates(item, now, CAL, learning or Learning())
    }


# ---------------------------------------------------------------- calendar


def test_calendar_work_hours_slots_and_working_days() -> None:
    assert CAL.in_work_hours(NOW) and not CAL.in_work_hours(_la(30, 19))
    assert CAL.next_work_slot(NOW) == NOW
    assert CAL.next_work_slot(_la(30, 7)) == _la(30, 9)  # before work start: same morning
    assert CAL.next_work_slot(_la(2, 19, month=10)) == _la(5, 9, month=10)  # Friday evening → Monday
    assert CAL.add_working_days(_la(2, 9, month=10), 1) == _la(5, 9, month=10)
    assert CAL.working_days_between(_la(2, 9, month=10), _la(6, 9, month=10)) == 2  # Mon, Tue
    assert CAL.working_days_between(NOW, NOW - datetime.timedelta(days=1)) == 0


def test_quiet_hours_cross_midnight_and_custom_settings() -> None:
    assert CAL.in_quiet_hours(_la(30, 22)) and CAL.in_quiet_hours(_la(30, 6)) and not CAL.in_quiet_hours(NOW)
    custom = Calendar.of(
        "UTC", {"quiet_start_hour": 12, "quiet_end_hour": 14, "days": [5, 6], "start_hour": 99}
    )
    assert custom.in_quiet_hours(datetime.datetime(2026, 9, 30, 13, tzinfo=UTC))
    assert custom.days == (5, 6) and custom.start_hour == 9  # an invalid hour falls back
    assert not Calendar.of("UTC", {"quiet_start_hour": 8, "quiet_end_hour": 8}).in_quiet_hours(NOW)
    assert Calendar.of("Not/AZone", {}).tz.key == "UTC"


# ---------------------------------------------------------------- item rules


def test_deadline_slots_and_hard_due_day() -> None:
    due = _la(2, 17, month=10)  # Friday 17:00
    kinds = _kinds(_item(due_at=due))
    assert kinds[("deadline", "day_before")] == _la(1, 9, month=10)
    assert kinds[("deadline", "due_day")] == _la(2, 9, month=10)  # hard: "by" with day precision
    soft = _kinds(_item(due_at=due, due_kind="around"))
    assert ("deadline", "due_day") not in soft and ("deadline", "day_before") in soft
    assert _kinds(_item(due_at=due, priority=30.0)) == {}  # below the threshold of 40
    mine = _kinds(_item(due_at=due, direction="my_commitment"))
    assert ("deadline", "due_day") not in mine  # the commitment rule covers the due day


def test_overdue_cycles_every_two_working_days_for_my_items_only() -> None:
    due = _la(28, 17)  # Monday
    assert _kinds(_item(due_at=due, direction="my_task"))[("overdue", "cycle:0")] == _la(29, 9)
    later = _la(2, 12, month=10)  # Friday: 3 working days after the first fire
    assert ("overdue", "cycle:1") in _kinds(_item(due_at=due, direction="my_task"), now=later)
    assert not any(k[0] == "overdue" for k in _kinds(_item(due_at=due, direction="waiting_for")))


def test_commitment_due_today_or_tomorrow() -> None:
    tomorrow = _la(1, 17, month=10)
    assert _kinds(_item(due_at=tomorrow, direction="my_commitment"))[("commitment", "once")] == _la(
        1, 9, month=10
    )
    far = _la(9, 17, month=10)
    assert ("commitment", "once") not in _kinds(_item(due_at=far, direction="my_commitment"))


def test_waiting_for_past_due_silence_or_five_quiet_working_days() -> None:
    past_due = _item(due_at=_la(28, 17), last_activity_at=_la(25, 10))
    assert ("waiting_for", "once") in _kinds(past_due)
    replied_after_due = _item(due_at=_la(28, 17), last_activity_at=_la(29, 10))
    assert ("waiting_for", "once") not in _kinds(replied_after_due)
    quiet = _item(last_activity_at=_la(22, 10))  # 6 working days before now
    assert _kinds(quiet)[("waiting_for", "once")] == NOW
    assert ("waiting_for", "once") not in _kinds(_item(last_activity_at=_la(28, 10)))


@pytest.mark.parametrize(
    "change",
    [{"lifecycle_status": "done"}, {"archived": True}, {"verification_status": "rejected"}],
)
def test_closed_archived_or_rejected_items_never_remind(change: dict[str, object]) -> None:
    assert _kinds(_item(due_at=_la(28, 17), direction="my_task", **change)) == {}


def test_low_confidence_suggestions_are_not_proactive() -> None:
    cands = item_candidates(
        _item(due_at=_la(2, 17, month=10), verification_status="suggested", confidence_band="low"),
        NOW,
        CAL,
        Learning(),
    )
    assert cands and not any(c.proactive_eligible for c in cands)


def test_keys_change_with_material_facts_only() -> None:
    a = item_candidates(_item(due_at=_la(2, 17, month=10)), NOW, CAL, Learning())
    b = item_candidates(_item(due_at=_la(2, 17, month=10), priority=60.0), NOW, CAL, Learning())
    c = item_candidates(_item(due_at=_la(5, 17, month=10)), NOW, CAL, Learning())
    assert [x.fingerprint for x in a] == [x.fingerprint for x in b]  # priority is not material
    assert {x.fingerprint for x in a}.isdisjoint({x.fingerprint for x in c})  # a moved due date is
    assert len({x.fingerprint for x in a}) == len(a)  # one per slot
    assert material_key("x", None) == material_key("x", None)


# ---------------------------------------------------------------- learning


def test_dismissals_raise_the_deadline_threshold_and_suppress_proactive() -> None:
    learning = Learning(dismissals={(PERSON, "deadline"): 2})
    assert learning.deadline_threshold(PERSON) == 60.0 and learning.deadline_threshold(None) == 40.0
    assert Learning(dismissals={(PERSON, "deadline"): 9}).deadline_threshold(PERSON) == DEADLINE_THRESHOLD_MAX
    assert _kinds(_item(due_at=_la(2, 17, month=10)), learning=learning) == {}  # priority 50 < 60
    suppressed = Learning(dismissals={(PERSON, "waiting_for"): 3})
    cands = item_candidates(_item(last_activity_at=_la(22, 10)), NOW, CAL, suppressed)
    assert cands and not cands[0].proactive_eligible


def test_snooze_learning_shifts_fire_times_within_bounds() -> None:
    hours = datetime.timedelta(hours=1)
    assert Learning(snooze_delays={"deadline": [hours, hours]}).shift("deadline") == datetime.timedelta(0)
    assert Learning(snooze_delays={"deadline": [hours, 2 * hours, 3 * hours]}).shift("deadline") == 2 * hours
    capped = Learning(snooze_delays={"deadline": [10 * hours] * 3})
    assert capped.shift("deadline") == datetime.timedelta(hours=4)
    due = _la(1, 11, month=10)
    shifted = _kinds(_item(due_at=due), learning=capped)
    assert shifted[("deadline", "day_before")] == _la(30, 13)  # 09:00 + 4 h, still before the due time


# ---------------------------------------------------------------- threads, meetings, snooze


def test_follow_up_after_three_working_days() -> None:
    thread = ThreadFacts(
        uuid.UUID(int=3), last_message_at=_la(24, 10), last_inbound_at=None, priority=10.0, person_id=PERSON
    )
    c = follow_up_candidate(thread, NOW, CAL, Learning())
    assert c is not None and c.reminder_type == "follow_up" and c.fire_at == NOW
    recent = ThreadFacts(
        uuid.UUID(int=3), last_message_at=_la(29, 10), last_inbound_at=None, priority=10.0, person_id=PERSON
    )
    assert follow_up_candidate(recent, NOW, CAL, Learning()) is None


def test_meeting_prep_needs_an_open_item_or_question_within_24_hours() -> None:
    start = NOW + datetime.timedelta(hours=2)
    c = meeting_candidate(
        MeetingFacts(uuid.UUID(int=4), start, "confirmed", open_items=0, priority=1.0, open_questions=1), NOW
    )
    assert c is not None and c.fire_at == start - datetime.timedelta(minutes=30)
    assert c.reason["open_questions"] == 1
    assert meeting_candidate(MeetingFacts(uuid.UUID(int=4), start, "confirmed", 0, 1.0), NOW) is None
    assert meeting_candidate(MeetingFacts(uuid.UUID(int=4), start, "cancelled", 3, 1.0), NOW) is None
    far = NOW + datetime.timedelta(hours=30)
    assert meeting_candidate(MeetingFacts(uuid.UUID(int=4), far, "confirmed", 3, 1.0), NOW) is None


def test_snooze_presets_and_limits() -> None:
    assert snooze_until(NOW, CAL, preset="1h", until=None) == NOW + datetime.timedelta(hours=1)
    assert snooze_until(NOW, CAL, preset="tomorrow", until=None) == _la(1, 9, month=10)
    assert snooze_until(NOW, CAL, preset=None, until=NOW + datetime.timedelta(days=8)) is None  # > 7 days
    assert snooze_until(NOW, CAL, preset=None, until=NOW - datetime.timedelta(minutes=1)) is None
    assert snooze_until(NOW, CAL, preset=None, until=datetime.datetime(2026, 10, 1, 9)) is None  # naive
    assert snooze_until(NOW, CAL, preset="next-year", until=None) is None
