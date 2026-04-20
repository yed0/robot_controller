#!/usr/bin/env python3
"""
g1_control_arm.py – Simple right-arm control for Unitree G-1.

Demonstrates low-level arm joint control via the rt/arm_sdk topic.
The robot should be in DAMP mode (hanging, not standing) for safety.

What this script does:
  1. Reads current joint positions from rt/lowstate
  2. Smoothly ramps right arm to the "initial" position (3 seconds)
  3. Oscillates the right elbow back and forth (6 seconds)
  4. Smoothly releases arm back to rest (3 seconds)

IMPORTANT — why we use rt/arm_sdk and not LocoClient:
  LocoClient controls LEGS (walking, standing, sitting).
  Arms have no high-level API — we control them by publishing
  raw joint commands (position + PD gains) to the rt/arm_sdk topic.

IMPORTANT — why we also command joints we're NOT moving:
  The rt/arm_sdk topic controls ALL upper body joints (12-28).
  If we only send commands for the right arm (22-28), the left arm
  and waist (12-21) receive default/zero values and will GLITCH.
  So we HOLD those joints at their current position every cycle.

Joint layout (29 motors total):
   0-5   : Right leg
   6-11  : Left leg
  12-14  : Waist (yaw, roll, pitch)         ← arm_sdk controls these
  15-21  : Left arm (shoulder×3, elbow×2, wrist×2)  ← arm_sdk controls these
  22-28  : Right arm (shoulder×3, elbow×2, wrist×2) ← arm_sdk controls these
  29     : Special "weight" — blends between built-in control and our commands
           0.0 = robot's own control,  1.0 = fully our commands

Usage:
    python g1_control_arm.py eno1
"""

from __future__ import annotations

import argparse
import math
import signal
import sys
import time

import numpy as np

from unitree_sdk2py.core.channel import (
    ChannelFactoryInitialize,
    ChannelPublisher,
    ChannelSubscriber,
)
from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowCmd_
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowCmd_, LowState_
from unitree_sdk2py.utils.crc import CRC
from unitree_sdk2py.utils.thread import RecurrentThread


# ╔════════════════════════════════════════════════════════════════════════════╗
# ║ CONFIGURATION                                                            ║
# ╚════════════════════════════════════════════════════════════════════════════╝

NUM_MOTORS = 29
CONTROL_DT = 0.002           # 500 Hz control loop — must match robot expectation

RAMP_DURATION      = 3.0     # seconds to smoothly reach initial pose
OSCILLATE_DURATION = 3.0     # seconds of elbow oscillation
OSCILLATE_PERIOD   = 1.0     # seconds per full swing cycle
OSCILLATE_AMPLITUDE = 0.5    # radians of elbow swing

# ── Joint indices ────────────────────────────────────────────────────────────
WAIST_INDICES     = [12, 13, 14]
LEFT_ARM_INDICES  = [15, 16, 17, 18, 19, 20, 21]
RIGHT_ARM_INDICES = [22, 23, 24, 25, 26, 27, 28]
ALL_UPPER_INDICES = WAIST_INDICES + LEFT_ARM_INDICES + RIGHT_ARM_INDICES

RIGHT_ELBOW_IDX = 25         # the joint we will oscillate
WEIGHT_IDX      = 29         # arm control weight (motor_cmd[29].q)

# ── PD gains per joint (tuned for G1 upper body) ────────────────────────────
#    Higher Kp = stiffer position tracking, Kd = damping
Kp = np.array([
    100, 100, 100, 150, 40, 40,       # right leg (not used, but array must be full)
    100, 100, 100, 150, 40, 40,       # left leg
    200,  40,  40,                     # waist
     40,  40,  40,  40, 40, 40, 40,   # left arm
     40,  40,  40,  40, 40, 40, 40,   # right arm
], dtype=float)

Kd = np.array([
    2, 2, 2, 4, 2, 2,                 # right leg
    2, 2, 2, 4, 2, 2,                 # left leg
    5, 5, 5,                           # waist
    1, 1, 1, 1, 1, 1, 1,              # left arm
    1, 1, 1, 1, 1, 1, 1,              # right arm
], dtype=float)

# Target pose for right arm — all zeros = the "default" straight-ish pose
TARGET_RIGHT_ARM = np.zeros(NUM_MOTORS)

g_running = True


def signal_handler(sig, frame):
    global g_running
    g_running = False


# ╔════════════════════════════════════════════════════════════════════════════╗
# ║ ARM CONTROLLER                                                           ║
# ╚════════════════════════════════════════════════════════════════════════════╝

class ArmController:
    """
    Simple right-arm controller that publishes to rt/arm_sdk at 500 Hz.

    Phases:
      idle      → do nothing
      ramp      → smoothly blend from current position to target (cosine ease)
      hold      → maintain target position
      oscillate → sinusoidal elbow pumping
      release   → smoothly blend back and reduce weight to 0
    """

    def __init__(self):
        self.low_cmd = unitree_hg_msg_dds__LowCmd_()
        self.low_state = None
        self.crc = CRC()

        # mode_machine must match the robot's current mode
        self.mode_machine = 0
        self.mode_received = False

        self.time = 0.0
        self.phase = "idle"
        self.phase_start = 0.0

        self.initial_q = None        # snapshot of joint positions at start
        self.elbow_rest = 0.0        # elbow target in the "hold" pose
        self._thread = None

    # ── Initialise publisher / subscriber ────────────────────────────────
    def init(self):
        self.pub = ChannelPublisher("rt/arm_sdk", LowCmd_)
        self.pub.Init()

        self.sub = ChannelSubscriber("rt/lowstate", LowState_)
        self.sub.Init(self._on_state, 1)

        time.sleep(1)  # give subscriber time to receive first message

    def _on_state(self, msg: LowState_):
        """Called every time we receive a state update from the robot."""
        self.low_state = msg
        if not self.mode_received:
            self.mode_machine = msg.mode_machine
            self.mode_received = True

    def wait_ready(self):
        """Block until we have state from the robot."""
        print("[Arm] Waiting for robot state...")
        while self.low_state is None:
            time.sleep(0.1)
        while not self.mode_received:
            time.sleep(0.1)
        print(f"[Arm] Connected. mode_machine = {self.mode_machine}")

    # ── Phase transitions ────────────────────────────────────────────────
    def start_ramp(self):
        """Begin smooth ramp from current position to target."""
        # Snapshot every joint's current position
        self.initial_q = np.array([
            self.low_state.motor_state[i].q for i in range(NUM_MOTORS)
        ])
        self.elbow_rest = TARGET_RIGHT_ARM[RIGHT_ELBOW_IDX]
        self.time = 0.0
        self.phase = "ramp"
        self.phase_start = 0.0

        # Start the 500 Hz control thread (runs self._control_step)
        if self._thread is None:
            self._thread = RecurrentThread(
                interval=CONTROL_DT, target=self._control_step, name="arm_ctrl"
            )
            self._thread.Start()

    def start_oscillate(self):
        """Begin sinusoidal elbow oscillation."""
        self.phase = "oscillate"
        self.phase_start = self.time

    def start_release(self):
        """Begin smooth release back to rest."""
        # Snapshot current positions for blending
        self.release_q = {}
        for i in ALL_UPPER_INDICES:
            self.release_q[i] = self.low_state.motor_state[i].q
        self.phase = "release"
        self.phase_start = self.time

    def stop(self):
        """Give control back to the robot."""
        self.phase = "idle"
        self.low_cmd.motor_cmd[WEIGHT_IDX].q = 0.0
        self.low_cmd.mode_pr = 0
        self.low_cmd.mode_machine = self.mode_machine
        self.low_cmd.crc = self.crc.Crc(self.low_cmd)
        self.pub.Write(self.low_cmd)

    # ── 500 Hz control loop ──────────────────────────────────────────────
    def _control_step(self):
        """Called every 2 ms by RecurrentThread. This is where the magic happens."""
        if self.low_state is None:
            return

        self.time += CONTROL_DT
        pt = self.time - self.phase_start  # seconds since current phase started

        # ── Compute right arm target positions ───────────────────────────
        right_target = {i: TARGET_RIGHT_ARM[i] for i in RIGHT_ARM_INDICES}
        weight = 0.0

        if self.phase == "ramp":
            # Cosine ease-in-out: r goes 0→1 over RAMP_DURATION
            r = np.clip(pt / RAMP_DURATION, 0, 1)
            r = 0.5 * (1 - math.cos(math.pi * r))   # smooth S-curve

            for i in RIGHT_ARM_INDICES:
                right_target[i] = (1 - r) * self.initial_q[i] + r * TARGET_RIGHT_ARM[i]
            weight = r  # gradually take over control

            if pt >= RAMP_DURATION:
                self.phase = "hold"
                self.phase_start = self.time

        elif self.phase == "hold":
            weight = 1.0

        elif self.phase == "oscillate":
            weight = 1.0
            if pt < OSCILLATE_DURATION:
                # sin() produces smooth back-and-forth motion
                offset = -OSCILLATE_AMPLITUDE * math.sin(
                    2 * math.pi * pt / OSCILLATE_PERIOD
                )
                right_target[RIGHT_ELBOW_IDX] = self.elbow_rest + offset
            else:
                self.phase = "hold"
                self.phase_start = self.time

        elif self.phase == "release":
            # Cosine ease back, reducing weight to 0
            r = np.clip(pt / RAMP_DURATION, 0, 1)
            r = 0.5 * (1 - math.cos(math.pi * r))
            weight = 1.0 - r

            for i in RIGHT_ARM_INDICES:
                start = self.release_q[i]
                current = self.low_state.motor_state[i].q
                right_target[i] = (1 - r) * start + r * current

            if pt >= RAMP_DURATION:
                self.phase = "done"
                weight = 0.0

        elif self.phase in ("idle", "done"):
            return

        # ── Write commands to ALL upper body joints ──────────────────────

        # WAIST — hold at current position (don't move it)
        for i in WAIST_INDICES:
            self.low_cmd.motor_cmd[i].mode = 1
            self.low_cmd.motor_cmd[i].q   = float(self.low_state.motor_state[i].q)
            self.low_cmd.motor_cmd[i].dq  = 0.0
            self.low_cmd.motor_cmd[i].kp  = float(Kp[i])
            self.low_cmd.motor_cmd[i].kd  = float(Kd[i])
            self.low_cmd.motor_cmd[i].tau = 0.0

        # LEFT ARM — hold at current position (prevents glitch)
        for i in LEFT_ARM_INDICES:
            self.low_cmd.motor_cmd[i].mode = 1
            self.low_cmd.motor_cmd[i].q   = float(self.low_state.motor_state[i].q)
            self.low_cmd.motor_cmd[i].dq  = 0.0
            self.low_cmd.motor_cmd[i].kp  = float(Kp[i])
            self.low_cmd.motor_cmd[i].kd  = float(Kd[i])
            self.low_cmd.motor_cmd[i].tau = 0.0

        # RIGHT ARM — our animated targets
        for i in RIGHT_ARM_INDICES:
            self.low_cmd.motor_cmd[i].mode = 1
            self.low_cmd.motor_cmd[i].q   = float(right_target[i])
            self.low_cmd.motor_cmd[i].dq  = 0.0
            self.low_cmd.motor_cmd[i].kp  = float(Kp[i])
            self.low_cmd.motor_cmd[i].kd  = float(Kd[i])
            self.low_cmd.motor_cmd[i].tau = 0.0

        # WEIGHT — blend between robot's own control and ours
        self.low_cmd.motor_cmd[WEIGHT_IDX].q = float(weight)

        # CRC checksum + publish
        self.low_cmd.mode_pr = 0
        self.low_cmd.mode_machine = self.mode_machine
        self.low_cmd.crc = self.crc.Crc(self.low_cmd)
        self.pub.Write(self.low_cmd)


# ╔════════════════════════════════════════════════════════════════════════════╗
# ║ MAIN                                                                     ║
# ╚════════════════════════════════════════════════════════════════════════════╝

def main():
    parser = argparse.ArgumentParser(description="G1 simple arm control demo")
    parser.add_argument("iface", nargs="?", default="", help="Network interface (e.g. eno1); omit for simulation")
    parser.add_argument("--sim", action="store_true", help="Use MuJoCo simulation (domain_id=1)")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # ── Initialise SDK ───────────────────────────────────────────────────
    if args.sim or args.iface == "":
        ChannelFactoryInitialize(1)
    else:
        ChannelFactoryInitialize(0, args.iface)

    arm = ArmController()
    arm.init()
    arm.wait_ready()

    # ── Phase 1: Ramp to initial position ────────────────────────────────
    print(f"\n[Demo] Ramping arm to initial position ({RAMP_DURATION}s)...")
    arm.start_ramp()
    time.sleep(RAMP_DURATION + 0.5)
    print("[Demo] Arm in position.")

    print("[Demo] Holding position for 3 seconds...")
    time.sleep(3.0)

    # ── Phase 2: Oscillate elbow ─────────────────────────────────────────
    print(f"[Demo] Oscillating elbow ({OSCILLATE_DURATION}s)...")
    arm.start_oscillate()
    time.sleep(OSCILLATE_DURATION + 0.5)
    print("[Demo] Oscillation complete. Releasing arm in 3 seconds...")

    time.sleep(3.0)

    # ── Phase 3: Release ─────────────────────────────────────────────────
    print(f"[Demo] Releasing arm ({RAMP_DURATION}s)...")
    arm.start_release()
    time.sleep(RAMP_DURATION + 0.5)

    # ── Cleanup ──────────────────────────────────────────────────────────
    arm.stop()
    print("[Demo] Done. Goodbye.")


if __name__ == "__main__":
    main()