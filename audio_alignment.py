"""Audio-to-audio alignment primitives for the remaster workflow.

This module is intentionally independent from the existing visual sync path.
It compares two explicitly selected audio streams and returns a global offset
plus confidence, so the algorithm can be tested before it is wired into the
AVSync command-line workflow.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy import signal


DEFAULT_SAMPLE_RATE = 16000
DEFAULT_ANALYZE_SECONDS = 60.0
ENVELOPE_HOP = 160
ENVELOPE_WINDOW = 800


@dataclass
class AudioAlignment:
    """Result of aligning source audio to reference audio."""

    offset_seconds: float
    confidence: float
    method: str


@dataclass
class AudioAnchor:
    """A locally validated correspondence between reference and source time."""

    reference_time: float
    source_time: float
    offset_seconds: float
    confidence: float
    method: str


@dataclass
class AudioSegmentRecipe:
    """Instruction for transforming one source interval to reference time."""

    reference_start: float
    reference_end: float
    source_start: float
    source_end: float
    tempo: float


@dataclass
class SilenceDifference:
    """A silence interval whose duration differs between two aligned signals."""

    reference_start: float
    reference_end: float
    source_start: float
    source_end: float
    duration_difference: float
    kind: str


@dataclass
class EditorialEdit:
    """Timeline edit derived from a detected source/reference difference."""

    operation: str
    reference_position: float
    source_start: float
    source_end: float
    duration_seconds: float
    cumulative_shift_seconds: float
    reason: str


def extract_mono_audio(path, stream_index=0, sample_rate=DEFAULT_SAMPLE_RATE,
                       duration=None, tempo=1.0, normalize_loudness=False):
    """Extract one audio stream as mono float32 samples using FFmpeg.

    ``tempo`` changes playback duration while preserving pitch. A value below
    one slows the source down, which is needed when converting 25 FPS material
    to 24000/1001 FPS timing.
    
    ``normalize_loudness`` applies loudness normalization (-23 LUFS) to handle
    low-level audio (e.g., AC3 streams recorded at reduced levels). This 
    improves anchor detection when input audio has highly variable levels.
    """
    if tempo <= 0 or tempo > 2:
        raise ValueError("tempo must be greater than 0 and no greater than 2")

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if duration is not None:
        command.extend(["-t", str(duration)])
    command.extend([
        "-i", str(Path(path)),
        "-map", f"0:{stream_index}",
        "-ac", "1", "-ar", str(sample_rate),
    ])
    
    # Build audio filter chain (order matters: atempo must come before loudnorm)
    filters = []
    if tempo != 1.0:
        filters.append(f"atempo={tempo:.12f}")
    if normalize_loudness:
        filters.append("loudnorm=I=-23:TP=-1.5:LRA=11")
    
    if filters:
        command.extend(["-af", ",".join(filters)])
    
    command.extend(["-f", "f32le", "-"])

    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"FFmpeg audio extraction failed: {detail}")
    if len(result.stdout) < 4:
        raise RuntimeError(f"No audio extracted from {Path(path).name}")

    usable_bytes = len(result.stdout) - (len(result.stdout) % 4)
    return np.frombuffer(result.stdout[:usable_bytes], dtype=np.float32).copy()


def _normalize(values):
    values = values.astype(np.float64, copy=False)
    values = values - values.mean()
    deviation = values.std()
    return values / deviation if deviation > 0 else values


def normalize_analysis_level(values, target_peak=0.9):
    """Scale a signal for level-independent silence analysis only."""
    values = np.asarray(values, dtype=np.float64)
    if values.size == 0:
        return values.copy()
    peak = float(np.percentile(np.abs(values), 99.5))
    if peak <= 1e-12:
        return values.copy()
    return values * (target_peak / peak)


def _confidence(correlation, peak_index, guard):
    peak = abs(float(correlation[peak_index]))
    if peak <= 1e-12:
        return 0.0

    mask = np.ones(len(correlation), dtype=bool)
    mask[max(0, peak_index - guard):min(len(correlation), peak_index + guard + 1)] = False
    if not np.any(mask):
        return float("inf")

    secondary = float(np.max(np.abs(correlation[mask])))
    return peak / secondary if secondary > 1e-12 else float("inf")


def correlate_offset(reference, source, sample_rate, guard_seconds=0.25):
    """Return source start offset and peak-ratio confidence.

    A positive offset means the source starts later than the reference.
    """
    reference = _normalize(reference)
    source = _normalize(source)
    correlation = signal.correlate(reference, source, mode="full", method="fft")
    peak_index = int(np.argmax(np.abs(correlation)))
    zero_lag_index = len(source) - 1
    lag_samples = peak_index - zero_lag_index
    confidence = _confidence(
        correlation,
        peak_index,
        max(1, int(guard_seconds * sample_rate)),
    )
    return -lag_samples / sample_rate, confidence


def _envelope(samples, sample_rate):
    """Build a log-energy envelope suitable for cross-microphone matching."""
    hop = max(1, int(ENVELOPE_HOP * sample_rate / DEFAULT_SAMPLE_RATE))
    window = max(hop, int(ENVELOPE_WINDOW * sample_rate / DEFAULT_SAMPLE_RATE))
    if len(samples) < window:
        return np.zeros(0, dtype=np.float32), sample_rate / hop

    squared = samples.astype(np.float64) ** 2
    cumulative = np.concatenate(([0.0], np.cumsum(squared)))
    starts = np.arange(0, len(samples) - window + 1, hop)
    rms = np.sqrt(np.maximum(1e-12, (cumulative[starts + window] - cumulative[starts]) / window))
    envelope = np.log(rms + 1e-3).astype(np.float32)
    if len(envelope) > 8:
        cutoff = min(0.05, (sample_rate / hop) * 0.25)
        envelope = signal.sosfiltfilt(
            signal.butter(2, cutoff, btype="high", fs=sample_rate / hop, output="sos"),
            envelope,
        ).astype(np.float32)
    return envelope, sample_rate / hop


def is_safe_splice_point(samples, sample_rate, time_seconds, threshold_db=-35.0,
                          window_seconds=0.15):
    """Check whether hard-cutting ``samples`` at ``time_seconds`` lands on a natural pause.

    Used before splicing reference audio into a foreign gap: if the reference
    signal is still loud right at the cut point (e.g. mid-note in a song), a
    hard cut there would sound like an abrupt interruption. Returns True when
    the local energy around the cut is at or below ``threshold_db`` (safe to
    hard-cut), False when audible content would be interrupted abruptly.
    """
    if sample_rate <= 0:
        return True
    samples = np.asarray(samples)
    window_samples = max(1, int(round(window_seconds * sample_rate)))
    center = int(round(time_seconds * sample_rate))
    start = max(0, center - window_samples)
    end = min(len(samples), center + window_samples)
    if end <= start:
        return True
    segment = samples[start:end].astype(np.float64)
    rms = np.sqrt(np.mean(np.square(segment)))
    db = 20.0 * np.log10(rms + 1e-9)
    return db <= threshold_db


def detect_silence_intervals(samples, sample_rate, threshold_db=-40.0, min_duration=0.2,
                             frame_seconds=0.02):
    """Return sustained low-energy intervals in an audio signal.

    Times are expressed in the signal's current timeline. This function is
    intended for already tempo-normalized signals, not for changing playback
    speed during correction.
    """
    if sample_rate <= 0 or min_duration <= 0 or frame_seconds <= 0:
        raise ValueError("sample_rate, min_duration, and frame_seconds must be positive")
    samples = np.asarray(samples)
    if samples.size == 0:
        return []

    frame_size = max(1, int(round(frame_seconds * sample_rate)))
    frame_count = len(samples) // frame_size
    if frame_count == 0:
        return []
    frames = samples[:frame_count * frame_size].reshape(frame_count, frame_size)
    rms = np.sqrt(np.mean(np.square(frames.astype(np.float64)), axis=1))
    threshold = 10.0 ** (threshold_db / 20.0)
    silent = rms <= threshold

    intervals = []
    run_start = None
    for index, is_silent in enumerate(np.append(silent, False)):
        if is_silent and run_start is None:
            run_start = index
        elif not is_silent and run_start is not None:
            start = run_start * frame_seconds
            end = index * frame_seconds
            if end - start >= min_duration:
                intervals.append((start, end))
            run_start = None
    return intervals


def compare_silence_profiles(reference, source, sample_rate, offset=0.0,
                             threshold_db=-40.0, min_duration=0.2,
                             tolerance_seconds=0.25):
    """Compare silence intervals after timeline normalization.

    ``offset`` is positive when source content occurs later than reference
    content. It can be a single float (constant offset for the whole file) or
    a sorted list of ``(reference_time, offset)`` pairs, in which case the
    expected offset at each reference silence is piecewise-linearly
    interpolated - a single global offset misses real differences wherever
    the true offset has drifted away from it (e.g. an editorial cut earlier
    in the file that a global first-anchor offset doesn't account for).
    Differences are reported only for reference silences with a nearby source
    silence, avoiding claims based on unrelated quiet passages. ``kind`` is
    ``source_longer`` or ``source_shorter``.
    """
    if isinstance(offset, (int, float)):
        offset_points = None
        constant_offset = float(offset)
    else:
        offset_points = sorted(offset, key=lambda item: item[0])
        constant_offset = None

    def _expected_offset(reference_time):
        if offset_points is None:
            return constant_offset
        if reference_time <= offset_points[0][0]:
            return offset_points[0][1]
        if reference_time >= offset_points[-1][0]:
            return offset_points[-1][1]
        for (time_a, offset_a), (time_b, offset_b) in zip(offset_points, offset_points[1:]):
            if time_a <= reference_time <= time_b:
                if time_b == time_a:
                    return offset_a
                fraction = (reference_time - time_a) / (time_b - time_a)
                return offset_a + fraction * (offset_b - offset_a)
        return offset_points[-1][1]

    reference_analysis = normalize_analysis_level(reference)
    source_analysis = normalize_analysis_level(source)
    reference_silences = detect_silence_intervals(
        reference_analysis, sample_rate, threshold_db, min_duration)
    source_silences = detect_silence_intervals(
        source_analysis, sample_rate, threshold_db, min_duration)
    differences = []
    used_source_indices = set()
    for reference_start, reference_end in reference_silences:
        local_offset = _expected_offset(reference_start)
        expected_start = reference_start + local_offset
        expected_end = reference_end + local_offset
        available = [
            (index, interval) for index, interval in enumerate(source_silences)
            if index not in used_source_indices
        ]
        candidate_index, candidate = min(
            available,
            key=lambda item: abs(item[1][0] - expected_start),
            default=(None, None),
        )
        if candidate is None or abs(candidate[0] - expected_start) > tolerance_seconds:
            continue
        used_source_indices.add(candidate_index)
        source_start, source_end = candidate
        duration_difference = (source_end - source_start) - (reference_end - reference_start)
        if abs(duration_difference) < tolerance_seconds:
            continue
        differences.append(SilenceDifference(
            reference_start=reference_start,
            reference_end=reference_end,
            source_start=source_start,
            source_end=source_end,
            duration_difference=duration_difference,
            kind="source_longer" if duration_difference > 0 else "source_shorter",
        ))
    return differences


def build_editorial_edit_recipe(silence_differences, source_tempo=1.0):
    """Translate silence differences into cut/insert timeline operations.

    The differences must come from the globally normalized source timeline.
    ``source_tempo`` maps source timestamps back to the original source file.
    No playback-rate change is prescribed by this recipe.

    A longer source silence produces a source-only deletion after the shared
    silence. A shorter source silence produces an insertion of reference audio
    at the end of the source silence. ``cumulative_shift_seconds`` describes
    how later source material moves after each operation.
    """
    if source_tempo <= 0:
        raise ValueError("source_tempo must be positive")

    ordered = sorted(silence_differences, key=lambda item: item.reference_start)
    edits = []
    cumulative_shift = 0.0
    for difference in ordered:
        duration = abs(difference.duration_difference)
        if duration <= 0:
            continue

        if difference.kind == "source_longer":
            operation = "delete_source_silence"
            source_start = difference.source_end - duration
            source_end = difference.source_end
            reason = "source silence is longer than reference silence"
            cumulative_shift -= duration
        elif difference.kind == "source_shorter":
            operation = "insert_reference_audio"
            source_start = difference.source_end
            source_end = difference.source_end
            reason = "source silence is shorter than reference silence"
            cumulative_shift += duration
        else:
            raise ValueError(f"Unknown silence difference kind: {difference.kind}")

        edits.append(EditorialEdit(
            operation=operation,
            reference_position=difference.reference_end,
            source_start=source_start * source_tempo,
            source_end=source_end * source_tempo,
            duration_seconds=duration,
            cumulative_shift_seconds=cumulative_shift,
            reason=reason,
        ))
    return edits


def _seconds_to_samples(seconds, sample_rate):
    """Convert a relative time in seconds to sample index."""
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    return max(0, int(round(seconds * sample_rate)))


def apply_editorial_edit_recipe(source_audio, reference_audio, edits, sample_rate=DEFAULT_SAMPLE_RATE):
    """Apply a sequence of timeline edits to the source signal.

    The algorithm is intentionally simple and deterministic: it rewrites the
    source signal in chronological order using the edit instructions returned by
    ``build_editorial_edit_recipe``. A delete operation removes a source interval;
    an insert operation injects the corresponding reference segment at the
    matching source position.

    The returned array is the corrected source timeline ready for export or for
    deeper FFmpeg-based mixing. If ``reference_audio`` is shorter than the
    requested insertion, the available samples are used and the insertion is
    clipped to the valid range.
    """
    source_audio = np.asarray(source_audio, dtype=np.float64).copy()
    reference_audio = np.asarray(reference_audio, dtype=np.float64).copy()
    if source_audio.size == 0:
        return source_audio.copy()
    if not edits:
        return source_audio.copy()

    ordered_edits = sorted(edits, key=lambda edit: edit.source_start)
    corrected = []
    source_cursor = 0.0

    for edit in ordered_edits:
        if edit.source_start < source_cursor:
            continue
        start_index = _seconds_to_samples(source_cursor, sample_rate)
        end_index = _seconds_to_samples(edit.source_start, sample_rate)
        if end_index > start_index:
            corrected.append(source_audio[start_index:end_index])

        if edit.operation == "delete_source_silence":
            source_cursor = max(source_cursor, edit.source_end)
            continue

        if edit.operation == "insert_reference_audio":
            ref_start = _seconds_to_samples(max(0.0, edit.reference_position), sample_rate)
            ref_end = _seconds_to_samples(
                max(0.0, edit.reference_position + max(0.0, edit.duration_seconds)),
                sample_rate,
            )
            if ref_start < len(reference_audio):
                insert_segment = reference_audio[ref_start:ref_end]
                if insert_segment.size:
                    corrected.append(insert_segment)
            source_cursor = max(source_cursor, edit.source_start)
            continue

        raise ValueError(f"Unsupported editorial operation: {edit.operation}")

    tail_start = _seconds_to_samples(source_cursor, sample_rate)
    if tail_start < len(source_audio):
        corrected.append(source_audio[tail_start:])

    if not corrected:
        return np.zeros(0, dtype=np.float64)
    return np.concatenate(corrected).astype(np.float64, copy=False)


def map_source_time_to_reference(source_time, edits, source_tempo=1.0):
    """Map an original source timestamp onto the edited reference timeline.

    Deleted source intervals collapse to their edit boundary. Insertions shift
    all later source timestamps by the inserted duration.
    """
    if source_tempo <= 0:
        raise ValueError("source_tempo must be positive")
    if source_time < 0:
        return 0.0

    normalized_time = source_time / source_tempo
    timeline_shift = 0.0
    for edit in sorted(edits, key=lambda item: item.source_start):
        if edit.operation == "delete_source_silence":
            if source_time < edit.source_start:
                break
            if source_time <= edit.source_end:
                return max(0.0, edit.reference_position)
            timeline_shift += -edit.duration_seconds
        elif edit.operation == "insert_reference_audio":
            if source_time < edit.source_start:
                break
            timeline_shift += edit.duration_seconds
        else:
            raise ValueError(f"Unsupported editorial operation: {edit.operation}")

    return max(0.0, normalized_time + timeline_shift)


def correlate_envelope_offset(reference, source, sample_rate):
    """Return offset and confidence using log-energy envelope correlation."""
    reference_envelope, envelope_rate = _envelope(reference, sample_rate)
    source_envelope, _ = _envelope(source, sample_rate)
    if len(reference_envelope) == 0 or len(source_envelope) == 0:
        raise ValueError("Audio is too short for envelope alignment")
    return correlate_offset(reference_envelope, source_envelope, envelope_rate)


def find_windowed_anchors(reference, source, sample_rate,
                          window_seconds=60.0, step_seconds=60.0,
                          expected_offset=0.0, search_radius_seconds=15.0,
                          min_confidence=2.0, agreement_seconds=0.15):
    """Find locally consistent audio anchors in two time-normalized arrays.

    ``reference`` and ``source`` must use the same sample rate and playback
    speed. The source search is restricted around ``expected_offset`` to avoid
    distant false matches. A window is accepted when at least one method has
    sufficient confidence; if both methods are reliable, they must agree.
    Negative offsets mean the source starts before the reference.
    """
    if window_seconds <= 0 or step_seconds <= 0 or search_radius_seconds < 0:
        raise ValueError("window, step, and search radius must be positive")
    window_samples = int(window_seconds * sample_rate)
    step_samples = max(1, int(step_seconds * sample_rate))
    if len(reference) < window_samples or len(source) < 1:
        return []

    anchors = []
    for ref_start in range(0, len(reference) - window_samples + 1, step_samples):
        ref_end = ref_start + window_samples
        reference_window = reference[ref_start:ref_end]
        expected_source_start = ref_start / sample_rate - expected_offset
        search_start = max(0, int((expected_source_start - search_radius_seconds) * sample_rate))
        search_end = min(
            len(source),
            int((expected_source_start + window_seconds + search_radius_seconds) * sample_rate),
        )
        source_search = source[search_start:search_end]
        if len(source_search) < window_samples:
            continue

        waveform_offset, waveform_confidence = correlate_offset(
            reference_window, source_search, sample_rate)
        envelope_offset, envelope_confidence = correlate_envelope_offset(
            reference_window, source_search, sample_rate)
        search_start_seconds = search_start / sample_rate
        reference_start_seconds = ref_start / sample_rate
        waveform_offset += search_start_seconds - reference_start_seconds
        envelope_offset += search_start_seconds - reference_start_seconds
        best_confidence = max(waveform_confidence, envelope_confidence)
        if best_confidence < min_confidence:
            continue
        both_methods_reliable = (
            waveform_confidence >= min_confidence
            and envelope_confidence >= min_confidence
        )
        if both_methods_reliable and abs(waveform_offset - envelope_offset) > agreement_seconds:
            continue

        if envelope_confidence >= waveform_confidence:
            offset = envelope_offset
            method = "envelope"
        else:
            offset = waveform_offset
            method = "waveform"
        ref_time = ref_start / sample_rate
        # offset is positive when the source lags the reference, so the matching
        # source position is LATER by that amount: source_time = ref_time + offset.
        source_time = ref_time + offset
        anchors.append(AudioAnchor(
            reference_time=ref_time,
            source_time=source_time,
            offset_seconds=offset,
            confidence=best_confidence,
            method=method,
        ))
    return anchors


def find_partial_anchor(reference, source, sample_rate, reference_end,
                        source_end, reference_start=0.0, expected_offset=0.0,
                        window_seconds=2.0, step_seconds=0.5,
                        search_radius_seconds=8.0, min_confidence=1.5,
                        agreement_seconds=0.2, min_consistent_matches=2,
                        prefer='earliest'):
    """Find a short common fragment inside an otherwise mismatched interval.

    By default the search covers ``[0, reference_end)`` and returns the
    *earliest* consistent match, recovering a fragment hidden inside an
    unmatched prefix (e.g. a different intro). Passing ``reference_start``
    restricts the search to ``[reference_start, reference_end)``, and
    ``prefer='latest'`` returns the match closest to ``reference_end``
    instead - used to recover a fragment hidden inside an unmatched suffix
    (e.g. the last few seconds of content after the last accepted anchor).
    ``expected_offset`` centers the source search window when the source is
    not expected to start near zero-offset from the reference (as is the
    case near the end of a file, after edits may have shifted the timeline).
    """
    if (reference_end <= 0 or source_end <= 0 or window_seconds <= 0
            or step_seconds <= 0 or search_radius_seconds < 0):
        raise ValueError("partial-anchor search parameters are invalid")
    if prefer not in ('earliest', 'latest'):
        raise ValueError("prefer must be 'earliest' or 'latest'")
    window_samples = int(window_seconds * sample_rate)
    step_samples = max(1, int(step_seconds * sample_rate))
    ref_start_sample = max(0, int(reference_start * sample_rate))
    ref_limit = min(len(reference), int(reference_end * sample_rate))
    source_limit = min(len(source), int(source_end * sample_rate))
    if ref_limit - ref_start_sample < window_samples or source_limit < window_samples:
        return None

    candidates = []
    for ref_start in range(ref_start_sample, ref_limit - window_samples + 1, step_samples):
        reference_window = reference[ref_start:ref_start + window_samples]
        ref_start_seconds = ref_start / sample_rate
        expected_source_start = ref_start_seconds + expected_offset
        search_start = max(0, int((expected_source_start - search_radius_seconds) * sample_rate))
        search_end = min(source_limit, int((expected_source_start + window_seconds + search_radius_seconds) * sample_rate))
        source_search = source[search_start:search_end]
        if len(source_search) < window_samples:
            continue
        waveform_offset, waveform_confidence = correlate_offset(
            reference_window, source_search, sample_rate)
        envelope_offset, envelope_confidence = correlate_envelope_offset(
            reference_window, source_search, sample_rate)
        shift = search_start / sample_rate - ref_start_seconds
        waveform_offset += shift
        envelope_offset += shift
        if max(waveform_confidence, envelope_confidence) < min_confidence:
            continue
        if (waveform_confidence >= min_confidence
                and envelope_confidence >= min_confidence
                and abs(waveform_offset - envelope_offset) > agreement_seconds):
            continue
        offset = (envelope_offset if envelope_confidence >= waveform_confidence
                  else waveform_offset)
        confidence = max(waveform_confidence, envelope_confidence)
        candidates.append((ref_start_seconds, offset, confidence))

    if not candidates:
        return None

    # Group into contiguous chronological runs of consistent offset. A run can span an
    # arbitrarily long matching stretch (e.g. an identical outro spanning a minute), so
    # clustering by proximity to a single peak-confidence point (as before) stopped the
    # search a few seconds into the match instead of following it all the way to its end.
    candidates.sort(key=lambda item: item[0])
    runs = [[candidates[0]]]
    max_gap = step_seconds * 3.0 + 1e-6
    for candidate in candidates[1:]:
        previous = runs[-1][-1]
        if (candidate[0] - previous[0] <= max_gap
                and abs(candidate[1] - previous[1]) <= agreement_seconds):
            runs[-1].append(candidate)
        else:
            runs.append([candidate])

    eligible_runs = [run for run in runs if len(run) >= min_consistent_matches]
    if not eligible_runs:
        return None
    chosen_run = eligible_runs[0] if prefer == 'earliest' else eligible_runs[-1]
    best = chosen_run[0] if prefer == 'earliest' else chosen_run[-1]
    best_confidence = max(candidate[2] for candidate in chosen_run)
    return AudioAnchor(
        reference_time=best[0],
        source_time=best[0] + best[1],
        offset_seconds=best[1],
        confidence=best_confidence,
        method="partial",
    )


def build_segment_recipe(anchors, reference_duration, source_tempo=1.0):
    """Convert audio anchors into per-segment source ranges and tempo factors.

    Anchor source times are expressed after FPS normalization. ``source_tempo``
    converts them back to the original source audio timeline. The returned
    ``tempo`` is the FFmpeg ``atempo`` value needed to make each extracted
    source interval occupy its reference interval.
    """
    if not anchors or reference_duration <= 0 or source_tempo <= 0:
        raise ValueError("anchors, reference duration, and source tempo are invalid")

    ordered = sorted(anchors, key=lambda anchor: anchor.reference_time)
    if ordered[0].reference_time > 1e-6:
        raise ValueError("anchors must start at reference time zero")

    recipes = []
    for current, following in zip(ordered, ordered[1:]):
        ref_start = max(0.0, current.reference_time)
        ref_end = min(reference_duration, following.reference_time)
        ref_duration = ref_end - ref_start
        source_start = current.source_time * source_tempo
        source_end = following.source_time * source_tempo
        source_duration = source_end - source_start
        if ref_duration <= 0 or source_duration <= 0:
            continue
        recipes.append(AudioSegmentRecipe(
            reference_start=ref_start,
            reference_end=ref_end,
            source_start=max(0.0, source_start),
            source_end=max(0.0, source_end),
            tempo=source_duration / ref_duration,
        ))
    if not recipes:
        raise ValueError("anchors do not define any positive-duration segment")
    return recipes


def align_audio(reference_path, source_path, reference_stream=0, source_stream=0,
                analyze_seconds=DEFAULT_ANALYZE_SECONDS,
                sample_rate=DEFAULT_SAMPLE_RATE, method="envelope",
                source_tempo=1.0, normalize_loudness=True):
    """Align two selected audio streams and return an :class:`AudioAlignment`.

    ``source_tempo`` can normalize a source recorded at a different video
    frame rate before alignment, for example ``960 / 1001`` for 25 FPS to
    24000/1001 FPS.
    
    ``normalize_loudness`` applies loudness normalization (-23 LUFS) to both
    streams before alignment. Set to False only if audio is already normalized.
    """
    if analyze_seconds <= 0:
        raise ValueError("analyze_seconds must be positive")
    if method not in ("waveform", "envelope"):
        raise ValueError("method must be 'waveform' or 'envelope'")

    reference = extract_mono_audio(
        reference_path, reference_stream, sample_rate, analyze_seconds, normalize_loudness=normalize_loudness)
    source = extract_mono_audio(
        source_path, source_stream, sample_rate, analyze_seconds,
        tempo=source_tempo, normalize_loudness=normalize_loudness)
    if len(reference) < sample_rate or len(source) < sample_rate:
        raise ValueError("Both audio streams must contain at least one second")

    if method == "waveform":
        offset, confidence = correlate_offset(reference, source, sample_rate)
    else:
        offset, confidence = correlate_envelope_offset(reference, source, sample_rate)
    return AudioAlignment(offset, confidence, method)