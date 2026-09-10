# AVSync — Audio-to-Audio Dubbing Synchronization Engine

![Python](https://img.shields.io/badge/Python-3.8+-yellow.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

A command-line tool for moving a dubbed audio track from one release onto the timeline of another. It compares matching original-language audio streams, detects FPS drift and editorial differences, retimes the selected dub track, fills reference-only gaps when needed, and muxes the result into the reference video while preserving the reference file's streams, chapters, fonts, and metadata.

The current production path is **audio-to-audio synchronization** (`--anchor_source audio`). It is designed for remasters, international releases, DVD/WEB timing differences, eyecatches, censored shots, and other cases where both files still contain comparable original-language audio.

> **Fork notice.** This repository is a fork of [stinkybread/avsync](https://github.com/stinkybread/avsync). `v14` was the last upstream release before this fork diverged; everything described under [Audio Quality Preservation](#audio-quality-preservation), the audio-to-audio anchor pairing pipeline (`audio_alignment.py`, `--anchor_source audio`), and the bugfixes listed under [Maintenance / Bugfixes](#maintenance--bugfixes-post-v14-qc) were added here and are not part of upstream `v14`. The scope of these changes (a new module, a different default workflow, several behavior changes to defaults) is broad enough that this is maintained as an independent fork rather than a single upstream pull request; the self-contained bugfixes (channel-layout, `pts_time` parsing, VFR frame-rate detection) are reasonable candidates to upstream separately if useful to the original project.

## Current Status

- **Audio-to-audio sync:** primary supported workflow.
- **Validated real-world batch:** 53/53 One Piece episodes completed successfully by anchoring Japanese-to-Japanese, syncing the Netflix Spanish dub, and filling reference-only eyecatches from the reference track.
- **Video-to-video sync:** available as the older visual-anchor path, with a new analysis roadmap underway. It is useful when comparable audio is unavailable, but it is not yet as robust as audio-to-audio for editorial differences.

## Primary Workflow

The scenario this tool is built for:

**Source file (source of truth for the dub/subtitles):**
- Low-quality video track
- Original-language audio track
- Foreign-language audio track(s) — 1, 2, 3...
- Subtitle track(s) — 1, 2, 3...

**Reference file (source of truth for video/timing):**
- High-quality video track
- Original-language audio track

(Only the original-language audio track is required from each file — the dub/subtitle tracks are optional and just need to exist in the source file.)

**Strategy:**
1. Compare the source original audio against the reference original audio.
2. Normalize FPS/tempo differences onto the reference timeline.
3. Detect editorial differences: cuts, insertions, missing eyecatches, silence-length changes, and source/reference-only endings.
4. Apply the resulting recipe to the selected dub track(s) and subtitles.

**Final File (the muxed output):**
- Video track — from the reference file
- Original-language audio — from the reference file
- Foreign-language audio track(s) — from the source file, corrected
- Subtitle track(s) — from the source file, corrected

(Only the tracks actually present in the source file are included.)

In CLI terms: the high-quality/timing file is `reference`, the file containing the dub is `foreign` or `source`, `--anchor_source audio` drives the comparison, `--foreign_anchor_stream_idx` selects the comparable original-language source stream, and `--foreign_stream_idx` selects the dub that will be included in the output. See [Usage](#usage) for runnable examples.

## How It Works

1. **Audio Pairing (primary path)** — Matching original-audio streams are normalized for sample rate and optional FPS/tempo differences, then compared with waveform and energy-envelope correlation to build local anchors (`--anchor_source audio`). A consensus pass recovers a stable offset from concordant windows even when an editorially different opening makes individual windows ambiguous. Abrupt editorial changes (extra/missing shots) are localized with local offset probes and bracketed with transition or replacement anchors, instead of being smeared across the whole 30-120s window between two anchors.
2. **Visual Pairing (fallback / roadmap path)** — When audio correlation is unavailable, scene-change frames can be extracted from both videos with FFmpeg and matched via OpenCV template matching (`TM_CCOEFF_NORMED`) to build timeline anchors. Frames are letterbox-resized (aspect ratio preserved, not stretched) before matching, so a low-resolution or non-16:9 source doesn't collapse match scores against a sharp reference. This path is being expanded toward a full visual editorial recipe; see [Roadmap: Video-to-Video Synchronization](#roadmap-video-to-video-synchronization).
3. **Audio Synchronization** — The foreign audio is split into segments defined by the anchors. Each segment is time-stretched (`atempo`) so its duration matches the corresponding reference segment, then concatenated and padded to align with the reference start. If a segment has no matching foreign content at all (e.g. an eyecatch, censored shot, missing original-language line, or reference-only ending), the gap can be filled from the reference audio or with silence according to `--missing_foreign_fill` (see [Reference-Only Intervals / Eyecatches](#reference-only-intervals--eyecatches)).
4. **Subtitle Synchronization** — Text subtitles are retimed with the same per-segment mapping as the audio, including any silence insert/delete edits detected during audio pairing. Bitmap subtitles are passed through unchanged (see below).
5. **Muxing** — The reference video, its original audio, the synced foreign track(s), and subtitles are combined into the final file. On MKV, `mkvmerge` is used so all original streams, chapters, fonts, and metadata are preserved untouched. Foreign audio codec/bitrate default to `auto`, matching the source's own format (see below).

## Audio Quality Preservation

Earlier iterations of the audio-pairing workflow normalized loudness (`loudnorm`) on the same audio that was later used to build the final output, and always re-encoded the synced track to AAC 192k regardless of the source. Both behaviors have been separated out:

- **Pristine extraction.** Boundary/silence detection uses a resampled + loudness-normalized *analysis copy* of the audio, but the actual output segments are always cut from an untouched PCM copy of the source. The final track keeps the source's original dynamics/volume; normalization is never baked into the output.
- **Splice-safety check for reference-audio fills.** When `--missing_foreign_fill auto` is used and a gap in the foreign audio has to be filled from the reference track (see above), the fill only happens if the cut point lands on a natural pause (measured via local RMS level). Otherwise the gap is filled with silence of the same duration, since a hard cut into arbitrary music/dialogue content is far more jarring than a brief silence. Use `--missing_foreign_fill reference` when reference-only content must be preserved, such as eyecatches or undubbed/censored sections.
- **Level-matching gain for reference-audio fills.** The two sources can differ in overall level even when the codec/format matches. When a gap *is* filled with reference audio, its mean volume (via ffmpeg `volumedetect`) is compared against the immediately surrounding foreign audio and a single linear gain (`volume=XdB`, clamped to ±15dB) is applied so the inserted clip doesn't suddenly sound much louder/quieter — this is a one-shot gain, not dynamic-range compression, so it doesn't alter the clip's dynamics.
- **Auto codec/bitrate matching.** `--mux_foreign_codec` / `--mux_foreign_bitrate` default to `auto`, which detects the foreign source's own codec and bitrate (via ffprobe) and re-encodes to match it, instead of forcing AAC 192k. Falls back to lossless FLAC if the source codec has no available encoder. Explicit values still override.
- **Optional enhancement filters.** `--audio_filters` accepts any ffmpeg `-af` chain (e.g. `loudnorm=I=-16:TP=-1.5:LRA=11` or a de-noise chain) applied only at final encode, fully opt-in — useful for genuinely low-quality sources (e.g. a 128k MP3) without imposing that processing on every run.

## Reference-Only Intervals / Eyecatches

Audio-to-audio mode can detect material that exists in the reference timeline but is missing from the source timeline. Typical examples are broadcast eyecatches, censored shots, parts left undubbed in one release, and reference endings not present in the source.

Detection is driven by abrupt offset jumps between the matching original-audio streams. A negative jump means the source timeline has skipped over content that still exists in the reference. The engine then:

1. Confirms the previous and next offset states with local probes.
2. Accepts an early confirmed post-cut probe when a quiet musical pickup would otherwise delay detection until the loud body of the cue.
3. Refines the replacement start with the reference audio envelope, preserving a small pre-roll for low-level musical intros.
4. Refines the replacement end against a nearby low-energy point so the inserted reference clip does not end mid-note.
5. Inserts a pair of forced anchors with identical source time, so the reference-only interval renders as a real segment rather than being stretched across neighboring dialogue.
6. Repeats the same process for every detected interval in the episode; it is not limited to one eyecatch.

For cases where the reference-only content should be kept, use:

```bash
python AVSync_v14.py reference.mkv source.mkv output.mkv \
    --anchor_source audio \
    --ref_stream_idx 9 \
    --foreign_stream_idx 1 \
    --foreign_anchor_stream_idx 2 \
    --foreign_tracks 1 \
    --foreign_lang spa \
    --missing_foreign_fill reference \
    --no_subtitles \
    --output_csv segments.csv \
    --anchor_report_csv anchors.csv \
    --transition_report_csv transitions.csv
```

The One Piece Netflix/Arait workflow validated this pattern by anchoring Japanese-to-Japanese (`reference #9` vs `source #2`), syncing the Netflix Spanish track (`source #1`), and filling missing eyecatches from the reference Japanese track. A 53-episode batch completed successfully with this strategy.

## Feature Summary

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
- Cache checkpoint version bumped to 20 as the audio-audio recipe and reference-fill behavior evolved.
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

### Recommended audio-to-audio workflow

```bash
python AVSync_v14.py reference_hq.mkv source_with_dub.mkv output.mkv \
    --anchor_source audio \
    --ref_stream_idx 1 \
    --foreign_stream_idx 1 \
    --foreign_anchor_stream_idx 2 \
    --foreign_lang spa \
    --foreign_tracks 1
```

Use `ffprobe` to identify the stream indices before running:

- `--ref_stream_idx`: original-language audio in the reference file.
- `--foreign_anchor_stream_idx`: matching original-language audio in the source file, used only for correlation.
- `--foreign_stream_idx`: dub track from the source file that should be retimed and added to the output.

If the same source stream is both the comparable original audio and the output track, omit `--foreign_anchor_stream_idx`.

### Language-based selection

```bash
python AVSync_v14.py ref.mkv foreign.mkv out.mkv \
    --anchor_source audio --ref_lang eng --foreign_lang jpn --auto_detect
```

### Reference-only intervals

When the reference contains material absent from the source, such as eyecatches or censored/undubbed sections, preserve that reference content with:

```bash
python AVSync_v14.py reference_hq.mkv source_with_dub.mkv output.mkv \
    --anchor_source audio \
    --ref_stream_idx 9 \
    --foreign_stream_idx 1 \
    --foreign_anchor_stream_idx 2 \
    --foreign_lang spa \
    --missing_foreign_fill reference
```

### Visual fallback

When comparable original-language audio is unavailable, the older visual-anchor path can still be used:

```bash
python AVSync_v14.py reference.mkv source.mkv output.mkv \
    --anchor_source visual --foreign_lang spa --auto_detect
```

For analysis-only visual mapping, see [Roadmap: Video-to-Video Synchronization](#roadmap-video-to-video-synchronization).

## Examples

### Sync all source audio tracks

Carry every source audio track through the same audio-to-audio timing recipe:

```bash
python AVSync_v14.py reference_hq.mkv source_lowquality.mkv output.mkv \
    --anchor_source audio \
    --ref_stream_idx 1 --foreign_stream_idx 1 --foreign_anchor_stream_idx 2 \
    --foreign_lang jpn --foreign_tracks all --verbose
```

### Recode the output audio and apply enhancement filters

The default `auto` codec/bitrate matches the source. Use explicit values only when you want to change format or clean up a low-quality track:

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
| `--missing_foreign_fill` | `auto` | How to render reference-only intervals found by audio-audio sync: `auto` uses reference audio only at safe splice points, `reference` always inserts reference audio, `silence` always uses silence |
| `--mux_foreign_codec` / `--mux_foreign_bitrate` | `auto` / `auto` | Output codec/bitrate for synced foreign audio; `auto` matches the source's own codec/bitrate so quality is neither gained nor lost |
| `--audio_filters` | none | Optional ffmpeg `-af` chain applied to the synced foreign track before final encoding (opt-in enhancement, e.g. for a low-quality source) |
| `--no_subtitles` | off | Skip subtitle handling entirely |
| `--qc_output_dir` | none | Write side-by-side QC comparison images |
| `--output_csv` | none | Write an anchor/segment report |
| `--visual_map_only` | off | Run visual scene-map analysis and exit without syncing audio or muxing |
| `--visual_map_report_csv` | none | Write a visual map CSV with raw and FPS-normalized scene-change timestamps |
| `--visual_map_qc_dir` | none | Optional with `--visual_map_report_csv`; keep extracted scene frames for manual review |
| `--verbose` | off | Show DEBUG-level detail |

## Subtitle Behavior at a Glance

| Source format | Handling | Timing adjusted? |
|---|---|---|
| SRT / SubRip | Parsed, retimed, written as SRT | Yes |
| ASS / SSA | Parsed natively, styles preserved, retimed | Yes (timestamps only) |
| PGS (`hdmv_pgs_subtitle`) | Passed through unchanged | No — matches foreign source |
| VobSub (`dvd_subtitle`) | Passed through unchanged | No — matches foreign source |

Subtitles that fall outside the anchored segment range are dropped and logged individually with a reason.

## Roadmap: Video-to-Video Synchronization

The current visual route is still useful when comparable original audio is unavailable, but it should eventually produce the same kind of explicit editorial recipe as audio-to-audio mode instead of only returning visual anchors. The planned incremental path is:

1. **FPS-normalized visual timeline.** Reuse the `compute_auto_source_tempo()` idea from audio-audio and compare source scene timestamps on the reference timeline (`source_time / source_tempo`) without re-encoding the video.
2. **Scene-change classification.** For every scene-change frame, inspect frames immediately before and after the change and classify it as black-to-image, image-to-black, image-to-image, fade-in, fade-out, or ambiguous. Use luminance, dark-pixel ratio, and frame-difference scores, not just FFmpeg's `scene` value.
3. **Visual map CSV.** Emit a report containing normalized timestamp, scene type, black interval duration, representative frame, match score, and neighboring scene duration for both reference and source.
4. **Dense visual offset curve.** Between accepted visual anchors, sample local frames every 1-2 seconds and estimate the local visual offset. This mirrors the audio transition report and exposes drift, eyecatches, censures, and source-only/reference-only intervals.
5. **Abrupt visual transition detection.** Detect stable-before and stable-after offset states. Treat large jumps as editorial candidates rather than stretching the entire segment between coarse anchors.
6. **Visual transition refinement.** Locate the cut using local frame evidence: first confirmed post-cut frame, last confirmed pre-cut frame, black/fade boundaries, and nearest low-motion/black interval.
7. **Shared editorial recipe.** Convert visual-only findings into the same replacement/hard-cut structure used by audio-audio (`reference_start`, `reference_end`, `source_splice_time`, confidence, reason), so the existing audio segment renderer can apply it.
8. **Multiple intervals per episode.** Support any number of reference-only, source-only, or differently edited intervals in one episode, ordered and de-overlapped before rendering.
9. **QC-first rollout.** Start as analysis-only CSV/QC images, validate known problem episodes, then enable recipe application once the visual offset curve is trustworthy.

Step 1 is available as an analysis-only command:

```bash
python AVSync_v14.py reference.mkv source.mkv \
    --visual_map_only \
    --visual_map_report_csv visual_map.csv \
    --visual_map_qc_dir visual_map_frames
```

This extracts scene-change frames from both videos, writes raw and FPS-normalized timestamps, and exits without syncing audio or muxing. Omit `--visual_map_qc_dir` to keep only the CSV.

## Funding / GitHub Sponsors

AVSync is currently driven by the validated audio-to-audio workflow: it already handles real-world remastering cases, batch repairs, and reference-only interval preservation across difficult episodes. That is the stable product and the primary focus for publication.

The next funded milestone is video-to-video synchronization: turning the visual scene map into a full editorial recipe for cuts, replacements, and drift corrections, with QC coverage and a smoother batch workflow behind it.

Supporting the project helps fund:

- **VV sync research and refinement** — more reliable scene maps, cut localization, and confidence scoring.
- **QC and validation tooling** — visual reports, per-episode checks, and better failure diagnostics.
- **Batch and workflow polish** — automation for episode lists, repeatable pipelines, and practical production ergonomics.
- **Release maintenance** — documentation, packaging, and long-term stability of the AA pipeline.

GitHub Sponsors is the natural next step to fund the roadmap without compromising the current AA product. Once the sponsor page is activated in the repository, the link can be added here to make the funding call-to-action visible from the main project page.

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
