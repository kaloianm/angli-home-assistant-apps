"""
Adapter tests: the AppDaemon layer driven against a fake ``hass.Hass``.

The adapter makes no decisions, so these tests are about wiring: what it listens to, what it filters
out, how it survives malformed external input, and that every :class:`Action` lands on exactly the
right service call.
"""

import unittest
from datetime import datetime, timedelta

from gradhermetic_cover_control.executor import (
    ACTION_ARM_SETTLE_TIMER,
    ACTION_CANCEL_SETTLE_TIMER,
    ACTION_CLOSE_FULL,
    ACTION_MOVE_TO,
    ACTION_NOTIFY,
    ACTION_OPEN_FULL,
    ACTION_PUBLISH_POSITION,
    ACTION_STOP,
    NOTIFY_INVARIANT,
    NOTIFY_STALL,
    Action,
)
from gradhermetic_cover_control.gradhermetic_cover_control import (
    COMMAND_EVENT,
    RECOVERY_DELAY_SECONDS,
    GradhermeticCoverControl,
)
from gradhermetic_cover_control.runtime import COMMAND_RATE_LIMIT

REAL_COVER = "cover.living_room_blind"
VIRTUAL_ID = "living_room"
POSITION_ENTITY = f"sensor.gradhermetic_{VIRTUAL_ID}_position"
MOVE_ADDRESS = "2/6/0"
STEP_ADDRESS = "2/6/1"

ARGS = {
    "real_cover": REAL_COVER,
    "virtual_id": VIRTUAL_ID,
    "virtual_name": "Living Room Blind",
    "tilt_zone_upper_pct": 44.0,
    "tilt_zone_lower_pct": 38.0,
    "tilt_zone_epsilon_pct": 2.0,
    "tilt_step_pct": 20.0,
    "knx_move_address": MOVE_ADDRESS,
    "knx_step_address": STEP_ADDRESS,
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
        Prepare an app instance; call :meth:`start` to run ``initialize`` and startup recovery.

        Pass ``state=None`` for a ``real_cover`` that does not exist in Home Assistant.
        """
        self.args = dict(ARGS if args is None else args)
        self.cover_state = cover_state(100.0) if state is _DEFAULT_STATE else state
        self.logs = []
        self.service_calls = []
        self.published = {}
        self.state_listeners = []
        self.event_listeners = []
        self.registered_services = []
        self.timers = {}
        self.now = datetime(2026, 4, 15, 12, 0, 0)
        self._handles = 0

    # -- Test helpers ------------------------------------------------------------------------------

    def start(self, run_recovery=True):
        """
        Run ``initialize`` and, unless suppressed, the delayed startup recovery.
        """
        self.initialize()
        if run_recovery:
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

    def knx_event(self, destination, value):
        """
        Deliver a KNX telegram to the adapter.
        """
        self._deliver_event("knx_event", {"destination": destination, "data": value})

    def press(self, entity, old="2026-04-15T11:00:00+00:00", new="2026-04-15T12:00:00+00:00"):
        """
        Deliver an ``input_button`` state change to the adapter.
        """
        for watched, callback in self.state_listeners:
            if watched == entity:
                callback(entity, "state", old, new, {})

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

    def register_service(self, name, callback):
        self.registered_services.append((name, callback))

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

    def test_wires_every_listener_and_the_service(self):
        app = FakeApp().start(run_recovery=False)
        watched = [entity for entity, _ in app.state_listeners]
        self.assertIn(REAL_COVER, watched)
        self.assertIn(f"input_button.gradhermetic_{VIRTUAL_ID}_step_up", watched)
        self.assertIn(f"input_button.gradhermetic_{VIRTUAL_ID}_step_down", watched)
        self.assertIn(f"input_button.gradhermetic_{VIRTUAL_ID}_tilt", watched)
        self.assertEqual([COMMAND_EVENT, "knx_event"], [e for e, _ in app.event_listeners])
        self.assertEqual(["gradhermetic_cover_control/set_tilt_mode"],
                         [name for name, _ in app.registered_services])
        self.assertEqual([RECOVERY_DELAY_SECONDS], [s for _, s in app.timers.values()])

    def test_no_knx_listener_without_addresses(self):
        args = dict(ARGS)
        del args["knx_move_address"]
        del args["knx_step_address"]
        app = FakeApp(args).start(run_recovery=False)
        self.assertEqual([COMMAND_EVENT], [e for e, _ in app.event_listeners])

    def test_missing_real_cover_is_reported(self):
        app = FakeApp(state=None).start(run_recovery=False)
        self.assertTrue(any(level == "ERROR" and "does not exist" in message
                            for level, message in app.logs))


class TestStartupRecovery(unittest.TestCase):

    def test_outside_the_band_resumes_and_publishes(self):
        app = FakeApp(state=cover_state(80.0)).start()
        self.assertEqual([], app.calls_to("cover/open_cover"))
        self.assertEqual(80, app.published[POSITION_ENTITY]["state"])

    def test_inside_the_band_recovers_upward(self):
        app = FakeApp(state=cover_state(41.0)).start()
        self.assertEqual([{"entity_id": REAL_COVER}], app.calls_to("cover/open_cover"))

    def test_an_unreadable_position_recovers_upward(self):
        app = FakeApp(state=cover_state(None, "unavailable")).start()
        self.assertEqual([{"entity_id": REAL_COVER}], app.calls_to("cover/open_cover"))

    def test_commands_before_recovery_are_ignored(self):
        app = FakeApp(state=cover_state(80.0)).start(run_recovery=False)
        app.command_event(command="close")
        app.knx_event(MOVE_ADDRESS, 1)
        app.press(f"input_button.gradhermetic_{VIRTUAL_ID}_tilt")
        self.assertEqual([], app.service_calls)

    def test_a_failing_recovery_disables_and_notifies(self):
        app = FakeApp(state=cover_state(80.0)).start(run_recovery=False)
        app.get_state = _raises
        app.fire_timers()
        self.assertIn("GradhermeticCoverControl error", app.notify_titles())
        self.assertTrue(app._runtime.disabled)  # pylint: disable=protected-access


class TestCommandEvents(unittest.TestCase):

    def setUp(self):
        self.app = FakeApp(state=cover_state(80.0)).start()

    def test_commands_for_another_blind_are_ignored(self):
        self.app.command_event(virtual_id="kitchen", command="close")
        self.assertEqual([], self.app.calls_to("cover/close_cover"))

    def test_close_reaches_the_real_cover(self):
        self.app.command_event(command="close")
        self.assertEqual([{"entity_id": REAL_COVER}], self.app.calls_to("cover/close_cover"))

    def test_open_reaches_the_real_cover(self):
        self.app.command_event(command="open")
        self.assertEqual([{"entity_id": REAL_COVER}], self.app.calls_to("cover/open_cover"))

    def test_stop_reaches_the_real_cover(self):
        self.app.command_event(command="stop")
        self.assertEqual([{"entity_id": REAL_COVER}], self.app.calls_to("cover/stop_cover"))

    def test_set_position_sends_a_whole_percent(self):
        self.app.command_event(command="set_position", position="30.4")
        self.assertEqual([{"entity_id": REAL_COVER, "position": 30}],
                         self.app.calls_to("cover/set_cover_position"))

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


class TestKnxTelegrams(unittest.TestCase):

    def setUp(self):
        self.app = FakeApp(state=cover_state(80.0)).start()

    def test_a_long_press_uses_the_move_address(self):
        self.app.knx_event(MOVE_ADDRESS, 1)  # 1 = down = less light
        self.assertEqual([{"entity_id": REAL_COVER}], self.app.calls_to("cover/close_cover"))

    def test_a_short_press_uses_the_step_address(self):
        self.app.knx_event(STEP_ADDRESS, 1)  # from above the zone, a down press enters tilt
        self.assertEqual([{"entity_id": REAL_COVER}], self.app.calls_to("cover/open_cover"))

    def test_telegrams_for_other_addresses_are_ignored(self):
        self.app.knx_event("9/9/9", 1)
        self.assertEqual([], self.app.service_calls)

    def test_a_telegram_without_a_destination_is_ignored(self):
        self.app._deliver_event("knx_event", {"data": 1})  # pylint: disable=protected-access
        self.assertEqual([], self.app.service_calls)

    def test_a_malformed_payload_is_ignored_not_fatal(self):
        self.app.knx_event(MOVE_ADDRESS, "sideways")
        self.assertEqual([], self.app.service_calls)
        self.assertFalse(self.app._runtime.disabled)  # pylint: disable=protected-access

    def test_a_list_payload_is_decoded(self):
        self.app.knx_event(MOVE_ADDRESS, [1])
        self.assertEqual([{"entity_id": REAL_COVER}], self.app.calls_to("cover/close_cover"))


class TestButtonPresses(unittest.TestCase):

    def setUp(self):
        self.app = FakeApp(state=cover_state(80.0)).start()
        self.tilt = f"input_button.gradhermetic_{VIRTUAL_ID}_tilt"
        self.step_up = f"input_button.gradhermetic_{VIRTUAL_ID}_step_up"

    def test_the_tilt_button_toggles_tilt_mode(self):
        self.app.press(self.tilt)
        self.assertEqual(1, len(self.app.calls_to("cover/open_cover")))

    def test_a_transition_out_of_unknown_is_not_a_press(self):
        self.app.press(self.tilt, old="unknown")
        self.app.press(self.tilt, old=None)
        self.app.press(self.tilt, new="unavailable")
        self.assertEqual([], self.app.service_calls)

    def test_an_unchanged_timestamp_is_not_a_press(self):
        stamp = "2026-04-15T12:00:00+00:00"
        self.app.press(self.tilt, old=stamp, new=stamp)
        self.assertEqual([], self.app.service_calls)

    def test_a_step_press_pointing_away_from_the_zone_does_nothing(self):
        # Resting at 80, above the zone: up is away from it, and the long-press equivalent (the
        # cover's own open) covers the extremes.
        self.app.press(self.step_up)
        self.assertEqual([], self.app.service_calls)

    def test_a_step_press_toward_the_zone_enters_tilt(self):
        # A dashboard-only blind has no wall switch, so the step helpers are the only directional
        # way into tilt; entry always begins by re-referencing at the top limit.
        step_down = f"input_button.gradhermetic_{VIRTUAL_ID}_step_down"
        self.app.press(step_down)
        self.assertEqual([{"entity_id": REAL_COVER}], self.app.calls_to("cover/open_cover"))


class TestServiceTargeting(unittest.TestCase):

    def setUp(self):
        self.app = FakeApp(state=cover_state(80.0)).start()
        self.handler = self.app.registered_services[0][1]

    def _call(self, **data):
        self.handler("appdaemon", "gradhermetic_cover_control", "set_tilt_mode", data)

    def test_targeted_by_virtual_id(self):
        self._call(virtual_id=VIRTUAL_ID, enabled=True)
        self.assertEqual(1, len(self.app.calls_to("cover/open_cover")))

    def test_targeted_by_entity_id(self):
        self._call(entity_id=f"cover.gradhermetic_{VIRTUAL_ID}", enabled=True)
        self.assertEqual(1, len(self.app.calls_to("cover/open_cover")))

    def test_targeted_by_entity_id_list(self):
        self._call(entity_id=[f"cover.gradhermetic_{VIRTUAL_ID}"], enabled=True)
        self.assertEqual(1, len(self.app.calls_to("cover/open_cover")))

    def test_untargeted_applies_to_every_instance(self):
        self._call(enabled=True)
        self.assertEqual(1, len(self.app.calls_to("cover/open_cover")))

    def test_another_blind_is_ignored(self):
        self._call(virtual_id="kitchen", enabled=True)
        self.assertEqual([], self.app.service_calls)

    def test_missing_enabled_is_ignored(self):
        self._call(virtual_id=VIRTUAL_ID)
        self.assertEqual([], self.app.service_calls)


class TestActionTranslation(unittest.TestCase):

    def setUp(self):
        self.app = FakeApp(state=cover_state(80.0)).start()
        self.app.service_calls.clear()

    def _apply(self, *actions):
        self.app._apply_actions(list(actions))  # pylint: disable=protected-access

    def test_move_close_open_and_stop(self):
        self._apply(Action(ACTION_MOVE_TO, position=42.8), Action(ACTION_OPEN_FULL),
                    Action(ACTION_CLOSE_FULL), Action(ACTION_STOP))
        self.assertEqual([
            ("cover/set_cover_position", {"entity_id": REAL_COVER, "position": 43}),
            ("cover/open_cover", {"entity_id": REAL_COVER}),
            ("cover/close_cover", {"entity_id": REAL_COVER}),
            ("cover/stop_cover", {"entity_id": REAL_COVER}),
        ], self.app.service_calls)

    def test_publish_writes_the_position_sensor(self):
        self._apply(Action(ACTION_PUBLISH_POSITION, position=66.6))
        published = self.app.published[POSITION_ENTITY]
        self.assertEqual(67, published["state"])
        self.assertEqual("%", published["attributes"]["unit_of_measurement"])
        self.assertEqual("Living Room Blind Position", published["attributes"]["friendly_name"])

    def test_the_settle_timer_is_armed_and_cancelled(self):
        self._apply(Action(ACTION_ARM_SETTLE_TIMER, seconds=45))
        self.assertEqual([45], [seconds for _, seconds in self.app.timers.values()])
        self._apply(Action(ACTION_CANCEL_SETTLE_TIMER))
        self.assertEqual({}, self.app.timers)

    def test_arming_twice_replaces_the_timer(self):
        self._apply(Action(ACTION_ARM_SETTLE_TIMER, seconds=45))
        self._apply(Action(ACTION_ARM_SETTLE_TIMER, seconds=45))
        self.assertEqual(1, len(self.app.timers))

    def test_notifications_carry_the_right_title(self):
        self._apply(Action(ACTION_NOTIFY, notify_kind=NOTIFY_STALL, message="did not reach 0%"))
        self._apply(Action(ACTION_NOTIFY, notify_kind=NOTIFY_INVARIANT, message="failed a check"))
        self.assertEqual(["GradhermeticCoverControl stalled", "GradhermeticCoverControl error"],
                         self.app.notify_titles())
        self.assertIn(f"Cover '{VIRTUAL_ID}' did not reach 0%",
                      self.app.calls_to("persistent_notification/create")[0]["message"])


class TestFeedbackAndTheSettleTimer(unittest.TestCase):

    def setUp(self):
        self.app = FakeApp(state=cover_state(80.0)).start()

    def test_feedback_advances_a_plan(self):
        self.app.command_event(command="set_position", position=30)
        self.app.report(30.0)
        self.assertEqual(30, self.app.published[POSITION_ENTITY]["state"])
        # pylint: disable-next=protected-access
        self.assertFalse(self.app._runtime.logic.has_pending_plan)

    def test_an_unavailable_cover_clears_the_position_belief(self):
        self.app.report(70.0, state="closing")
        self.app.report(None, state="unavailable")
        logic = self.app._runtime.logic  # pylint: disable=protected-access
        self.assertIsNone(logic.last_position)
        self.assertFalse(logic.is_moving)

    def test_a_non_numeric_position_is_treated_as_unavailable(self):
        self.app.report("unknown")
        logic = self.app._runtime.logic  # pylint: disable=protected-access
        self.assertIsNone(logic.last_position)
        self.assertFalse(self.app._runtime.disabled)  # pylint: disable=protected-access

    def test_the_settle_timer_reads_the_controller_and_stalls(self):
        self.app.command_event(command="set_position", position=30)
        self.app.service_calls.clear()
        self.app.cover_state = cover_state(55.0)  # settled well short of the target
        self.app.fire_timers()
        self.assertEqual([{"entity_id": REAL_COVER}], self.app.calls_to("cover/stop_cover"))
        self.assertIn("GradhermeticCoverControl stalled", self.app.notify_titles())

    def test_the_settle_timer_completes_a_silent_move(self):
        self.app.command_event(command="set_position", position=30)
        self.app.cover_state = cover_state(30.0)  # arrived, but never told us
        self.app.fire_timers()
        self.assertEqual(30, self.app.published[POSITION_ENTITY]["state"])
        self.assertEqual({}, self.app.timers)


class TestSafetyBoundaries(unittest.TestCase):

    def test_the_rate_limit_disables_and_notifies(self):
        app = FakeApp(state=cover_state(80.0)).start()
        runtime = app._runtime  # pylint: disable=protected-access
        for index in range(COMMAND_RATE_LIMIT + 1):
            app.now += timedelta(seconds=1)
            app._command(runtime, "cover/stop_cover")  # pylint: disable=protected-access
        self.assertTrue(runtime.disabled)
        self.assertIn("GradhermeticCoverControl disabled", app.notify_titles())
        # Nothing reaches the real cover afterwards.
        app.service_calls.clear()
        app.command_event(command="close")
        self.assertEqual([], app.service_calls)

    def test_an_unhandled_callback_exception_disables_and_notifies(self):
        app = FakeApp(state=cover_state(80.0)).start()
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
