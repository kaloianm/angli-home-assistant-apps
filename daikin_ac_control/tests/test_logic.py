import unittest

from daikin_ac_control.logic import (
    AC_MODE_COLD,
    ACTION_SET_COOL,
    ACTION_TURN_OFF,
    ACEntityLogic,
    DaikinACLogic,
    EntityState,
)

OFF_HYST = 0.7
ON_HYST = 0.3

ENTITY_A = "climate.room_a_ac"
ENTITY_B = "climate.room_b_ac"


def _kinds(actions):
    return [action.kind for action in actions]


def _entity_kinds(entity_actions):
    return [(eid, a.kind) for eid, a in entity_actions]


class TestACEntityLogicFromIdle(unittest.TestCase):

    def setUp(self):
        self.logic = ACEntityLogic(OFF_HYST, ON_HYST)

    def test_initial_state_is_idle(self):
        self.assertEqual(EntityState.IDLE, self.logic.state)

    def test_cool_mode_transitions_to_cooling(self):
        actions = self.logic.update("cool", 22.0, 22.0)
        self.assertEqual([], actions)
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_non_cool_modes_stay_idle(self):
        for mode in ("off", "fan_only", "heat", "dry", "auto", ""):
            logic = ACEntityLogic(OFF_HYST, ON_HYST)
            logic.update(mode, 22.0, 22.0)
            self.assertEqual(EntityState.IDLE, logic.state, f"mode={mode!r} should stay IDLE")

    def test_no_action_on_cool_mode_entry_at_target(self):
        actions = self.logic.update("cool", 23.0, 22.0)
        self.assertEqual([], _kinds(actions))

    def test_missing_temperature_does_not_prevent_idle_to_cooling(self):
        actions = self.logic.update("cool", None, None)
        self.assertEqual([], actions)
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_entering_cool_from_idle_evaluates_temperature_on_same_update(self):
        actions = self.logic.update("cool", 21.2, 22.0)
        self.assertEqual([ACTION_TURN_OFF], _kinds(actions))
        self.assertEqual(EntityState.OFF, self.logic.state)


class TestACEntityLogicFromCooling(unittest.TestCase):

    def setUp(self):
        self.logic = ACEntityLogic(OFF_HYST, ON_HYST)
        self.logic.update("cool", 23.0, 22.0)

    def test_above_target_stays_cooling_no_action(self):
        actions = self.logic.update("cool", 23.0, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_exactly_at_target_stays_cooling(self):
        actions = self.logic.update("cool", 22.0, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_below_off_threshold_turns_off(self):
        actions = self.logic.update("cool", 21.2, 22.0)
        self.assertEqual([ACTION_TURN_OFF], _kinds(actions))
        self.assertEqual(EntityState.OFF, self.logic.state)

    def test_exactly_at_off_threshold_stays_cooling(self):
        actions = self.logic.update("cool", 21.3, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_missing_temperatures_stays_cooling(self):
        actions = self.logic.update("cool", None, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.COOLING, self.logic.state)

        actions = self.logic.update("cool", 21.0, None)
        self.assertEqual([], _kinds(actions))

    def test_manual_off_disables_management(self):
        actions = self.logic.update("off", 23.0, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.IDLE, self.logic.state)

    def test_manual_fan_only_disables_management(self):
        actions = self.logic.update("fan_only", 23.0, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.IDLE, self.logic.state)

    def test_manual_heat_disables_management(self):
        actions = self.logic.update("heat", 23.0, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.IDLE, self.logic.state)


class TestACEntityLogicFromOff(unittest.TestCase):

    def setUp(self):
        self.logic = ACEntityLogic(OFF_HYST, ON_HYST)
        self.logic.update("cool", 22.0, 22.0)
        self.logic.update("cool", 21.2, 22.0)

    def test_state_is_off(self):
        self.assertEqual(EntityState.OFF, self.logic.state)

    def test_below_on_threshold_stays_off(self):
        actions = self.logic.update("off", 21.2, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.OFF, self.logic.state)

    def test_above_on_threshold_resumes_cooling(self):
        actions = self.logic.update("off", 22.4, 22.0)
        self.assertEqual([ACTION_SET_COOL], _kinds(actions))
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_exactly_at_on_threshold_stays_off(self):
        # Use 22.29 so delta stays strictly below 0.3 (22.3 - 22.0 has float error).
        actions = self.logic.update("off", 22.29, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.OFF, self.logic.state)

    def test_missing_temperatures_stays_off(self):
        actions = self.logic.update("off", None, None)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.OFF, self.logic.state)

    def test_manual_cool_from_off_enforces_off_hysteresis_on_same_update(self):
        actions = self.logic.update("cool", 21.2, 22.0)
        self.assertEqual([ACTION_TURN_OFF], _kinds(actions))
        self.assertEqual(EntityState.OFF, self.logic.state)

    def test_manual_cool_from_off_at_target_stays_cooling(self):
        actions = self.logic.update("cool", 22.0, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_manual_fan_only_disables_management(self):
        actions = self.logic.update("fan_only", 21.2, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.IDLE, self.logic.state)


class TestACEntityLogicReset(unittest.TestCase):

    def test_reset_from_cooling_returns_to_idle(self):
        logic = ACEntityLogic(OFF_HYST, ON_HYST)
        logic.update("cool", 23.0, 22.0)
        logic.reset()
        self.assertEqual(EntityState.IDLE, logic.state)

    def test_reset_from_off_returns_to_idle(self):
        logic = ACEntityLogic(OFF_HYST, ON_HYST)
        logic.update("cool", 22.0, 22.0)
        logic.update("cool", 21.2, 22.0)
        logic.reset()
        self.assertEqual(EntityState.IDLE, logic.state)

    def test_after_reset_entering_cool_restarts_management(self):
        logic = ACEntityLogic(OFF_HYST, ON_HYST)
        logic.update("cool", 22.0, 22.0)
        logic.update("cool", 21.2, 22.0)
        logic.reset()
        logic.update("cool", 23.0, 22.0)
        self.assertEqual(EntityState.COOLING, logic.state)


class TestManualDisableAndReEnable(unittest.TestCase):

    def setUp(self):
        self.logic = ACEntityLogic(OFF_HYST, ON_HYST)

    def test_manual_fan_then_cool_re_enables_management(self):
        self.logic.update("cool", 23.0, 22.0)
        self.logic.update("fan_only", 23.0, 22.0)
        self.assertEqual(EntityState.IDLE, self.logic.state)

        actions = self.logic.update("cool", 23.0, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_manual_off_then_cool_re_enables_management(self):
        self.logic.update("cool", 23.0, 22.0)
        self.logic.update("off", 23.0, 22.0)
        self.assertEqual(EntityState.IDLE, self.logic.state)

        actions = self.logic.update("cool", 23.0, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.COOLING, self.logic.state)

    def test_after_manual_disable_app_does_not_manage_off_state(self):
        self.logic.update("cool", 22.0, 22.0)
        self.logic.update("cool", 21.2, 22.0)
        self.assertEqual(EntityState.OFF, self.logic.state)

        self.logic.update("fan_only", 22.4, 22.0)
        self.assertEqual(EntityState.IDLE, self.logic.state)

        actions = self.logic.update("off", 22.4, 22.0)
        self.assertEqual([], _kinds(actions))
        self.assertEqual(EntityState.IDLE, self.logic.state)


class TestDaikinACLogicModeHandling(unittest.TestCase):

    def _make_logic(self):
        return DaikinACLogic(
            ac_entities=[ENTITY_A, ENTITY_B],
            off_hysteresis=OFF_HYST,
            on_hysteresis=ON_HYST,
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

    def test_mode_change_away_from_cold_disables_management(self):
        logic = self._make_logic()
        logic.on_mode_change(AC_MODE_COLD)
        logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        logic.on_mode_change("heat")
        self.assertEqual(EntityState.IDLE, logic.entity_state(ENTITY_A))


class TestDaikinACLogicEntityUpdates(unittest.TestCase):

    def setUp(self):
        self.logic = DaikinACLogic(
            ac_entities=[ENTITY_A, ENTITY_B],
            off_hysteresis=OFF_HYST,
            on_hysteresis=ON_HYST,
        )
        self.logic.on_mode_change(AC_MODE_COLD)

    def test_entity_enters_cooling_returns_no_actions(self):
        actions = self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        self.assertEqual([], actions)
        self.assertEqual(EntityState.COOLING, self.logic.entity_state(ENTITY_A))

    def test_temperature_drop_triggers_off_action(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        actions = self.logic.on_entity_changed(ENTITY_A, "cool", 21.2, 22.0)
        self.assertEqual([(ENTITY_A, ACTION_TURN_OFF)], _entity_kinds(actions))

    def test_temperature_recovery_from_off_resumes_cooling(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        self.logic.on_entity_changed(ENTITY_A, "cool", 21.2, 22.0)
        actions = self.logic.on_entity_changed(ENTITY_A, "off", 22.4, 22.0)
        self.assertEqual([(ENTITY_A, ACTION_SET_COOL)], _entity_kinds(actions))

    def test_entities_are_managed_independently(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        self.logic.on_entity_changed(ENTITY_B, "cool", 23.0, 22.0)

        actions_a = self.logic.on_entity_changed(ENTITY_A, "cool", 21.2, 22.0)
        self.assertEqual([(ENTITY_A, ACTION_TURN_OFF)], _entity_kinds(actions_a))
        self.assertEqual(EntityState.COOLING, self.logic.entity_state(ENTITY_B))

    def test_full_cycle_cooling_off_cooling(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)

        actions = self.logic.on_entity_changed(ENTITY_A, "cool", 21.2, 22.0)
        self.assertEqual(ACTION_TURN_OFF, actions[0][1].kind)

        actions = self.logic.on_entity_changed(ENTITY_A, "off", 22.4, 22.0)
        self.assertEqual(ACTION_SET_COOL, actions[0][1].kind)
        self.assertEqual(EntityState.COOLING, self.logic.entity_state(ENTITY_A))


class TestIdempotency(unittest.TestCase):

    def setUp(self):
        self.logic = DaikinACLogic(
            ac_entities=[ENTITY_A],
            off_hysteresis=OFF_HYST,
            on_hysteresis=ON_HYST,
        )
        self.logic.on_mode_change(AC_MODE_COLD)

    def test_repeated_cooling_observation_above_threshold_is_no_op(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        actions = self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        self.assertEqual([], actions)
        self.assertEqual(EntityState.COOLING, self.logic.entity_state(ENTITY_A))

    def test_repeated_off_observation_below_threshold_is_no_op(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)
        self.logic.on_entity_changed(ENTITY_A, "cool", 21.2, 22.0)
        actions = self.logic.on_entity_changed(ENTITY_A, "off", 21.2, 22.0)
        self.assertEqual([], actions)
        self.assertEqual(EntityState.OFF, self.logic.entity_state(ENTITY_A))


class TestAppCommandEchoEvents(unittest.TestCase):

    def setUp(self):
        self.logic = DaikinACLogic(
            ac_entities=[ENTITY_A],
            off_hysteresis=OFF_HYST,
            on_hysteresis=ON_HYST,
        )
        self.logic.on_mode_change(AC_MODE_COLD)

    def test_echo_of_off_after_app_turns_off_is_no_op(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)
        actions = self.logic.on_entity_changed(ENTITY_A, "cool", 21.2, 22.0)
        self.assertEqual(ACTION_TURN_OFF, actions[0][1].kind)

        echo_actions = self.logic.on_entity_changed(ENTITY_A, "off", 21.2, 22.0)
        self.assertEqual([], echo_actions)
        self.assertEqual(EntityState.OFF, self.logic.entity_state(ENTITY_A))

    def test_echo_of_cool_after_app_resumes_cooling_is_no_op(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)
        self.logic.on_entity_changed(ENTITY_A, "cool", 21.2, 22.0)
        actions = self.logic.on_entity_changed(ENTITY_A, "off", 22.4, 22.0)
        self.assertEqual(ACTION_SET_COOL, actions[0][1].kind)

        echo_actions = self.logic.on_entity_changed(ENTITY_A, "cool", 22.4, 22.0)
        self.assertEqual([], echo_actions)
        self.assertEqual(EntityState.COOLING, self.logic.entity_state(ENTITY_A))

    def test_off_received_while_in_cooling_is_manual_disable(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        actions = self.logic.on_entity_changed(ENTITY_A, "off", 23.0, 22.0)
        self.assertEqual([], actions)
        self.assertEqual(EntityState.IDLE, self.logic.entity_state(ENTITY_A))

    def test_fan_only_received_while_in_cooling_is_manual_disable(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 23.0, 22.0)
        actions = self.logic.on_entity_changed(ENTITY_A, "fan_only", 23.0, 22.0)
        self.assertEqual([], actions)
        self.assertEqual(EntityState.IDLE, self.logic.entity_state(ENTITY_A))


class TestSetpointChanges(unittest.TestCase):

    def setUp(self):
        self.logic = DaikinACLogic(
            ac_entities=[ENTITY_A],
            off_hysteresis=OFF_HYST,
            on_hysteresis=ON_HYST,
        )
        self.logic.on_mode_change(AC_MODE_COLD)

    def test_raising_setpoint_while_cooling_triggers_off(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)
        actions = self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 23.0)
        self.assertEqual([(ENTITY_A, ACTION_TURN_OFF)], _entity_kinds(actions))

    def test_lowering_setpoint_while_off_resumes_cooling(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)
        self.logic.on_entity_changed(ENTITY_A, "cool", 21.2, 22.0)
        actions = self.logic.on_entity_changed(ENTITY_A, "off", 21.2, 20.8)
        self.assertEqual([(ENTITY_A, ACTION_SET_COOL)], _entity_kinds(actions))

    def test_raising_setpoint_while_off_keeps_off(self):
        self.logic.on_entity_changed(ENTITY_A, "cool", 22.0, 22.0)
        self.logic.on_entity_changed(ENTITY_A, "cool", 21.2, 22.0)
        actions = self.logic.on_entity_changed(ENTITY_A, "off", 21.2, 23.0)
        self.assertEqual([], actions)
        self.assertEqual(EntityState.OFF, self.logic.entity_state(ENTITY_A))


if __name__ == "__main__":
    unittest.main()
