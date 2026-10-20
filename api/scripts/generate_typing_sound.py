"""Generate the built-in "typing" ambient sound (api/assets/typing-*-mono.wav).

The sound is synthesized rather than recorded so it carries no licence: each
keystroke is a short burst of band-passed noise with a fast decay over a soft
low "thock", typed in runs of a few words. It is seeded, so re-running
produces the same files.

It is built for short waits (the "thinking" and "tool calls" play modes play
a second or two at a time), so the typing never pauses for long: pauses
between runs stay under SILENT_RUN_SECS in ambient_sound.py. It is normalised
by peak rather than average level, because clicks sound quieter than a
steady sound of the same average level; at volume 1.0 it sits clearly under
the bot's voice without clipping.

    python -m api.scripts.generate_typing_sound
"""

from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

ASSETS = Path(__file__).resolve().parents[1] / "assets"
SAMPLE_RATES = (8000, 16000)
LOOP_SECS = 30.0
PEAK = 14000  # about -7 dBFS
SEED = 7


def _keystroke(rng: np.random.Generator, rate: int) -> np.ndarray:
    length = int(rate * rng.uniform(0.035, 0.06))
    t = np.arange(length) / rate
    click = rng.standard_normal(length) * np.exp(-t / rng.uniform(0.004, 0.008))
    low, high = rng.uniform(900, 1400), min(rate / 2 - 200, rng.uniform(3000, 3600))
    click = sosfilt(butter(2, [low, high], btype="band", fs=rate, output="sos"), click)
    thock = np.sin(2 * np.pi * rng.uniform(140, 220) * t) * np.exp(-t / 0.012) * 0.35
    return (click + thock) * rng.uniform(0.6, 1.0)


def generate(rate: int) -> np.ndarray:
    rng = np.random.default_rng(SEED)
    out = np.zeros(int(LOOP_SECS * rate))
    pos = int(0.02 * rate)
    # Type right up to the end so the loop has no silent seam where it wraps.
    last = len(out) - int(0.08 * rate)
    while pos < last:
        for _ in range(rng.integers(8, 30)):  # one run of typing
            key = _keystroke(rng, rate)
            end = min(len(out), pos + len(key))
            out[pos:end] += key[: end - pos]
            pos += int((rng.uniform(0.07, 0.2) if rng.random() > 0.15 else rng.uniform(0.2, 0.26)) * rate)
            if pos >= last:
                break
        pos += int(rng.uniform(0.12, 0.2) * rate)  # brief pause between runs
    # Soften the loop ends so the wrap-around doesn't click.
    fade = int(0.01 * rate)
    out[:fade] *= np.linspace(0, 1, fade)
    out[-fade:] *= np.linspace(1, 0, fade)
    out *= PEAK / np.abs(out).max()
    return np.clip(out, -32768, 32767).astype(np.int16)


def main() -> None:
    for rate in SAMPLE_RATES:
        path = ASSETS / f"typing-{rate}-mono.wav"
        audio = generate(rate)
        sf.write(path, audio, rate, subtype="PCM_16")
        rms = np.sqrt(np.mean(audio.astype(np.float64) ** 2))
        print(f"wrote {path} ({len(audio) / rate:.0f}s, peak {np.abs(audio).max()}, rms {rms:.0f})")


if __name__ == "__main__":
    main()
