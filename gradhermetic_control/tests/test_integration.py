"""
Integration tests: drive the BlindController against the Rust emulator via MQTT.

Requires:
  - Docker running (pytest-mqtt auto-starts an eclipse-mosquitto container),
    OR an MQTT broker already listening (auto-detected and reused).
  - The gradhermetic-emulator binary (or set EMULATOR_BIN env var)

Run with:
    pytest tests/test_integration.py -v --timeout=30

Override broker address:
    pytest tests/test_integration.py --mqtt-host 192.168.1.10 --mqtt-port 1883

These tests are skipped automatically when the emulator binary is not found.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

paho = pytest.importorskip("paho.mqtt.client",
                           reason="paho-mqtt not installed")
yaml = pytest.importorskip("yaml", reason="PyYAML not installed")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))

from gradhermetic_logic import (  # pylint: disable=import-error,wrong-import-position
    BlindController, SetCoverPosition, OpenCover, CloseCover, StopCover,
    ScheduleTimer, CancelTimer,
)

EMULATOR_BIN = os.environ.get(
    "EMULATOR_BIN",
    str(
        Path(__file__).resolve().parent.parent.parent / "emulator" / "target" /
        "release" / "gradhermetic-emulator"),
)
BLIND_ID = "integration_test"

# Fast travel time so the emulator moves quickly.
TRAVEL_TIME = 5.0

# ── Helpers ─────────────────────────────────────────────────────────────


class MqttHelper:
    """Thin wrapper around paho-mqtt for test convenience."""

    def __init__(self, host: str, port: int):
        self.client = paho.Client(paho.CallbackAPIVersion.VERSION2,
                                  client_id="integration-test")
        self.received: dict[str, str] = {}
        self.client.on_message = self._on_message
        self.client.connect(host, port)
        self.client.loop_start()

    def _on_message(self, client, userdata, msg):
        self.received[msg.topic] = msg.payload.decode()

    def subscribe(self, topic: str):
        self.client.subscribe(topic, qos=1)

    def publish(self, topic: str, payload: str):
        self.client.publish(topic, payload, qos=1)

    def wait_for(self, topic: str, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if topic in self.received:
                return self.received[topic]
            time.sleep(0.1)
        return None

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


class ActionExecutor:
    """
    Executes BlindController actions by publishing MQTT commands to the
    emulator (which acts as the "real cover" in this test).
    """

    def __init__(self, mq: MqttHelper, emulator_prefix: str):
        self.mq = mq
        self.pfx = emulator_prefix
        self.timers: dict[str, float] = {}  # timer_id → deadline

    def execute(self, actions) -> None:
        for action in actions:
            if isinstance(action, SetCoverPosition):
                self.mq.publish(f"{self.pfx}/position/set",
                                str(action.position))
            elif isinstance(action, OpenCover):
                self.mq.publish(f"{self.pfx}/set", "OPEN")
            elif isinstance(action, CloseCover):
                self.mq.publish(f"{self.pfx}/set", "CLOSE")
            elif isinstance(action, StopCover):
                self.mq.publish(f"{self.pfx}/set", "STOP")
            elif isinstance(action, ScheduleTimer):
                self.timers[
                    action.timer_id] = time.monotonic() + action.seconds
            elif isinstance(action, CancelTimer):
                self.timers.pop(action.timer_id, None)

    def wait_timers(self, ctrl: BlindController):
        """Block until all pending timers have expired, firing each one."""
        while self.timers:
            tid, deadline = min(self.timers.items(), key=lambda kv: kv[1])
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            del self.timers[tid]
            # Feed the current emulator position to the controller.
            pos = self._read_position()
            ctrl.on_real_cover_changed("open", pos)
            actions = ctrl.on_timer(tid)
            self.execute(actions)

    def _read_position(self) -> float:
        val = self.mq.received.get(f"{self.pfx}/position", "0")
        try:
            return float(val)
        except ValueError:
            return 0.0

    def wait_emulator_idle(self, timeout: float = TRAVEL_TIME + 3):
        """Wait until the emulator stops moving."""
        deadline = time.monotonic() + timeout
        prev_pos = None
        stable_count = 0
        while time.monotonic() < deadline:
            pos = self.mq.received.get(f"{self.pfx}/position")
            if pos is not None and pos == prev_pos:
                stable_count += 1
                if stable_count >= 3:
                    return
            else:
                stable_count = 0
            prev_pos = pos
            time.sleep(0.3)


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def emulator(mosquitto):
    """Start the Rust emulator; depends on mosquitto to ensure broker is up."""
    mqtt_host, mqtt_port = mosquitto
    mqtt_port = int(mqtt_port)

    if not Path(EMULATOR_BIN).exists():
        pytest.skip(f"Emulator binary not found at {EMULATOR_BIN}")

    config = {
        "mqtt": {
            "host": mqtt_host,
            "port": mqtt_port,
            "client_id": "emulator-integration-test",
        },
        "blinds": [{
            "id": BLIND_ID,
            "name": "Integration Test Blind",
            "tilt_lower_pct": 3.0,
            "tilt_upper_pct": 10.0,
            "epsilon_pct": 2.0,
            "full_travel_time_secs": TRAVEL_TIME,
            "step_pct": 5.0,
            "tilt_step_pct": 10.0,
        }],
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml",
                                     delete=False) as cfg_file:
        yaml.dump(config, cfg_file)
        cfg_path = cfg_file.name

    proc = subprocess.Popen(
        [EMULATOR_BIN, "-c", cfg_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(2)  # let it connect and publish discovery
    yield proc
    proc.terminate()
    proc.wait(timeout=5)
    os.unlink(cfg_path)


@pytest.fixture()
def env(emulator, mosquitto):
    mqtt_host, mqtt_port = mosquitto
    mq = MqttHelper(mqtt_host, int(mqtt_port))
    mq.subscribe(f"gradhermetic/{BLIND_ID}/#")
    time.sleep(0.5)

    executor = ActionExecutor(mq, f"gradhermetic/{BLIND_ID}")

    ctrl = BlindController(
        tilt_lower_pct=3.0,
        tilt_upper_pct=10.0,
        epsilon_pct=2.0,
        full_travel_time_secs=TRAVEL_TIME,
        tilt_step_pct=10.0,
    )

    yield ctrl, executor, mq
    mq.close()


# ── Tests ───────────────────────────────────────────────────────────────


class TestEmulatorDirect:
    """Verify the emulator works before testing the controller against it."""

    def test_open_and_close(self, env):
        ctrl, ex, mq = env

        mq.publish(f"gradhermetic/{BLIND_ID}/set", "OPEN")
        time.sleep(TRAVEL_TIME + 2)
        assert mq.wait_for(f"gradhermetic/{BLIND_ID}/state") == "open"

        mq.publish(f"gradhermetic/{BLIND_ID}/set", "CLOSE")
        time.sleep(TRAVEL_TIME + 2)
        assert mq.wait_for(f"gradhermetic/{BLIND_ID}/state") == "closed"

    def test_set_position(self, env):
        ctrl, ex, mq = env

        mq.publish(f"gradhermetic/{BLIND_ID}/position/set", "50")
        time.sleep(TRAVEL_TIME + 2)
        pos = mq.wait_for(f"gradhermetic/{BLIND_ID}/position")
        assert pos is not None
        assert abs(float(pos) - 50.0) < 2.0


class TestControllerAgainstEmulator:
    """Drive the BlindController, execute actions on the emulator."""

    def test_open_via_controller(self, env):
        ctrl, ex, mq = env

        actions = ctrl.handle_open()
        ex.execute(actions)
        time.sleep(TRAVEL_TIME + 2)

        pos = mq.wait_for(f"gradhermetic/{BLIND_ID}/position")
        assert pos is not None
        assert float(pos) >= 99.0

    def test_engagement_and_tilt(self, env):
        ctrl, ex, mq = env

        # Start at 50%.
        actions = ctrl.handle_set_position(50.0)
        ex.execute(actions)
        time.sleep(TRAVEL_TIME + 2)
        ex.wait_emulator_idle()

        # Update controller with real position.
        pos = float(mq.received.get(f"gradhermetic/{BLIND_ID}/position", "50"))
        ctrl.on_real_cover_changed("open", pos)

        # Request tilt — should trigger engagement.
        actions = ctrl.handle_set_tilt(50.0)
        ex.execute(actions)
        assert ctrl.engaging

        # Run through the timer-driven engagement phases.
        ex.wait_timers(ctrl)

        assert ctrl.tilt_engaged
        assert not ctrl.engaging
