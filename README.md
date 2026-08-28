# AVSync — Audio/Video/Subtitle Synchronization Engine

![Python](https://img.shields.io/badge/Python-3.8+-yellow.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

A command-line tool that synchronizes a **foreign-language audio track** (and its subtitles) to a **reference video** using audio-to-audio alignment and precise per-segment timing. It aligns dubbed audio and subtitles from one release to the exact timeline of another, then muxes a clean output that preserves all of the reference video's original content.

Typical use case: you have an English reference release with correct timing, and a foreign release (e.g. Japanese or French) whose audio you want retimed to match the reference video frame-for-frame.

> **Fork notice.** This repository is a fork of [stinkybread/avsync](https://github.com/stinkybread/avsync). `v14` was the last upstream release before this fork diverged; everything described under [Audio Quality Preservation](#audio-quality-preservation), the audio-to-audio anchor pairing pipeline (`audio_alignment.py`, `--anchor_source audio`), and the bugfixes listed under [Maintenance / Bugfixes](#maintenance--bugfixes-post-v14-qc) were added here and are not part of upstream `v14`. The scope of these changes (a new module, a different default workflow, several behavior changes to defaults) is broad enough that this is maintained as an independent fork rather than a single upstream pull request; the self-contained bugfixes (channel-layout, `pts_time` parsing, VFR frame-rate detection) are reasonable candidates to upstream separately if useful to the original project.

## Primary Use Case

The scenario this tool is built for:

**File 1 (source of truth for audio/subtitles, low-quality video):**
- Low-quality video track
- Original-language audio track
- Foreign-language audio track(s) — 1, 2, 3...
- Subtitle track(s) — 1, 2, 3...

**File 2 (source of truth for video, high-quality release):**
- High-quality video track
- Original-language audio track

(Only the original-language audio track is required from each file — the foreign audio/subtitle tracks are optional and just need to exist in File 1.)

**Strategy:**
1. Compare File 1's original audio against File 2's original audio to detect every editorial difference between them (cuts, insertions, silence-length changes).
2. Apply the same corrections to every other track in File 1 (foreign audio tracks, subtitles), producing corrected copies retimed to File 2's timeline.

**Final File (the muxed output):**
- Video track — from File 2
- Original-language audio — from File 2
- Foreign-language audio track(s) — from File 1, corrected
- Subtitle track(s) — from File 1, corrected

(Only the tracks actually present in File 1 are included.)

In CLI terms: File 2 is the `reference` video, File 1 is the `foreign` video, `--anchor_source audio` drives the comparison, and `--foreign_tracks all` carries over every foreign audio track found in File 1. See [Quick Test](#quick-test) below for a runnable example.

## How It Works

1. **Audio Pairing (recommended)** — Matching original-audio streams are normalized for sample rate and optional FPS/tempo differences, then compared with waveform and energy-envelope correlation to build local anchors (`--anchor_source audio`). A consensus pass recovers a stable offset from concordant windows even when an editorially different opening makes individual windows ambiguous. Abrupt editorial changes (extra/missing shots) are binary-searched down to the exact instant and bracketed with a hard-cut pair of anchors, instead of being smeared across the whole 30-120s window between two anchors.
2. **Visual Pairing (alternative)** — When audio correlation is unavailable, scene-change frames can be extracted from both videos with FFmpeg and matched via OpenCV template matching (`TM_CCOEFF_NORMED`) to build timeline anchors. Frames are letterbox-resized (aspect ratio preserved, not stretched) before matching, so a low-resolution or non-16:9 source doesn't collapse match scores against a sharp reference.
3. **Audio Synchronization** — The foreign audio is split into segments defined by the anchors. Each segment is time-stretched (`atempo`) so its duration matches the corresponding reference segment, then concatenated and padded to align with the reference start. If a segment has no matching foreign content at all (e.g. the reference has a short intro/prefix the foreign recording lacks), the gap is filled from the reference audio **only when the cut lands on a natural pause**; otherwise it's filled with silence to avoid an audible mid-note interruption (see [Audio Quality Preservation](#audio-quality-preservation)).
4. **Subtitle Synchronization** — Text subtitles are retimed with the same per-segment mapping as the audio, including any silence insert/delete edits detected during audio pairing. Bitmap subtitles are passed through unchanged (see below).
5. **Muxing** — The reference video, its original audio, the synced foreign track(s), and subtitles are combined into the final file. On MKV, `mkvmerge` is used so all original streams, chapters, fonts, and metadata are preserved untouched. Foreign audio codec/bitrate default to `auto`, matching the source's own format (see below).

## Audio Quality Preservation

Earlier iterations of the audio-pairing workflow normalized loudness (`loudnorm`) on the same audio that was later used to build the final output, and always re-encoded the synced track to AAC 192k regardless of the source. Both behaviors have been separated out:

- **Pristine extraction.** Boundary/silence detection uses a resampled + loudness-normalized *analysis copy* of the audio, but the actual output segments are always cut from an untouched PCM copy of the source. The final track keeps the source's original dynamics/volume; normalization is never baked into the output.
- **Splice-safety check for reference-audio fills.** When a gap in the foreign audio has to be filled from the reference track (see above), the fill only happens if the cut point lands on a natural pause (measured via local RMS level). Otherwise the gap is filled with silence of the same duration, since a hard cut into arbitrary music/dialogue content is far more jarring than a brief silence.
- **Level-matching gain for reference-audio fills.** The two sources can differ in overall level even when the codec/format matches. When a gap *is* filled with reference audio, its mean volume (via ffmpeg `volumedetect`) is compared against the immediately surrounding foreign audio and a single linear gain (`volume=XdB`, clamped to ±15dB) is applied so the inserted clip doesn't suddenly sound much louder/quieter — this is a one-shot gain, not dynamic-range compression, so it doesn't alter the clip's dynamics.
- **Auto codec/bitrate matching.** `--mux_foreign_codec` / `--mux_foreign_bitrate` default to `auto`, which detects the foreign source's own codec and bitrate (via ffprobe) and re-encodes to match it, instead of forcing AAC 192k. Falls back to lossless FLAC if the source codec has no available encoder. Explicit values still override.
- **Optional enhancement filters.** `--audio_filters` accepts any ffmpeg `-af` chain (e.g. `loudnorm=I=-16:TP=-1.5:LRA=11` or a de-noise chain) applied only at final encode, fully opt-in — useful for genuinely low-quality sources (e.g. a 128k MP3) without imposing that processing on every run.

## What's New in v14

- **Anchor-and-follow frame matching.** The first frame is located with a wide search window (±6% of reference duration) to establish an anchor. Every subsequent frame is then searched only in a narrow window **0 to +10 seconds forward** of its estimated position (derived from the anchor offset). Combined with a foreign-frame cache and lower match resolution, this is dramatically faster than the previous fixed-window scan.
- **Lower template-match resolution (640×360).** Frames are compared at half the previous resolution. Whole-frame scene matching is unaffected in accuracy but much faster.
- **Native ASS/SSA subtitle preservation.** Styled subtitles keep their fonts, colors, positioning, and inline tags. Only `Dialogue:` timestamps are adjusted; the entire header, styles, and any `[Fonts]` sections are preserved verbatim. Non-ASS text subs are handled as SRT.
- **Bitmap subtitle pass-through (PGS / VobSub).** Image-based subtitles (`hdmv_pgs_subtitle`, `dvd_subtitle`, etc.) cannot be text-parsed or retimed. They are now copied through into the output unchanged, with a clear log warning that their timing matches the foreign source rather than the adjusted audio.
- **Visible dropped-subtitle logging.** Any subtitle falling outside the anchored segment range is logged individually — with its index, timestamps, a text preview, and the specific reason (before the first boundary, after the last, or in a gap) — instead of being silently discarded.
- **Audio-to-audio anchor pairing (`--anchor_source audio`).** A full alternative anchor-detection pipeline for pairs where visual template matching is unreliable (e.g. mismatched resolution/aspect ratio/compression), driven entirely by original-audio cross-correlation. See `audio_alignment.py` and [Audio Quality Preservation](#audio-quality-preservation).
- **Silence-based editorial recipe.** Matched silence intervals between reference and source (after FPS normalization) are converted into an explicit insert/delete recipe applied to the source audio timeline, and shared with subtitle retiming so both stay consistent.
- **Resilient segment processing.** If iterative per-segment time-stretching fails to converge, a direct time-slice fallback now recovers that single segment instead of aborting the whole run — applied consistently to the primary track, additional foreign tracks, and reference-audio gap fills.

### Maintenance / Bugfixes (post-v14 QC)

- Fixed broken stage-header log messages (control-character placeholders).
- Fixed ffmpeg concat demuxer path resolution (absolute paths are now used so processing no longer depends on the process working directory).
- Cache checkpoint version bumped to 14 for compatibility.
- Batch script: fixed a copy-paste bug that prevented detection of an empty foreign directory; improved episode-code matching diagnostics; the main engine is now located relative to the batch script itself.
- Fixed VFR/AVI frame-rate detection: uses `avg_frame_rate` instead of `r_frame_rate`, which can report a nonsensical value (e.g. a huge least-common-multiple fraction) for some Xvid/AVI files.
- Fixed scene-change `pts_time` parsing to scan ffmpeg's raw stderr text directly instead of splitting into lines first, which silently dropped most timestamps whenever ffmpeg's log output reflowed a single log record across line breaks.
- Fixed a silent mono/stereo corruption bug: `ffmpeg`'s `anullsrc` `cl=` parameter is a channel-layout name/mask, not a channel count — passing the raw channel count integer (`cl=2`) was silently generating mono silence, which corrupted playback speed/pitch for everything concatenated after it. All silence-generation now uses `cl=stereo`/`cl=mono`.

## Requirements

- **Python** 3.8+
- **FFmpeg** and **FFprobe** (full build with SoxR recommended for high-quality resampling)
- **MKVToolNix** (`mkvmerge`) for MKV muxing
- Python packages: see `requirements.txt` (OpenCV, NumPy, SciPy, tqdm; optional `imagehash` + `Pillow` for similarity filtering)

```bash
pip install -r requirements.txt
```

FFmpeg full builds: https://ffbinaries.com/downloads · MKVToolNix: https://mkvtoolnix.download/

## Usage

### Single pair

```bash
python AVSync_v14.py "reference.mkv" "foreign.mkv" "output.mkv"
```

You'll be prompted to pick reference and foreign audio streams. Pass `--auto_detect` to skip the prompts and select streams by language automatically.

```bash
python AVSync_v14.py ref.mkv foreign.mkv out.mkv \
    --ref_lang eng --foreign_lang jpn --auto_detect
```

For the audio-to-audio remaster workflow, select the matching original-audio streams explicitly or by language:

```bash
python AVSync_v14.py reference_hq.mkv source_with_subtitles.mkv output.mkv \
    --anchor_source audio --ref_lang eng --foreign_lang jpn --auto_detect
```

The `--ref_*` stream identifies the original audio in the high-quality reference file. The `--foreign_*` stream identifies the matching original audio in the source file; additional foreign tracks can be included with `--foreign_tracks all`.

## Quick Test

A minimal end-to-end run of the primary use case (see [Primary Use Case](#primary-use-case)), matching original audio explicitly by absolute stream index and carrying over every foreign track:

```bash
python AVSync_v14.py reference_hq.mkv source_lowquality.mkv output.mkv \
    --anchor_source audio \
    --ref_stream_idx 1 --foreign_stream_idx 1 --foreign_anchor_stream_idx 2 \
    --foreign_lang jpn --foreign_tracks all --verbose
```

- `--ref_stream_idx` / `--foreign_stream_idx`: absolute audio stream indices to use as the *original-audio* comparison pair (use `ffprobe` to find them if you don't already know them).
- `--foreign_anchor_stream_idx`: only needed when the foreign file's original-audio track (used for correlation) is a **different** stream than the one you want in the final output (e.g. correlate against a low-level AC3 original track while muxing an MP3 dub as `--foreign_stream_idx`).

**Recoding the output audio and applying enhancement filters** (default `auto` codec/bitrate just matches the source — use this only when you explicitly want to change format or clean up a low-quality track):

```bash
python AVSync_v14.py reference_hq.mkv source_lowquality.mkv output.mkv \
    --anchor_source audio --foreign_lang jpn --foreign_tracks all \
    --mux_foreign_codec libopus --mux_foreign_bitrate 128k \
    --audio_filters "loudnorm=I=-16:TP=-1.5:LRA=11,highpass=f=80"
```

### Batch (match by SxxExx episode code)

`AVSync_batch.py` pairs files across two folders by their `SxxExx` season/episode code (case-insensitive) rather than exact filename, then runs the engine on each pair. `--auto_detect` is injected automatically.

```bash
python AVSync_batch.py ./ref ./foreign ./output --foreign_lang jpn --foreign_tracks all
```

- Skips outputs that already exist unless `--overwrite` is given.
- All arguments after the three folders are passed straight through to `AVSync_v14.py`.
- A valid `--foreign_lang` is required in batch mode (the engine needs it for metadata tagging).

## Key Options

| Option | Default | Description |
|---|---|---|
| `--ref_lang` / `--foreign_lang` | `eng` / `foreign` | ISO 639-2 language codes (e.g. `eng`, `jpn`, `fre`, `hin`) |
| `--auto_detect` | off | Skip interactive prompts; select streams by language |
| `--foreign_tracks` | primary | `primary`, `all`, or comma-separated stream indices to sync |
| `--scene_threshold` | `0.25` | Scene-change sensitivity for frame extraction (0.0–1.0) |
| `--match_threshold` | `0.7` | Template-match acceptance threshold (0.0–1.0) |
| `--similarity_threshold` | `4` | Perceptual-hash dedup distance (`-1` to disable) |
| `--db_threshold` | `-40.0` | Audio content detection threshold in dB |
| `--min_segment_duration` | `5.0` | Minimum reference segment length in seconds |
| `--first_segment_adjust` / `--last_segment_adjust` | `0.0` | Manual timing nudge in **milliseconds** for the first/last segment |
| `--force_sync_points` | none | Manual anchor points for problem sections |
| `--anchor_source` | `visual` | `visual` (frame matching) or `audio` (cross-correlation, recommended for the audio-first remaster workflow) |
| `--foreign_anchor_stream_idx` | none | Absolute stream index of the foreign file's *original* audio, used only for anchor correlation (independent of `--foreign_stream_idx`, the track that ends up in the output) |
| `--mux_foreign_codec` / `--mux_foreign_bitrate` | `auto` / `auto` | Output codec/bitrate for synced foreign audio; `auto` matches the source's own codec/bitrate so quality is neither gained nor lost |
| `--audio_filters` | none | Optional ffmpeg `-af` chain applied to the synced foreign track before final encoding (opt-in enhancement, e.g. for a low-quality source) |
| `--no_subtitles` | off | Skip subtitle handling entirely |
| `--qc_output_dir` | none | Write side-by-side QC comparison images |
| `--output_csv` | none | Write an anchor/segment report |
| `--verbose` | off | Show DEBUG-level detail |

## Subtitle Behavior at a Glance

| Source format | Handling | Timing adjusted? |
|---|---|---|
| SRT / SubRip | Parsed, retimed, written as SRT | Yes |
| ASS / SSA | Parsed natively, styles preserved, retimed | Yes (timestamps only) |
| PGS (`hdmv_pgs_subtitle`) | Passed through unchanged | No — matches foreign source |
| VobSub (`dvd_subtitle`) | Passed through unchanged | No — matches foreign source |

Subtitles that fall outside the anchored segment range are dropped and logged individually with a reason.

## Tips for Best Results

- Both videos should contain essentially the same scenes. Different intros, ads, or missing scenes will reduce anchor quality.
- If no matches are found, lower `--scene_threshold` (e.g. `0.15`) and/or `--match_threshold` (e.g. `0.6`).
- Uniform, clear scene changes give the most reliable anchors.
- Use `--force_sync_points` for sections that consistently misalign.

## License

MIT — see [LICENSE](LICENSE).

## Credits
**Shout-Outs** [NP-Gaming]((https://github.com/NP-Gaming)
**Developer:** [Vaibhav Bhat](https://github.com/stinkybread)
Built with FFmpeg, OpenCV, SciPy, and MKVToolNix.
