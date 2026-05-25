import unittest

from daikin_ac_control.logic import (
    AC_MODE_COLD,
    ACTION_SET_COOL,
    ACTION_SET_FAN_ONLY,
    ACTION_TURN_OFF,
    ACEntityLogic,
    DaikinACLogic,
    EntityState,
)

SWITCH_HYST = 0.5
ON_OFF_HYST = 1.0

ENTITY_A = "climate.room_a_ac"
ENTITY_B = "climate.room_b_ac"


def _kinds(actions):
    return [action.kind for action in actions]


def _entity_kinds(entity_actions):
    return [(eid, a.kind) for eid, a in entity_actions]


class TestACEntityLogicFromIdle(unittest.TestCase):

    def setUp(self):
        self.logic = ACEntityLogic(SWITCH_HYST, ON_OFF_HYST)

    def test_initial_state_is_idle(self):
        self.assertEqual(EntityState.IDLE, self.logic.state)

    def test_cool_mode_transitions_to_cooling(self):
        actions = self.logic.update("cool", 22.0, 22.0)
        self.assertEqual([], actions)
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_non_cool_modes_stay_idle(self):
        for mode in ("off", "fan_only", "heat", "dry", "auto", ""):
            logic = ACEntityLogic(SWITCH_HYST, ON_OFF_HYST)
            logic.update(mode, 22.0, 22.0)
            self.assertEqual(EntityState.IDLE, logic.state, f"mode={mode!r} should stay IDLE")

    def test_no_action_on_cool_mode_entry(self):
        actions = self.logic.update("cool", 23.0, 22.0)
        self.assertEqual([], _kinds(actions))

    def test_missing_temperature_does_not_prevent_idle_to_cooling(self):
        actions = self.logic.update("cool", None, None)
        self.assertEqual([], actions)
        self.assertEqual(EntityState.COOLING, self.logic.state)


class TestACEntityLogicFromCooling(unittest.TestCase):

    def setUp(self):
        self.logic = ACEntityLogic(SWITCH_HYST, ON_OFF_HYST)
        self.logic.update("cool", 23.0, 22.0)  # enter COOLING

    def test_above_target_stays_cooling_no_action(self):
        actions = self.logic.update("cool", 23.0, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_exactly_at_target_stays_cooling(self):
        actions = self.logic.update("cool", 22.0, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_below_switch_threshold_switches_to_ventilation(self):
        # current - target = 21.4 - 22.0 = -0.6 < -0.5
        actions = self.logic.update("cool", 21.4, 22.0)
        self.assertEqual([ACTION_SET_FAN_ONLY], _kinds(actions))
        self.assertEqual(EntityState.VENTILATION, self.logic.state)

    def test_exactly_at_switch_threshold_stays_cooling(self):
        # current - target = 21.5 - 22.0 = -0.5, not strictly less
        actions = self.logic.update("cool", 21.5, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_below_on_off_threshold_skips_to_off(self):
        # current - target = 20.9 - 22.0 = -1.1 < -1.0
        actions = self.logic.update("cool", 20.9, 22.0)
        self.assertEqual([ACTION_TURN_OFF], _kinds(actions))
        self.assertEqual(EntityState.OFF, self.logic.state)

    def test_missing_temperatures_stays_cooling(self):
        actions = self.logic.update("cool", None, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.COOLING, self.logic.state)

        actions = self.logic.update("cool", 21.0, None)
        self.assertEqual([], _kinds(actions))

    def test_manual_off_transitions_to_idle(self):
        actions = self.logic.update("off", 23.0, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.IDLE, self.logic.state)

    def test_manual_heat_transitions_to_idle(self):
        actions = self.logic.update("heat", 23.0, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.IDLE, self.logic.state)

    def test_manual_fan_only_transitions_to_idle(self):
        actions = self.logic.update("fan_only", 23.0, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.IDLE, self.logic.state)


class TestACEntityLogicFromVentilation(unittest.TestCase):

    def setUp(self):
        self.logic = ACEntityLogic(SWITCH_HYST, ON_OFF_HYST)
        self.logic.update("cool", 22.0, 22.0)  # → COOLING
        self.logic.update("cool", 21.4, 22.0)  # → VENTILATION (delta = -0.6)

    def test_state_is_ventilation(self):
        self.assertEqual(EntityState.VENTILATION, self.logic.state)

    def test_between_thresholds_stays_ventilation(self):
        # delta = -0.7 (below switch but above on_off)
        actions = self.logic.update("fan_only", 21.3, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.VENTILATION, self.logic.state)

    def test_below_on_off_threshold_turns_off(self):
        actions = self.logic.update("fan_only", 20.9, 22.0)
        self.assertEqual([ACTION_TURN_OFF], _kinds(actions))
        self.assertEqual(EntityState.OFF, self.logic.state)

    def test_above_on_off_threshold_switches_back_to_cooling(self):
        # delta = 23.1 - 22.0 = 1.1 > 1.0
        actions = self.logic.update("fan_only", 23.1, 22.0)
        self.assertEqual([ACTION_SET_COOL], _kinds(actions))
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_exactly_at_on_off_threshold_stays_ventilation(self):
        # delta = 23.0 - 22.0 = 1.0, not strictly greater
        actions = self.logic.update("fan_only", 23.0, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.VENTILATION, self.logic.state)

    def test_missing_temperatures_stays_ventilation(self):
        actions = self.logic.update("fan_only", None, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.VENTILATION, self.logic.state)

    def test_manual_cool_restores_cooling_state(self):
        actions = self.logic.update("cool", 21.4, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_manual_off_transitions_to_idle(self):
        actions = self.logic.update("off", 21.4, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.IDLE, self.logic.state)

    def test_manual_heat_transitions_to_idle(self):
        actions = self.logic.update("heat", 21.4, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.IDLE, self.logic.state)


class TestACEntityLogicFromOff(unittest.TestCase):

    def setUp(self):
        self.logic = ACEntityLogic(SWITCH_HYST, ON_OFF_HYST)
        self.logic.update("cool", 22.0, 22.0)  # → COOLING
        self.logic.update("cool", 20.9, 22.0)  # → OFF (skip-to-OFF, delta = -1.1)

    def test_state_is_off(self):
        self.assertEqual(EntityState.OFF, self.logic.state)

    def test_below_on_off_threshold_stays_off(self):
        actions = self.logic.update("off", 20.9, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.OFF, self.logic.state)

    def test_above_on_off_threshold_resumes_cooling(self):
        # delta = 23.1 - 22.0 = 1.1 > 1.0
        actions = self.logic.update("off", 23.1, 22.0)
        self.assertEqual([ACTION_SET_COOL], _kinds(actions))
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_exactly_at_on_off_threshold_stays_off(self):
        actions = self.logic.update("off", 23.0, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.OFF, self.logic.state)

    def test_missing_temperatures_stays_off(self):
        actions = self.logic.update("off", None, None)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.OFF, self.logic.state)

    def test_manual_cool_resumes_management_in_cooling(self):
        actions = self.logic.update("cool", 20.9, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_manual_fan_only_transitions_to_idle(self):
        actions = self.logic.update("fan_only", 20.9, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.IDLE, self.logic.state)

    def test_manual_heat_transitions_to_idle(self):
        actions = self.logic.update("heat", 20.9, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.IDLE, self.logic.state)


class TestACEntityLogicReset(unittest.TestCase):

    def test_reset_from_cooling_returns_to_idle(self):
        logic = ACEntityLogic(SWITCH_HYST, ON_OFF_HYST)
        logic.update("cool", 23.0, 22.0)
        self.assertEqual(EntityState.COOLING, logic.state)
        logic.reset()
        self.assertEqual(EntityState.IDLE, logic.state)

    def test_reset_from_ventilation_returns_to_idle(self):
        logic = ACEntityLogic(SWITCH_HYST, ON_OFF_HYST)
        logic.update("cool", 22.0, 22.0)
        logic.update("cool", 21.4, 22.0)
        logic.reset()
        self.assertEqual(EntityState.IDLE, logic.state)

    def test_reset_from_off_returns_to_idle(self):
        logic = ACEntityLogic(SWITCH_HYST, ON_OFF_HYST)
        logic.update("cool", 22.0, 22.0)
        logic.update("cool", 20.9, 22.0)
        logic.reset()
        self.assertEqual(EntityState.IDLE, logic.state)

    def test_after_reset_entering_cool_restarts_management(self):
        logic = ACEntityLogic(SWITCH_HYST, ON_OFF_HYST)
        logic.update("cool", 22.0, 22.0)
        logic.update("cool", 20.9, 22.0)
        logic.reset()
        logic.update("cool", 23.0, 22.0)
        self.assertEqual(EntityState.COOLING, logic.state)


class TestDaikinACLogicModeHandling(unittest.TestCase):

    def _make_logic(self):
        return DaikinACLogic(
            ac_entities=[ENTITY_A, ENTITY_B],
            ventilation_hysteresis=SWITCH_HYST,
            on_off_hysteresis=ON_OFF_HYST,
        )

    def test_initial_mode_is_not_cold(self):
        logic = self._make_logic()
        self.assertFalse(logic.mode_is_cold)

    def test_entity_updates_ignored_when_mode_not_cold(self):
        logic = self._make_logic()
        actions = logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        self.assertEqual([], actions)

    def test_mode_change_to_cold_enables_management(self):
        logic = self._make_logic()
        logic.on_mode_change(AC_MODE_COLD)
        self.assertTrue(logic.mode_is_cold)

    def test_mode_change_to_cold_returns_no_actions(self):
        logic = self._make_logic()
        actions = logic.on_mode_change(AC_MODE_COLD)
        self.assertEqual([], actions)

    def test_mode_change_away_from_cold_disables_management(self):
        logic = self._make_logic()
        logic.on_mode_change(AC_MODE_COLD)
        logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        self.assertEqual(EntityState.COOLING, logic.entity_state(ENTITY_A))

        logic.on_mode_change("heat")
        self.assertFalse(logic.mode_is_cold)
        self.assertEqual(EntityState.IDLE, logic.entity_state(ENTITY_A))
        self.assertEqual(EntityState.IDLE, logic.entity_state(ENTITY_B))

    def test_mode_change_away_returns_no_actions(self):
        logic = self._make_logic()
        logic.on_mode_change(AC_MODE_COLD)
        logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        actions = logic.on_mode_change("heat")
        self.assertEqual([], actions)

    def test_non_cold_mode_values_all_disable_management(self):
        for mode in ("heat", "off", "auto", ""):
            logic = self._make_logic()
            logic.on_mode_change(AC_MODE_COLD)
            logic.on_mode_change(mode)
            self.assertFalse(logic.mode_is_cold, f"mode={mode!r}")

    def test_untracked_entity_update_returns_no_actions(self):
        logic = self._make_logic()
        logic.on_mode_change(AC_MODE_COLD)
        actions = logic.on_entity_changed("climate.unknown", "cool", 23.0, 22.0)
        self.assertEqual([], actions)


class TestDaikinACLogicEntityUpdates(unittest.TestCase):

    def setUp(self):
        self.logic = DaikinACLogic(
            ac_entities=[ENTITY_A, ENTITY_B],
            ventilation_hysteresis=SWITCH_HYST,
            on_off_hysteresis=ON_OFF_HYST,
        )
        self.logic.on_mode_change(AC_MODE_COLD)

    def test_entity_enters_cooling_returns_no_actions(self):
        actions = self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        self.assertEqual([], actions)
        self.assertEqual(EntityState.COOLING, self.logic.entity_state(ENTITY_A))

    def test_temperature_drop_triggers_ventilation_action(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        actions = self.logic.on_entity_changed(ENTITY_A, "cool", 21.4, 22.0)
        self.assertEqual([(ENTITY_A, ACTION_SET_FAN_ONLY)], _entity_kinds(actions))

    def test_large_temperature_drop_skips_to_off(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        actions = self.logic.on_entity_changed(ENTITY_A, "cool", 20.9, 22.0)
        self.assertEqual([(ENTITY_A, ACTION_TURN_OFF)], _entity_kinds(actions))

    def test_temperature_recovery_from_off_resumes_cooling(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        self.logic.on_entity_changed(ENTITY_A, "cool", 20.9, 22.0)
        actions = self.logic.on_entity_changed(ENTITY_A, "off", 23.1, 22.0)
        self.assertEqual([(ENTITY_A, ACTION_SET_COOL)], _entity_kinds(actions))

    def test_entities_are_managed_independently(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        self.logic.on_entity_changed(ENTITY_B, "cool", 23.0, 22.0)

        actions_a = self.logic.on_entity_changed(ENTITY_A, "cool", 21.4, 22.0)
        self.assertEqual([(ENTITY_A, ACTION_SET_FAN_ONLY)], _entity_kinds(actions_a))
        self.assertEqual(EntityState.COOLING, self.logic.entity_state(ENTITY_B))

        actions_b = self.logic.on_entity_changed(ENTITY_B, "cool", 23.0, 22.0)
        self.assertEqual([], actions_b)

    def test_full_cycle_cooling_ventilation_off_cooling(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)

        actions = self.logic.on_entity_changed(ENTITY_A, "cool", 21.4, 22.0)
        self.assertEqual(ACTION_SET_FAN_ONLY, actions[0][1].kind)

        actions = self.logic.on_entity_changed(ENTITY_A, "fan_only", 20.9, 22.0)
        self.assertEqual(ACTION_TURN_OFF, actions[0][1].kind)

        actions = self.logic.on_entity_changed(ENTITY_A, "off", 23.1, 22.0)
        self.assertEqual(ACTION_SET_COOL, actions[0][1].kind)
        self.assertEqual(EntityState.COOLING, self.logic.entity_state(ENTITY_A))


class TestIdempotency(unittest.TestCase):
    """
    Verify that the state machine produces no actions when called repeatedly with the same inputs.
    The adapter removes the new==old guard from its callbacks and relies on this property instead.
    """

    def setUp(self):
        self.logic = DaikinACLogic(
            ac_entities=[ENTITY_A],
            ventilation_hysteresis=SWITCH_HYST,
            on_off_hysteresis=ON_OFF_HYST,
        )
        self.logic.on_mode_change(AC_MODE_COLD)

    def test_repeated_mode_change_to_cold_is_no_op(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        actions = self.logic.on_mode_change(AC_MODE_COLD)
        self.assertEqual([], actions)

    def test_repeated_cooling_observation_above_threshold_is_no_op(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        actions = self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        self.assertEqual([], actions)
        self.assertEqual(EntityState.COOLING, self.logic.entity_state(ENTITY_A))

    def test_repeated_ventilation_observation_between_thresholds_is_no_op(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)
        self.logic.on_entity_changed(ENTITY_A, "cool", 21.4, 22.0)  # → VENTILATION
        actions = self.logic.on_entity_changed(ENTITY_A, "fan_only", 21.4, 22.0)
        self.assertEqual([], actions)
        self.assertEqual(EntityState.VENTILATION, self.logic.entity_state(ENTITY_A))

    def test_repeated_off_observation_below_threshold_is_no_op(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)
        self.logic.on_entity_changed(ENTITY_A, "cool", 20.9, 22.0)  # → OFF
        actions = self.logic.on_entity_changed(ENTITY_A, "off", 20.9, 22.0)
        self.assertEqual([], actions)
        self.assertEqual(EntityState.OFF, self.logic.entity_state(ENTITY_A))

    def test_repeated_temperature_update_in_stable_cooling_is_no_op(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        for _ in range(3):
            actions = self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
            self.assertEqual([], actions)


class TestAppCommandEchoEvents(unittest.TestCase):
    """
    Verify that state-change callbacks triggered by the app's own service calls (echo events) are
    absorbed correctly and do not cause unintended behaviour.

    When the adapter executes an action (e.g. set_hvac_mode fan_only), Home Assistant fires a
    state-change event back to the app via the same listen_state callback used for genuine user
    changes. The logic must distinguish these echo events from manual overrides purely by the
    invariant that its internal state is already updated before the callback arrives.
    """

    def setUp(self):
        self.logic = DaikinACLogic(
            ac_entities=[ENTITY_A],
            ventilation_hysteresis=SWITCH_HYST,
            on_off_hysteresis=ON_OFF_HYST,
        )
        self.logic.on_mode_change(AC_MODE_COLD)

    def test_echo_of_fan_only_after_app_switches_to_ventilation_is_no_op(self):
        # App decides to switch to ventilation.
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)  # → COOLING
        actions = self.logic.on_entity_changed(ENTITY_A, "cool", 21.4, 22.0)  # → VENTILATION
        self.assertEqual(ACTION_SET_FAN_ONLY, actions[0][1].kind)

        # HA echoes the mode change back as a state-change event. Temperature unchanged.
        # This must be absorbed as a no-op; no further action, no state change.
        echo_actions = self.logic.on_entity_changed(ENTITY_A, "fan_only", 21.4, 22.0)
        self.assertEqual([], echo_actions)
        self.assertEqual(EntityState.VENTILATION, self.logic.entity_state(ENTITY_A))

    def test_echo_of_off_after_app_turns_off_is_no_op(self):
        # App decides to turn off (skip-to-OFF path).
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)  # → COOLING
        actions = self.logic.on_entity_changed(ENTITY_A, "cool", 20.9, 22.0)  # → OFF
        self.assertEqual(ACTION_TURN_OFF, actions[0][1].kind)

        # HA echoes the off state back.
        echo_actions = self.logic.on_entity_changed(ENTITY_A, "off", 20.9, 22.0)
        self.assertEqual([], echo_actions)
        self.assertEqual(EntityState.OFF, self.logic.entity_state(ENTITY_A))

    def test_echo_of_cool_after_app_resumes_cooling_is_no_op(self):
        # App was in OFF and decides to resume cooling.
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)  # → COOLING
        self.logic.on_entity_changed(ENTITY_A, "cool", 20.9, 22.0)  # → OFF
        actions = self.logic.on_entity_changed(ENTITY_A, "off", 23.1, 22.0)  # → COOLING
        self.assertEqual(ACTION_SET_COOL, actions[0][1].kind)

        # HA echoes the cool state back.
        echo_actions = self.logic.on_entity_changed(ENTITY_A, "cool", 23.1, 22.0)
        self.assertEqual([], echo_actions)
        self.assertEqual(EntityState.COOLING, self.logic.entity_state(ENTITY_A))

    def test_fan_only_received_while_in_cooling_is_treated_as_manual_override(self):
        # Contrast: if fan_only arrives while the app is still in COOLING (i.e. the app did NOT
        # command it), it must be treated as a manual change and cause a retreat to IDLE.
        self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)  # → COOLING
        actions = self.logic.on_entity_changed(ENTITY_A, "fan_only", 23.0, 22.0)
        self.assertEqual([], actions)
        self.assertEqual(EntityState.IDLE, self.logic.entity_state(ENTITY_A))


class TestSetpointChanges(unittest.TestCase):
    """
    Verify that changes to the target temperature setpoint (not the room temperature) correctly
    trigger state transitions. This is important because the adapter listens to the 'temperature'
    attribute separately and feeds the updated setpoint into the same logic path.
    """

    def setUp(self):
        self.logic = DaikinACLogic(
            ac_entities=[ENTITY_A],
            ventilation_hysteresis=SWITCH_HYST,
            on_off_hysteresis=ON_OFF_HYST,
        )
        self.logic.on_mode_change(AC_MODE_COLD)

    def test_raising_setpoint_while_cooling_triggers_ventilation(self):
        # Room at 22.0, setpoint 22.0: comfortably at target, app does nothing.
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)
        self.assertEqual(EntityState.COOLING, self.logic.entity_state(ENTITY_A))

        # User raises setpoint to 23.0. Room (22.0) is now exactly 1.0°C below the new target.
        # The skip-to-OFF threshold is strictly < -on_off_hysteresis so delta=-1.0 does not
        # qualify; instead the ventilation threshold (< -0.5) is hit first.
        actions = self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 23.0)
        self.assertEqual([(ENTITY_A, ACTION_SET_FAN_ONLY)], _entity_kinds(actions))

    def test_raising_setpoint_slightly_while_cooling_triggers_ventilation(self):
        # Room at 22.0, setpoint 22.0: at target.
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)

        # User raises setpoint to 22.6. Room is now 0.6°C below target → ventilation threshold.
        actions = self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.6)
        self.assertEqual([(ENTITY_A, ACTION_SET_FAN_ONLY)], _entity_kinds(actions))

    def test_raising_setpoint_while_in_ventilation_triggers_off(self):
        # Enter ventilation: room at 21.4, setpoint 22.0 (delta = -0.6).
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)
        self.logic.on_entity_changed(ENTITY_A, "cool", 21.4, 22.0)
        self.assertEqual(EntityState.VENTILATION, self.logic.entity_state(ENTITY_A))

        # User raises setpoint to 23.0. Room (21.4) is now 1.6°C below target → turn off.
        actions = self.logic.on_entity_changed(ENTITY_A, "fan_only", 21.4, 23.0)
        self.assertEqual([(ENTITY_A, ACTION_TURN_OFF)], _entity_kinds(actions))

    def test_lowering_setpoint_while_in_ventilation_resumes_cooling(self):
        # Enter ventilation: room at 21.4, setpoint 22.0 (delta = -0.6).
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)
        self.logic.on_entity_changed(ENTITY_A, "cool", 21.4, 22.0)
        self.assertEqual(EntityState.VENTILATION, self.logic.entity_state(ENTITY_A))

        # User lowers setpoint to 20.0. Room (21.4) is now 1.4°C above target → resume cooling.
        actions = self.logic.on_entity_changed(ENTITY_A, "fan_only", 21.4, 20.0)
        self.assertEqual([(ENTITY_A, ACTION_SET_COOL)], _entity_kinds(actions))

    def test_lowering_setpoint_while_off_resumes_cooling(self):
        # Skip to OFF: room at 20.9, setpoint 22.0 (delta = -1.1).
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)
        self.logic.on_entity_changed(ENTITY_A, "cool", 20.9, 22.0)
        self.assertEqual(EntityState.OFF, self.logic.entity_state(ENTITY_A))

        # User lowers setpoint to 19.5. Room (20.9) is now 1.4°C above target → turn back on.
        actions = self.logic.on_entity_changed(ENTITY_A, "off", 20.9, 19.5)
        self.assertEqual([(ENTITY_A, ACTION_SET_COOL)], _entity_kinds(actions))

    def test_raising_setpoint_while_off_keeps_off(self):
        # Skip to OFF: room at 20.9, setpoint 22.0.
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)
        self.logic.on_entity_changed(ENTITY_A, "cool", 20.9, 22.0)

        # User raises setpoint further to 23.0; room is even further below target.
        actions = self.logic.on_entity_changed(ENTITY_A, "off", 20.9, 23.0)
        self.assertEqual([], actions)
        self.assertEqual(EntityState.OFF, self.logic.entity_state(ENTITY_A))


if __name__ == "__main__":
    unittest.main()
