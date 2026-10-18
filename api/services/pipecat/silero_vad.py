"""Silero VAD v6.2 with resets only during clear non-speech.

Ported from pipecat main (pipecat-ai/pipecat #6202 and #6207, October 2026),
which our pinned pipecat predates. Our pipecat's ``SileroVADAnalyzer`` resets
the model's recurrent state every five seconds of wall-clock time. A reset that
lands inside a word makes the model's confidence dip long enough for the VAD to
report the caller stopping mid-sentence, so STT finalizes early and the turn
logic sees cut-off speech. Pipecat measured that most resets falling inside an
utterance did this.

This analyzer instead:

- runs the Silero **v6.2** model (``api/native/silero/silero_vad_v6_2.onnx``),
  whose state holds steady through long silence, and
- resets about every **2 s of audio**, but only once the VAD is quiet and the
  last ~160 ms all scored below 0.1, so a reset never lands on speech. Without
  any resets, music (hold music on outbound calls) or long silence on 8 kHz
  phone audio leaves the model unable to detect the speech that follows; with
  them it recovers within about one interval.

Time is counted in audio windows, not the wall clock, so the same audio always
resets at the same points.

Volume is also measured the way newer pipecat does (``AudioVolumeTracker``,
pipecat 1.8): over a rolling 400 ms window rather than per analysis frame, so a
single quiet frame inside a word doesn't drop below ``min_volume``.
"""

from __future__ import annotations

import math
from typing import cast

import numpy as np
import onnxruntime
from loguru import logger

from api.constants import APP_ROOT_DIR
from pipecat.audio.utils import calculate_audio_volume, exp_smoothing
from pipecat.audio.vad.vad_analyzer import VADAnalyzer, VADParams, VADState

MODEL_PATH = APP_ROOT_DIR / "native" / "silero" / "silero_vad_v6_2.onnx"

# Seconds of audio between resets. A shorter interval recovers sooner after
# non-speech but puts more resets just ahead of speech.
MODEL_RESET_INTERVAL_SECS = 2.0
# A due reset waits for this many consecutive windows (~160 ms) below this
# confidence, far under the speech threshold, so a dip inside a word or a soft
# word start never counts.
MODEL_RESET_MAX_CONFIDENCE = 0.1
MODEL_RESET_LOW_CONFIDENCE_WINDOWS = 5
# Volume is measured over this much recent audio (pipecat's AudioVolumeTracker).
VOLUME_WINDOW_SECS = 0.4


class AudioVolumeTracker:
    """Volume of the most recent VOLUME_WINDOW_SECS of a mono 16-bit stream."""

    def __init__(self):
        self._sample_rate = 0
        self._window_num_bytes = 0
        self._buffer = bytearray()
        self._volume: float | None = 0.0

    @property
    def volume(self) -> float:
        """Reads 0 until the window holds a full window of audio."""
        if self._volume is None:
            self._volume = calculate_audio_volume(bytes(self._buffer), self._sample_rate)
        return self._volume

    def update(self, audio: bytes, sample_rate: int):
        if sample_rate != self._sample_rate:
            self._sample_rate = sample_rate
            self._window_num_bytes = math.ceil(VOLUME_WINDOW_SECS * sample_rate) * 2
            self.reset()
        self._buffer += audio
        if len(self._buffer) > self._window_num_bytes:
            del self._buffer[: len(self._buffer) - self._window_num_bytes]
        if len(self._buffer) == self._window_num_bytes:
            self._volume = None

    def reset(self):
        self._buffer = bytearray()
        self._volume = 0.0


class SileroOnnxModel:
    """ONNX wrapper for the Silero VAD model (8 kHz and 16 kHz)."""

    def __init__(self, path, force_onnx_cpu: bool = True):
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        if force_onnx_cpu and "CPUExecutionProvider" in onnxruntime.get_available_providers():
            self.session = onnxruntime.InferenceSession(
                str(path), providers=["CPUExecutionProvider"], sess_options=opts
            )
        else:
            self.session = onnxruntime.InferenceSession(str(path), sess_options=opts)
        self.reset_states()
        self.sample_rates = [8000, 16000]

    def _validate_input(self, x, sr: int):
        if np.ndim(x) == 1:
            x = np.expand_dims(x, 0)
        if np.ndim(x) > 2:
            raise ValueError(f"Too many dimensions for input audio chunk {x.ndim}")
        if sr not in self.sample_rates:
            raise ValueError(f"Supported sampling rates: {self.sample_rates}")
        if sr / np.shape(x)[1] > 31.25:
            raise ValueError("Input audio chunk is too short")
        return x, sr

    def reset_states(self, batch_size: int = 1):
        self._state = np.zeros((2, batch_size, 128), dtype="float32")
        self._context = np.zeros((batch_size, 0), dtype="float32")
        self._last_sr = 0
        self._last_batch_size = 0

    def __call__(self, x, sr: int) -> np.ndarray:
        x, sr = self._validate_input(x, sr)
        num_samples = 512 if sr == 16000 else 256
        if np.shape(x)[-1] != num_samples:
            raise ValueError(
                f"Provided number of samples is {np.shape(x)[-1]} "
                "(Supported values: 256 for 8000 sample rate, 512 for 16000)"
            )
        batch_size = np.shape(x)[0]
        context_size = 64 if sr == 16000 else 32
        if not self._last_batch_size:
            self.reset_states(batch_size)
        if self._last_sr and self._last_sr != sr:
            self.reset_states(batch_size)
        if self._last_batch_size and self._last_batch_size != batch_size:
            self.reset_states(batch_size)
        if not np.shape(self._context)[1]:
            self._context = np.zeros((batch_size, context_size), dtype="float32")
        x = np.concatenate((self._context, x), axis=1)
        ort_outs = self.session.run(
            None, {"input": x, "state": self._state, "sr": np.array(sr, dtype="int64")}
        )
        out = cast(np.ndarray, ort_outs[0])
        self._state = cast(np.ndarray, ort_outs[1])
        self._context = x[..., -context_size:]
        self._last_sr = sr
        self._last_batch_size = batch_size
        return out


class DograhSileroVADAnalyzer(VADAnalyzer):
    """Silero v6.2 VAD whose model resets only during clear non-speech."""

    def __init__(self, *, sample_rate: int | None = None, params: VADParams | None = None):
        super().__init__(sample_rate=sample_rate, params=params)
        self._model = SileroOnnxModel(MODEL_PATH, force_onnx_cpu=True)
        # Windows scored since the last reset, and how many of the most recent
        # windows in a row scored below MODEL_RESET_MAX_CONFIDENCE.
        self._windows_since_reset = 0
        self._low_confidence_windows = 0
        self._volume_tracker = AudioVolumeTracker()
        logger.debug("Loaded Silero VAD v6.2")

    def set_sample_rate(self, sample_rate: int):
        if sample_rate not in (8000, 16000):
            raise ValueError(
                f"Silero VAD sample rate needs to be 16000 or 8000 (sample rate: {sample_rate})"
            )
        super().set_sample_rate(sample_rate)

    def _get_smoothed_volume(self, audio: bytes) -> float:
        self._volume_tracker.update(audio, self.sample_rate)
        return exp_smoothing(self._volume_tracker.volume, self._prev_volume, self._smoothing_factor)

    def num_frames_required(self) -> int:
        return 512 if self.sample_rate == 16000 else 256

    def voice_confidence(self, buffer) -> float:
        try:
            audio_float32 = np.frombuffer(buffer, np.int16).astype(np.float32) / 32768.0
            confidence = self._model(audio_float32, self.sample_rate)[0]
            self._maybe_reset_model(confidence.item())
            return confidence
        except Exception as e:
            logger.error(f"Error analyzing audio with Silero VAD: {e}")
            return 0

    def _maybe_reset_model(self, confidence: float):
        """Reset the model once a reset is due and the caller is clearly quiet.

        ``_vad_state`` is still the state from before this window (the base
        class updates it from the confidence returned here), so a reset takes
        effect from the next window and never while speech is in progress.
        """
        self._windows_since_reset += 1
        if confidence < MODEL_RESET_MAX_CONFIDENCE:
            self._low_confidence_windows += 1
        else:
            self._low_confidence_windows = 0
        if (
            self._windows_since_reset * self.num_frames_required()
            >= MODEL_RESET_INTERVAL_SECS * self.sample_rate
            and self._vad_state == VADState.QUIET
            and self._low_confidence_windows >= MODEL_RESET_LOW_CONFIDENCE_WINDOWS
        ):
            self._model.reset_states()
            self._windows_since_reset = 0
