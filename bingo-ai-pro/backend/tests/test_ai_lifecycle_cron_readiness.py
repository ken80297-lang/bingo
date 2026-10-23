from datetime import datetime, timedelta
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.ai_lifecycle_cron_once import TAIPEI, current_draw_slot, draw_datetime, prediction_is_timely, wait_for_current_draw


class Clock:
    def __init__(self, value):
        self.value = value
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


def test_waits_for_current_issue_instead_of_processing_previous_draw():
    slot = datetime(2026, 10, 6, 22, 10, tzinfo=TAIPEI)
    clock = Clock(slot + timedelta(seconds=12))
    seen = []

    def read():
        seen.append(clock.now())
        return {"issue": "115056616" if clock.now() >= slot + timedelta(seconds=90) else "115056615",
                "draw_time": (slot if clock.now() >= slot + timedelta(seconds=90) else slot - timedelta(minutes=5)).isoformat()}

    draw = wait_for_current_draw(read, now=clock.now, sleep=clock.sleep)
    assert draw["issue"] == "115056616"
    assert [(value - slot).total_seconds() for value in seen] == [70, 90]
    assert clock.sleeps == [58, 20]


def test_stale_draw_never_launches_lifecycle():
    slot = datetime(2026, 10, 6, 22, 10, tzinfo=TAIPEI)
    clock = Clock(slot)
    draw = {"issue": "115056615", "draw_time": (slot - timedelta(minutes=5)).isoformat()}
    assert wait_for_current_draw(lambda: draw, now=clock.now, sleep=clock.sleep) is None
    assert clock.now() == slot + timedelta(seconds=180)


def test_readiness_stops_when_database_read_crosses_deadline():
    slot = datetime(2026, 10, 6, 22, 10, tzinfo=TAIPEI)
    clock = Clock(slot)
    calls = []

    def read():
        calls.append(1)
        clock.value += timedelta(seconds=125)
        return {"issue": "115056615", "draw_time": (slot - timedelta(minutes=5)).isoformat()}

    assert wait_for_current_draw(read, now=clock.now, sleep=clock.sleep) is None
    assert len(calls) == 1


def test_wrong_date_future_draw_and_invalid_timestamp_are_rejected():
    slot = datetime(2026, 10, 6, 22, 10, tzinfo=TAIPEI)
    for draw_time in [(slot - timedelta(days=1)).isoformat(), (slot + timedelta(minutes=5)).isoformat(), 'bad']:
        clock = Clock(slot)
        assert wait_for_current_draw(lambda: {"issue": "115056616", "draw_time": draw_time}, now=clock.now, sleep=clock.sleep) is None


def test_timezones_and_first_last_draw_slots():
    assert draw_datetime({"draw_time": "2026-10-06T14:10:00+00:00"}) == datetime(2026, 10, 6, 22, 10, tzinfo=TAIPEI)
    assert current_draw_slot(datetime(2026, 10, 6, 7, 4, tzinfo=TAIPEI)) is None
    assert current_draw_slot(datetime(2026, 10, 6, 7, 5, tzinfo=TAIPEI)).hour == 7
    assert current_draw_slot(datetime(2026, 10, 6, 23, 55, tzinfo=TAIPEI)).minute == 55
    assert current_draw_slot(datetime(2026, 10, 7, 0, 0, tzinfo=TAIPEI)) is None


def test_prediction_deadline_uses_draw_time_even_when_next_draw_not_in_database():
    slot = datetime(2026, 10, 6, 22, 10, tzinfo=TAIPEI)
    draw = {"draw_time": slot.isoformat()}
    assert prediction_is_timely(draw, now=slot + timedelta(seconds=299))
    assert not prediction_is_timely(draw, now=slot + timedelta(seconds=300))
    assert not prediction_is_timely(draw, now=slot + timedelta(seconds=360))
    assert not prediction_is_timely({"draw_time": "invalid"}, now=slot)


@pytest.mark.parametrize('override,expected', [(None, '1'), ('0', '0')])
def test_cron_child_enables_metadata_capture_with_core_worker_protection(monkeypatch, override, expected):
    from scripts import ai_lifecycle_cron_once as cron
    from types import SimpleNamespace
    captured = {}
    if override is None:
        monkeypatch.delenv('TOP_K_SHADOW_CAPTURE_ENABLED', raising=False)
    else:
        monkeypatch.setenv('TOP_K_SHADOW_CAPTURE_ENABLED', override)
    monkeypatch.setattr(cron, 'wait_for_current_draw', lambda reader: {'issue': '115059999'})
    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(cron.subprocess, 'run', run)
    assert cron.main() == 0
    assert captured['env']['TOP_K_SHADOW_CAPTURE_ENABLED'] == expected
    assert captured['env']['AI_LIFECYCLE_CRON_CORE_ONLY'] == '1'
    assert captured['env']['AI_LIFECYCLE_ISSUE'] == '115059999'
    assert captured['command'][1].endswith('scripts/ai_lifecycle_worker_once.py')
