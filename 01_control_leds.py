import sys
import time
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} networkInterface")
        sys.exit(-1)

    ChannelFactoryInitialize(0, sys.argv[1])
    client = AudioClient()
    client.SetTimeout(10.0)
    client.Init()

    # Cycle through colors
    colors = [
        ("Red",     255, 0,   0),
        ("Green",   0,   255, 0),
        ("Blue",    0,   0,   255),
        ("Yellow",  255, 255, 0),
        ("Magenta", 255, 0,   255),
        ("Cyan",    0,   255, 255),
        ("White",   255, 255, 255),
        ("Off",     0,   0,   0),
    ]

    for name, r, g, b in colors:
        print(f"LED: {name} ({r}, {g}, {b})")
        client.LedControl(r, g, b)
        time.sleep(0.5)  # must be > 200ms between calls

    print("Done!")
