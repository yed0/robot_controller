import sys
import time
import subprocess
import os
from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

def play_pcm_stream(client, pcm_bytes, app_name="my_app", chunk_size=96000):
    stream_id = str(int(time.time() * 1000))
    offset = 0
    chunk_index = 0
    # 16000 samples/sec * 2 bytes/sample = 32000 bytes/sec
    sleep_time = chunk_size / 32000

    while offset < len(pcm_bytes):
        chunk = pcm_bytes[offset:offset + chunk_size]
        ret_code, _ = client.PlayStream(app_name, stream_id, chunk)
        if ret_code != 0:
            print(f"[ERROR] Chunk {chunk_index} failed, code: {ret_code}")
            break
        print(f"[INFO] Chunk {chunk_index} sent ({len(chunk)} bytes)")
        offset += chunk_size
        chunk_index += 1
        time.sleep(sleep_time)

    # Wait for the last chunk to finish
    last_chunk_size = len(pcm_bytes) - (offset - chunk_size)
    time.sleep(last_chunk_size / 32000)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(f"Usage: python3 {sys.argv[0]} networkInterface audio.wav")
        sys.exit(-1)

    wav_file = sys.argv[2]
    if not os.path.exists(wav_file):
        print(f"File not found: {wav_file}")
        sys.exit(-1)

    ChannelFactoryInitialize(0, sys.argv[1])
    client = AudioClient()
    client.SetTimeout(10.0)
    client.Init()
    client.SetVolume(100)

    # Convert to correct format (16K, mono, 16-bit)
    converted = "converted_temp.wav"
    print(f"Converting {wav_file}...")
    subprocess.run([
        "ffmpeg", "-y", "-i", wav_file,
        "-ar", "16000", "-ac", "1", "-sample_fmt", "s16",
        converted
    ], check=True, capture_output=True)

    # Read raw PCM (skip 44-byte WAV header)
    with open(converted, "rb") as f:
        f.read(44)
        pcm_bytes = f.read()

    print(f"PCM size: {len(pcm_bytes)} bytes, duration: {len(pcm_bytes)/32000:.1f}s")
    print("Playing...")
    play_pcm_stream(client, pcm_bytes)
    client.PlayStop("my_app")

    os.remove(converted)
    print("Done!")