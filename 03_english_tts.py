import struct
import sys
import time
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

SAMPLE_RATE = 16000

def _prime_speaker(client: AudioClient) -> None:
    """Send a short silent PCM burst via PlayStream to open the speaker pipeline.
    Without this, TtsMaker is silently dropped on a cold boot."""
    silence = struct.pack(f"{SAMPLE_RATE // 10}h", *([0] * (SAMPLE_RATE // 10)))  # 100 ms
    stream_id = str(int(time.time() * 1000))
    client.PlayStream("tts_primer", stream_id, silence)
    time.sleep(0.15)
    client.PlayStop("tts_primer")
    time.sleep(0.1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python3 {sys.argv[0]} networkInterface")
        sys.exit(-1)

    ChannelFactoryInitialize(0, sys.argv[1])
    client = AudioClient()
    client.SetTimeout(10.0)
    client.Init()
    time.sleep(1.0)  # let DDS discover the audio service before first RPC

    client.SetVolume(100)
    time.sleep(0.2)  # let SetVolume apply server-side
    print("Volume set to 100")

    _prime_speaker(client)  # open the speaker pipeline so TtsMaker is not silently dropped

    # speaker_id: 0 = Chinese, 1 = English
    client.TtsMaker("Hello, this is Unitree G1 course", 1)
    print("Speaking...")
    time.sleep(5)
    print("Done!")
