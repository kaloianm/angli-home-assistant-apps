/// Physical/config parameters for a single blind.
///
/// Fields are private — the only way to obtain an instance is through
/// `BlindParams::new()`, which validates all invariants upfront.
/// Getter methods provide read access within the crate.
#[derive(Debug, Clone)]
pub struct BlindParams {
    // Total motor travel time in seconds from fully closed/fully down (0%) to fully open/fully up (100%).
    full_travel_time_secs: f64,
    // Position (0-100%) below which the engagement pin can be reached by upward movement.
    tilt_lower_pct: f64,
    // Position (0-100%) above which tilt mode disengages by upward movement and engages by downward movement.
    tilt_upper_pct: f64,
    // Overshoot distance (in %) required to trigger/release the mechanical pin when engaging/disengaging.
    epsilon_pct: f64,
    step_pct: f64,
    tilt_step_pct: f64,
}

impl BlindParams {
    pub fn new(
        full_travel_time_secs: f64,
        tilt_lower_pct: f64,
        tilt_upper_pct: f64,
        epsilon_pct: f64,
        step_pct: f64,
        tilt_step_pct: f64,
    ) -> Result<Self, String> {
        macro_rules! ensure {
            ($cond:expr, $err:expr) => {
                if !($cond) {
                    return Err($err.into());
                }
            };
        }

        ensure!(
            full_travel_time_secs > 5.0,
            "full_travel_time_secs must be > 5.0"
        );
        ensure!(tilt_lower_pct > 0.0, "tilt_lower_pct must be positive");
        ensure!(tilt_upper_pct > 0.0, "tilt_upper_pct must be positive");
        ensure!(
            tilt_lower_pct < tilt_upper_pct,
            "tilt_lower_pct must be < tilt_upper_pct"
        );
        ensure!(epsilon_pct > 0.0, "epsilon_pct must be positive");
        ensure!(
            tilt_lower_pct > epsilon_pct,
            "tilt_lower_pct must be > epsilon_pct so the engage point is > 0"
        );
        ensure!(
            tilt_upper_pct + epsilon_pct <= 100.0,
            "tilt_upper_pct + epsilon_pct must be <= 100"
        );
        ensure!(step_pct > 0.0, "step_pct must be positive");
        ensure!(tilt_step_pct > 0.0, "tilt_step_pct must be positive");

        Ok(Self {
            full_travel_time_secs,
            tilt_lower_pct,
            tilt_upper_pct,
            epsilon_pct,
            step_pct,
            tilt_step_pct,
        })
    }

    pub(crate) fn full_travel_time_secs(&self) -> f64 {
        self.full_travel_time_secs
    }
    pub(crate) fn tilt_lower_pct(&self) -> f64 {
        self.tilt_lower_pct
    }
    pub(crate) fn tilt_upper_pct(&self) -> f64 {
        self.tilt_upper_pct
    }
    pub(crate) fn epsilon_pct(&self) -> f64 {
        self.epsilon_pct
    }
    pub(crate) fn step_pct(&self) -> f64 {
        self.step_pct
    }
    pub(crate) fn tilt_step_pct(&self) -> f64 {
        self.tilt_step_pct
    }
}

#[cfg(test)]
#[path = "blind_params_tests.rs"]
mod tests;
