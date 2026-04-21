#!/usr/bin/env python3
"""
input_handler.py — Gamepad / Joystick Abstraction Layer

Reads hardware input and converts it into a queue of discrete events
(button presses, combos, axis moves, etc.) for consumption by the main loop.

Hardware backend options (choose ONE — see BACKEND section below):
  A) inputs  library  — works with most USB/Bluetooth gamepads on Linux
  B) Unitree SDK       — replace _loop_unitree_backend() when SDK is available
  C) Simulated         — deterministic input for unit tests / CI
"""

import logging
import queue
import threading
import time
from typing import Optional

log = logging.getLogger("input_handler")

# ─── Backend selector ─────────────────────────────────────────────────────────
# Set to "inputs", "unitree", or "simulated"
BACKEND = "unitree"

# ─── Unitree SDK network interface ────────────────────────────────────────────
# Used by the "unitree" backend to initialise DDS.
# Run 'ip link' on the robot to find the correct interface (typically "eth0").
UNITREE_NETWORK_INTERFACE = "enP8p1s0"

# ─── Allowed robot modes for controller input ────────────────────────────────
# Button events are suppressed unless the robot's mode_machine value is in
# this set.  This prevents accidental triggers while the robot is limp,
# initialising, or getting up.
#
# Typical Unitree G1 mode_machine values:
#   0 = IDLE          — uninitialised / not yet standing
#   1 = DAMPING       — joints limp (safe / emergency mode)
#   2 = STAND_UP      — transitioning to stand
#   3 = BALANCE_STAND — standing balanced  ("regular mode")
#   4 = WALK          — walking
#   5 = RUN           — running             ("running mode")
#
# Run with DEBUG logging to observe actual values: logging.basicConfig(level=logging.DEBUG)
ALLOWED_ROBOT_MODES: set[int] = {5}   # running mode only

# ─── Combo detection window ───────────────────────────────────────────────────
# Two buttons pressed within this many seconds of each other count as a combo.
# 0.15 s (150 ms) is a comfortable "simultaneous press" tolerance for humans.
COMBO_WINDOW_SECONDS = 0.15

# ─── Button name map ──────────────────────────────────────────────────────────
# Maps raw evdev event codes → logical button names.
# For the `inputs` library:
#   BTN_SOUTH = A,  BTN_EAST = B,  BTN_WEST = X,  BTN_NORTH = Y
#   BTN_TL = LB,    BTN_TR = RB,   BTN_SELECT = Back, BTN_START = Start
BUTTON_MAP = {
    # inputs library evdev codes (used by "inputs" backend)
    "BTN_SOUTH": "A",
    "BTN_EAST":  "B",
    "BTN_WEST":  "X",
    "BTN_NORTH": "Y",
    "BTN_TL":    "LB",
    "BTN_TR":    "RB",
    "BTN_TL2":   "L2",
    "BTN_TR2":   "R2",
    "BTN_START": "Start",
    "BTN_SELECT":"Back",
    # Unitree SDK logical pass-through (used by "unitree" backend)
    "A": "A", "B": "B", "X": "X", "Y": "Y",
    "LB": "LB", "RB": "RB",
    "L2": "L2", "R2": "R2",
    "F1": "F1", "F2": "F2",
    "Start": "Start", "Back": "Back",
    "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right",
}

# Modifier keys: holding these alone never fires a single-press action.
# They only fire as part of a combo with another button.
MODIFIER_KEYS = {"L2", "R2", "LB", "RB"}


class GamepadHandler:
    """
    Thread-safe gamepad abstraction with combo detection.

    Public API:
        handler.start()          → begin background reading thread
        handler.stop()           → signal thread to exit and join
        handler.get_events()     → return list of queued events (non-blocking)

    Event dict schema:
        Single press:  {"type": "button_press",   "button": "A",               "value": 1.0, "ts": float}
        Release:       {"type": "button_release",  "button": "A",               "value": 0.0, "ts": float}
        Combo:         {"type": "combo_press",     "combo": frozenset({"A","B"}),"value": 1.0, "ts": float}
        Axis:          {"type": "axis_move",       "axis":  "LX",               "value": float,"ts": float}

    Combo detection logic:
        When a button goes down it is placed in a "pending" pool instead of
        being emitted immediately.  A background flush thread checks every 5 ms:
          • If 2+ buttons are pending at the same time → emit one "combo_press"
            event containing all of them, then clear the pool.
          • If a button has been pending for > COMBO_WINDOW_SECONDS with no
            partner → emit it as a normal "button_press" and remove it from
            the pool.
        This means a single press is delayed by at most COMBO_WINDOW_SECONDS
        (150 ms), which is imperceptible in practice.
    """

    def __init__(self):
        self._event_queue: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Tracks which logical buttons are physically held down right now.
        self._button_state: dict[str, bool] = {}

        # ── Combo detection state ─────────────────────────────────────────
        # Buttons pressed recently but not yet emitted — waiting for a partner.
        # Format: { logical_name: press_timestamp }
        self._pending_presses: dict[str, float] = {}
        self._combo_lock = threading.Lock()

        self._combo_flush_thread: Optional[threading.Thread] = None

    # ── Public Methods ────────────────────────────────────────────────────────

    def start(self):
        """Start the background reader thread and the combo-flush watcher thread."""
        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._reader_loop,
            name="gamepad-reader",
            daemon=True,
        )
        self._thread.start()

        # Separate light thread that flushes pending presses (runs at ~200 Hz)
        self._combo_flush_thread = threading.Thread(
            target=self._combo_flush_loop,
            name="combo-flush",
            daemon=True,
        )
        self._combo_flush_thread.start()

        log.info(f"GamepadHandler started (backend={BACKEND})")

    def stop(self):
        """Signal reader threads to stop and wait for them."""
        self._stop_event.set()
        for t in (self._thread, self._combo_flush_thread):
            if t and t.is_alive():
                t.join(timeout=2.0)
        log.info("GamepadHandler stopped")

    def get_events(self) -> list[dict]:
        """
        Drain and return all queued events since last call.
        Non-blocking — returns [] immediately if nothing is waiting.
        """
        events = []
        while True:
            try:
                events.append(self._event_queue.get_nowait())
            except queue.Empty:
                break
        return events

    # ── Combo Flush Loop ──────────────────────────────────────────────────────

    def _combo_flush_loop(self):
        """
        Background thread that decides what to do with pending button presses.

        Runs every 5 ms. Two outcomes possible:
          1) Two or more buttons are simultaneously pending → combo event.
          2) A single button has exceeded COMBO_WINDOW_SECONDS → single press event.
        """
        while not self._stop_event.is_set():
            time.sleep(0.005)   # 5 ms tick
            now = time.monotonic()

            with self._combo_lock:
                if not self._pending_presses:
                    continue

                # ── Case 1: Multiple buttons pending simultaneously → combo ─
                if len(self._pending_presses) >= 2:
                    combo_keys = frozenset(self._pending_presses.keys())
                    log.debug(f"Combo detected: {sorted(combo_keys)}")
                    self._event_queue.put({
                        "type":  "combo_press",
                        "combo": combo_keys,
                        "value": 1.0,
                        "ts":    now,
                    })
                    self._pending_presses.clear()
                    continue

                # ── Case 2: Single button, check if its window has expired ─
                stale = {
                    btn: ts
                    for btn, ts in self._pending_presses.items()
                    if now - ts > COMBO_WINDOW_SECONDS
                }
                for btn, ts in stale.items():
                    del self._pending_presses[btn]
                    # Modifier keys held alone never emit a single-press —
                    # they only act as combo partners.
                    if btn in MODIFIER_KEYS:
                        log.debug(f"Modifier key [{btn}] held alone — no single-press emitted")
                        continue
                    log.debug(f"Single press emitted (window expired): {btn}")
                    self._event_queue.put({
                        "type":   "button_press",
                        "button": btn,
                        "value":  1.0,
                        "ts":     ts,
                    })

    # ── Internal Button Event Builder ─────────────────────────────────────────

    def _push_button_event(self, raw_code: str, value: int):
        """
        Convert a raw hardware button code + value into an event.

        Press  (value=1) → add to pending pool for combo detection.
        Release(value=0) → emit "button_release" immediately (no combo logic).
        """
        logical_name = BUTTON_MAP.get(raw_code)
        if logical_name is None:
            log.debug(f"Unmapped button code: {raw_code} (value={value})")
            return

        pressed = bool(value)
        was_pressed = self._button_state.get(logical_name, False)

        if pressed and not was_pressed:
            self._button_state[logical_name] = True
            with self._combo_lock:
                # If any button is already held, group immediately into a combo
                # rather than waiting for the combo window. This ensures
                # "hold L2, then press B" always fires as L2+B, never as lone B.
                held = [k for k, v in self._button_state.items()
                        if v and k != logical_name]
                if held:
                    combo = frozenset(held + [logical_name])
                    log.debug(f"Immediate combo from held buttons: {sorted(combo)}")
                    self._event_queue.put({
                        "type":  "combo_press",
                        "combo": combo,
                        "value": 1.0,
                        "ts":    time.monotonic(),
                    })
                    # Clear any pending presses so they don't also emit
                    self._pending_presses.clear()
                else:
                    self._pending_presses[logical_name] = time.monotonic()
            log.debug(f"Button queued for combo check: {logical_name}")

        elif not pressed and was_pressed:
            self._button_state[logical_name] = False
            self._event_queue.put({
                "type":   "button_release",
                "button": logical_name,
                "value":  0.0,
                "ts":     time.monotonic(),
            })
            log.debug(f"Button release: {logical_name}")

    # ── Reader Loop ───────────────────────────────────────────────────────────

    def _reader_loop(self):
        if BACKEND == "inputs":
            self._loop_inputs_backend()
        elif BACKEND == "unitree":
            self._loop_unitree_backend()
        elif BACKEND == "simulated":
            self._loop_simulated_backend()
        else:
            log.error(f"Unknown backend: {BACKEND}")

    # ── Backend A: `inputs` library (USB/BT gamepad) ─────────────────────────

    def _loop_inputs_backend(self):
        """Uses the `inputs` PyPI package. Works with most evdev gamepads on Linux."""
        try:
            import inputs as inp
        except ImportError:
            log.error(
                "`inputs` library not found. Run: pip install inputs\n"
                "Falling back to simulated backend."
            )
            self._loop_simulated_backend()
            return

        log.info("inputs backend: waiting for gamepad device...")
        while not self._stop_event.is_set():
            try:
                events = inp.get_gamepad()
                for event in events:
                    if event.ev_type == "Key":
                        self._push_button_event(event.code, event.state)
                    elif event.ev_type == "Absolute":
                        self._push_axis_event(event.code, event.state)
            except inp.UnpluggedError:
                log.warning("Gamepad unplugged — retrying in 2 s...")
                time.sleep(2.0)
            except Exception as exc:
                log.exception(f"inputs backend error: {exc}")
                time.sleep(0.5)

    def _push_axis_event(self, raw_code: str, raw_value: int):
        """Convert raw axis value to a normalised [-1.0, 1.0] axis event."""
        AXIS_MAP = {
            "ABS_X": "LX", "ABS_Y": "LY",
            "ABS_RX":"RX", "ABS_RY":"RY",
            "ABS_Z": "LT", "ABS_RZ":"RT",
        }
        MAX_AXIS = 32767.0
        logical = AXIS_MAP.get(raw_code)
        if logical is None:
            return
        normalised = max(-1.0, min(1.0, raw_value / MAX_AXIS))
        self._event_queue.put({
            "type":  "axis_move",
            "axis":  logical,
            "value": normalised,
            "ts":    time.monotonic(),
        })

    # ── Backend B: Unitree SDK placeholder ───────────────────────────────────

    def _loop_unitree_backend(self):
        """
        Reads the wireless controller state from the Unitree G1 DDS topic
        rt/lf/lowstate and emits button press/release events.

        The controller sends a 40-byte wireless_remote payload inside every
        LowState_ message.  Bytes 2 and 3 are bitmasks for all buttons:

          Byte 2 (shoulder / menu):
            bit 0 = RB,  bit 1 = LB,  bit 2 = Start,  bit 3 = Back

          Byte 3 (face / D-pad):
            bit 0 = A,   bit 1 = B,    bit 2 = X,    bit 3 = Y
            bit 4 = Up,  bit 5 = Right, bit 6 = Down, bit 7 = Left

        State is polled at 50 Hz; edge detection (prev vs current) converts
        level values into the press/release events the rest of the system
        expects.  Requires unitree_sdk2py and UNITREE_NETWORK_INTERFACE.
        """
        try:
            from unitree_sdk2py.core.channel import ChannelSubscriber, ChannelFactoryInitialize
            from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowState_
            from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
        except ImportError as exc:
            log.error(
                f"unitree_sdk2py import failed: {exc}\n"
                "Falling back to simulated backend."
            )
            self._loop_simulated_backend()
            return

        log.info(f"Unitree backend: initialising DDS on interface '{UNITREE_NETWORK_INTERFACE}'")
        ChannelFactoryInitialize(0, UNITREE_NETWORK_INTERFACE)

        # DDS callback deposits the latest snapshot here; poll thread reads it.
        # Each snapshot is (wireless_remote_bytes, mode_machine_int).
        _latest: list = [None]
        _cb_lock = threading.Lock()

        def _handler(msg: LowState_):
            with _cb_lock:
                _latest[0] = (bytes(msg.wireless_remote), int(msg.mode_machine))

        sub = ChannelSubscriber("rt/lf/lowstate", LowState_)
        sub.Init(_handler, 10)
        log.info("Unitree backend: subscribed to rt/lf/lowstate — waiting for controller data...")

        def _parse(remote: bytes) -> dict:
            """Extract {logical_name: 0|1} from the 40-byte wireless_remote."""
            b2, b3 = remote[2], remote[3]
            return {
                # byte 2: shoulder / menu / trigger buttons
                "RB":    (b2 >> 0) & 1,
                "LB":    (b2 >> 1) & 1,
                "Start": (b2 >> 2) & 1,
                "Back":  (b2 >> 3) & 1,
                "R2":    (b2 >> 4) & 1,
                "L2":    (b2 >> 5) & 1,
                "F1":    (b2 >> 6) & 1,
                "F2":    (b2 >> 7) & 1,
                # byte 3: face buttons / D-pad
                "A":     (b3 >> 0) & 1,
                "B":     (b3 >> 1) & 1,
                "X":     (b3 >> 2) & 1,
                "Y":     (b3 >> 3) & 1,
                "Up":    (b3 >> 4) & 1,
                "Right": (b3 >> 5) & 1,
                "Down":  (b3 >> 6) & 1,
                "Left":  (b3 >> 7) & 1,
            }

        prev: dict = {}
        _last_mode: list[int] = [-1]   # track mode changes for logging
        _baseline_set: list[bool] = [False]  # True once we've silently consumed the first allowed cycle

        while not self._stop_event.is_set():
            time.sleep(0.02)   # 50 Hz

            with _cb_lock:
                snapshot = _latest[0]
            if snapshot is None:
                continue

            remote, mode = snapshot

            # Log mode transitions so the user can verify mode values.
            if mode != _last_mode[0]:
                allowed = mode in ALLOWED_ROBOT_MODES
                log.info(
                    f"Robot mode changed: {mode} "
                    f"({'controller ACTIVE' if allowed else 'controller SUPPRESSED'})"
                )
                _last_mode[0] = mode

            # Only forward button events when the robot is in an allowed mode.
            if mode not in ALLOWED_ROBOT_MODES:
                # Reset baseline flag so we re-snapshot on next allowed entry.
                _baseline_set[0] = False
                prev = {}
                continue

            current = _parse(remote)

            # First cycle after entering an allowed mode: silently snapshot the
            # current button state as a baseline.  Buttons held during the mode-
            # switch (e.g. the button used to activate this mode) must not be
            # treated as fresh presses.
            if not _baseline_set[0]:
                prev = current
                _baseline_set[0] = True
                continue

            for btn, val in current.items():
                if val != prev.get(btn, 0):
                    self._push_button_event(btn, val)
            prev = current

    # ── Backend C: Simulated (for testing) ───────────────────────────────────

    def _loop_simulated_backend(self):
        """
        Simulates:
          - A single Button-A press every 10 seconds (triggers LED script)
          - An A+B combo press on the 3rd cycle (triggers graceful exit)
        """
        log.info("Simulated backend active")
        cycle = 0
        while not self._stop_event.is_set():
            time.sleep(10.0)
            if self._stop_event.is_set():
                break

            cycle += 1
            if cycle % 3 == 0:
                log.debug("SIM: injecting A+B combo")
                self._push_button_event("BTN_SOUTH", 1)   # A press
                time.sleep(0.05)                           # 50 ms — within combo window
                self._push_button_event("BTN_EAST",  1)   # B press
                time.sleep(0.1)
                self._push_button_event("BTN_SOUTH", 0)
                self._push_button_event("BTN_EAST",  0)
            else:
                log.debug("SIM: injecting single A press")
                self._push_button_event("BTN_SOUTH", 1)
                time.sleep(0.1)
                self._push_button_event("BTN_SOUTH", 0)
