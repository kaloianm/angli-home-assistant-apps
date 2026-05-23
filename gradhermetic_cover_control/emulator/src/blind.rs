// Pure state-machine simulation of a single Gradhermetic blind. This module has
// no dependencies on networking, async, or MQTT — all I/O is handled by mqtt.rs.
// The simulation is driven by wall-clock time via `tick()`, and all external
// interaction goes through the `cmd_*` methods. This separation makes the blind
// logic fully testable with synchronous unit tests.
//
// State machine transitions:
//
//   Normal ──(cmd_set_tilt)──► EngagingPhase1 ──(reached lower)──► EngagingPhase2
//     ▲                                                                    │
//     │ (cmd_open/close/set_position exits tilt)           (reached upper) │
//     │                                                                    ▼
//     └───────────────(position > tilt_upper)──────────────────── Tilt ◄───┘
//                                                                 │
//                               (cmd_open/close/set_position) ────┘──► Normal

use crate::blind_params::BlindParams;
use std::time::Instant;

/// Motor movement direction.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MotorDirection {
    Up,
    Down,
    Stopped,
}

/// Internal mode of the blind state machine.
///
/// The engagement sequence (Phase1 → Phase2 → Tilt) models the physical
/// mechanism: the blind must first lower past the pin, then raise through it
/// to lock it in place — only then does motor movement control slat angle
/// instead of blind height.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BlindMode {
    /// Normal height control.
    Normal,
    /// Engagement phase 1: lowering to (tilt_lower - epsilon) to reach the pin.
    EngagingPhase1,
    /// Engagement phase 2: raising to (tilt_upper + epsilon) to lock the pin.
    EngagingPhase2,
    /// Tilt mode active — motor movement controls slat orientation within [tilt_lower, tilt_upper].
    Tilt,
}

/// Immutable snapshot decoupled from `Blind`'s internal mutable state.
/// The MQTT layer compares consecutive snapshots to avoid redundant publishes.
#[derive(Debug, Clone, PartialEq)]
pub struct BlindSnapshot {
    pub position: f64,
    /// Only `Some` when tilt mode is active — HA shows the tilt slider accordingly.
    pub tilt: Option<f64>,
    pub motor: MotorDirection,
    pub mode: BlindMode,
}

/// Mutable simulation state for one blind. Commands are accepted immediately
/// (setting motor direction and target), while actual position changes happen
/// incrementally in `tick()` based on real elapsed wall-clock time.
pub struct Blind {
    params: BlindParams,
    /// Current position: 0.0 = fully closed/down, 100.0 = fully open/up.
    position: f64,
    motor: MotorDirection,
    mode: BlindMode,
    /// Target position the motor is driving toward.
    target: Option<f64>,
    /// Tilt value requested before engagement completes. Stored here because the
    /// blind must go through the two-phase engagement sequence before it can
    /// actually move to the desired tilt position.
    pending_tilt: Option<f64>,
    last_update: Instant,
}

impl Blind {
    pub fn new(params: BlindParams) -> Self {
        Self {
            params,
            position: 0.0,
            motor: MotorDirection::Stopped,
            mode: BlindMode::Normal,
            target: None,
            pending_tilt: None,
            last_update: Instant::now(),
        }
    }

    pub fn snapshot(&self) -> BlindSnapshot {
        BlindSnapshot {
            position: self.position,
            tilt: if self.mode == BlindMode::Tilt {
                Some(self.position_to_tilt())
            } else {
                None
            },
            motor: self.motor,
            mode: self.mode,
        }
    }

    pub fn position(&self) -> f64 {
        self.position
    }

    pub fn is_tilt_active(&self) -> bool {
        self.mode == BlindMode::Tilt
    }

    // ── HA tilt value mapping ──────────────────────────────────────────
    //  HA default convention: tilt_opened_value=100, tilt_closed_value=0.
    //  Physical mapping:
    //    position @ tilt_upper  →  slats vertical/closed  →  HA tilt = 0
    //    position @ tilt_lower  →  slats horizontal/open  →  HA tilt = 100
    //  "Down movement opens slats, up movement closes slats."

    fn position_to_tilt(&self) -> f64 {
        let range = self.params.tilt_upper_pct() - self.params.tilt_lower_pct();
        if range <= 0.0 {
            return 0.0;
        }
        ((self.params.tilt_upper_pct() - self.position) / range * 100.0).clamp(0.0, 100.0)
    }

    fn tilt_to_position(&self, tilt: f64) -> f64 {
        let range = self.params.tilt_upper_pct() - self.params.tilt_lower_pct();
        (self.params.tilt_upper_pct() - (tilt / 100.0) * range)
            .clamp(self.params.tilt_lower_pct(), self.params.tilt_upper_pct())
    }

    /// Motor speed derived from config: if it takes `full_travel_time_secs` to go
    /// from 0% to 100%, speed is `100 / travel_time` percent per second.
    fn speed_pct_per_sec(&self) -> f64 {
        100.0 / self.params.full_travel_time_secs()
    }

    /// Set a target and pick motor direction. The 0.01 deadband avoids
    /// oscillation when the position is essentially already at the target
    /// (floating-point drift from repeated tick increments).
    fn start_toward(&mut self, target: f64) {
        self.target = Some(target);
        if target > self.position + 0.01 {
            self.motor = MotorDirection::Up;
        } else if target < self.position - 0.01 {
            self.motor = MotorDirection::Down;
        } else {
            self.position = target;
            self.motor = MotorDirection::Stopped;
            self.target = None;
        }
    }

    // ── Public commands ────────────────────────────────────────────────

    pub fn cmd_open(&mut self) {
        self.leave_tilt();
        self.start_toward(100.0);
    }

    pub fn cmd_close(&mut self) {
        self.leave_tilt();
        self.start_toward(0.0);
    }

    /// Stop aborts everything, including a mid-flight engagement sequence.
    /// If we were engaging (phase 1 or 2), we revert to Normal since the pin
    /// never fully locked.
    pub fn cmd_stop(&mut self) {
        self.motor = MotorDirection::Stopped;
        self.target = None;
        self.pending_tilt = None;
        if matches!(
            self.mode,
            BlindMode::EngagingPhase1 | BlindMode::EngagingPhase2
        ) {
            self.mode = BlindMode::Normal;
        }
    }

    pub fn cmd_set_position(&mut self, pos: f64) {
        let pos = pos.clamp(0.0, 100.0);
        self.leave_tilt();
        self.start_toward(pos);
    }

    /// If already in tilt mode, we can directly seek the position that corresponds
    /// to the requested tilt angle. Otherwise, the tilt value is deferred into
    /// `pending_tilt` and the engagement sequence is started — once the pin locks,
    /// `apply_mode_rules` will pick up the pending value and complete the move.
    pub fn cmd_set_tilt(&mut self, tilt: f64) {
        let tilt = tilt.clamp(0.0, 100.0);
        if self.mode == BlindMode::Tilt {
            let target_pos = self.tilt_to_position(tilt);
            self.start_toward(target_pos);
        } else {
            self.pending_tilt = Some(tilt);
            self.begin_engagement();
        }
    }

    // ── Internal helpers ───────────────────────────────────────────────

    /// Any height-based command (open/close/set_position) cancels tilt mode
    /// and any in-progress engagement, reverting to normal height control.
    fn leave_tilt(&mut self) {
        self.pending_tilt = None;
        if matches!(
            self.mode,
            BlindMode::Tilt | BlindMode::EngagingPhase1 | BlindMode::EngagingPhase2
        ) {
            self.mode = BlindMode::Normal;
        }
    }

    /// Starts the two-phase engagement sequence. Phase 1 lowers to the engage
    /// point; phase 2 raises through the tilt zone to lock the pin. If the
    /// blind is already at or below the engage point, phase 1 is skipped.
    fn begin_engagement(&mut self) {
        let lower_target = (self.params.tilt_lower_pct() - self.params.epsilon_pct()).max(0.0);
        if self.position <= lower_target + 0.01 {
            self.mode = BlindMode::EngagingPhase2;
            let upper_target =
                (self.params.tilt_upper_pct() + self.params.epsilon_pct()).min(100.0);
            self.start_toward(upper_target);
        } else {
            self.mode = BlindMode::EngagingPhase1;
            self.start_toward(lower_target);
        }
    }

    // ── Simulation tick ────────────────────────────────────────────────
    //
    // The simulation uses wall-clock time rather than fixed time steps, so
    // position updates are correct regardless of scheduling jitter in the
    // tick interval. The MQTT layer calls tick() at ~50ms intervals; each
    // call measures the actual elapsed time since the last call and moves
    // the position proportionally.

    /// Advance the simulation by real elapsed time. Returns `true` when state changed.
    pub fn tick(&mut self) -> bool {
        let now = Instant::now();
        let dt = now.duration_since(self.last_update).as_secs_f64();
        self.last_update = now;

        if self.motor == MotorDirection::Stopped {
            return false;
        }

        let delta = self.speed_pct_per_sec() * dt;
        let prev = self.position;

        match self.motor {
            MotorDirection::Up => self.position = (self.position + delta).min(100.0),
            MotorDirection::Down => self.position = (self.position - delta).max(0.0),
            MotorDirection::Stopped => return false,
        }

        // After moving, check if any mode-specific transition should fire
        // (e.g., phase 1 → phase 2, or tilt boundary enforcement).
        self.apply_mode_rules();

        (self.position - prev).abs() > 0.001
    }

    /// Per-mode rules applied after each position update. This is where mode
    /// transitions happen (engagement phase progression, tilt boundary enforcement).
    fn apply_mode_rules(&mut self) {
        match self.mode {
            BlindMode::Normal => {
                self.settle_target();
            }

            // Phase 1 complete: reached the engage point. Reverse direction
            // immediately — the motor now drives upward through the tilt zone.
            BlindMode::EngagingPhase1 => {
                let lower_target =
                    (self.params.tilt_lower_pct() - self.params.epsilon_pct()).max(0.0);
                if self.position <= lower_target + 0.01 {
                    self.position = lower_target;
                    self.mode = BlindMode::EngagingPhase2;
                    let upper_target =
                        (self.params.tilt_upper_pct() + self.params.epsilon_pct()).min(100.0);
                    self.target = Some(upper_target);
                    self.motor = MotorDirection::Up;
                }
            }

            // Phase 2 complete: pin locks. Snap position into the tilt zone
            // (at the top end) and fulfill the deferred tilt request if any.
            BlindMode::EngagingPhase2 => {
                let upper_target =
                    (self.params.tilt_upper_pct() + self.params.epsilon_pct()).min(100.0);
                if self.position >= upper_target - 0.01 {
                    self.position = self.params.tilt_upper_pct();
                    self.mode = BlindMode::Tilt;

                    if let Some(tilt) = self.pending_tilt.take() {
                        let tp = self.tilt_to_position(tilt);
                        self.start_toward(tp);
                    } else {
                        self.motor = MotorDirection::Stopped;
                        self.target = None;
                    }
                }
            }

            BlindMode::Tilt => {
                // Lower bound: the slats can't open further than fully horizontal.
                if self.position < self.params.tilt_lower_pct() {
                    self.position = self.params.tilt_lower_pct();
                    self.motor = MotorDirection::Stopped;
                    self.target = None;
                }
                // Upper bound: moving past tilt_upper disengages the pin,
                // returning to normal height control.
                if self.position > self.params.tilt_upper_pct() + 0.01 {
                    self.mode = BlindMode::Normal;
                }
                self.settle_target();
            }
        }

        if self.position <= 0.0 || self.position >= 100.0 {
            self.motor = MotorDirection::Stopped;
            self.target = None;
        }
    }

    /// Checks whether the motor has reached (or overshot) the target position
    /// and snaps to it. The 0.01 tolerance absorbs floating-point accumulation
    /// from many small tick increments.
    fn settle_target(&mut self) {
        if let Some(target) = self.target {
            let reached = match self.motor {
                MotorDirection::Up => self.position >= target - 0.01,
                MotorDirection::Down => self.position <= target + 0.01,
                MotorDirection::Stopped => true,
            };
            if reached {
                self.position = target.clamp(0.0, 100.0);
                self.motor = MotorDirection::Stopped;
                self.target = None;
            }
        }
    }
}

#[cfg(test)]
#[path = "blind_tests.rs"]
mod tests;
