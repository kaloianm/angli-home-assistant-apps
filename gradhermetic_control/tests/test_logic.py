"""Unit tests for BlindController — no AppDaemon or MQTT needed."""

import sys
from pathlib import Path

# Allow importing from the apps/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from gradhermetic_logic import (  # pylint: disable=import-error,wrong-import-position
    BlindController, CloseCover, EngagePhase, OpenCover, PublishState,
    ScheduleTimer, SetCoverPosition, StopCover, CancelTimer,
)


def _actions_of_type(actions, cls):
    return [a for a in actions if isinstance(a, cls)]


def _has(actions, cls):
    return len(_actions_of_type(actions, cls)) > 0


# ── Defaults used by most tests ────────────────────────────────────────

DEFAULT = dict(
    tilt_lower_pct=3.0,
    tilt_upper_pct=10.0,
    epsilon_pct=2.0,
    full_travel_time_secs=60.0,
    step_pct=5.0,
    tilt_step_pct=10.0,
)

# ── Basic commands ──────────────────────────────────────────────────────


class TestBasicCommands:

    def test_open_returns_open_action(self):
        ctrl = BlindController(**DEFAULT)
        actions = ctrl.handle_open()
        assert _has(actions, OpenCover)

    def test_close_returns_close_action(self):
        ctrl = BlindController(**DEFAULT)
        actions = ctrl.handle_close()
        assert _has(actions, CloseCover)

    def test_stop_returns_stop_action(self):
        ctrl = BlindController(**DEFAULT)
        actions = ctrl.handle_stop()
        assert _has(actions, StopCover)

    def test_set_position_returns_set_cover_position(self):
        ctrl = BlindController(**DEFAULT)
        actions = ctrl.handle_set_position(42.0)
        moves = _actions_of_type(actions, SetCoverPosition)
        assert len(moves) == 1
        assert moves[0].position == 42

    def test_set_position_clamps(self):
        ctrl = BlindController(**DEFAULT)
        actions = ctrl.handle_set_position(150.0)
        moves = _actions_of_type(actions, SetCoverPosition)
        assert moves[0].position == 100

        actions = ctrl.handle_set_position(-20.0)
        moves = _actions_of_type(actions, SetCoverPosition)
        assert moves[0].position == 0


# ── Tilt conversion ────────────────────────────────────────────────────


class TestTiltConversion:

    def test_tilt_0_maps_to_upper(self):
        ctrl = BlindController(**DEFAULT)
        assert ctrl.tilt_to_position(0.0) == ctrl.tilt_upper

    def test_tilt_100_maps_to_lower(self):
        ctrl = BlindController(**DEFAULT)
        assert ctrl.tilt_to_position(100.0) == ctrl.tilt_lower

    def test_position_upper_maps_to_tilt_0(self):
        ctrl = BlindController(**DEFAULT)
        assert ctrl.position_to_tilt(ctrl.tilt_upper) == 0.0

    def test_position_lower_maps_to_tilt_100(self):
        ctrl = BlindController(**DEFAULT)
        assert ctrl.position_to_tilt(ctrl.tilt_lower) == 100.0

    def test_roundtrip(self):
        ctrl = BlindController(**DEFAULT)
        for tilt in (0, 25, 50, 75, 100):
            pos = ctrl.tilt_to_position(tilt)
            back = ctrl.position_to_tilt(pos)
            assert abs(back - tilt) < 0.01, f"roundtrip failed for tilt={tilt}"


# ── Engagement sequence ─────────────────────────────────────────────────


class TestEngagement:

    def test_set_tilt_starts_engagement(self):
        ctrl = BlindController(**DEFAULT)
        ctrl.real_position = 50.0
        actions = ctrl.handle_set_tilt(50.0)

        assert ctrl.engaging
        assert ctrl.engage_phase == EngagePhase.PHASE1
        assert _has(actions, SetCoverPosition)
        assert _has(actions, ScheduleTimer)

        move = _actions_of_type(actions, SetCoverPosition)[0]
        expected_lower = int(ctrl.tilt_lower - ctrl.epsilon)
        assert move.position == expected_lower

    def test_phase1_timer_transitions_to_phase2(self):
        ctrl = BlindController(**DEFAULT)
        ctrl.real_position = 50.0
        ctrl.handle_set_tilt(50.0)

        actions = ctrl.on_timer("engage")

        assert ctrl.engage_phase == EngagePhase.PHASE2
        assert _has(actions, SetCoverPosition)
        move = _actions_of_type(actions, SetCoverPosition)[0]
        expected_upper = int(ctrl.tilt_upper + ctrl.epsilon)
        assert move.position == expected_upper

    def test_phase2_timer_engages_tilt(self):
        ctrl = BlindController(**DEFAULT)
        ctrl.real_position = 50.0
        ctrl.handle_set_tilt(50.0)
        ctrl.on_timer("engage")

        actions = ctrl.on_timer("engage")

        assert ctrl.tilt_engaged
        assert not ctrl.engaging
        assert _has(actions, PublishState)

    def test_pending_tilt_applied_after_engagement(self):
        ctrl = BlindController(**DEFAULT)
        ctrl.real_position = 50.0
        ctrl.handle_set_tilt(75.0)
        ctrl.on_timer("engage")

        actions = ctrl.on_timer("engage")

        assert ctrl.tilt_engaged
        assert abs(ctrl.tilt_value - 75.0) < 0.01
        moves = _actions_of_type(actions, SetCoverPosition)
        assert len(moves) == 1
        expected_pos = int(ctrl.tilt_to_position(75.0))
        assert moves[0].position == expected_pos

    def test_skip_phase1_when_already_below(self):
        ctrl = BlindController(**DEFAULT)
        ctrl.real_position = 0.5  # already below engage point
        actions = ctrl.handle_set_tilt(50.0)

        assert ctrl.engage_phase == EngagePhase.PHASE2
        move = _actions_of_type(actions, SetCoverPosition)[0]
        expected_upper = int(ctrl.tilt_upper + ctrl.epsilon)
        assert move.position == expected_upper

    def test_enter_slat_starts_engagement(self):
        ctrl = BlindController(**DEFAULT)
        ctrl.real_position = 50.0
        actions = ctrl.handle_enter_slat()

        assert ctrl.engaging
        assert ctrl.pending_tilt == 50.0
        assert _has(actions, SetCoverPosition)

    def test_enter_slat_noop_when_already_engaged(self):
        ctrl = BlindController(**DEFAULT)
        ctrl.tilt_engaged = True
        actions = ctrl.handle_enter_slat()
        assert actions == []


# ── Tilt mode behaviour ────────────────────────────────────────────────


class TestTiltMode:

    def _engage(self, ctrl: BlindController) -> None:
        ctrl.real_position = 50.0
        ctrl.handle_set_tilt(50.0)
        ctrl.on_timer("engage")
        ctrl.on_timer("engage")
        assert ctrl.tilt_engaged

    def test_set_tilt_when_engaged_moves_cover(self):
        ctrl = BlindController(**DEFAULT)
        self._engage(ctrl)

        actions = ctrl.handle_set_tilt(80.0)
        moves = _actions_of_type(actions, SetCoverPosition)
        assert len(moves) == 1
        expected = int(ctrl.tilt_to_position(80.0))
        assert moves[0].position == expected
        assert abs(ctrl.tilt_value - 80.0) < 0.01

    def test_open_exits_tilt(self):
        ctrl = BlindController(**DEFAULT)
        self._engage(ctrl)

        actions = ctrl.handle_open()
        assert not ctrl.tilt_engaged
        assert _has(actions, OpenCover)

    def test_close_exits_tilt(self):
        ctrl = BlindController(**DEFAULT)
        self._engage(ctrl)

        actions = ctrl.handle_close()
        assert not ctrl.tilt_engaged
        assert _has(actions, CloseCover)

    def test_set_position_exits_tilt(self):
        ctrl = BlindController(**DEFAULT)
        self._engage(ctrl)

        actions = ctrl.handle_set_position(70.0)
        assert not ctrl.tilt_engaged
        assert _has(actions, SetCoverPosition)

    def test_real_cover_change_updates_tilt(self):
        ctrl = BlindController(**DEFAULT)
        self._engage(ctrl)

        actions = ctrl.on_real_cover_changed("open", 6.5)
        assert abs(ctrl.tilt_value - ctrl.position_to_tilt(6.5)) < 0.01
        assert _has(actions, PublishState)


# ── Engagement cancellation ─────────────────────────────────────────────


class TestCancellation:

    def test_stop_cancels_engagement(self):
        ctrl = BlindController(**DEFAULT)
        ctrl.real_position = 50.0
        ctrl.handle_set_tilt(50.0)
        assert ctrl.engaging

        actions = ctrl.handle_stop()
        assert not ctrl.engaging
        assert _has(actions, CancelTimer)
        assert _has(actions, StopCover)

    def test_open_cancels_engagement(self):
        ctrl = BlindController(**DEFAULT)
        ctrl.real_position = 50.0
        ctrl.handle_set_tilt(50.0)

        actions = ctrl.handle_open()
        assert not ctrl.engaging
        assert _has(actions, CancelTimer)
        assert _has(actions, OpenCover)

    def test_timer_ignored_after_cancellation(self):
        ctrl = BlindController(**DEFAULT)
        ctrl.real_position = 50.0
        ctrl.handle_set_tilt(50.0)
        ctrl.handle_stop()

        actions = ctrl.on_timer("engage")
        assert actions == []
        assert not ctrl.tilt_engaged


# ── Step commands ───────────────────────────────────────────────────────


class TestSteps:

    def _engage(self, ctrl: BlindController) -> None:
        ctrl.real_position = 50.0
        ctrl.handle_set_tilt(50.0)
        ctrl.on_timer("engage")
        ctrl.on_timer("engage")

    def test_slat_step_up_when_engaged(self):
        ctrl = BlindController(**DEFAULT)
        self._engage(ctrl)
        ctrl.tilt_value = 50.0

        actions = ctrl.handle_slat_step_up()
        assert abs(ctrl.tilt_value - 40.0) < 0.01
        assert _has(actions, SetCoverPosition)

    def test_slat_step_down_when_engaged(self):
        ctrl = BlindController(**DEFAULT)
        self._engage(ctrl)
        ctrl.tilt_value = 50.0

        actions = ctrl.handle_slat_step_down()
        assert abs(ctrl.tilt_value - 60.0) < 0.01
        assert _has(actions, SetCoverPosition)

    def test_slat_step_up_when_not_engaged_starts_engagement(self):
        ctrl = BlindController(**DEFAULT)
        ctrl.real_position = 50.0
        actions = ctrl.handle_slat_step_up()
        assert ctrl.engaging
        assert _has(actions, ScheduleTimer)

    def test_slat_step_clamps_at_boundaries(self):
        ctrl = BlindController(**DEFAULT)
        self._engage(ctrl)
        ctrl.tilt_value = 5.0

        ctrl.handle_slat_step_up()
        assert ctrl.tilt_value >= 0.0

        ctrl.tilt_value = 95.0
        ctrl.handle_slat_step_down()
        assert ctrl.tilt_value <= 100.0


# ── State publishing ───────────────────────────────────────────────────


class TestPublish:

    def test_publish_reflects_position(self):
        ctrl = BlindController(**DEFAULT)
        ctrl.real_position = 42.0
        ctrl.real_state = "opening"
        pub = ctrl.make_publish()
        assert pub.cover_state == "opening"
        assert pub.position == 42

    def test_stopped_at_100_is_open(self):
        ctrl = BlindController(**DEFAULT)
        ctrl.real_position = 100.0
        ctrl.real_state = "open"
        assert ctrl.make_publish().cover_state == "open"

    def test_stopped_at_0_is_closed(self):
        ctrl = BlindController(**DEFAULT)
        ctrl.real_position = 0.0
        ctrl.real_state = "closed"
        assert ctrl.make_publish().cover_state == "closed"
