#!/usr/bin/env python3
"""
debug_buttons.py — Print raw wireless_remote bytes whenever button state changes.

Run:  python3.10 debug_buttons.py
Then press buttons on the controller and watch the output.

Each line shows:
  byte2=0bXXXXXXXX  byte3=0bXXXXXXXX  (changed bits highlighted)
"""

import sys
import time
import threading
from unitree_sdk2py.core.channel import ChannelSubscriber, ChannelFactoryInitialize
from unitree_sdk2py.idl.default import unitree_hg_msg_dds__LowState_
from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_

NETWORK_INTERFACE = "enP8p1s0"

_latest = [None]
_lock = threading.Lock()

def _handler(msg: LowState_):
    with _lock:
        _latest[0] = bytes(msg.wireless_remote)

ChannelFactoryInitialize(0, NETWORK_INTERFACE)
sub = ChannelSubscriber("rt/lf/lowstate", LowState_)
sub.Init(_handler, 10)

print("Listening... press buttons on the controller (Ctrl-C to stop)\n")
print(f"{'byte2 (shoulder/triggers)':30s}  {'byte3 (face/dpad)':30s}  changed bits")
print("-" * 90)

prev_b2, prev_b3 = 0, 0

while True:
    time.sleep(0.05)
    with _lock:
        snap = _latest[0]
    if snap is None:
        continue

    b2, b3 = snap[2], snap[3]
    if b2 == prev_b2 and b3 == prev_b3:
        continue

    diff2 = b2 ^ prev_b2
    diff3 = b3 ^ prev_b3

    changed = []
    for bit in range(8):
        if (diff2 >> bit) & 1:
            state = "ON" if (b2 >> bit) & 1 else "off"
            changed.append(f"byte2[bit{bit}]={state}")
        if (diff3 >> bit) & 1:
            state = "ON" if (b3 >> bit) & 1 else "off"
            changed.append(f"byte3[bit{bit}]={state}")

    print(f"byte2={bin(b2):>10s}  byte3={bin(b3):>10s}  {', '.join(changed)}")

    prev_b2, prev_b3 = b2, b3
