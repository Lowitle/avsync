#!/usr/bin/env python3
"""Deterministic, synthetic validation of _localize_replacements_for_track.

Builds a reference silence and two independent foreign tracks with pauses in
different places, so an additional track must NOT just reuse the primary
track's already-placed splice boundary. Runs the function directly (no video
pipeline, no real season files needed) and asserts the additional track picks
its own quiet point instead of the primary's.
"""
import os
import tempfile

import numpy as np
from scipy.io import wavfile

import AVSync_v14 as av

SR = 8000


def tone(duration_s, freq=440.0, amplitude=10000):
    t = np.arange(int(duration_s * SR)) / SR
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def silence(duration_s):
    return np.zeros(int(duration_s * SR), dtype=np.int16)


def build_reference():
    # 0-8s content, 8-14s silence (the shared reference gap), 14-20s content
    return np.concatenate([tone(8.0), silence(6.0), tone(6.0)])


def build_additional_track():
    # Own quiet pause at 8.6-9.4s; content everywhere else in the search window,
    # including where the primary track placed its splice (10.5-11.0s).
    return np.concatenate([tone(8.6), silence(0.8), tone(20.0 - 9.4)])


def main():
    with tempfile.TemporaryDirectory() as tmp:
        ref_path = os.path.join(tmp, "ref_analysis.wav")
        track_path = os.path.join(tmp, "track_analysis.wav")
        wavfile.write(ref_path, SR, build_reference())
        wavfile.write(track_path, SR, build_additional_track())

        av.AUDIO_EDITORIAL_SOURCE_TEMPO = 1.0
        av.AUDIO_REPLACEMENT_RANGES.clear()
        av.AUDIO_REPLACEMENT_RANGES.append({
            "id": "AUDIO_REPLACEMENT_0001",
            # Position already chosen by the primary track (inside the ref silence,
            # but overlapping speech in this additional dub).
            "ref_start": 10.5,
            "ref_end": 11.0,
            "foreign_splice_time": 10.7,
            "use_silence": False,
            # Pre-move position, preserved by _move_replacements_to_quiet_primary_splices.
            "original_ref_start": 9.0,
            "original_ref_end": 9.5,
            "original_foreign_splice_time": 9.2,
        })

        class Args:
            per_track_splice_placement = True
            auto_silence_threshold = False

        final_segment_anchors = [
            (0.0, 0.0),
            (10.5, 10.7),
            (11.0, 10.7),
            (20.0, 20.0),
        ]

        result = av._localize_replacements_for_track(
            Args(), final_segment_anchors, ref_path, track_path, "test-track")

        print("Input :", final_segment_anchors)
        print("Result:", result)

        assert result != final_segment_anchors, "Expected the additional track to relocate its own splice"
        moved_start, moved_start_foreign = result[1]
        moved_end, moved_end_foreign = result[2]
        assert 8.5 <= moved_start <= 9.1, f"Unexpected relocated ref_start: {moved_start}"
        assert 9.0 <= moved_end <= 9.6, f"Unexpected relocated ref_end: {moved_end}"
        assert abs(moved_start_foreign - moved_end_foreign) < 0.01
        assert all(b[0] >= a[0] and b[1] >= a[1] for a, b in zip(result, result[1:])), "Anchors must stay monotonic"
        # And it must not be the primary track's own boundary (10.5/11.0) anymore.
        assert moved_start != 10.5 and moved_end != 11.0

        print("OK: additional track picked its own quiet splice point, independent of the primary track's.")

        # A rejected primary move must not mutate replacement metadata. Otherwise
        # the old anchors and newly moved metadata no longer match, and the generic
        # renderer receives a zero-length source range.
        av.AUDIO_REPLACEMENT_RANGES[:] = [{
            "id": "AUDIO_REPLACEMENT_0002",
            "ref_start": 10.5,
            "ref_end": 11.0,
            "foreign_splice_time": 10.7,
            "use_silence": False,
        }]
        original = dict(av.AUDIO_REPLACEMENT_RANGES[0])
        rejected_anchors = [
            ("START_ref", "START_foreign", 0.0, 0.0),
            ("AUDIO_REPLACEMENT_0002_a_ref", "AUDIO_REPLACEMENT_0002_a_foreign", 10.5, 10.7),
            ("AUDIO_REPLACEMENT_0002_b_ref", "AUDIO_REPLACEMENT_0002_b_foreign", 11.0, 10.7),
            ("NEXT_ref", "NEXT_foreign", 11.1, 8.5),
        ]
        rejected = av._move_replacements_to_quiet_primary_splices(
            Args(), rejected_anchors, ref_path, track_path)
        assert rejected == rejected_anchors
        for field in ("ref_start", "ref_end", "foreign_splice_time"):
            assert av.AUDIO_REPLACEMENT_RANGES[0][field] == original[field]
        print("OK: a rejected primary placement preserves its original replacement metadata.")


if __name__ == "__main__":
    main()
