#!/usr/bin/env python3
"""
main.py — Unitree G1 EDU Robot Startup Controller

Entry point.  Initializes all subsystems and runs the main control loop.
Designed to be launched via systemd on robot boot.

Behaviour summary:
  • Button A         → runs 01_control_leds.py (non-blocking, in a thread)
  • Button B         → runs 02_arm_control.py (non-blocking, in a thread)
  • Button X         → runs 03_english_tts.py (non-blocking, in a thread)
  • Button Y         → plays example.wav via 02_playback_wav.py
  • A + B (combo)    → gracefully exits this controller and launches the
                       Unitree default joystick program
  • Any unregistered combo (e.g. L2+B) → silently ignored, single-button
                       action does NOT fire
"""

import signal
import sys
import time
import logging
from input_handler import GamepadHandler
from behavior_manager import BehaviorManager

# ─── Logging Setup ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        # Uncomment to also log to file:
        # logging.FileHandler("/var/log/unitree_controller.log"),
    ],
)
log = logging.getLogger("main")


# ─── Graceful Shutdown ────────────────────────────────────────────────────────
# _running is read by the main loop every iteration.
# Setting it to False from any thread (signal handler or A+B callback) will
# cause the loop to exit after the current iteration finishes.
_running = True

def _set_not_running():
    """Zero-argument callable passed to BehaviorManager as exit_callback."""
    global _running
    log.info("Exit callback invoked — stopping main loop")
    _running = False

def _shutdown_handler(sig, frame):
    """Handles SIGINT (Ctrl-C) and SIGTERM (systemd stop)."""
    global _running
    log.info(f"Received signal {sig} — initiating graceful shutdown")
    _running = False

signal.signal(signal.SIGINT,  _shutdown_handler)
signal.signal(signal.SIGTERM, _shutdown_handler)


# ─── Main Loop ────────────────────────────────────────────────────────────────
def main():
    log.info("=== Unitree G1 EDU Controller Starting ===")

    # ── Subsystem initialisation ─────────────────────────────────────────────
    gamepad  = GamepadHandler()

    # Pass _set_not_running so the A+B action can stop the loop without
    # knowing anything about the loop itself.
    behavior = BehaviorManager(exit_callback=_set_not_running)

    # ── Button bindings ──────────────────────────────────────────────────────
    #
    # Single buttons:
    behavior.register_button("Start", behavior.action_button_start, cooldown=0.5)
    behavior.register_button("A", behavior.action_button_a, cooldown=1.0)
    #   cooldown=1.0 s prevents accidental double-fires of the LED script.
    behavior.register_button("B", behavior.action_button_b, cooldown=1.0)
    behavior.register_button("X", behavior.action_button_x, cooldown=2.0)
    behavior.register_button("Y", behavior.action_button_y, cooldown=2.0)
    #   cooldown=2.0 s — TTS takes ~5 s to speak, prevents re-trigger mid-speech.
    #
    # Combos  (frozenset because button order doesn't matter):
    behavior.register_combo(frozenset({"A", "B"}), behavior.action_combo_ab, cooldown=2.0)
    #   cooldown=2.0 s gives the exit action time to complete before any
    #   re-trigger could occur (though in practice the process will be
    #   shutting down anyway).
    #
    # ── Add more bindings here ───────────────────────────────────────────────
    # behavior.register_button("X", behavior.action_button_x)
    # behavior.register_combo(frozenset({"LB", "RB"}), behavior.action_combo_bumpers)

    gamepad.start()
    log.info("Gamepad listener started. Waiting for input...")

    LOOP_HZ = 100           # target loop frequency in Hz
    LOOP_DT = 1.0 / LOOP_HZ

    try:
        while _running:
            t0 = time.monotonic()

            # ── Poll gamepad events (non-blocking) ───────────────────────
            events = gamepad.get_events()

            for event in events:
                etype = event["type"]

                if etype == "button_press":
                    # Single button was pressed (not part of a combo)
                    behavior.handle_button(event["button"])

                elif etype == "combo_press":
                    # Multiple buttons pressed simultaneously
                    behavior.handle_combo(event["combo"])

                # button_release and axis_move events are available here too
                # if you need them for future features (e.g., axis-driven motion).

            # ── Place additional per-loop robot logic here ────────────────
            # e.g., safety watchdog, telemetry, state estimation

            # ── Rate limiter — sleep remaining time to hit LOOP_HZ ───────
            elapsed = time.monotonic() - t0
            sleep_time = LOOP_DT - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)

    finally:
        log.info("Stopping subsystems...")
        gamepad.stop()
        behavior.shutdown()
        log.info("=== Controller shut down cleanly ===")


if __name__ == "__main__":
    main()
