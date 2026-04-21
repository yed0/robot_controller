# Unitree G1 EDU Robot Controller

A Python-based gamepad controller for the **Unitree G1 EDU** humanoid robot. It reads the wireless gamepad state directly over the robot's internal DDS network and maps button presses to robot behaviors — LED effects, arm movements, TTS speech, and audio playback. It is designed to run automatically as a **systemd service on robot boot**.

---

## How It Works

The Unitree G1's wireless controller does **not** appear as a Linux gamepad device. Instead, its button state is embedded in every `LowState_` message published on the robot's internal DDS topic `rt/lf/lowstate`. This controller subscribes to that topic at 50 Hz, parses the `wireless_remote` bitmask fields, and fires press/release events using edge detection.

```
Wireless gamepad
      │
      │  (DDS / rt/lf/lowstate)
      ▼
input_handler.py        ← reads wireless_remote bytes, detects button edges,
      │                    buffers combo detection (150 ms window)
      ▼
main.py (100 Hz loop)   ← drains event queue, dispatches to BehaviorManager
      │
      ▼
behavior_manager.py     ← looks up registered action, runs it in a daemon thread
      │
      ▼
01_control_leds.py      ← Unitree AudioClient / LEDs
02_arm_control.py       ← arm joint control
03_english_tts.py       ← TTS speech via AudioClient
02_playback_wav.py      ← WAV file playback via AudioClient
```

Each behavior script runs in its own background thread so button presses never block the main control loop.

---

## Requirements

| Requirement | Notes |
|---|---|
| Unitree G1 EDU robot | Any firmware that exposes `rt/lf/lowstate` |
| **Python 3.10** | **Required.** `unitree_sdk2py` uses a compiled `cyclonedds` C extension with an ABI that only works on Python 3.10 on this robot. Do not use 3.11 or 3.13. |
| `unitree_sdk2py` | Unitree's Python SDK — not on PyPI, must be installed from the SDK package |
| `numpy` | Used by `02_arm_control.py` |

### Install Python dependencies

```bash
pip3.10 install numpy
```

### Install the Unitree SDK

```bash
# Replace the path with wherever the SDK source lives on your robot
pip3.10 install -e /path/to/unitree_sdk2py
```

---

## Setup

### 1. Clone the repository

```bash
git clone <repo-url> /home/unitree/unitree/robot_controller
cd /home/unitree/unitree/robot_controller
```

### 2. Find your network interface

The DDS layer needs to know which network interface connects to the robot's internal network.

```bash
ip link
```

The interface is typically `enP8p1s0` on the G1 EDU onboard computer. Look for the interface that is UP and on the `192.168.123.x` subnet.

### 3. Set the network interface in the code

Open **both** files below and update the constant near the top:

**`input_handler.py`** (line ~29):
```python
UNITREE_NETWORK_INTERFACE = "enP8p1s0"   # ← change to your interface
```

**`behavior_manager.py`** (line ~41):
```python
UNITREE_NETWORK_INTERFACE = "enP8p1s0"   # ← change to your interface
```

### 4. Set the allowed robot mode

By default, the controller only forwards button events when the robot is in **running mode** (mode 5). Change `ALLOWED_ROBOT_MODES` in `input_handler.py` if you want other modes to be active:

```python
# input_handler.py, line ~45
ALLOWED_ROBOT_MODES: set[int] = {5}   # running mode only

# To also allow balance-stand and walk:
# ALLOWED_ROBOT_MODES: set[int] = {3, 4, 5}
```

| `mode_machine` value | State |
|---|---|
| 0 | IDLE — uninitialised |
| 1 | DAMPING — joints limp (safe/emergency) |
| 2 | STAND_UP — transitioning to stand |
| 3 | BALANCE_STAND — standing balanced |
| 4 | WALK |
| 5 | RUN |

In any mode not in `ALLOWED_ROBOT_MODES`, all button input is suppressed and the controller stays silent.

---

## Running the Controller

### Manual run (recommended for first-time testing)

```bash
python3.10 main.py
```

Watch the log output. You should see:

```
=== Unitree G1 EDU Controller Starting ===
Unitree backend: initialising DDS on interface 'enP8p1s0'
Unitree backend: subscribed to rt/lf/lowstate — waiting for controller data...
```

As the robot's mode changes, you will see lines like:

```
Robot mode changed: 5 (controller ACTIVE)
```

The controller is now listening for button input. Press buttons on the wireless gamepad to trigger behaviors.

### Stop the manual run

Press `Ctrl+C` or press `A + B` on the gamepad.

---

## Button Map

> **Important:** All button actions (except **Start**) are suppressed until you press **Start** to enable the controller. The Start button toggles input on and off.

| Button / Combo | Action | Cooldown |
|---|---|---|
| **Start** | Toggle controller input ON / OFF | 0.5 s |
| **A** | Cycle robot LED colors through 8 colors (Red → Green → Blue → Yellow → Magenta → Cyan → White → Off) | 1.0 s |
| **B** | Run right-arm movement demo | 1.0 s |
| **X** | Speak "Hello, this is Unitree G1 course" via TTS | 2.0 s |
| **Y** | Play `example.wav` over the robot's speaker | 2.0 s |
| **A + B** | Exit this controller and hand back to the Unitree default joystick program | 2.0 s |
| **L2 / R2 / LB / RB + any button** | Combo — if no handler is registered, silently ignored. The single-button action does **not** fire. | — |

### Combo safety

Modifier keys (`L2`, `R2`, `LB`, `RB`) held alone never trigger a single-press action. When any button is pressed while another is already held, the event is emitted as a `combo_press`. If no combo handler is registered for that combination, the event is silently dropped. This means the Unitree default joystick combos (e.g. `L2+A` to sit down) pass through to the robot's own firmware without interference.

---

## Running as a systemd Service (auto-start on boot)

### 1. Copy and fix the service file

The service file needs its path and Python version corrected before installing:

```bash
# Copy to systemd
sudo cp unitree-controller.service /etc/systemd/system/

# Fix the Python path (service file defaults to python3, must be python3.10)
sudo sed -i 's|ExecStart=/usr/bin/python3 |ExecStart=/usr/bin/python3.10 |' \
    /etc/systemd/system/unitree-controller.service

# Fix the working directory path
sudo sed -i 's|WorkingDirectory=/home/unitree/robot_controller|WorkingDirectory=/home/unitree/unitree/robot_controller|' \
    /etc/systemd/system/unitree-controller.service

# Fix the script path in ExecStart as well
sudo sed -i 's|ExecStart=/usr/bin/python3.10 /home/unitree/robot_controller/main.py|ExecStart=/usr/bin/python3.10 /home/unitree/unitree/robot_controller/main.py|' \
    /etc/systemd/system/unitree-controller.service
```

### 2. Enable and start the service

```bash
sudo systemctl daemon-reload
sudo systemctl enable unitree-controller
sudo systemctl start unitree-controller
```

### 3. Check the service is running

```bash
sudo systemctl status unitree-controller
```

### 4. Watch live logs

```bash
journalctl -u unitree-controller -f
```

### Stop / restart the service

```bash
sudo systemctl stop unitree-controller
sudo systemctl restart unitree-controller
```

---

## Project Structure

```
robot_controller/
├── main.py                     # Entry point, main loop (100 Hz), button bindings
├── input_handler.py            # DDS gamepad reader, combo detection
├── behavior_manager.py         # Action registry and dispatch, all built-in actions
│
├── 01_control_leds.py          # Cycles LED colors (triggered by Button A)
├── 02_arm_control.py           # Right arm movement demo (triggered by Button B)
├── 03_english_tts.py           # English TTS speech (triggered by Button X)
├── 02_playback_wav.py          # WAV file playback (triggered by Button Y)
├── example.wav                 # Sample audio file for Button Y
│
├── debug_buttons.py            # Debug tool: prints raw wireless_remote bytes live
├── unitree-controller.service  # systemd unit file
└── requirements.txt
```

---

## Adding New Behaviors

### Step 1 — Write your behavior script

Create a new Python script (e.g. `04_my_behavior.py`) that accepts the network interface as `sys.argv[1]` and does whatever you need.

### Step 2 — Add the action to `behavior_manager.py`

At the top of the file, add a path constant:
```python
MY_SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "04_my_behavior.py")
```

Then add a method to the `BehaviorManager` class:
```python
def action_button_y(self):
    log.info("Button Y → launching my behavior")
    result = subprocess.run(
        [sys.executable, MY_SCRIPT_PATH, UNITREE_NETWORK_INTERFACE],
        capture_output=True, text=True, timeout=30.0,
    )
    if result.returncode != 0:
        log.error(f"Script failed: {result.stderr.strip()}")
```

### Step 3 — Register it in `main.py`

```python
# Single button (replace "Y" with any unbound button name)
behavior.register_button("Y", behavior.action_button_y, cooldown=1.0)

# Or a combo
behavior.register_combo(frozenset({"L2", "A"}), behavior.action_my_thing, cooldown=2.0)
```

Available button names: `A`, `B`, `X`, `Y`, `LB`, `RB`, `L2`, `R2`, `Start`, `Back`, `Up`, `Down`, `Left`, `Right`, `F1`, `F2`

---

## Debugging

### Print raw wireless_remote bytes

If you are mapping a new button and need to find its bit position:

```bash
python3.10 debug_buttons.py
```

Press buttons on the gamepad and watch which `byte2[bitX]` or `byte3[bitX]` field toggles.

### Controller receives no input

1. Verify the robot is in an allowed mode — check `ALLOWED_ROBOT_MODES` in `input_handler.py`.
2. Check the logs for: `Robot mode changed: X (controller SUPPRESSED)`. If you see this, either change `ALLOWED_ROBOT_MODES` or put the robot in running mode first.
3. Make sure `UNITREE_NETWORK_INTERFACE` is set correctly in both `input_handler.py` and `behavior_manager.py`.

### Behavior scripts fail with `ModuleNotFoundError: unitree_sdk2py`

You are running with the wrong Python. Make sure to use `python3.10` — it is the only version on this robot with a working `cyclonedds` ABI:

```bash
python3.10 main.py        # correct
python3 main.py           # wrong — may resolve to python3.11 or 3.13
```

The systemd service must also reference `/usr/bin/python3.10`. Verify with:

```bash
grep ExecStart /etc/systemd/system/unitree-controller.service
```

### TTS does not speak

`03_english_tts.py` sends a short silent PCM burst before the TTS call to open the speaker pipeline. Without this, `TtsMaker` is silently dropped on cold boot. If speech still does not play, check that the robot's audio service is running and that volume is not muted (the script calls `SetVolume(100)` automatically).

### Button A triggers LED but colors look wrong

LED commands require >200 ms between calls. The script already enforces 500 ms delays per color. If colors still look wrong, check that only one instance of the LED script is running (the 1.0 s cooldown on Button A prevents overlap, but check the logs for overlapping dispatches).
