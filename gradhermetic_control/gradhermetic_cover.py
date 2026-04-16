"""
Gradhermetic Supergradhermetic Cover — AppDaemon wrapper.

Thin adapter that wires the framework-agnostic BlindController (from
gradhermetic_logic) to AppDaemon's MQTT plugin and Home Assistant services.

Requires the AppDaemon MQTT plugin to be configured.
"""

import json
import mqttapi as mqtt

from gradhermetic_logic import (
    BlindController,
    SetCoverPosition,
    OpenCover,
    CloseCover,
    StopCover,
    ScheduleTimer,
    CancelTimer,
    PublishState,
    Log,
    Action,
)


class GradhhermeticCover(mqtt.Mqtt):
    """One instance per physical blind."""

    # ── Lifecycle ───────────────────────────────────────────────────────

    def initialize(self):
        self.real_cover: str = self.args["real_cover"]
        self.virtual_id: str = self.args.get("virtual_id",
                                             "gradhermetic_blind")
        self.virtual_name: str = self.args.get("virtual_name",
                                               "Gradhermetic Blind")
        self.topic_prefix: str = f"gradhermetic/{self.virtual_id}"

        self.controller = BlindController(
            tilt_lower_pct=float(self.args.get("tilt_lower_pct", 3.0)),
            tilt_upper_pct=float(self.args.get("tilt_upper_pct", 10.0)),
            epsilon_pct=float(self.args.get("epsilon_pct", 2.0)),
            full_travel_time_secs=float(
                self.args.get("full_travel_time_secs", 60.0)),
            step_pct=float(self.args.get("step_pct", 5.0)),
            tilt_step_pct=float(self.args.get("tilt_step_pct", 10.0)),
        )

        self._timers: dict[str, object] = {}

        self._publish_discovery_cover()
        self._publish_discovery_buttons()
        self._subscribe()

        self.listen_state(self._on_real_cover_change,
                          self.real_cover,
                          namespace="default")

        self._mqtt_pub(f"{self.topic_prefix}/availability", "online")
        self._execute(self.controller.on_real_cover_changed("closed", 0.0))

        self.log(f"Gradhermetic wrapper ready — real={self.real_cover}  "
                 f"tilt_zone=[{self.controller.tilt_lower}, "
                 f"{self.controller.tilt_upper}] ε={self.controller.epsilon}")

    def terminate(self):
        self._mqtt_pub(f"{self.topic_prefix}/availability", "offline")

    # ── Action executor ─────────────────────────────────────────────────

    def _execute(self, actions: list[Action]):
        for action in actions:
            if isinstance(action, SetCoverPosition):
                self._ha_call(
                    "cover/set_cover_position",
                    entity_id=self.real_cover,
                    position=action.position,
                )
            elif isinstance(action, OpenCover):
                self._ha_call("cover/open_cover", entity_id=self.real_cover)
            elif isinstance(action, CloseCover):
                self._ha_call("cover/close_cover", entity_id=self.real_cover)
            elif isinstance(action, StopCover):
                self._ha_call("cover/stop_cover", entity_id=self.real_cover)
            elif isinstance(action, ScheduleTimer):
                if action.timer_id in self._timers:
                    self.cancel_timer(self._timers[action.timer_id])
                self._timers[action.timer_id] = self.run_in(
                    self._on_timer, action.seconds, timer_id=action.timer_id)
            elif isinstance(action, CancelTimer):
                handle = self._timers.pop(action.timer_id, None)
                if handle is not None:
                    self.cancel_timer(handle)
            elif isinstance(action, PublishState):
                self._mqtt_pub(f"{self.topic_prefix}/state",
                               action.cover_state)
                self._mqtt_pub(f"{self.topic_prefix}/position",
                               str(action.position))
                self._mqtt_pub(f"{self.topic_prefix}/tilt", str(action.tilt))
            elif isinstance(action, Log):
                self.log(action.message)

    # ── Callbacks ───────────────────────────────────────────────────────

    def _on_timer(self, kwargs):
        timer_id = kwargs.get("timer_id")
        self._timers.pop(timer_id, None)
        self._execute(self.controller.on_timer(timer_id))

    def _on_real_cover_change(self, entity, attribute, old, new, kwargs):
        state = self.get_state(self.real_cover,
                               namespace="default") or "closed"
        position = self._get_real_position()
        self._execute(self.controller.on_real_cover_changed(state, position))

    def _on_mqtt(self, event_name, data, kwargs):
        topic = data.get("topic", "")
        payload = data.get("payload", "").strip()

        if not topic.startswith(self.topic_prefix):
            return

        subtopic = topic[len(self.topic_prefix) + 1:]

        try:
            actions = self._dispatch_command(subtopic, payload)
        except (ValueError, TypeError):
            return

        if actions:
            self._execute(actions)

    def _dispatch_command(self, subtopic: str, payload: str) -> list[Action]:
        ctrl = self.controller
        if subtopic == "set":
            if payload == "OPEN":
                return ctrl.handle_open()
            if payload == "CLOSE":
                return ctrl.handle_close()
            if payload == "STOP":
                return ctrl.handle_stop()
        elif subtopic == "position/set":
            return ctrl.handle_set_position(float(payload))
        elif subtopic == "tilt/set":
            return ctrl.handle_set_tilt(float(payload))
        elif subtopic == "enter_slat":
            return ctrl.handle_enter_slat()
        elif subtopic == "slat_step_up":
            return ctrl.handle_slat_step_up()
        elif subtopic == "slat_step_down":
            return ctrl.handle_slat_step_down()
        return []

    # ── MQTT helpers ────────────────────────────────────────────────────

    def _mqtt_pub(self, topic: str, payload: str, retain: bool = True):
        self.mqtt_publish(topic, payload, namespace="mqtt", retain=retain)

    def _subscribe(self):
        for suffix in ("set", "position/set", "tilt/set", "enter_slat",
                       "slat_step_up", "slat_step_down"):
            self.mqtt_subscribe(f"{self.topic_prefix}/{suffix}",
                                namespace="mqtt")
        self.listen_event(self._on_mqtt, "MQTT_MESSAGE", namespace="mqtt")

    def _get_real_position(self) -> float:
        state = self.get_state(self.real_cover,
                               attribute="all",
                               namespace="default")
        if state and isinstance(state, dict):
            return float(
                state.get("attributes", {}).get("current_position", 0))
        return 0.0

    def _ha_call(self, service: str, **kwargs):
        self.call_service(service, namespace="default", **kwargs)

    # ── HA MQTT Discovery ───────────────────────────────────────────────

    def _publish_discovery_cover(self):
        payload = {
            "~": self.topic_prefix,
            "name": self.virtual_name,
            "unique_id": f"gradhermetic_{self.virtual_id}",
            "object_id": f"gradhermetic_{self.virtual_id}",
            "command_topic": "~/set",
            "state_topic": "~/state",
            "position_topic": "~/position",
            "set_position_topic": "~/position/set",
            "tilt_status_topic": "~/tilt",
            "tilt_command_topic": "~/tilt/set",
            "availability_topic": "~/availability",
            "payload_available": "online",
            "payload_not_available": "offline",
            "payload_open": "OPEN",
            "payload_close": "CLOSE",
            "payload_stop": "STOP",
            "position_open": 100,
            "position_closed": 0,
            "tilt_opened_value": 100,
            "tilt_closed_value": 0,
            "tilt_min": 0,
            "tilt_max": 100,
            "device_class": "blind",
            "device": {
                "identifiers": [f"gradhermetic_{self.virtual_id}"],
                "name": self.virtual_name,
                "manufacturer": "Gradhermetic",
                "model": "Supergradhermetic (AppDaemon)",
            },
        }
        topic = f"homeassistant/cover/gradhermetic/{self.virtual_id}/config"
        self._mqtt_pub(topic, json.dumps(payload))

    def _publish_discovery_buttons(self):
        buttons = [
            ("enter_slat", "Enter Slat Mode"),
            ("slat_step_up", "Slat Step Up"),
            ("slat_step_down", "Slat Step Down"),
        ]
        for suffix, label in buttons:
            payload = {
                "name": f"{self.virtual_name} {label}",
                "unique_id": f"gradhermetic_{self.virtual_id}_{suffix}",
                "command_topic": f"{self.topic_prefix}/{suffix}",
                "device": {
                    "identifiers": [f"gradhermetic_{self.virtual_id}"],
                    "name": self.virtual_name,
                },
            }
            topic = (f"homeassistant/button/gradhermetic/"
                     f"{self.virtual_id}_{suffix}/config")
            self._mqtt_pub(topic, json.dumps(payload))
