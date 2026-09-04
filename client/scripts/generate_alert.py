"""Generate a simple alert beep wav file."""
import struct
import wave
import math

SAMPLE_RATE = 44100
DURATION = 0.3  # seconds
FREQ = 880      # Hz (A5 note)

samples = []
for i in range(int(SAMPLE_RATE * DURATION)):
    t = i / SAMPLE_RATE
    # Simple sine wave with envelope
    envelope = 1.0 - (i / (SAMPLE_RATE * DURATION))
    value = int(16000 * math.sin(2 * math.pi * FREQ * t) * envelope)
    samples.append(struct.pack("<h", value))

with wave.open("resources/alert.wav", "w") as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(SAMPLE_RATE)
    wf.writeframes(b"".join(samples))

print("Generated resources/alert.wav")
