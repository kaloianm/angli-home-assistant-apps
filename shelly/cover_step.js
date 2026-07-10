// Exposes a KNX-style "step" lever for a Shelly-controlled cover.
//
// KNX blind actuators nudge a blind by pulsing the motor for a fixed, actuator-timed interval,
// exposed on their Stop/Step communication object. A Shelly cover entity in Home Assistant offers
// only absolute positioning, whose 1% granularity is far coarser than one pulse. Cover.Open and
// Cover.Close accept a `duration`, though, so the device can time the same pulse itself.
//
// The two virtual buttons this script listens for are created by deploy_cover_step.py, with the ids
// pinned below. They are deliberately not declared as managed components in a `@meta` header: that
// form needs Script.getVcHandle, which older firmware does not have. Shelly.addEventHandler works
// everywhere.

let COVER_ID = 0;

// Seconds. The Cover RPC floor is 0.1; matches the KNX actuator's step time.
let STEP_SECONDS = 0.1;

// Must agree with the ids deploy_cover_step.py passes to Virtual.Add.
let STEP_UP_KEY = "button:200";
let STEP_DOWN_KEY = "button:201";

function step(method) {
  Shelly.call(method, { id: COVER_ID, duration: STEP_SECONDS }, function (result, code, message) {
    if (code !== 0) {
      print("cover step ", method, " failed: code=", code, " ", message);
    }
  });
}

Shelly.addEventHandler(function (event) {
  if (!event.info || event.info.event !== "single_push") {
    return;
  }
  if (event.component === STEP_UP_KEY) {
    step("Cover.Open");
  } else if (event.component === STEP_DOWN_KEY) {
    step("Cover.Close");
  }
});
