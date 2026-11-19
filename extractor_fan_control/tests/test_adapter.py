"""
Adapter tests: the AppDaemon layer driven against a fake ``hass.Hass``.

The logic is covered by test_logic.py; these tests are about wiring: what the adapter listens to and
schedules, which service calls and sensor writes the logic's actions become, and the safety
boundaries.
"""

import unittest
from datetime import datetime, timedelta, timezone

from extractor_fan_control.extractor_fan_control import (
    FAN_CMD_RATE_LIMIT,
    ExtractorFanControl,
)

LIGHT = "light.wc"
FAN = "switch.wc_fan"
OFF_AT = "sensor.wc_fan_off_at"
LOCAL_TZ = timezone(timedelta(hours=1))

ARGS = {
    "name": "wc",
    "light_entity": LIGHT,
    "fan_switch_entity": FAN,
    "off_at_sensor_entity": OFF_AT,
    "min_light_on_for_fan_seconds": 6,
    "short_visit_threshold_seconds": 60,
    "staircase_interval_seconds": 30,
    "pulse_guard_seconds": 5,
}

DAILY_ARGS = dict(ARGS, daily_run_time="16:00", daily_run_duration_seconds=900)


class FakeApp(ExtractorFanControl):
    """
    The real app with AppDaemon's API replaced by recording stubs.
    """

    def __init__(self, args=None, existing=(LIGHT, FAN)):
        """
        Prepare an app instance; call :meth:`start` to run ``initialize``.
        """
        self.args = dict(ARGS if args is None else args)
        self.existing = set(existing)
        self.now = datetime(2026, 4, 15, 12, 0, 0)
        self.logs = []
        self.service_calls = []
        self.published = {}
        self.state_listeners = []
        self.daily = []
        # handle -> [callback, due, interval or None, kwargs]
        self.timers = {}
        self._handles = 0

    # -- Test helpers ------------------------------------------------------------------------------

    def start(self):
        """
        Run ``initialize``.
        """
        self.initialize()
        return self

    def light(self, new, old=None):
        """
        Deliver a light state change.
        """
        if old is None:
            old = "off" if new == "on" else "on"
        for entity, callback in self.state_listeners:
            if entity == LIGHT:
                callback(LIGHT, "state", old, new, {})

    def advance(self, seconds):
        """
        Move the clock forward, firing every timer that falls due on the way, in time order.
        """
        until = self.now + timedelta(seconds=seconds)
        while True:
            due = [(timer[1], handle) for handle, timer in self.timers.items() if timer[1] <= until]
            if not due:
                break
            at, handle = min(due)
            callback, _, interval, kwargs = self.timers[handle]
            self.now = at
            if interval is None:
                del self.timers[handle]
            else:
                self.timers[handle][1] = at + timedelta(seconds=interval)
            callback(kwargs)
        self.now = until

    def fire_daily(self):
        """
        Fire the daily schedule callback.
        """
        for callback, _ in self.daily:
            callback({})

    def calls_to(self, service):
        """
        Payloads of every call made to one service.
        """
        return [data for name, data in self.service_calls if name == service]

    def fan_commands(self):
        """
        The fan switch services called, in order.
        """
        return [name for name, data in self.service_calls if data.get("entity_id") == FAN]

    def notify_titles(self):
        """
        Titles of every persistent notification raised.
        """
        return [data["title"] for data in self.calls_to("persistent_notification/create")]

    def off_at(self):
        """
        The countdown sensor's current state.
        """
        return self.published[OFF_AT]["state"]

    # -- AppDaemon API -----------------------------------------------------------------------------
    # Stand-ins with AppDaemon's signatures.
    # pylint: disable=missing-function-docstring

    def log(self, message, level="INFO"):
        self.logs.append((level, message))

    def get_state(self, entity_id, default=None):
        return "off" if entity_id in self.existing else default

    def listen_state(self, callback, entity, **_kwargs):
        self.state_listeners.append((entity, callback))
        return self._handle()

    def run_daily(self, callback, start, **_kwargs):
        self.daily.append((callback, start))
        return self._handle()

    def parse_time(self, value):
        return value

    def run_at(self, callback, at, **kwargs):
        handle = self._handle()
        self.timers[handle] = [callback, at, None, kwargs]
        return handle

    def run_every(self, callback, start, interval, **kwargs):
        handle = self._handle()
        self.timers[handle] = [callback, start, interval, kwargs]
        return handle

    def cancel_timer(self, handle):
        self.timers.pop(handle, None)

    def call_service(self, service, **data):
        self.service_calls.append((service, data))

    def set_state(self, entity_id, **kwargs):
        self.published[entity_id] = kwargs

    def datetime(self, aware=False):
        return self.now.replace(tzinfo=LOCAL_TZ) if aware else self.now

    def _handle(self):
        self._handles += 1
        return self._handles


class TestInitialize(unittest.TestCase):

    def test_listens_to_the_light_and_schedules_no_daily_run_by_default(self):
        app = FakeApp().start()
        self.assertEqual([LIGHT], [entity for entity, _ in app.state_listeners])
        self.assertEqual([], app.daily)
        self.assertEqual({}, app.timers)

    def test_schedules_the_daily_run_when_configured(self):
        app = FakeApp(DAILY_ARGS).start()
        self.assertEqual(["16:00"], [start for _, start in app.daily])

    def test_publishes_the_countdown_sensor_as_unknown(self):
        app = FakeApp().start()
        published = app.published[OFF_AT]
        self.assertEqual("unknown", published["state"])
        self.assertEqual("timestamp", published["attributes"]["device_class"])
        self.assertEqual("Wc Fan Off At", published["attributes"]["friendly_name"])

    def test_missing_entities_are_reported_without_failing(self):
        app = FakeApp(existing=()).start()
        errors = [message for level, message in app.logs if level == "ERROR"]
        self.assertEqual(2, len(errors))
        self.assertIn(LIGHT, errors[0])
        self.assertIn(FAN, errors[1])

    def test_invalid_config_fails_startup(self):
        with self.assertRaisesRegex(ValueError, "name is required"):
            FakeApp({key: value for key, value in ARGS.items() if key != "name"}).start()


class TestLightDrivenRun(unittest.TestCase):

    def setUp(self):
        self.app = FakeApp().start()

    def test_the_fan_starts_once_the_light_has_been_on_long_enough(self):
        self.app.light("on")
        self.app.advance(5)
        self.assertEqual([], self.app.fan_commands())
        self.app.advance(1)
        self.assertEqual([{"entity_id": FAN}], self.app.calls_to("switch/turn_on"))
        self.assertEqual("unknown", self.app.off_at())

    def test_a_light_off_before_activation_sends_nothing(self):
        self.app.light("on")
        self.app.advance(3)
        self.app.light("off")
        self.app.advance(60)
        self.assertEqual([], self.app.fan_commands())
        self.assertEqual({}, self.app.timers)

    def test_keepalive_pulses_repeat_the_on_command(self):
        self.app.light("on")
        self.app.advance(6)
        self.app.advance(25 * 2)
        self.assertEqual(["switch/turn_on"] * 3, self.app.fan_commands())
        keepalive_logs = [(level, message) for level, message in self.app.logs
                          if "(keepalive)" in message]
        self.assertEqual(["DEBUG", "DEBUG"], [level for level, _ in keepalive_logs])

    def test_a_short_visit_stops_the_fan_at_once(self):
        self.app.light("on")
        self.app.advance(30)
        self.app.light("off")
        self.assertEqual(["switch/turn_on", "switch/turn_off"], self.app.fan_commands())
        self.assertEqual({}, self.app.timers)

    def test_a_long_visit_runs_the_fan_on_and_publishes_the_countdown(self):
        self.app.light("on")
        self.app.advance(120)
        self.app.light("off")
        # The post-run lasts as long as the visit, published as a local, timezone-aware time.
        self.assertEqual("2026-04-15T12:04:00+01:00", self.app.off_at())
        self.assertNotIn("switch/turn_off", self.app.fan_commands())

        self.app.advance(120)
        self.assertEqual("switch/turn_off", self.app.fan_commands()[-1])
        self.assertEqual("unknown", self.app.off_at())
        self.assertEqual({}, self.app.timers)

    def test_repeated_and_unusable_light_states_are_ignored(self):
        self.app.light("on", old="on")
        self.app.light("unavailable", old="off")
        self.app.advance(60)
        self.assertEqual([], self.app.fan_commands())


class TestDailyRun(unittest.TestCase):

    def test_the_daily_run_starts_the_fan_and_stops_it_after_its_duration(self):
        app = FakeApp(DAILY_ARGS).start()
        app.fire_daily()
        self.assertEqual(["switch/turn_on"], app.fan_commands())
        self.assertEqual("2026-04-15T12:15:00+01:00", app.off_at())
        app.advance(900)
        self.assertEqual("switch/turn_off", app.fan_commands()[-1])
        self.assertEqual("unknown", app.off_at())


class TestSafetyBoundaries(unittest.TestCase):

    def test_the_rate_limit_disables_and_notifies(self):
        app = FakeApp().start()
        app.light("on")
        app.advance(6)
        # The activation sent the first command, so these take it one past the limit.
        for _ in range(FAN_CMD_RATE_LIMIT):
            app.now += timedelta(milliseconds=1)
            app._turn_fan(on=True)  # pylint: disable=protected-access
        self.assertTrue(app._disabled)  # pylint: disable=protected-access
        self.assertIn("ExtractorFanControl disabled", app.notify_titles())
        self.assertEqual("unknown", app.off_at())
        self.assertEqual({}, app.timers)
        # Nothing reaches the fan afterwards.
        app.service_calls.clear()
        app.light("off")
        app.light("on")
        app.advance(60)
        self.assertEqual([], app.service_calls)

    def test_a_keepalive_tick_after_disabling_is_ignored(self):
        app = FakeApp().start()
        app.light("on")
        app.advance(6)
        keepalive = next(timer for timer in app.timers.values() if timer[2] is not None)
        app._shutdown()  # pylint: disable=protected-access
        app.service_calls.clear()
        keepalive[0]({})
        self.assertEqual([], app.service_calls)

    def test_an_unhandled_callback_exception_disables_and_notifies(self):
        app = FakeApp().start()
        app.light("on")
        app.advance(6)
        app._logic.on_light_off = _raises  # pylint: disable=protected-access
        app.light("off")
        self.assertTrue(app._disabled)  # pylint: disable=protected-access
        self.assertIn("ExtractorFanControl error", app.notify_titles())
        self.assertEqual("unknown", app.off_at())
        self.assertEqual({}, app.timers)


def _raises(*args, **kwargs):
    """
    Stand-in for a call that blows up inside a callback.
    """
    raise RuntimeError("boom")


if __name__ == "__main__":
    unittest.main()
