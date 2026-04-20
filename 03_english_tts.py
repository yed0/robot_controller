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

    client.SetVolume(100)
    print("Volume set to 100")

    # speaker_id: 0 = Chinese, 1 = English
    client.TtsMaker("Hello, this is Unitree G1 course", 1)
    print("Speaking...")
    time.sleep(5)
    print("Done!")

