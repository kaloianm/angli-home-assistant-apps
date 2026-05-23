use super::*;
use std::time::Duration;

fn test_params() -> BlindParams {
    BlindParams::new(
        10.0, // full_travel_time_secs — fast but above the 5s minimum
        3.0,  // tilt_lower_pct
        10.0, // tilt_upper_pct
        2.0,  // epsilon_pct
        5.0,  // step_pct
        10.0, // tilt_step_pct
    )
    .unwrap()
}

fn run_until_stopped(blind: &mut Blind, max_ticks: usize) {
    for _ in 0..max_ticks {
        blind.tick();
        if blind.motor == MotorDirection::Stopped {
            break;
        }
        blind.last_update = Instant::now() - Duration::from_millis(100);
    }
}

#[test]
fn test_open_close() {
    let mut blind = Blind::new(test_params());
    assert!((blind.position() - 0.0).abs() < 0.1);

    blind.cmd_open();
    run_until_stopped(&mut blind, 2000);
    assert!((blind.position() - 100.0).abs() < 0.5);

    blind.cmd_close();
    run_until_stopped(&mut blind, 2000);
    assert!(blind.position() < 0.5);
}

#[test]
fn test_set_position() {
    let mut blind = Blind::new(test_params());
    blind.cmd_set_position(50.0);
    run_until_stopped(&mut blind, 2000);
    assert!((blind.position() - 50.0).abs() < 1.0);
}

#[test]
fn test_tilt_engagement_and_range() {
    let mut blind = Blind::new(test_params());
    blind.cmd_set_tilt(50.0);
    assert!(matches!(
        blind.mode,
        BlindMode::EngagingPhase1 | BlindMode::EngagingPhase2
    ));

    run_until_stopped(&mut blind, 5000);
    assert!(blind.is_tilt_active());

    let expected_pos = blind.tilt_to_position(50.0);
    assert!(
        (blind.position() - expected_pos).abs() < 0.5,
        "position {} should be near {}",
        blind.position(),
        expected_pos,
    );
}

#[test]
fn test_tilt_lower_safety() {
    let mut blind = Blind::new(test_params());
    blind.cmd_set_tilt(100.0); // fully open = tilt_lower position
    run_until_stopped(&mut blind, 5000);
    assert!(blind.is_tilt_active());
    assert!(blind.position() >= blind.params.tilt_lower_pct() - 0.1);
}

#[test]
fn test_leave_tilt_on_position_cmd() {
    let mut blind = Blind::new(test_params());
    blind.cmd_set_tilt(50.0);
    run_until_stopped(&mut blind, 5000);
    assert!(blind.is_tilt_active());

    blind.cmd_set_position(80.0);
    assert!(!blind.is_tilt_active());
    run_until_stopped(&mut blind, 5000);
    assert!((blind.position() - 80.0).abs() < 1.0);
}
