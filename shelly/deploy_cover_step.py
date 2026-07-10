#!/usr/bin/env python3
"""
Deploy cover_step.js and the virtual buttons it listens for onto a Shelly Gen2+/Gen3 device.

Re-running is idempotent: the buttons are reconfigured in place and the script is overwritten rather
than duplicated.

The device password, when authentication is enabled, is read from $SHELLY_PASSWORD. Shelly Gen2+ RPC
uses HTTP digest authentication and the username is always "admin".
"""

import argparse
import json
import os
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path
from typing import Any

SCRIPT = Path(__file__).parent / "cover_step.js"

# How the script is listed on the device, and the key this deployer matches on to overwrite it in
# place. Renaming it strands the previously deployed copy under its old name.
SCRIPT_NAME = "Cover Step"

# Button ids are pinned so cover_step.js can hardcode its component keys; keep the two in sync. Home
# Assistant only builds an entity when meta.ui.view names one of the modes it maps for the platform,
# so a button without it stays hidden, in the device web UI and in Home Assistant alike.
BUTTONS = ((200, "Step Up"), (201, "Step Down"))
BUTTON_META = {"ui": {"view": "button"}}


def rpc(opener: urllib.request.OpenerDirector, host: str, method: str, **params: Any) -> Any:
    """
    Issue one JSON-RPC call against the device and return its result.
    """
    payload = json.dumps({"id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(f"http://{host}/rpc", data=payload,
                                     headers={"Content-Type": "application/json"})
    try:
        response = json.load(opener.open(request))
    except urllib.error.HTTPError as failure:
        # Shelly reports RPC errors with a non-2xx status and the detail in the body, which urllib
        # raises rather than returns. Read it back out; anything else propagates.
        response = json.load(failure)
    if "error" in response:
        raise RuntimeError(f"{method} failed: {response['error']}")
    return response["result"]


def main() -> None:
    """
    Deploy the script and its buttons to the device named on the command line.
    """
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host", help="device hostname or IP")
    host = parser.parse_args().host

    opener = urllib.request.build_opener()
    password = os.environ.get("SHELLY_PASSWORD")
    if password:
        passwords = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        passwords.add_password(None, f"http://{host}/", "admin", password)
        opener.add_handler(urllib.request.HTTPDigestAuthHandler(passwords))
    call = partial(rpc, opener, host)

    info = call("Shelly.GetDeviceInfo")
    print(f"device: {info['model']} fw={info['ver']} gen={info['gen']}")

    # The device answered above, so a failure here means there is no cover:0, not a transport fault.
    call("Cover.GetStatus", id=0)

    for button_id, name in BUTTONS:
        key = f"button:{button_id}"
        config = {"name": name, "meta": BUTTON_META}
        if call("Shelly.GetComponents", keys=[key], dynamic_only=True)["total"]:
            # Rewrite rather than skip, so a re-run repairs a button created without the view.
            call("Button.SetConfig", id=button_id, config=config)
            print(f"updated virtual {key} ({name})")
        else:
            call("Virtual.Add", type="button", id=button_id, config=config)
            print(f"created virtual {key} ({name})")

    existing = [s for s in call("Script.List")["scripts"] if s["name"] == SCRIPT_NAME]
    if existing:
        script_id = existing[0]["id"]
        call("Script.Stop", id=script_id)
    else:
        script_id = call("Script.Create", name=SCRIPT_NAME)["id"]

    call("Script.PutCode", id=script_id, code=SCRIPT.read_text(), append=False)
    call("Script.SetConfig", id=script_id, config={"enable": True})
    call("Script.Start", id=script_id)
    print(f"deployed {SCRIPT.name} as \"{SCRIPT_NAME}\" (script id={script_id}); "
          "buttons appear in Home Assistant after the next Shelly poll")


if __name__ == "__main__":
    main()
