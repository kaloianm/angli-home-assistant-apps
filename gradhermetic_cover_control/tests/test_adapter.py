"""
Adapter tests: the AppDaemon layer driven against a fake ``hass.Hass``.

The adapter makes no decisions, so these tests are about wiring: what it listens to, what it filters
out, how it survives malformed external input, and that every :class:`Action` lands on exactly the
right service call.
"""

import unittest
from datetime import datetime, timedelta

from gradhermetic_cover_control.executor import Action, ActionKind, Motion, NotifyKind
from gradhermetic_cover_control.gradhermetic_cover_control import (
    COMMAND_EVENT,
    STARTUP_DELAY_SECONDS,
    GradhermeticCoverControl,
)
from gradhermetic_cover_control.runtime import COMMAND_RATE_LIMIT

REAL_COVER = "cover.living_room_blind"
VIRTUAL_ID = "living_room"
POSITION_ENTITY = f"sensor.gradhermetic_{VIRTUAL_ID}_position"
TILT_MODE_ENTITY = f"binary_sensor.gradhermetic_{VIRTUAL_ID}_tilt_mode"

ARGS = {
    "real_cover": REAL_COVER,
    "virtual_id": VIRTUAL_ID,
    "virtual_name": "Living Room Blind",
    "tilt_zone_upper_pct": 44,
    "tilt_zone_lower_pct": 38,
    "tilt_zone_epsilon_pct": 2,
    "tilt_zone_release_pct": 46,
    "tilt_step_pct": 2,
    "tilt_zone_enter_pct": 44,
    "height_step_pct": 2,
}

# Distinguishes "the test did not care" from "the entity does not exist".
_DEFAULT_STATE = object()


def cover_state(position, state="open"):
    """
    A full Home Assistant cover state object as ``listen_state(attribute="all")`` delivers it.
    """
    return {"state": state, "attributes": {"current_position": position}}


class FakeApp(GradhermeticCoverControl):
    """
    The real app with AppDaemon's API replaced by recording stubs.
    """

    def __init__(self, args=None, state=_DEFAULT_STATE):
        """
        Prepare an app instance; call :meth:`start` to run ``initialize`` and the startup seed.

        Pass ``state=None`` for a ``real_cover`` that does not exist in Home Assistant.
        """
        self.args = dict(ARGS if args is None else args)
        self.cover_state = cover_state(100) if state is _DEFAULT_STATE else state
        self.logs = []
        self.service_calls = []
        self.published = {}
        self.state_listeners = []
        self.event_listeners = []
        self.timers = {}
        self.now = datetime(2026, 4, 15, 12, 0, 0)
        self._handles = 0

    # -- Test helpers ------------------------------------------------------------------------------

    def start(self, seed_startup_state=True):
        """
        Run ``initialize`` and, unless suppressed, the delayed startup state seed.
        """
        self.initialize()
        if seed_startup_state:
            self.fire_timers()
        return self

    def fire_timers(self):
        """
        Fire every scheduled timer, in the order they were scheduled.
        """
        for handle in sorted(self.timers):
            callback, _ = self.timers.pop(handle)
            callback({})

    def calls_to(self, service):
        """
        Payloads of every call made to one service.
        """
        return [data for name, data in self.service_calls if name == service]

    def notify_titles(self):
        """
        Titles of every persistent notification raised.
        """
        return [data["title"] for data in self.calls_to("persistent_notification/create")]

    def command_event(self, **data):
        """
        Deliver a ``gradhermetic_command`` event to the adapter.
        """
        payload = {"virtual_id": VIRTUAL_ID}
        payload.update(data)
        self._deliver_event(COMMAND_EVENT, payload)

    def report(self, position, state="open"):
        """
        Deliver a real-cover state update, updating what ``get_state`` would return too.
        """
        self.cover_state = cover_state(position, state)
        for watched, callback in self.state_listeners:
            if watched == REAL_COVER:
                callback(REAL_COVER, "all", None, self.cover_state, {})

    def _deliver_event(self, name, data):
        for watched, callback in self.event_listeners:
            if watched == name:
                callback(name, data, {})

    # -- AppDaemon API -----------------------------------------------------------------------------

    def log(self, message, level="INFO"):
        self.logs.append((level, message))

    def get_state(self, entity_id, attribute=None, default=None):
        if entity_id != REAL_COVER:
            return default
        if attribute == "all":
            return self.cover_state
        return default if self.cover_state is None else self.cover_state.get("state", default)

    def listen_state(self, callback, entity, **kwargs):
        self.state_listeners.append((entity, callback))
        return self._handle()

    def listen_event(self, callback, event, **kwargs):
        self.event_listeners.append((event, callback))
        return self._handle()

    def run_in(self, callback, seconds, **kwargs):
        handle = self._handle()
        self.timers[handle] = (callback, seconds)
        return handle

    def cancel_timer(self, handle):
        self.timers.pop(handle, None)

    def call_service(self, service, **data):
        self.service_calls.append((service, data))

    def set_state(self, entity_id, **kwargs):
        self.published[entity_id] = kwargs

    def datetime(self):
        return self.now

    def _handle(self):
        self._handles += 1
        return self._handles


class TestInitialize(unittest.TestCase):

    def test_wires_every_listener(self):
        app = FakeApp().start(seed_startup_state=False)
        self.assertEqual([REAL_COVER], [entity for entity, _ in app.state_listeners])
        self.assertEqual([COMMAND_EVENT], [e for e, _ in app.event_listeners])
        self.assertEqual([STARTUP_DELAY_SECONDS], [s for _, s in app.timers.values()])

    def test_missing_real_cover_is_reported(self):
        app = FakeApp(state=None).start(seed_startup_state=False)
        self.assertTrue(
            any(level == "ERROR" and "does not exist" in message for level, message in app.logs))


class TestStartupSeed(unittest.TestCase):
    """
    Startup seeds the belief and nothing else: whatever the reported position, no blind moves.
    """

    def test_outside_the_band_resumes_and_publishes(self):
        app = FakeApp(state=cover_state(80)).start()
        self.assertEqual([], app.calls_to("cover/open_cover"))
        self.assertEqual("80", app.published[POSITION_ENTITY]["state"])

    def test_fully_closed_publishes_zero_as_a_string(self):
        # AppDaemon strips falsy values from the request body, so an integer 0 would never arrive.
        app = FakeApp(state=cover_state(0, "closed")).start()
        self.assertEqual("0", app.published[POSITION_ENTITY]["state"])

    def test_inside_the_band_does_not_move_the_blind(self):
        app = FakeApp(state=cover_state(41)).start()
        self.assertEqual([], app.service_calls)
        self.assertTrue(app._ready)  # pylint: disable=protected-access

    def test_an_unreadable_position_does_not_move_the_blind(self):
        app = FakeApp(state=cover_state(None, "unavailable")).start()
        self.assertEqual([], app.service_calls)
        self.assertTrue(app._ready)  # pylint: disable=protected-access

    def test_commands_before_the_state_is_seeded_are_ignored(self):
        app = FakeApp(state=cover_state(80)).start(seed_startup_state=False)
        app.command_event(command="close")
        app.command_event(command="toggle_tilt_mode")
        app.command_event(command="step", direction="up")
        self.assertEqual([], app.service_calls)

    def test_a_failing_startup_seed_disables_and_notifies(self):
        app = FakeApp(state=cover_state(80)).start(seed_startup_state=False)
        app.get_state = _raises
        app.fire_timers()
        self.assertIn("GradhermeticCoverControl error", app.notify_titles())
        self.assertTrue(app._runtime.disabled)  # pylint: disable=protected-access


class TestCommandEvents(unittest.TestCase):

    def setUp(self):
        self.app = FakeApp(state=cover_state(80)).start()

    def test_commands_for_another_blind_are_ignored(self):
        self.app.command_event(virtual_id="kitchen", command="close")
        self.assertEqual([], self.app.calls_to("cover/close_cover"))

    def test_close_reaches_the_real_cover(self):
        self.app.command_event(command="close")
        self.assertEqual([{"entity_id": REAL_COVER}], self.app.calls_to("cover/close_cover"))

    def test_open_reaches_the_real_cover(self):
        self.app.command_event(command="open")
        self.assertEqual([{"entity_id": REAL_COVER}], self.app.calls_to("cover/open_cover"))

    def test_stop_reaches_the_real_cover_while_a_move_is_in_flight(self):
        self.app.command_event(command="close")
        self.app.command_event(command="stop")
        self.assertEqual([{"entity_id": REAL_COVER}], self.app.calls_to("cover/stop_cover"))

    def test_stop_on_an_idle_blind_sends_nothing(self):
        # On a KNX actuator without a stop object a stop is carried on the step object, which
        # nudges an idle blind; with nothing moving there is nothing to send.
        self.app.command_event(command="stop")
        self.assertEqual([], self.app.calls_to("cover/stop_cover"))

    def test_set_position_sends_a_whole_percent(self):
        # A fractional payload is rounded once, here at the boundary, halves going up.
        self.app.command_event(command="set_position", position="30.4")
        self.assertEqual([{
            "entity_id": REAL_COVER,
            "position": 30
        }], self.app.calls_to("cover/set_cover_position"))
        self.app.service_calls.clear()
        self.app.command_event(command="set_position", position=24.5)
        self.assertEqual([{
            "entity_id": REAL_COVER,
            "position": 25
        }], self.app.calls_to("cover/set_cover_position"))

    def test_a_non_finite_position_is_ignored_not_fatal(self):
        for raw in ("nan", "inf", True):
            with self.subTest(raw=raw):
                self.app.command_event(command="set_position", position=raw)
                self.assertEqual([], self.app.service_calls)
                self.assertFalse(self.app._runtime.disabled)  # pylint: disable=protected-access

    def test_a_malformed_position_is_ignored_not_fatal(self):
        self.app.command_event(command="set_position", position="banana")
        self.assertEqual([], self.app.service_calls)
        self.assertFalse(self.app._runtime.disabled)  # pylint: disable=protected-access

    def test_set_tilt_mode_without_enabled_is_ignored(self):
        self.app.command_event(command="set_tilt_mode")
        self.assertEqual([], self.app.service_calls)

    def test_set_tilt_mode_coerces_its_flag(self):
        self.app.command_event(command="set_tilt_mode", enabled="true")
        self.assertEqual(1, len(self.app.calls_to("cover/open_cover")))

    def test_an_unknown_command_is_ignored(self):
        self.app.command_event(command="fly")
        self.assertEqual([], self.app.service_calls)
        self.assertFalse(self.app._runtime.disabled)  # pylint: disable=protected-access


class TestStepAndToggleCommands(unittest.TestCase):

    def setUp(self):
        self.app = FakeApp(state=cover_state(80)).start()

    def test_toggle_tilt_mode_enters_tilt(self):
        self.app.command_event(command="toggle_tilt_mode")
        self.assertEqual(1, len(self.app.calls_to("cover/open_cover")))

    def test_a_step_nudges_the_height_when_not_latched(self):
        self.app.command_event(command="step", direction="up")
        self.assertEqual([{
            "entity_id": REAL_COVER,
            "position": 82
        }], self.app.calls_to("cover/set_cover_position"))
        self.app.report(82)
        self.app.command_event(command="step", direction="down")
        self.assertEqual({
            "entity_id": REAL_COVER,
            "position": 80
        },
                         self.app.calls_to("cover/set_cover_position")[-1])

    def test_a_step_never_enters_tilt(self):
        self.app.command_event(command="step", direction="down")
        self.assertEqual([], self.app.calls_to("cover/open_cover"))
        self.assertFalse(self.app._runtime.logic.in_tilt)  # pylint: disable=protected-access

    def test_a_step_while_moving_stops_the_blind(self):
        self.app.command_event(command="close")
        self.app.report(70, state="closing")
        self.app.command_event(command="step", direction="up")
        self.assertEqual([{"entity_id": REAL_COVER}], self.app.calls_to("cover/stop_cover"))
        self.assertEqual([], self.app.calls_to("cover/set_cover_position"))

    def test_a_step_without_a_valid_direction_is_ignored_not_fatal(self):
        for direction in (None, "sideways"):
            with self.subTest(direction=direction):
                self.app.command_event(command="step", direction=direction)
                self.assertEqual([], self.app.service_calls)
                self.assertFalse(self.app._runtime.disabled)  # pylint: disable=protected-access


class TestActionTranslation(unittest.TestCase):

    def setUp(self):
        self.app = FakeApp(state=cover_state(80)).start()
        self.app.service_calls.clear()

    def _apply(self, *actions):
        self.app._apply_actions(list(actions))  # pylint: disable=protected-access

    def test_move_close_open_and_stop(self):
        self._apply(Action(ActionKind.MOVE_TO, position=43), Action(ActionKind.OPEN_FULL),
                    Action(ActionKind.CLOSE_FULL), Action(ActionKind.STOP))
        self.assertEqual([
            ("cover/set_cover_position", {
                "entity_id": REAL_COVER,
                "position": 43
            }),
            ("cover/open_cover", {
                "entity_id": REAL_COVER
            }),
            ("cover/close_cover", {
                "entity_id": REAL_COVER
            }),
            ("cover/stop_cover", {
                "entity_id": REAL_COVER
            }),
        ], self.app.service_calls)

    def test_commands_log_a_short_human_readable_line(self):
        self._apply(Action(ActionKind.MOVE_TO, position=43), Action(ActionKind.OPEN_FULL),
                    Action(ActionKind.CLOSE_FULL), Action(ActionKind.STOP))
        cover_logs = [
            message for level, message in self.app.logs
            if level == "INFO" and message.startswith("Cover ")
        ]
        self.assertEqual([
            "Cover move to 43%",
            "Cover open fully",
            "Cover close fully",
            "Cover stop",
        ], cover_logs)

    def test_publish_writes_the_position_sensor(self):
        self._apply(Action(ActionKind.PUBLISH_STATE, position=67))
        published = self.app.published[POSITION_ENTITY]
        self.assertEqual("67", published["state"])
        self.assertEqual("%", published["attributes"]["unit_of_measurement"])
        self.assertEqual("Living Room Blind Position", published["attributes"]["friendly_name"])
        self.assertEqual(Motion.IDLE, published["attributes"]["motion"])

    def test_publish_writes_the_motion_beside_the_position(self):
        # The template cover's state template reads it to report opening/closing.
        self._apply(
            Action(ActionKind.PUBLISH_STATE, position=40, in_tilt=False, motion=Motion.CLOSING))
        published = self.app.published[POSITION_ENTITY]
        self.assertEqual(Motion.CLOSING, published["attributes"]["motion"])

    def test_the_tilt_sensor_is_written_only_when_the_mode_changes(self):
        # The position sensor tracks travel percent by percent; rewriting the mode beside it with
        # the value it already holds on every one of those reports is pure churn.
        self._apply(Action(ActionKind.PUBLISH_STATE, position=40, in_tilt=False))
        self.app.published.pop(TILT_MODE_ENTITY)
        self._apply(Action(ActionKind.PUBLISH_STATE, position=41, in_tilt=False))
        self.assertNotIn(TILT_MODE_ENTITY, self.app.published)
        # A real change is written.
        self._apply(Action(ActionKind.PUBLISH_STATE, position=42, in_tilt=True))
        self.assertEqual("on", self.app.published[TILT_MODE_ENTITY]["state"])

    def test_an_unknown_position_publishes_the_sensor_as_unavailable(self):
        self._apply(
            Action(ActionKind.PUBLISH_STATE, position=None, in_tilt=False, motion=Motion.IDLE))
        self.assertEqual("unavailable", self.app.published[POSITION_ENTITY]["state"])
        self.assertEqual("off", self.app.published[TILT_MODE_ENTITY]["state"])

    def test_publish_writes_the_tilt_mode_sensor_beside_it(self):
        # Both are written from the one action, so a dashboard can never read a slat angle as a
        # height: whichever scale the position is on, the flag beside it says so.
        self._apply(Action(ActionKind.PUBLISH_STATE, position=67, in_tilt=True))
        published = self.app.published[TILT_MODE_ENTITY]
        self.assertEqual("on", published["state"])
        self.assertEqual("Living Room Blind Slat Mode", published["attributes"]["friendly_name"])

        self._apply(Action(ActionKind.PUBLISH_STATE, position=20, in_tilt=False))
        self.assertEqual("off", self.app.published[TILT_MODE_ENTITY]["state"])

    def test_an_unknown_tilt_mode_publishes_as_off(self):
        # in_tilt is only ever None on an action built without it; "off" is the safe reading, since
        # slat control is offered exactly when the app is confident it is latched.
        self._apply(Action(ActionKind.PUBLISH_STATE, position=67))
        self.assertEqual("off", self.app.published[TILT_MODE_ENTITY]["state"])

    def test_the_settle_timer_is_armed_and_cancelled(self):
        self._apply(Action(ActionKind.ARM_SETTLE_TIMER, seconds=45))
        self.assertEqual([45], [seconds for _, seconds in self.app.timers.values()])
        self._apply(Action(ActionKind.CANCEL_SETTLE_TIMER))
        self.assertEqual({}, self.app.timers)

    def test_arming_twice_replaces_the_timer(self):
        self._apply(Action(ActionKind.ARM_SETTLE_TIMER, seconds=45))
        self._apply(Action(ActionKind.ARM_SETTLE_TIMER, seconds=45))
        self.assertEqual(1, len(self.app.timers))

    def test_notifications_carry_the_right_title(self):
        self._apply(
            Action(ActionKind.NOTIFY, notify_kind=NotifyKind.STALL, message="did not reach 0%"))
        self._apply(
            Action(ActionKind.NOTIFY, notify_kind=NotifyKind.INVARIANT, message="failed a check"))
        self.assertEqual(["GradhermeticCoverControl stalled", "GradhermeticCoverControl error"],
                         self.app.notify_titles())
        self.assertIn(f"Cover '{VIRTUAL_ID}' did not reach 0%",
                      self.app.calls_to("persistent_notification/create")[0]["message"])


class TestFeedbackAndTheSettleTimer(unittest.TestCase):

    def setUp(self):
        self.app = FakeApp(state=cover_state(80)).start()

    def test_feedback_advances_a_plan(self):
        self.app.command_event(command="set_position", position=30)
        self.app.report(30)
        self.assertEqual("30", self.app.published[POSITION_ENTITY]["state"])
        # pylint: disable-next=protected-access
        self.assertFalse(self.app._runtime.logic.has_pending_plan)

    def test_an_unavailable_cover_clears_the_position_belief(self):
        self.app.report(70, state="closing")
        self.app.report(None, state="unavailable")
        logic = self.app._runtime.logic  # pylint: disable=protected-access
        self.assertIsNone(logic.last_position)
        self.assertFalse(logic.is_moving)

    def test_a_fractional_reading_is_rounded_to_a_whole_percent(self):
        # Nothing past the adapter handles a fraction, even from a cover that reports one.
        self.app.report(41.6)
        logic = self.app._runtime.logic  # pylint: disable=protected-access
        self.assertEqual(42, logic.last_position)
        self.assertIs(int, type(logic.last_position))

    def test_a_non_numeric_position_is_treated_as_unavailable(self):
        self.app.report("unknown")
        logic = self.app._runtime.logic  # pylint: disable=protected-access
        self.assertIsNone(logic.last_position)
        self.assertFalse(self.app._runtime.disabled)  # pylint: disable=protected-access

    def test_the_settle_timer_reads_the_controller_and_stalls(self):
        self.app.command_event(command="set_position", position=30)
        self.app.service_calls.clear()
        self.app.cover_state = cover_state(55)  # settled well short of the target
        self.app.fire_timers()
        # The blind is at rest, so no stop goes out: on KNX that would be a step telegram.
        self.assertEqual([], self.app.calls_to("cover/stop_cover"))
        self.assertIn("GradhermeticCoverControl stalled", self.app.notify_titles())
        self.assertEqual("55", self.app.published[POSITION_ENTITY]["state"])

    def test_feedback_shows_travel_as_it_happens(self):
        self.app.command_event(command="close")
        self.app.report(60, state="closing")
        published = self.app.published[POSITION_ENTITY]
        self.assertEqual("60", published["state"])
        self.assertEqual("closing", published["attributes"]["motion"])
        self.app.report(0, state="closed")
        published = self.app.published[POSITION_ENTITY]
        self.assertEqual("0", published["state"])
        self.assertEqual("idle", published["attributes"]["motion"])

    def test_the_direction_comes_from_the_cover_state(self):
        # An external move: no plan of the app's own, so the cover's own state is the only source.
        self.app.report(85, state="opening")
        self.assertEqual("opening", self.app.published[POSITION_ENTITY]["attributes"]["motion"])
        self.assertEqual("85", self.app.published[POSITION_ENTITY]["state"])

    def test_the_settle_timer_carries_the_direction_it_read(self):
        # The timer reads the controller itself, so it sees the travel direction too; dropping it
        # would leave the belief's direction stale for the whole of a long move.
        self.app.command_event(command="close")
        self.app.cover_state = cover_state(55, state="closing")
        self.app.fire_timers()
        logic = self.app._runtime.logic  # pylint: disable=protected-access
        self.assertTrue(logic.is_moving)
        self.assertEqual("closing", self.app.published[POSITION_ENTITY]["attributes"]["motion"])

    def test_the_settle_timer_completes_a_silent_move(self):
        self.app.command_event(command="set_position", position=30)
        self.app.cover_state = cover_state(30)  # arrived, but never told us
        self.app.fire_timers()
        self.assertEqual("30", self.app.published[POSITION_ENTITY]["state"])
        self.assertEqual({}, self.app.timers)


class TestSafetyBoundaries(unittest.TestCase):

    def test_the_rate_limit_disables_and_notifies(self):
        app = FakeApp(state=cover_state(80)).start()
        runtime = app._runtime  # pylint: disable=protected-access
        # A runaway replan loop is synchronous, so its commands land milliseconds apart; that is
        # the shape the limit exists to catch, and the only one that can gather COMMAND_RATE_LIMIT
        # commands inside a COMMAND_RATE_WINDOW_SECONDS window at all.
        for index in range(COMMAND_RATE_LIMIT + 1):
            app.now += timedelta(milliseconds=1)
            app._command(runtime, "cover/stop_cover")  # pylint: disable=protected-access
        self.assertTrue(runtime.disabled)
        self.assertIn("GradhermeticCoverControl disabled", app.notify_titles())
        # Nothing reaches the real cover afterwards.
        app.service_calls.clear()
        app.command_event(command="close")
        self.assertEqual([], app.service_calls)

    def test_an_unhandled_callback_exception_disables_and_notifies(self):
        app = FakeApp(state=cover_state(80)).start()
        app._runtime.logic.on_close = _raises  # pylint: disable=protected-access
        app.command_event(command="close")
        self.assertTrue(app._runtime.disabled)  # pylint: disable=protected-access
        self.assertIn("GradhermeticCoverControl error", app.notify_titles())


def _raises(*args, **kwargs):
    """
    Stand-in for a call that blows up inside a callback.
    """
    raise RuntimeError("boom")


if __name__ == "__main__":
    unittest.main()
