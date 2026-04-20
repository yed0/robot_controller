#!/usr/bin/env python3
"""
behavior_manager.py — Action / Behavior Manager

Owns the mapping of logical button names and combos → callable behaviors.
Executes behaviors in background threads so the main loop is never blocked.

Key behaviors in this file:
  • action_button_a()     — runs 01_control_leds.py
  • action_combo_ab()     — graceful exit back to Unitree default joystick control
"""

import logging
import os
import subprocess
import sys
import threading
import time
from typing import Callable, FrozenSet, Optional

log = logging.getLogger("behavior_manager")

# ─── Path to the LED control script ──────────────────────────────────────────
# Adjust this path to wherever 01_control_leds.py lives on the robot.
# If it is in the same directory as main.py, the default below is correct.
LED_SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "01_control_leds.py")

# ─── Path to the arm control script ──────────────────────────────────────────
ARM_SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "02_arm_control.py")

# ─── Path to the TTS script ───────────────────────────────────────────────────
TTS_SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "03_english_tts.py")

# ─── Network interface for the Unitree SDK ────────────────────────────────────
# 01_control_leds.py requires a network interface argument (e.g. "eth0").
# Set this to the correct interface name on your robot.
UNITREE_NETWORK_INTERFACE = "enP8p1s0"

# ─── Path / command to launch the Unitree default joystick program ────────────
# This is executed when the A+B exit combo is pressed.
# Set to "" to simply exit without launching a handoff program (the robot's
# onboard firmware resumes native control automatically when this exits).
# Replace with the actual command on your robot if a handoff binary exists, e.g.:
#   /home/unitree/unitree_sdk/joystick_control
DEFAULT_JOYSTICK_COMMAND = ""

class BehaviorManager:
    """
    Maintains a registry of button/combo → behavior callables and
    dispatches them on-demand in daemon threads (non-blocking).

    Usage in main.py:
        bm = BehaviorManager(exit_callback=set_running_false)
        bm.register_button("A",  bm.action_button_a)
        bm.register_combo(frozenset({"A","B"}), bm.action_combo_ab)
        bm.handle_button("A")
        bm.handle_combo(frozenset({"A","B"}))
    """

    def __init__(self, exit_callback: Optional[Callable] = None):
        """
        Args:
            exit_callback: Zero-argument callable invoked when the A+B combo
                           fires.  main.py passes a lambda that sets _running=False
                           so the main loop exits cleanly.
        """
        self._exit_callback = exit_callback

        # Single-button registry: { "A": callable, ... }
        self._button_registry: dict[str, Callable] = {}
        # Combo registry: { frozenset({"A","B"}): callable, ... }
        self._combo_registry: dict[FrozenSet[str], Callable] = {}

        self._cooldown_seconds: dict[str, float] = {}
        self._last_triggered:   dict[str, float] = {}

        self._active_threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    # ── Registry API ─────────────────────────────────────────────────────────

    def register_button(self, button: str, action: Callable, cooldown: float = 0.5):
        """
        Bind a logical button name (e.g. "A") to a callable.

        Args:
            button:   Logical name matching BUTTON_MAP in input_handler.py
            action:   Zero-argument callable run when the button is pressed
            cooldown: Minimum seconds between successive triggers (default 0.5 s)
        """
        self._button_registry[button] = action
        self._cooldown_seconds[button] = cooldown
        log.info(f"Registered button action [{button}] (cooldown={cooldown}s)")

    def register_combo(self, combo: FrozenSet[str], action: Callable, cooldown: float = 1.0):
        """
        Bind a button combo (e.g. frozenset({"A","B"})) to a callable.

        Args:
            combo:    Frozenset of logical button names pressed simultaneously
            action:   Zero-argument callable run when the combo fires
            cooldown: Minimum seconds between successive triggers (default 1.0 s)
        """
        key = self._combo_key(combo)
        self._combo_registry[combo] = action
        self._cooldown_seconds[key]  = cooldown
        log.info(f"Registered combo action {sorted(combo)} (cooldown={cooldown}s)")

    def handle_button(self, button: str):
        """
        Called by the main loop when a "button_press" event is received.
        Looks up the registered action and runs it in a background thread.
        """
        action = self._button_registry.get(button)
        if action is None:
            log.debug(f"No action registered for button [{button}]")
            return
        self._dispatch(button, action)

    def handle_combo(self, combo: FrozenSet[str]):
        """
        Called by the main loop when a "combo_press" event is received.
        Looks up the registered combo action and runs it in a background thread.
        """
        action = self._combo_registry.get(combo)
        if action is None:
            log.debug(f"No action registered for combo {sorted(combo)}")
            return
        key = self._combo_key(combo)
        self._dispatch(key, action)

    def shutdown(self):
        """Wait for all running action threads to complete (up to 5 s each)."""
        log.info("Waiting for running actions to finish...")
        with self._lock:
            threads = list(self._active_threads.values())
        for t in threads:
            t.join(timeout=5.0)
            if t.is_alive():
                log.warning(f"Thread {t.name} did not finish in time")
        log.info("BehaviorManager shut down")

    # ── Internal Dispatcher ───────────────────────────────────────────────────

    def _combo_key(self, combo: FrozenSet[str]) -> str:
        """Stable string key for a combo, used for cooldown tracking."""
        return "+".join(sorted(combo))

    def _dispatch(self, key: str, action: Callable):
        """
        Shared dispatch logic for both single buttons and combos.

        Checks:
          1. Cooldown — ignores rapid re-triggers.
          2. Already running — skips if a previous action for this key is live.
        Then starts a daemon thread to run the action.
        """
        now = time.monotonic()
        last = self._last_triggered.get(key, 0.0)
        cooldown = self._cooldown_seconds.get(key, 0.5)
        if now - last < cooldown:
            log.debug(f"[{key}] ignored (cooldown active)")
            return

        with self._lock:
            existing = self._active_threads.get(key)
            if existing and existing.is_alive():
                log.warning(f"[{key}] triggered but previous action still running — skipping")
                return

        self._last_triggered[key] = now

        t = threading.Thread(
            target=self._run_action,
            args=(key, action),
            name=f"action-{key}",
            daemon=True,
        )
        with self._lock:
            self._active_threads[key] = t
        t.start()
        log.info(f"Dispatched action for [{key}]")

    def _run_action(self, key: str, action: Callable):
        """Wrapper that catches exceptions so a bad action cannot kill the loop."""
        try:
            action()
        except Exception as exc:
            log.exception(f"Exception in action [{key}]: {exc}")

    # =========================================================================
    # ── BUILT-IN ACTIONS ─────────────────────────────────────────────────────
    # =========================================================================

    def action_button_a(self):
        """
        Button A — runs 01_control_leds.py as a child process.

        The script requires a network interface argument so the Unitree SDK
        can find the robot.  UNITREE_NETWORK_INTERFACE at the top of this
        file controls which interface is passed.

        This method runs in its OWN THREAD — it is safe to block here.
        The main loop continues normally while the LED script is executing.
        """
        log.info("Button A → launching LED control script")
        try:
            result = subprocess.run(
                [sys.executable, LED_SCRIPT_PATH, UNITREE_NETWORK_INTERFACE],
                capture_output=True,
                text=True,
                timeout=30.0,   # abort if the script hangs for 30 s
            )
            if result.returncode == 0:
                log.info(f"LED script finished OK\n{result.stdout.strip()}")
            else:
                log.error(
                    f"LED script exited with code {result.returncode}\n"
                    f"stderr: {result.stderr.strip()}"
                )
        except subprocess.TimeoutExpired:
            log.error("LED script timed out after 30 s")
        except FileNotFoundError:
            log.error(f"LED script not found: {LED_SCRIPT_PATH}")
        except Exception as exc:
            log.exception(f"Unexpected error running LED script: {exc}")

    def action_button_b(self):
        """Button B — runs 02_arm_control.py as a child process."""
        log.info("Button B → launching arm control script")
        try:
            result = subprocess.run(
                [sys.executable, ARM_SCRIPT_PATH, UNITREE_NETWORK_INTERFACE],
                capture_output=True,
                text=True,
                timeout=60.0,
            )
            if result.returncode == 0:
                log.info(f"Arm control script finished OK\n{result.stdout.strip()}")
            else:
                log.error(
                    f"Arm control script exited with code {result.returncode}\n"
                    f"stderr: {result.stderr.strip()}"
                )
        except subprocess.TimeoutExpired:
            log.error("Arm control script timed out after 60 s")
        except FileNotFoundError:
            log.error(f"Arm control script not found: {ARM_SCRIPT_PATH}")
        except Exception as exc:
            log.exception(f"Unexpected error running arm control script: {exc}")

    def action_button_x(self):
        """
        Button X — runs 03_english_tts.py as a child process.

        Speaks a TTS phrase over the robot's speaker.
        Runs in its own thread so the main loop is not blocked.
        """
        log.info("Button X → launching TTS script")
        try:
            result = subprocess.run(
                [sys.executable, TTS_SCRIPT_PATH, UNITREE_NETWORK_INTERFACE],
                capture_output=True,
                text=True,
                timeout=15.0,
            )
            if result.returncode == 0:
                log.info(f"TTS script finished OK\n{result.stdout.strip()}")
            else:
                log.error(
                    f"TTS script exited with code {result.returncode}\n"
                    f"stderr: {result.stderr.strip()}"
                )
        except subprocess.TimeoutExpired:
            log.error("TTS script timed out after 15 s")
        except FileNotFoundError:
            log.error(f"TTS script not found: {TTS_SCRIPT_PATH}")
        except Exception as exc:
            log.exception(f"Unexpected error running TTS script: {exc}")

    def action_combo_ab(self):
        """
        A + B combo — exit this controller and hand control back to the
        Unitree default joystick program.

        Steps:
          1. Log the intent.
          2. Signal the main loop to stop (via exit_callback).
          3. Launch DEFAULT_JOYSTICK_COMMAND as a detached process so it
             keeps running after this Python process exits.

        The detached launch uses os.setsid() to put the child in its own
        process group, ensuring it outlives the controller process.
        """
        log.info("A+B combo → exiting controller, handing off to default joystick")

        # ── Step 1: Tell the main loop to stop ───────────────────────────
        if self._exit_callback:
            self._exit_callback()
        else:
            log.warning("No exit_callback registered — main loop will not stop automatically")

        # ── Step 2: Launch the default joystick program (detached) ───────
        # os.setsid() creates a new session so the child is not killed when
        # this process exits.
        if not DEFAULT_JOYSTICK_COMMAND:
            log.info("No DEFAULT_JOYSTICK_COMMAND configured — exiting cleanly")
            return
        try:
            subprocess.Popen(
                [DEFAULT_JOYSTICK_COMMAND],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,   # detach from our process group
            )
            log.info(f"Launched detached: {DEFAULT_JOYSTICK_COMMAND}")
        except FileNotFoundError:
            log.error(
                f"Default joystick program not found: {DEFAULT_JOYSTICK_COMMAND}\n"
                "Update DEFAULT_JOYSTICK_COMMAND at the top of behavior_manager.py"
            )
        except Exception as exc:
            log.exception(f"Failed to launch default joystick program: {exc}")
