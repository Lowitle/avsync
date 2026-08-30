#!/usr/bin/env python3
"""Deterministic validation of --auto_silence_threshold (per-track noise floor calibration).

Builds a foreign track whose "quiet" pause is real low-level hiss (~-33 dBFS),
not true digital silence - representative of a noisy analog/AC3 dub. The
fixed -35 dBFS default misses this pause entirely (too strict for this
track); calibrating the threshold from the track's own measured noise floor
recognizes it correctly. Runs the actual functions used in production
(audio_alignment.calibrate_silence_threshold_db /
AVSync_v14._localize_replacements_for_track), no video pipeline needed.
"""
import os
import tempfile

import numpy as np
from scipy.io import wavfile

import audio_alignment as aa
import AVSync_v14 as av

SR = 8000
RNG = np.random.default_rng(42)


def tone(duration_s, freq=440.0, amplitude=16000):
    t = np.arange(int(duration_s * SR)) / SR
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def hiss(duration_s, rms_amplitude=750):
    # ~750/32768 ~= -32.8 dBFS RMS for white noise, safely above the fixed -35 dB default.
    samples = RNG.normal(0.0, rms_amplitude, int(duration_s * SR))
    return np.clip(samples, -32768, 32767).astype(np.int16)


def build_reference():
    return np.concatenate([tone(8.0), np.zeros(int(6.0 * SR), dtype=np.int16), tone(6.0)])


def build_noisy_additional_track():
    # 9-11s is a real dub pause, but with the track's own background hiss, not true zero.
    return np.concatenate([tone(9.0), hiss(2.0), tone(9.0)])


def measured_rms_db(samples, sr, start, end):
    segment = samples[int(start * sr):int(end * sr)].astype(np.float64) / 32768.0
    rms = np.sqrt(np.mean(np.square(segment)))
    return 20.0 * np.log10(rms + 1e-9)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        ref_path = os.path.join(tmp, "ref_analysis.wav")
        track_path = os.path.join(tmp, "track_analysis.wav")
        track_samples = build_noisy_additional_track()
        wavfile.write(ref_path, SR, build_reference())
        wavfile.write(track_path, SR, track_samples)

        pause_db = measured_rms_db(track_samples, SR, 9.0, 11.0)
        print(f"Measured pause RMS: {pause_db:.1f} dB (fixed threshold is -35.0 dB)")
        assert pause_db > -35.0, "Test setup invalid: pause must be louder than the fixed threshold"

        # 1) Fixed threshold misses the pause entirely.
        fixed_intervals = aa.detect_silence_intervals(
            aa.normalize_analysis_level(track_samples), SR, threshold_db=-35.0, min_duration=0.2)
        print("Fixed -35 dB intervals near the pause:", fixed_intervals)
        assert not any(start <= 10.0 <= end for start, end in fixed_intervals), \
            "Test setup invalid: fixed threshold should NOT find this noisy pause"

        # 2) Calibrated threshold, derived from the track's own noise floor, does find it.
        normalized = aa.normalize_analysis_level(track_samples)
        threshold_db, noise_floor_db = aa.calibrate_silence_threshold_db(normalized, SR)
        print(f"Calibrated: noise_floor={noise_floor_db:.1f} dB, threshold={threshold_db:.1f} dB")
        calibrated_intervals = aa.detect_silence_intervals(
            normalized, SR, threshold_db=threshold_db, min_duration=0.2)
        print("Calibrated intervals near the pause:", calibrated_intervals)
        assert any(start <= 10.0 <= end for start, end in calibrated_intervals), \
            "Calibrated threshold should recognize the track's own quiet pause"

        # 3) End-to-end: _localize_replacements_for_track only relocates when calibration is on.
        av.AUDIO_EDITORIAL_SOURCE_TEMPO = 1.0
        av.AUDIO_REPLACEMENT_RANGES.clear()
        av.AUDIO_REPLACEMENT_RANGES.append({
            "id": "AUDIO_REPLACEMENT_0001",
            "ref_start": 8.5, "ref_end": 9.0, "foreign_splice_time": 8.7,
            "use_silence": False,
            "original_ref_start": 8.5, "original_ref_end": 9.0,
            "original_foreign_splice_time": 8.7,
        })
        final_segment_anchors = [(0.0, 0.0), (8.5, 8.7), (9.0, 8.7), (20.0, 20.0)]

        class ArgsOff:
            per_track_splice_placement = True
            auto_silence_threshold = False

        class ArgsOn(ArgsOff):
            auto_silence_threshold = True

        result_off = av._localize_replacements_for_track(
            ArgsOff(), final_segment_anchors, ref_path, track_path, "fixed-threshold")
        result_on = av._localize_replacements_for_track(
            ArgsOn(), final_segment_anchors, ref_path, track_path, "calibrated-threshold")

        print("Fixed threshold result     :", result_off)
        print("Calibrated threshold result:", result_on)
        assert result_off == final_segment_anchors, \
            "Without calibration, the noisy pause should stay undetected (no relocation)"
        assert result_on != final_segment_anchors, \
            "With calibration, the track's own noisy pause should be found and used"

        print("OK: auto_silence_threshold lets a noisy track recognize its own real pauses.")


if __name__ == "__main__":
    main()
