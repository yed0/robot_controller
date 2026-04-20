# Session Snapshot — April 14, 2026 (pre-reboot)

## What was wrong

1. **`ModuleNotFoundError: No module named 'unitree_sdk2py'`** when pressing Button A.
   - The systemd service runs `/usr/bin/python3` (Python 3.11), which does not have the SDK.
   - The SDK is installed as an editable package under miniforge3 (Python 3.13), but its compiled `cyclonedds` C extension has an ABI mismatch on 3.13.
   - **`/usr/bin/python3.10` is the only Python that can import `unitree_sdk2py` cleanly.**

2. **`Gamepad unplugged — retrying in 2 s...`** — the `inputs` library was wrong approach entirely.
   - `cat /proc/bus/input/devices` confirmed there are zero gamepad/joystick entries in `/dev/input/`.
   - The Unitree G1 wireless controller communicates via DDS (same middleware as all SDK scripts), not as a Linux evdev device.

---

## Changes already made to the code

| File | Change |
|------|--------|
| `behavior_manager.py` | `subprocess.run` for LED script changed from `"python3"` to `"python3.10"` |
| `input_handler.py` | `BACKEND` changed from `"inputs"` to `"unitree"` |
| `input_handler.py` | Added `UNITREE_NETWORK_INTERFACE = "eth0"` config constant |
| `input_handler.py` | Added identity entries to `BUTTON_MAP` for Unitree logical names (A, B, X, Y, LB, RB, Start, Back, Up, Down, Left, Right) |
| `input_handler.py` | Implemented `_loop_unitree_backend()` — subscribes to `rt/lf/lowstate`, parses `wireless_remote` bytes 2 & 3 bitmasks, does 50 Hz edge-detection polling, pushes events via `_push_button_event()` |

---

## One thing still pending (requires sudo)

The systemd service file still points to `/usr/bin/python3`. This command must be run after reboot:

```bash
sudo sed -i 's|ExecStart=/usr/bin/python3 |ExecStart=/usr/bin/python3.10 |' /etc/systemd/system/unitree-controller.service
sudo systemctl daemon-reload
sudo systemctl restart unitree-controller
```

---

## How the Unitree backend works

The wireless controller data is embedded in every `LowState_` message on the DDS topic `rt/lf/lowstate` as a 40-byte `wireless_remote` field:

- **Byte 2** (shoulder/menu): bit0=RB, bit1=LB, bit2=Start, bit3=Back
- **Byte 3** (face/D-pad): bit0=A, bit1=B, bit2=X, bit3=Y, bit4=Up, bit5=Right, bit6=Down, bit7=Left

The backend subscribes via `ChannelSubscriber`, polls at 50 Hz, and fires press/release events only on state changes (edge detection).

---

## Expected log output after fix

```
Unitree backend: initialising DDS on interface 'eth0'
Unitree backend: subscribed to rt/lf/lowstate — waiting for controller data...
```

Pressing Button A should then log:
```
Button A → launching LED control script
LED script finished OK
```
