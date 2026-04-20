# Unitree G1 EDU Robot Controller

A Python-based controller for the Unitree G1 EDU humanoid robot. Reads the wireless gamepad via DDS and maps button presses to robot behaviors (LED control, arm movement, TTS speech). Runs as a systemd service on boot.

---

## Features

- Wireless controller input via Unitree SDK DDS (no USB gamepad needed)
- Combo detection with modifier-key safety — holding L2/R2/LB/RB blocks single-button actions
- Non-blocking behavior execution (each script runs in its own thread)
- Per-action cooldowns to prevent accidental re-triggers
- Graceful shutdown on A+B or SIGTERM
- Runs as a systemd service

---

## Button Map

| Input | Action |
|-------|--------|
| A | Cycle robot LED colors |
| B | Right arm movement demo |
| X | Speak via TTS (English) |
| A + B | Exit controller, hand back to Unitree default joystick |
| L2 + _any_ | Combo with no registered handler → silently ignored, single-button action does NOT fire |

> Any unregistered combo is safely ignored. Add new combos in `main.py` and `behavior_manager.py`.

---

## Robot Mode Gating

Button events are only processed when the robot is in one of these modes:

| mode_machine | State |
|---|---|
| 3 | Balance stand |
| 4 | Walk |
| 5 | Run |

In all other modes (idle, damping, getting up) all input is suppressed.

---

## Requirements

- Unitree G1 EDU with `unitree_sdk2py` installed
- Python **3.10** (the only version with a compatible `cyclonedds` C extension on this robot)
- `numpy` (for arm control)

Install Python deps:
```bash
pip3.10 install numpy
```

The `inputs` entry in `requirements.txt` is only needed if you switch to the USB gamepad backend — it is not required for the default Unitree DDS backend.

---

## Setup

### 1. Clone the repo

```bash
git clone <repo-url> /home/unitree/unitree/robot_controller
```

### 2. Set your network interface

Check your interface name:
```bash
ip link
```

Update the constant in both files:

```python
# input_handler.py
UNITREE_NETWORK_INTERFACE = "enP8p1s0"

# behavior_manager.py
UNITREE_NETWORK_INTERFACE = "enP8p1s0"
```

### 3. Run manually (for testing)

```bash
python3.10 main.py
```

### 4. Install as a systemd service

```bash
sudo cp unitree-controller.service /etc/systemd/system/
sudo sed -i 's|ExecStart=/usr/bin/python3 |ExecStart=/usr/bin/python3.10 |' /etc/systemd/system/unitree-controller.service
sudo systemctl daemon-reload
sudo systemctl enable unitree-controller
sudo systemctl start unitree-controller
```

Check logs:
```bash
journalctl -u unitree-controller -f
```

---

## Project Structure

```
robot_controller/
├── main.py                  # Entry point, main loop, button bindings
├── input_handler.py         # DDS gamepad reader, combo detection
├── behavior_manager.py      # Action registry and dispatch
├── 01_control_leds.py       # LED color cycle (triggered by Button A)
├── 02_arm_control.py        # Right arm movement demo (triggered by Button B)
├── 03_english_tts.py        # TTS speech (triggered by Button X)
├── debug_buttons.py         # Debug tool: print raw wireless_remote bytes
├── unitree-controller.service  # systemd unit file
└── requirements.txt
```

---

## Adding New Behaviors

**1. Add the action method to `behavior_manager.py`:**
```python
def action_my_thing(self):
    log.info("Doing my thing")
    subprocess.run([sys.executable, MY_SCRIPT_PATH, UNITREE_NETWORK_INTERFACE], ...)
```

**2. Register it in `main.py`:**
```python
# Single button
behavior.register_button("Y", behavior.action_my_thing, cooldown=1.0)

# Combo
behavior.register_combo(frozenset({"L2", "A"}), behavior.action_my_thing, cooldown=2.0)
```

---

## Combo Safety Logic

Modifier keys (`L2`, `R2`, `LB`, `RB`) held alone never fire a single-press action. When any button is pressed while another is already held, the event is emitted as a `combo_press` instead of a `button_press`. If no combo handler is registered, the event is silently dropped — the individual button action does not run.

This means the Unitree default joystick combos (L2+A, L2+B, etc.) pass through to the robot's own firmware without interference.

---

## Debugging

### Check which bit a button uses

If you add a new button and are unsure of its bit position in `wireless_remote`:

```bash
python3.10 debug_buttons.py
```

Press buttons and watch which `byte2[bitX]` or `byte3[bitX]` flips.

### Verify the controller is sending data

```bash
python3.10 main.py
```

Look for:
```
Robot mode changed: 3 (controller ACTIVE)
```

If you see mode 0 or 1, the robot is not yet standing — put it in balance-stand mode first.
