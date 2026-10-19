"""Generate the built-in "typing" ambient sound (api/assets/typing-*-mono.wav).

The sound is synthesized rather than recorded so it carries no licence: each
keystroke is a short burst of band-passed noise with a fast decay over a soft
low "thock", typed in bursts of a few words with natural pauses. It is seeded,
so re-running produces the same files.

Loudness is matched to the office ambience asset (RMS about 250), so the same
volume setting sounds comparable for either built-in sound.

    python -m api.scripts.generate_typing_sound
"""

from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

ASSETS = Path(__file__).resolve().parents[1] / "assets"
SAMPLE_RATES = (8000, 16000)
LOOP_SECS = 30.0
TARGET_RMS = 250
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
    pos = int(0.3 * rate)
    while pos < len(out) - rate:
        for _ in range(rng.integers(8, 30)):  # one burst of typing
            key = _keystroke(rng, rate)
            end = min(len(out), pos + len(key))
            out[pos:end] += key[: end - pos]
            gap = rng.uniform(0.07, 0.2) if rng.random() > 0.15 else rng.uniform(0.25, 0.4)
            pos += int(gap * rate)
            if pos >= len(out) - rate:
                break
        pos += int(rng.uniform(0.6, 1.6) * rate)  # pause between bursts
    # Fade the loop ends so the wrap-around is seamless.
    fade = int(0.05 * rate)
    out[:fade] *= np.linspace(0, 1, fade)
    out[-fade:] *= np.linspace(1, 0, fade)
    out *= TARGET_RMS / np.sqrt(np.mean(out**2))
    return np.clip(out, -32768, 32767).astype(np.int16)


def main() -> None:
    for rate in SAMPLE_RATES:
        path = ASSETS / f"typing-{rate}-mono.wav"
        audio = generate(rate)
        sf.write(path, audio, rate, subtype="PCM_16")
        print(f"wrote {path} ({len(audio) / rate:.0f}s, peak {np.abs(audio).max()})")


if __name__ == "__main__":
    main()
