"""Sends whatever your Mac is playing (through BlackHole) to the ESP32 speaker.

Usage:  ./play.sh                  (finds the board as esp-speaker.local)
        ./play.sh 192.168.1.50     (or give the board's address)
"""
import queue
import socket
import sys
import time

import sounddevice as sd

HOST = sys.argv[1] if len(sys.argv) > 1 else "esp-speaker.local"
PORT = 3333
RATE = 48000  # must match SAMPLE_RATE in main/main.c


def find_blackhole():
    for i, dev in enumerate(sd.query_devices()):
        if "BlackHole" in dev["name"] and dev["max_input_channels"] >= 2:
            return i
    sys.exit("BlackHole not found. Install it with:  brew install blackhole-2ch  (then restart your Mac)")


def main():
    device = find_blackhole()
    # A short queue keeps the delay low: if the Wi-Fi falls behind, old audio is dropped
    chunks = queue.Queue(maxsize=20)

    def on_audio(indata, frames, t, status):
        try:
            chunks.put_nowait(bytes(indata))
        except queue.Full:
            pass

    with sd.RawInputStream(device=device, samplerate=RATE, channels=2, dtype="int16",
                           blocksize=1024, callback=on_audio):
        while True:
            try:
                print(f"Connecting to {HOST}:{PORT} ...")
                with socket.create_connection((HOST, PORT), timeout=5) as sock:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    while not chunks.empty():
                        chunks.get_nowait()
                    print("Connected! Play something on Spotify. Press Ctrl+C to stop.")
                    while True:
                        sock.sendall(chunks.get())
            except OSError as e:
                print(f"Can't reach the board ({e}). Is it on and on the same Wi-Fi? Retrying...")
                time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
