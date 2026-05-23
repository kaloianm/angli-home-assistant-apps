use super::*;

#[test]
fn test_rejects_short_travel_time() {
    assert!(BlindParams::new(3.0, 3.0, 10.0, 2.0, 5.0, 10.0).is_err());
}

#[test]
fn test_rejects_zero_epsilon() {
    assert!(BlindParams::new(10.0, 3.0, 10.0, 0.0, 5.0, 10.0).is_err());
}

#[test]
fn test_rejects_inverted_tilt_range() {
    assert!(BlindParams::new(10.0, 15.0, 5.0, 2.0, 5.0, 10.0).is_err());
}

#[test]
fn test_rejects_negative_step() {
    assert!(BlindParams::new(10.0, 3.0, 10.0, 2.0, -1.0, 10.0).is_err());
}
