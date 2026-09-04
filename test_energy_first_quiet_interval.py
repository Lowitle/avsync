#!/usr/bin/env python3
"""Deterministic validation that quiet-interval selection prefers actual silence
over duration.

Builds two candidate low-energy gaps around a splice point: a longer one that
is only marginally under the silence threshold (representative of quiet
background music/hiss, ~-37 dB) and a shorter one that is genuine digital
silence. The old ranking picked the longest interval regardless of how quiet
it actually was; the fixed ranking must prefer the genuinely silent gap even
though it is shorter, and use duration only as a tie-breaker.
"""
import numpy as np

import AVSync_v14 as av

SR = 8000


def tone(duration_s, freq, amplitude):
    t = np.arange(int(round(duration_s * SR))) / SR
    return amplitude * np.sin(2 * np.pi * freq * t)


def main():
    # 8.0-9.5s: quiet but not silent (~-37 dB, still under the -35 dB threshold) - LONGER (1.5s)
    # 10.5-10.8s: true digital silence - SHORTER (0.3s) but far quieter
    samples = np.concatenate([
        tone(8.0, 440.0, 0.9),     # content before
        tone(1.5, 200.0, 0.02),   # gap A: longer, only marginally quiet (~-37 dB)
        tone(1.0, 440.0, 0.9),     # content separating the two gaps
        np.zeros(int(round(0.3 * SR))),  # gap B: shorter, genuinely silent
        tone(3.2, 440.0, 0.9),     # content after
    ])

    result = av._find_nearby_quiet_interval(samples, SR, center_time=10.0, search_seconds=3.0, threshold_db=-35.0)
    print("Selected interval:", result)

    assert result is not None, "Expected a quiet interval to be found"
    start, end = result
    # Must pick gap B (genuine silence), not gap A (longer but less quiet).
    assert 10.4 <= start <= 10.6, f"Expected the genuinely silent gap, got start={start}"
    assert 10.7 <= end <= 10.9, f"Expected the genuinely silent gap, got end={end}"

    print("OK: quiet-interval selection prefers the genuinely silent gap over the longer, noisier one.")


if __name__ == "__main__":
    main()
