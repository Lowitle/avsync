import cv2
import os
import shutil
import subprocess
from tqdm import tqdm
import glob
import concurrent.futures
import numpy as np
import argparse
import re
import tempfile
import sys
from scipy.io import wavfile
import time
import json
import logging
import csv
import platform
import ctypes
import pickle
import hashlib

# --- DUAL LOGGING SYSTEM ---
# File logger: detailed output to log file
# Console logger: minimal output with progress indicators

class FileFormatter(logging.Formatter):
    """Detailed formatter for log file"""
    def __init__(self):
        super().__init__('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')


class ConsoleFormatter(logging.Formatter):
    """Minimal formatter for console - ASCII safe"""
    
    ANSI_COLORS = {
        'RESET': '\033[0m', 'RED': '\033[31m', 'GREEN': '\033[32m',
        'YELLOW': '\033[33m', 'CYAN': '\033[36m', 'BOLD': '\033[1m', 'DIM': '\033[2m',
    }
    NO_COLORS = {k: '' for k in ANSI_COLORS.keys()}

    def __init__(self):
        super().__init__('%(message)s')
        self.use_colors = self._init_colors()

    def _init_colors(self):
        """Initialize color support"""
        if os.getenv('NO_COLOR'):
            self.colors = self.NO_COLORS
            return False
        if platform.system() == 'Windows':
            try:
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.GetStdHandle(-11)
                if handle and handle != -1:
                    mode = ctypes.c_uint32()
                    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
                        self.colors = self.ANSI_COLORS
                        return True
            except: pass
            self.colors = self.NO_COLORS
            return False
        self.colors = self.ANSI_COLORS
        return True

    def format(self, record):
        msg = super().format(record)
        c = self.colors
        
        # Only show important messages on console
        if record.levelno >= logging.ERROR:
            return f"{c['RED']}{c['BOLD']}[ERROR] {msg}{c['RESET']}"
        elif record.levelno >= logging.WARNING:
            return f"{c['YELLOW']}[WARN] {msg}{c['RESET']}"
        elif record.levelno >= logging.INFO:
            # Stage headers
            if msg.startswith("=====") or msg.startswith("---"):
                return f"{c['CYAN']}{c['BOLD']}{msg}{c['RESET']}"
            # Success messages
            if "[OK]" in msg or "SUCCESS" in msg.upper():
                return f"{c['GREEN']}{msg}{c['RESET']}"
            # Cache messages
            if "[CACHE]" in msg:
                return f"{c['DIM']}{msg}{c['RESET']}"
            return msg
        return msg


# Custom filter to suppress verbose messages on console
class ConsoleFilter(logging.Filter):
    """Filter out verbose messages from console output"""
    
    # Class variable to control warning display (set via --show-warnings)
    show_warnings = False
    
    VERBOSE_PATTERNS = [
        "Iter ",              # Individual iteration details
        "-> Running:",        # FFmpeg command starts
        "-> Completed:",      # FFmpeg command completions
        "Speed=",             # Speed adjustments
        "Duration=",          # Duration checks
        "MinRefDur Filter",
        "MaxDiff Filter",
        "Skipping visual anchor",
        "Process Segment",
        "Trim Segment",
        "Create Silence",
        "Add Silence Pad",
        "segment duration",
        # Additional patterns for clean progress bar (no scrolling)
        "-> Segment ",        # Segment processing details
        "Segment ",           # All segment-related messages
        "Target=",            # Target duration info
        "Achieved target",    # Success messages per segment
        "First segment:",     # First segment adjustments
        "Last segment:",      # Last segment adjustments
        "Adjusting speed",    # Speed adjustment debug
        "silence pad",        # Silence padding messages
    ]
    
    def filter(self, record):
        msg = record.getMessage()
        # Errors always shown
        if record.levelno >= logging.ERROR:
            return True
        # Warnings only shown if --show-warnings is set
        if record.levelno >= logging.WARNING:
            return ConsoleFilter.show_warnings
        # Filter out verbose patterns for INFO level
        for pattern in self.VERBOSE_PATTERNS:
            if pattern in msg:
                return False
        return True


def setup_logging(log_file=None, verbose=False):
    """
    Set up dual logging: file (detailed) + console (minimal).
    
    Args:
        log_file: Path to log file. If None, uses default based on output.
        verbose: If True, console shows all messages (like old behavior)
    
    Returns:
        tuple: (logger, log_file_path)
    """
    logger = logging.getLogger('avsync')
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    
    # File handler - always detailed
    if log_file:
        file_handler = logging.FileHandler(log_file, mode='w', encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(FileFormatter())
        logger.addHandler(file_handler)
    
    # Console handler - minimal by default
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(ConsoleFormatter())
    if not verbose:
        console_handler.addFilter(ConsoleFilter())
    logger.addHandler(console_handler)
    
    return logger, log_file


# Global logger placeholder
logger = None


def get_log_path(output_video):
    """Generate log file path based on output video"""
    output_dir = os.path.dirname(os.path.abspath(output_video))
    output_name = os.path.splitext(os.path.basename(output_video))[0]
    return os.path.join(output_dir, f"{output_name}_avsync.log")


# --- PROGRESS BAR HELPERS ---
class SegmentProgressBar:
    """Progress bar for segment processing"""
    
    def __init__(self, total_segments, desc="Processing segments"):
        self.pbar = tqdm(
            total=total_segments,
            desc=desc,
            unit="seg",
            ncols=80,
            bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]'
        )
        self.current = 0
        self.warnings = 0
        self.corrections = 0
    
    def update(self, correction_applied=False):
        """Update progress bar"""
        self.current += 1
        if correction_applied:
            self.corrections += 1
        self.pbar.update(1)
    
    def set_warning(self):
        """Increment warning counter"""
        self.warnings += 1
    
    def close(self):
        """Close progress bar and show summary"""
        self.pbar.close()
        if self.corrections > 0:
            print(f"  -> {self.corrections} segments needed final correction")


# --- END OF LOGGING SYSTEM ---

# --- CACHING FUNCTIONS ---
def get_file_hash(filepath):
    """Calculate SHA256 hash of file for cache validation"""
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def get_cache_key(args):
    """
    Generate cache key based on inputs and detection parameters.
    Excludes adjustment parameters that don't affect detection.
    """
    cache_params = {
        'ref_video': get_file_hash(args.ref_video) if os.path.exists(args.ref_video) else None,
        'foreign_video': get_file_hash(args.foreign_video) if os.path.exists(args.foreign_video) else None,
        'scene_threshold': args.scene_threshold,
        'match_threshold': args.match_threshold,
        'similarity_threshold': args.similarity_threshold,
        'db_threshold': args.db_threshold,
        'min_segment_duration': args.min_segment_duration,
        'ref_lang': args.ref_lang,
        'foreign_lang': args.foreign_lang,
        'anchor_source': getattr(args, 'anchor_source', 'visual'),
        'audio_anchor_window': getattr(args, 'audio_anchor_window', None),
        'audio_anchor_step': getattr(args, 'audio_anchor_step', None),
        'audio_anchor_min_confidence': getattr(args, 'audio_anchor_min_confidence', None),
        'audio_anchor_search_radius': getattr(args, 'audio_anchor_search_radius', None),
        'source_tempo': getattr(args, 'source_tempo', None),
        'foreign_anchor_stream_idx': getattr(args, 'foreign_anchor_stream_idx', None),
    }
    
    cache_str = json.dumps(cache_params, sort_keys=True)
    return hashlib.sha256(cache_str.encode()).hexdigest()


def get_cache_path(args):
    """Get cache file path based on output video name"""
    output_dir = os.path.dirname(os.path.abspath(args.output_video))
    output_name = os.path.splitext(os.path.basename(args.output_video))[0]
    cache_dir = os.path.join(output_dir, f".avsync_cache")
    os.makedirs(cache_dir, exist_ok=True)
    
    cache_key = get_cache_key(args)
    cache_file = os.path.join(cache_dir, f"{output_name}_{cache_key[:16]}.pkl")
    return cache_file


def save_checkpoint(cache_path, visual_anchors_details):
    """Save checkpoint data to disk"""
    checkpoint_data = {
        'version': 17,
        'visual_anchors_details': visual_anchors_details,
        # Anchor pairing also derives this global state (gap-fill ranges, silence-based
        # editorial edits); it must travel with the anchors or segment processing breaks
        # when a cache hit skips run_audio_pairing_stage() entirely.
        'audio_replacement_ranges': AUDIO_REPLACEMENT_RANGES,
        'audio_hard_cut_ranges': AUDIO_HARD_CUT_RANGES,
        'audio_editorial_edits': AUDIO_EDITORIAL_EDITS,
        'audio_editorial_source_tempo': AUDIO_EDITORIAL_SOURCE_TEMPO,
        'timestamp': time.time()
    }
    
    try:
        with open(cache_path, 'wb') as f:
            pickle.dump(checkpoint_data, f)
        logger.info(f"[CACHE] Checkpoint saved to {cache_path}")
        return True
    except Exception as e:
        logger.warning(f"Failed to save checkpoint: {e}")
        return False


def load_checkpoint(cache_path):
    """Load checkpoint data from disk"""
    if not os.path.exists(cache_path):
        return None
    
    try:
        with open(cache_path, 'rb') as f:
            checkpoint_data = pickle.load(f)
        
        if checkpoint_data.get('version') != 15:
            logger.warning(f"[CACHE] Version mismatch, ignoring cache")
            return None
            
        logger.info(f"[CACHE] Loaded checkpoint from {cache_path}")
        cache_age = time.time() - checkpoint_data.get('timestamp', 0)
        logger.info(f"[CACHE] Age: {cache_age/3600:.1f} hours")
        
        return checkpoint_data
    except Exception as e:
        logger.warning(f"Failed to load checkpoint: {e}")
        return None
# --- END CACHING FUNCTIONS ---


# --- FORCED SYNC POINT FUNCTIONS ---
def parse_timestamp_to_seconds(timestamp_str):
    """
    Parse HH:MM:SS:MS timestamp format to seconds.
    
    Format: HH:MM:SS:MS (e.g., "00:01:30:500" = 1 minute, 30 seconds, 500 milliseconds)
    
    Returns:
        float: Time in seconds, or None if parsing fails.
    """
    timestamp_str = timestamp_str.strip()
    parts = timestamp_str.split(':')
    
    if len(parts) != 4:
        logger.warning(f"Invalid timestamp format '{timestamp_str}' (expected HH:MM:SS:MS)")
        return None
    
    try:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2])
        milliseconds = int(parts[3])
        
        if minutes >= 60 or seconds >= 60 or milliseconds >= 1000:
            logger.warning(f"Invalid timestamp values in '{timestamp_str}' (MM<60, SS<60, MS<1000)")
            return None
        
        if hours < 0 or minutes < 0 or seconds < 0 or milliseconds < 0:
            logger.warning(f"Timestamp values must be non-negative in '{timestamp_str}'")
            return None
        
        total_seconds = hours * 3600 + minutes * 60 + seconds + milliseconds / 1000.0
        return total_seconds
        
    except ValueError as e:
        logger.warning(f"Could not parse timestamp '{timestamp_str}': {e}")
        return None


def parse_force_sync_points(force_sync_str):
    """
    Parse forced sync points from command line argument.
    
    Format: "HH:MM:SS:MS>HH:MM:SS:MS,HH:MM:SS:MS>HH:MM:SS:MS,..."
    Example: "00:00:10:500>00:00:10:800,00:02:00:300>00:02:01:000"
    
    The '>' separates reference time from foreign time.
    Multiple sync points are comma-separated.
    
    Returns:
        List of tuples: [(ref_time_seconds, foreign_time_seconds), ...]
        Returns empty list if parsing fails or input is None/empty.
    """
    if not force_sync_str:
        return []
    
    sync_points = []
    pairs = force_sync_str.strip().split(',')
    
    for i, pair in enumerate(pairs):
        pair = pair.strip()
        if not pair:
            continue
        
        if '>' not in pair:
            logger.warning(f"Invalid sync point format '{pair}' (expected 'HH:MM:SS:MS>HH:MM:SS:MS'). Skipping.")
            continue
        
        try:
            parts = pair.split('>')
            if len(parts) != 2:
                logger.warning(f"Invalid sync point format '{pair}' (expected exactly 2 timestamps separated by '>'). Skipping.")
                continue
            
            ref_time = parse_timestamp_to_seconds(parts[0])
            foreign_time = parse_timestamp_to_seconds(parts[1])
            
            if ref_time is None or foreign_time is None:
                logger.warning(f"Skipping sync point '{pair}' due to timestamp parsing errors.")
                continue
            
            sync_points.append((ref_time, foreign_time))
            logger.debug(f"  Parsed forced sync point {i+1}: Ref={ref_time:.3f}s, Foreign={foreign_time:.3f}s")
            
        except Exception as e:
            logger.warning(f"Could not parse sync point '{pair}': {e}. Skipping.")
            continue
    
    return sync_points


def inject_forced_sync_points(visual_anchors_details, forced_sync_points):
    """
    Inject forced sync points into visual_anchors_details list.
    
    Forced sync points are given synthetic filenames starting with 'FORCED_SYNC_' 
    to distinguish them from scene-detected anchors. This prefix is used later
    to ensure they bypass filtering.
    
    Args:
        visual_anchors_details: List of tuples (ref_filename, foreign_filename, ref_time, foreign_time)
        forced_sync_points: List of tuples (ref_time, foreign_time)
    
    Returns:
        New list with forced sync points injected, sorted by reference time.
    """
    if not forced_sync_points:
        return visual_anchors_details
    
    logger.info(f"--- Injecting {len(forced_sync_points)} Forced Sync Points ---")
    
    # Create a mutable copy
    combined = list(visual_anchors_details) if visual_anchors_details else []
    
    # Add forced sync points with synthetic filenames
    for i, (ref_time, foreign_time) in enumerate(forced_sync_points):
        synthetic_ref_name = f"FORCED_SYNC_{i+1:03d}_ref"
        synthetic_foreign_name = f"FORCED_SYNC_{i+1:03d}_foreign"
        combined.append((synthetic_ref_name, synthetic_foreign_name, ref_time, foreign_time))
        logger.info(f"  > Injected forced sync point {i+1}: Ref={format_time(ref_time)} ({ref_time:.3f}s) -> Foreign={format_time(foreign_time)} ({foreign_time:.3f}s)")
    
    # Sort by reference time (index 2 in the tuple)
    combined.sort(key=lambda x: x[2])
    
    logger.info(f"  -> Total anchors after injection: {len(combined)} ({len(forced_sync_points)} forced + {len(visual_anchors_details) if visual_anchors_details else 0} detected)")
    
    return combined
# --- END FORCED SYNC POINT FUNCTIONS ---



# --- Try Importing Image Similarity Libs ---
try:
    import imagehash
    from PIL import Image
    similarity_libs_available = True
except ImportError:
    print("Warning: 'imagehash' or 'Pillow' not found. Similarity filtering will be skipped.")
    similarity_libs_available = False

# --- Constants ---
RESIZE_WIDTH = 640
RESIZE_HEIGHT = 360
DEFAULT_DB_THRESHOLD = -40.0
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_CHANNELS = 2
MIN_ATEMPO = 0.5
MAX_ATEMPO = 100.0
# MIN_ALLOWED_REF_DURATION_S = 5.0 # Replaced by argument --min_segment_duration
DEFAULT_MIN_SEGMENT_DURATION = 5.0 # New default value
MAX_ALLOWED_DURATION_PERCENT_DIFF = 6.0 # Max % difference allowed between ref/foreign segment durations
MIN_DELAY_S = 0.001 # Minimum delay to apply padding
DEFAULT_REF_LANG = "eng"
DEFAULT_FOREIGN_LANG = "foreign" # Changed from "hin"
DEFAULT_MUX_ACODEC = "auto"
DEFAULT_MUX_ABITRATE = "auto"
DEFAULT_MUX_ABITRATE_FALLBACK = "192k"  # used only if source bitrate can't be detected
QC_IMAGE_HEIGHT = 720
FFMPEG_EXEC = None
FFPROBE_EXEC = None
MKVMERGE_EXEC = None
MATCH_WINDOW_PERCENT = 0.06 # Percentage of ref video duration for INITIAL anchor search window
ANCHOR_FOLLOW_FORWARD_WINDOW_S = 10.0 # Seconds forward from estimated position for subsequent matches
AUDIO_REPLACEMENT_RANGES = [] # Reference intervals to fill from the reference audio when foreign content is missing
AUDIO_HARD_CUT_RANGES = [] # Near-instant reference ranges that delete extra source timeline material
AUDIO_EDITORIAL_EDITS = [] # Explicit source-timeline edits shared with subtitle retiming
AUDIO_EDITORIAL_SOURCE_TEMPO = 1.0
THRESHOLD_CALIBRATION_LOG = [] # (label, noise_floor_db, threshold_db) rows for --threshold_calibration_csv

# Logger will be initialized in main() with proper file output
# Use a temporary logger for early errors
logger = logging.getLogger('avsync')
logger.setLevel(logging.INFO)
if not logger.handlers:
    _temp_handler = logging.StreamHandler(sys.stdout)
    _temp_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(_temp_handler)

# --- Utility & FFmpeg/FFprobe Functions ---
def find_executable(name):
    """Finds an executable in the system PATH."""
    exec_path = shutil.which(name)
    if exec_path:
        logger.debug(f"Found executable '{name}' at: {exec_path}")
        return exec_path
    else:
        logger.error(f"'{name}' command not found in system PATH.")
        return None

def run_ffmpeg(cmd_list, desc="FFmpeg Task", verbose_success=False, capture_stderr=False):
    """Runs an FFmpeg command with logging and error handling."""
    global FFMPEG_EXEC
    if not FFMPEG_EXEC:
        logger.error("FFmpeg path not set.")
        return False, "" if capture_stderr else False
    cmd_list[0] = FFMPEG_EXEC
    logger.info(f"  -> Running: {desc}...")
    start_time = time.time()
    stderr_output = ""
    try:
        # Use Popen for better handling of large stderr, prevent potential deadlocks
        process = subprocess.Popen(cmd_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8', errors='ignore', startupinfo=None)
        stdout, stderr = process.communicate() # Wait for process to finish
        returncode = process.returncode
        elapsed_time = time.time() - start_time

        if capture_stderr:
            stderr_output = stderr

        if returncode != 0:
            logger.error(f"\nERROR: {desc} failed! (Exit code: {returncode}, Time: {elapsed_time:.2f}s)")
            logger.error(f"  FFmpeg Command: {' '.join(cmd_list)}")
            # Log only last few lines of stderr if it's huge
            stderr_lines = stderr.strip().splitlines()
            max_lines = 20
            log_stderr = "\n".join(stderr_lines[-max_lines:])
            if len(stderr_lines) > max_lines:
                 log_stderr = f"(Showing last {max_lines} lines)\n" + log_stderr
            logger.error(f"  FFmpeg Stderr:\n{log_stderr}")
            return False, stderr_output if capture_stderr else False
        else:
            logger.info(f"  -> Completed: {desc} (Time: {elapsed_time:.2f}s)")
            if verbose_success and stderr:
                 warnings = [line for line in stderr.splitlines() if 'warning' in line.lower()]
                 if warnings:
                      logger.warning(f"FFmpeg warnings during {desc}:\n" + "\n".join(warnings))
            return True, stderr_output if capture_stderr else True
    except FileNotFoundError:
        logger.error(f"'{cmd_list[0]}' not found. Check installation/PATH.")
        return False, "" if capture_stderr else False
    except Exception as e:
        logger.error(f"ERROR: Unexpected error running {desc}: {e}", exc_info=True)
        return False, "" if capture_stderr else False

# --- ISO 639-2 Language Code Validation ---
# Common 3-letter ISO 639-2/B codes for validation reference
COMMON_LANG_CODES = {
    'aar', 'abk', 'afr', 'aka', 'amh', 'ara', 'arg', 'asm', 'ava', 'ave',
    'aym', 'aze', 'bak', 'bam', 'bel', 'ben', 'bih', 'bis', 'bod', 'bos',
    'bre', 'bul', 'cat', 'ces', 'cha', 'che', 'chi', 'chu', 'chv', 'cor',
    'cos', 'cre', 'cym', 'dan', 'deu', 'div', 'dzo', 'ell', 'eng', 'epo',
    'est', 'eus', 'ewe', 'fao', 'fas', 'fij', 'fin', 'fra', 'fre', 'fry',
    'ful', 'gla', 'gle', 'glg', 'glv', 'ger', 'grn', 'guj', 'hat', 'hau',
    'hbs', 'heb', 'her', 'hin', 'hmo', 'hrv', 'hun', 'hye', 'ibo', 'ido',
    'iii', 'iku', 'ile', 'ina', 'ind', 'ipk', 'isl', 'ita', 'jav', 'jpn',
    'kal', 'kan', 'kas', 'kat', 'kau', 'kaz', 'khm', 'kik', 'kin', 'kir',
    'kor', 'kua', 'kur', 'lao', 'lat', 'lav', 'lim', 'lin', 'lit', 'ltz',
    'lub', 'lug', 'mac', 'mah', 'mal', 'mar', 'mkd', 'mlg', 'mlt', 'mon',
    'mri', 'msa', 'may', 'mya', 'nau', 'nav', 'nbl', 'nde', 'ndo', 'nep',
    'nld', 'dut', 'nno', 'nob', 'nor', 'nya', 'oci', 'oji', 'ori', 'orm',
    'oss', 'pan', 'pli', 'pol', 'por', 'pus', 'que', 'roh', 'ron', 'rum',
    'run', 'rus', 'sag', 'san', 'sin', 'slk', 'slo', 'slv', 'sme', 'smo',
    'sna', 'snd', 'som', 'sot', 'spa', 'sqi', 'alb', 'srd', 'srp', 'ssw',
    'sun', 'swa', 'swe', 'tah', 'tam', 'tat', 'tel', 'tgk', 'tgl', 'tha',
    'tir', 'ton', 'tsn', 'tso', 'tuk', 'tur', 'twi', 'uig', 'ukr', 'und',
    'urd', 'uzb', 'ven', 'vie', 'vol', 'wln', 'wol', 'xho', 'yid', 'yor',
    'zha', 'zho', 'zul',
}


def validate_language_code(lang_code):
    """
    Validate a 3-letter ISO 639-2 language code.
    Returns True if it looks valid (3 lowercase letters).
    """
    if not lang_code or not isinstance(lang_code, str):
        return False
    lang_code = lang_code.strip().lower()
    if len(lang_code) != 3 or not lang_code.isalpha():
        return False
    return True


def prompt_for_language_code(stream_type="foreign"):
    """
    Prompt user to enter a valid 3-letter ISO 639-2 language code.
    Returns the validated code or None if user cancels.
    """
    print(f"\n  A valid 3-letter language code is required for the {stream_type} audio track.")
    print(f"  Common codes: eng (English), hin (Hindi), jpn (Japanese), spa (Spanish),")
    print(f"                fra (French), deu (German), ita (Italian), por (Portuguese),")
    print(f"                kor (Korean), zho (Chinese), ara (Arabic), rus (Russian)")
    
    while True:
        try:
            user_input = input(f"  Enter 3-letter language code for {stream_type} audio (or 'q' to quit): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        
        if user_input == 'q':
            return None
        
        if validate_language_code(user_input):
            if user_input in COMMON_LANG_CODES:
                print(f"  -> Using language code: {user_input}")
            else:
                print(f"  -> Using language code: {user_input} (not in common list, but accepted)")
            return user_input
        else:
            print(f"  -> Invalid code '{user_input}'. Must be exactly 3 letters (e.g., 'hin', 'eng', 'jpn').")


def run_mkvmerge(cmd_list, desc="MKVMerge Task"):
    """Runs an mkvmerge command with logging and error handling."""
    global MKVMERGE_EXEC
    if not MKVMERGE_EXEC:
        logger.error("mkvmerge path not set.")
        return False
    cmd_list[0] = MKVMERGE_EXEC
    logger.info(f"  -> Running: {desc}...")
    start_time = time.time()
    try:
        process = subprocess.Popen(cmd_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8', errors='ignore')
        stdout, stderr = process.communicate()
        elapsed_time = time.time() - start_time

        # mkvmerge exit codes: 0=success, 1=warnings, 2=error
        if process.returncode >= 2:
            logger.error(f"\nERROR: {desc} failed! (Exit code: {process.returncode}, Time: {elapsed_time:.2f}s)")
            logger.error(f"  mkvmerge Command: {' '.join(cmd_list)}")
            output_lines = (stdout + stderr).strip().splitlines()
            max_lines = 20
            log_output = "\n".join(output_lines[-max_lines:])
            if len(output_lines) > max_lines:
                log_output = f"(Showing last {max_lines} lines)\n" + log_output
            logger.error(f"  mkvmerge Output:\n{log_output}")
            return False
        else:
            if process.returncode == 1:
                logger.warning(f"  mkvmerge completed with warnings for {desc}")
            logger.info(f"  -> Completed: {desc} (Time: {elapsed_time:.2f}s)")
            return True
    except FileNotFoundError:
        logger.error(f"'{cmd_list[0]}' not found. Check installation/PATH.")
        return False
    except Exception as e:
        logger.error(f"ERROR: Unexpected error running {desc}: {e}", exc_info=True)
        return False


def get_stream_info(video_path):
    """Gets stream information using ffprobe."""
    global FFPROBE_EXEC
    if not FFPROBE_EXEC:
        logger.error("FFprobe path not set.")
        return None
    if not os.path.exists(video_path):
        logger.error(f"Video file not found: {video_path}")
        return None
    try:
        # Added timeout to prevent hangs on corrupted files
        result = subprocess.run([FFPROBE_EXEC, '-v', 'error', '-print_format', 'json', '-show_streams', video_path],
                                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding='utf-8', timeout=60) # 60 second timeout
        return json.loads(result.stdout).get('streams', [])
    except subprocess.TimeoutExpired:
        logger.error(f"ffprobe timed out processing {os.path.basename(video_path)}")
        return None
    except Exception as e:
        logger.error(f"Failed to get stream info for {os.path.basename(video_path)}: {e}", exc_info=False) # Less verbose on failure
        return None

def find_audio_stream_index_by_lang(streams, lang_code):
    """Finds the first audio stream matching the given language code."""
    if not streams:
        return None
    logger.debug(f"Searching for language '{lang_code}' in streams...")
    stream_index = None
    found_match = False
    # Prioritize exact language match
    for stream in streams:
        if stream.get('codec_type') == 'audio':
            idx = stream.get('index') # This is the ABSOLUTE index
            tags = stream.get('tags', {})
            lang = tags.get('language', 'und') # 'und' for undetermined
            if idx is not None and lang.lower() == lang_code.lower():
                stream_index = idx
                logger.debug(f"    ^ Found matching language tag at index {idx}")
                found_match = True
                break # Take the first match

    if found_match:
        return stream_index

    # If no exact match, fall back to the first audio stream found
    logger.warning(f"Language tag '{lang_code}' not found. Looking for first available audio stream.")
    for stream in streams:
         if stream.get('codec_type') == 'audio':
              idx = stream.get('index') # Absolute index
              if idx is not None:
                   logger.warning(f"Falling back to first audio stream found (index: {idx}).")
                   return idx

    logger.error("No audio streams found at all.")
    return None

def get_audio_stream_details(video_path):
    """Gets detailed information about audio streams in a video file."""
    streams = get_stream_info(video_path)
    if not streams:
        return []

    audio_streams = []
    for i, stream in enumerate(streams):
        if stream.get('codec_type') == 'audio':
            stream_info = {
                'index': stream.get('index'), # Absolute index
                'codec': stream.get('codec_name', 'unknown'),
                'channels': stream.get('channels', 0),
                'sample_rate': stream.get('sample_rate', 'unknown'),
                'bit_rate': stream.get('bit_rate', 'unknown'),
                'language': stream.get('tags', {}).get('language', 'und'),
                'title': stream.get('tags', {}).get('title', '')
            }
            audio_streams.append(stream_info)

    return audio_streams

def prompt_user_for_audio_stream(video_path, stream_type="foreign"):
    """Prompts the user to select an audio stream from a video file."""
    audio_streams = get_audio_stream_details(video_path)
    if not audio_streams:
        logger.error(f"No audio streams found in {os.path.basename(video_path)}")
        return None

    print(f"\n--- Available {stream_type.capitalize()} Audio Streams in {os.path.basename(video_path)} ---")
    print("{:<5} {:<10} {:<8} {:<12} {:<10} {:<12}".format(
        "Sel#", "Stream#", "Language", "Codec", "Channels", "Sample Rate"))
    print("-" * 70)

    for i, stream in enumerate(audio_streams):
        print("{:<5} {:<10} {:<8} {:<12} {:<10} {:<12}".format(
            i,  # Selection number (0-based)
            stream['index'],  # Actual stream index (absolute)
            stream['language'],
            stream['codec'],
            stream['channels'],
            stream['sample_rate']))

    # Prompt user for selection
    while True:
        try:
            selection = input(f"\nSelect {stream_type} audio stream by Sel# (or press Enter for auto-detection): ")
            if not selection.strip():
                return None  # Auto-detection

            selected_idx = int(selection)
            # Check if the selection is valid based on our displayed numbers
            if 0 <= selected_idx < len(audio_streams):
                # Return the actual stream index, not our selection number
                return audio_streams[selected_idx]['index'] # Return the absolute index
            else:
                print(f"Error: Selection number {selected_idx} is out of range. Please choose 0-{len(audio_streams)-1}.")
                continue
        except ValueError:
            print("Error: Please enter a valid number.")


def prompt_user_for_foreign_tracks(video_path, primary_stream_idx=None):
    """
    Prompts the user to select which audio tracks from the foreign video to include.
    Returns a list of dicts: [{'stream_idx': int, 'language': str}, ...]
    The primary track is always first in the returned list.
    """
    audio_streams = get_audio_stream_details(video_path)
    if not audio_streams:
        logger.error(f"No audio streams found in {os.path.basename(video_path)}")
        return []

    if len(audio_streams) == 1:
        # Only one track, no need to prompt
        track = audio_streams[0]
        lang = track['language'] if track['language'] != 'und' else None
        return [{'stream_idx': track['index'], 'language': lang}]

    print(f"\n--- Foreign Audio Track Selection ---")
    print(f"  File: {os.path.basename(video_path)}")
    print(f"  {len(audio_streams)} audio track(s) available:\n")
    print("  {:<5} {:<10} {:<8} {:<12} {:<10} {:<15} {}".format(
        "Sel#", "Stream#", "Lang", "Codec", "Channels", "Sample Rate", "Title"))
    print("  " + "-" * 85)

    for i, stream in enumerate(audio_streams):
        primary_marker = " <-- Primary" if stream['index'] == primary_stream_idx else ""
        print("  {:<5} {:<10} {:<8} {:<12} {:<10} {:<15} {}{}".format(
            i,
            stream['index'],
            stream['language'],
            stream['codec'],
            stream['channels'],
            stream['sample_rate'],
            stream.get('title', ''),
            primary_marker))

    print(f"\n  Options:")
    print(f"    Enter Sel# for a single track (e.g., '0')")
    print(f"    Enter comma-separated Sel# for multiple tracks (e.g., '0,1,2')")
    print(f"    Enter 'all' to include all tracks")
    print(f"    Press Enter to use only the primary track")

    while True:
        try:
            selection = input(f"\n  Select foreign audio tracks to sync and include: ").strip().lower()

            if not selection:
                # Default: just the primary track
                if primary_stream_idx is not None:
                    for s in audio_streams:
                        if s['index'] == primary_stream_idx:
                            return [{'stream_idx': s['index'], 'language': s['language'] if s['language'] != 'und' else None}]
                # If no primary specified, return first track
                return [{'stream_idx': audio_streams[0]['index'], 'language': audio_streams[0]['language'] if audio_streams[0]['language'] != 'und' else None}]

            if selection == 'all':
                selected = []
                for s in audio_streams:
                    selected.append({'stream_idx': s['index'], 'language': s['language'] if s['language'] != 'und' else None})
                # Put primary first if specified
                if primary_stream_idx is not None:
                    selected.sort(key=lambda x: 0 if x['stream_idx'] == primary_stream_idx else 1)
                return selected

            # Parse comma-separated indices
            parts = [p.strip() for p in selection.split(',')]
            selected = []
            valid = True
            for p in parts:
                try:
                    idx = int(p)
                    if 0 <= idx < len(audio_streams):
                        s = audio_streams[idx]
                        selected.append({'stream_idx': s['index'], 'language': s['language'] if s['language'] != 'und' else None})
                    else:
                        print(f"  Error: Sel# {idx} out of range (0-{len(audio_streams)-1})")
                        valid = False
                        break
                except ValueError:
                    print(f"  Error: '{p}' is not a valid number")
                    valid = False
                    break

            if valid and selected:
                # Put primary first if specified
                if primary_stream_idx is not None:
                    selected.sort(key=lambda x: 0 if x['stream_idx'] == primary_stream_idx else 1)
                return selected

        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            return []


def resolve_track_languages(track_list, auto_detect=False):
    """
    Ensure all tracks in the list have valid language codes.
    Prompts user interactively for any track with missing/invalid language.
    In auto_detect mode, invalid languages cause an error.
    
    Args:
        track_list: list of {'stream_idx': int, 'language': str or None, ...}
        auto_detect: if True, don't prompt (error on invalid)
    
    Returns:
        True if all languages resolved, False if user cancelled or auto_detect failed
    """
    for i, track in enumerate(track_list):
        lang = track.get('language')
        if not lang or not validate_language_code(lang) or lang.lower() in ('und', 'unk', 'foreign'):
            if auto_detect:
                logger.error(f"Track stream #{track['stream_idx']}: invalid language '{lang}'. "
                           f"Specify valid language codes via --foreign_lang or per-track metadata.")
                return False
            else:
                print(f"\n  Track stream #{track['stream_idx']} has no valid language tag (current: '{lang or 'none'}')")
                new_lang = prompt_for_language_code(f"track #{track['stream_idx']}")
                if new_lang is None:
                    return False
                track['language'] = new_lang
    return True


def parse_foreign_tracks_arg(foreign_tracks_str, video_path, primary_stream_idx=None):
    """
    Parse --foreign_tracks argument into a track list.
    
    Args:
        foreign_tracks_str: "all", "primary", or comma-separated absolute stream indices
        video_path: path to foreign video for stream discovery
        primary_stream_idx: the primary foreign stream index (already selected)
    
    Returns:
        list of {'stream_idx': int, 'language': str or None}
    """
    audio_streams = get_audio_stream_details(video_path)
    if not audio_streams:
        return []

    if foreign_tracks_str.lower() == 'primary':
        # Just the primary track
        if primary_stream_idx is not None:
            for s in audio_streams:
                if s['index'] == primary_stream_idx:
                    return [{'stream_idx': s['index'], 'language': s['language'] if s['language'] != 'und' else None}]
        return [{'stream_idx': audio_streams[0]['index'], 'language': audio_streams[0]['language'] if audio_streams[0]['language'] != 'und' else None}]

    if foreign_tracks_str.lower() == 'all':
        selected = []
        for s in audio_streams:
            selected.append({'stream_idx': s['index'], 'language': s['language'] if s['language'] != 'und' else None})
        if primary_stream_idx is not None:
            selected.sort(key=lambda x: 0 if x['stream_idx'] == primary_stream_idx else 1)
        return selected

    # Parse comma-separated absolute stream indices
    selected = []
    stream_idx_map = {s['index']: s for s in audio_streams}
    parts = [p.strip() for p in foreign_tracks_str.split(',')]
    for p in parts:
        try:
            idx = int(p)
            if idx in stream_idx_map:
                s = stream_idx_map[idx]
                selected.append({'stream_idx': s['index'], 'language': s['language'] if s['language'] != 'und' else None})
            else:
                logger.warning(f"Stream index {idx} not found as audio stream in foreign video. Skipping.")
        except ValueError:
            logger.warning(f"Invalid stream index '{p}' in --foreign_tracks. Skipping.")

    if primary_stream_idx is not None:
        selected.sort(key=lambda x: 0 if x['stream_idx'] == primary_stream_idx else 1)
    return selected


def sync_additional_track(args, foreign_video, stream_idx, final_segment_anchors, ref_delay_s,
                          temp_dir, track_label="additional"):
    """
    Sync an additional foreign audio track using pre-computed segment anchors.
    
    This reuses the timing from the primary track's sync without re-doing 
    iterative refinement. Since all audio tracks in the same video share 
    the same timeline, the same anchor points apply.
    
    Args:
        foreign_video: path to the foreign video file
        stream_idx: absolute stream index of the additional track
        final_segment_anchors: list of (ref_time, foreign_time) tuples from primary sync
        ref_delay_s: reference delay for start padding
        temp_dir: temporary directory for intermediate files
        track_label: label for logging
    
    Returns:
        str: path to the synced WAV file, or None on failure
    """
    logger.info(f"\n--- Syncing Additional Track: Stream #{stream_idx} ({track_label}) ---")
    
    num_segments = len(final_segment_anchors) - 1
    if num_segments < 1:
        logger.error(f"  No segments to process for additional track #{stream_idx}")
        return None
    
    # Create a subdirectory for this track's temp files
    track_temp_dir = os.path.join(temp_dir, f"additional_track_{stream_idx}")
    os.makedirs(track_temp_dir, exist_ok=True)
    
    # Step 1: Extract the additional track to WAV
    foreign_wav_full = os.path.join(track_temp_dir, f"foreign_full_{stream_idx}.wav")
    aresample_filter = f'aresample=resampler=soxr:precision=28:cutoff={0.99 if DEFAULT_SAMPLE_RATE >= 44100 else 0.90}'
    
    extract_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
        "-i", foreign_video,
        "-map", f"0:{stream_idx}",
        "-vn",
        "-c:a", "pcm_s16le", "-ar", str(DEFAULT_SAMPLE_RATE), "-ac", str(DEFAULT_CHANNELS),
        "-af", aresample_filter, "-y", "-f", "wav", foreign_wav_full
    ]
    if not run_ffmpeg(extract_cmd, f"Extract Additional Track #{stream_idx}")[0]:
        return None

    # Let this track pick its own quiet splice point for each relocated replacement, rather
    # than blindly reusing the primary track's boundary (a safe pause in one dub can still
    # contain dialogue in another).
    track_final_segment_anchors = final_segment_anchors
    if args.per_track_splice_placement and AUDIO_REPLACEMENT_RANGES and not AUDIO_EDITORIAL_EDITS:
        ref_wav_analysis = os.path.join(temp_dir, "ref_audio_analysis.wav")
        if os.path.exists(ref_wav_analysis):
            track_wav_analysis = os.path.join(track_temp_dir, f"foreign_analysis_{stream_idx}.wav")
            loudness_filter = 'loudnorm=I=-23:TP=-1.5:LRA=11'
            audio_filter_chain = f'{aresample_filter},{loudness_filter}'
            extract_cmd_analysis = [
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
                "-i", foreign_video,
                "-map", f"0:{stream_idx}",
                "-vn",
                "-c:a", "pcm_s16le", "-ar", str(DEFAULT_SAMPLE_RATE), "-ac", str(DEFAULT_CHANNELS),
                "-af", audio_filter_chain, "-y", "-f", "wav", track_wav_analysis
            ]
            if run_ffmpeg(extract_cmd_analysis, f"Extract Additional Track #{stream_idx} (Analysis Copy)")[0]:
                track_final_segment_anchors = _localize_replacements_for_track(
                    args, final_segment_anchors, ref_wav_analysis, track_wav_analysis, track_label)
            else:
                logger.warning(f"  Failed to extract analysis copy for {track_label}; using shared splice placement.")
        else:
            logger.debug(f"  Reference analysis WAV unavailable; using shared splice placement for {track_label}.")

    # Reuse the primary track's explicit editorial recipe when available. This
    # keeps all foreign tracks on the same cut/insert timeline.
    if AUDIO_EDITORIAL_EDITS:
        try:
            sample_rate, source_audio = wavfile.read(foreign_wav_full)
            reference_wav = os.path.join(temp_dir, "ref_audio_full.wav")
            if not os.path.exists(reference_wav):
                logger.warning(f"  Editorial recipe skipped for track #{stream_idx}: reference WAV is unavailable")
            else:
                _, reference_audio = wavfile.read(reference_wav)
                source_channels = source_audio.ndim == 2
                reference_mono = (reference_audio.mean(axis=1)
                                  if reference_audio.ndim == 2 else reference_audio)
                corrected_channels = []
                for channel in (source_audio.T if source_channels else [source_audio]):
                    corrected_channels.append(_import_audio_alignment().apply_editorial_edit_recipe(
                        source_audio=channel,
                        reference_audio=reference_mono,
                        edits=AUDIO_EDITORIAL_EDITS,
                        sample_rate=sample_rate,
                    ))
                corrected_audio = (np.column_stack(corrected_channels)
                                   if source_channels else corrected_channels[0])
                recipe_wav = os.path.join(track_temp_dir, f"recipe_track_{stream_idx}.wav")
                wavfile.write(recipe_wav, sample_rate, corrected_audio.astype(source_audio.dtype))
                output_wav = os.path.join(track_temp_dir, f"synced_track_{stream_idx}.wav")
                if ref_delay_s >= MIN_DELAY_S:
                    delay_ms = int(ref_delay_s * 1000)
                    pad_cmd = [
                        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
                        "-i", recipe_wav,
                        "-af", f"adelay={delay_ms}|{delay_ms}",
                        "-c:a", "pcm_s16le", "-ar", str(DEFAULT_SAMPLE_RATE),
                        "-ac", str(DEFAULT_CHANNELS), "-y", output_wav,
                    ]
                    if not run_ffmpeg(pad_cmd, f"Apply Editorial Padding to Track #{stream_idx}")[0]:
                        shutil.copy2(recipe_wav, output_wav)
                else:
                    shutil.copy2(recipe_wav, output_wav)
                logger.info(f"  [OK] Applied primary editorial recipe to track #{stream_idx}")
                return output_wav
        except Exception as e:
            logger.warning(f"  Editorial recipe failed for track #{stream_idx}: {e}. Falling back to anchor segments.")
    
    # Step 2: Process each segment using the same anchors
    processed_segment_files = []
    
    pbar = tqdm(
        total=num_segments,
        desc=f"Track #{stream_idx}",
        unit="seg",
        ncols=80,
        bar_format='{desc}: {bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]',
        file=sys.stdout,
        dynamic_ncols=False
    )
    
    for i in range(num_segments):
        segment_num = i + 1
        ref_start, foreign_start = track_final_segment_anchors[i]
        ref_end, foreign_end = track_final_segment_anchors[i + 1]
        target_ref_duration = ref_end - ref_start
        
        # Use the same iterative processing as the primary track
        # (no first/last segment adjustments for additional tracks)
        segment_path = process_segment_iteratively(
            foreign_wav_full=foreign_wav_full,
            foreign_start=foreign_start,
            foreign_end=foreign_end,
            ref_duration=target_ref_duration,
            segment_num=segment_num,
            temp_dir=track_temp_dir,
            max_iterations=3,
            target_precision_ms=5,
            is_first_segment=False,
            is_last_segment=False,
            first_adjust_ms=0.0,
            last_adjust_ms=0.0
        )
        
        if segment_path and os.path.exists(segment_path):
            processed_segment_files.append(segment_path)
        else:
            fallback_path = os.path.join(track_temp_dir, f"segment_{segment_num:04d}_fallback.wav")
            logger.warning(f"  Segment {segment_num} for track #{stream_idx}: iterative processing failed. Activating direct segment fallback recovery.")
            if fallback_direct_segment(
                source_wav=foreign_wav_full,
                source_start=foreign_start,
                source_end=foreign_end,
                out_path=fallback_path,
                segment_num=segment_num,
                label=f"track #{stream_idx} segment"
            ):
                processed_segment_files.append(fallback_path)
            else:
                pbar.close()
                logger.error(f"  Failed to recover segment {segment_num} for track #{stream_idx}. Both iterative and direct fallback paths failed.")
                return None
        
        pbar.update(1)
    
    pbar.close()
    
    if not processed_segment_files:
        logger.error(f"  No segments processed for track #{stream_idx}")
        return None
    
    logger.info(f"  [OK] Processed {len(processed_segment_files)} segments for track #{stream_idx}")
    
    # Step 3: Concatenate segments
    concatenated_path = os.path.join(track_temp_dir, "concatenated.wav")
    concat_list_path = os.path.join(track_temp_dir, "concat_list.txt")
    
    try:
        with open(concat_list_path, 'w', encoding='utf-8') as f:
            for seg_path in processed_segment_files:
                # Absolute path required: concat demuxer resolves relative to process CWD, not list location
                abs_path = os.path.abspath(seg_path).replace("\\", "/")
                f.write(f"file '{abs_path}'\n")
    except IOError as e:
        logger.error(f"  Failed to create concat list for track #{stream_idx}: {e}")
        return None
    
    concat_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
        "-f", "concat", "-safe", "0",
        "-i", concat_list_path,
        "-c", "copy",
        "-y", concatenated_path
    ]
    if not run_ffmpeg(concat_cmd, f"Concatenate Track #{stream_idx}")[0]:
        return None
    
    # Step 4: Apply start delay padding
    output_wav = os.path.join(track_temp_dir, f"synced_track_{stream_idx}.wav")
    
    if ref_delay_s >= MIN_DELAY_S:
        delay_ms = int(ref_delay_s * 1000)
        pad_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
            "-i", concatenated_path,
            "-af", f"adelay={delay_ms}|{delay_ms}",
            "-c:a", "pcm_s16le",
            "-ar", str(DEFAULT_SAMPLE_RATE), "-ac", str(DEFAULT_CHANNELS),
            "-y", output_wav
        ]
        if not run_ffmpeg(pad_cmd, f"Apply Padding to Track #{stream_idx}")[0]:
            # Fallback: copy without padding
            try:
                shutil.copy2(concatenated_path, output_wav)
                logger.warning(f"  Copied unpadded audio for track #{stream_idx}")
            except Exception:
                return None
    else:
        try:
            shutil.copy2(concatenated_path, output_wav)
        except Exception as e:
            logger.error(f"  Failed to copy final audio for track #{stream_idx}: {e}")
            return None
    
    logger.info(f"  [OK] Additional track #{stream_idx} synced successfully")
    return output_wav

def format_time(seconds):
    """Formats seconds into HH:MM:SS:ms format."""
    if seconds is None or seconds < 0:
        return "00:00:00:000"
    milliseconds = int((seconds - int(seconds)) * 1000)
    total_seconds = int(seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}:{milliseconds:03d}"

def get_file_duration(file_path, media_type='audio'):
    """Get the duration of an audio or video file using ffprobe."""
    global FFPROBE_EXEC
    if not FFPROBE_EXEC:
        logger.error(f"FFprobe path not set for duration check of {os.path.basename(file_path)}.")
        return None
    if not os.path.exists(file_path):
        logger.error(f"File not found for duration check: {file_path}")
        return None

    try:
        probe_cmd = [FFPROBE_EXEC, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", file_path]
        # Added timeout
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True, timeout=30)
        duration_str = result.stdout.strip()
        if duration_str:
            return float(duration_str)
        logger.warning(f"ffprobe returned empty duration for {os.path.basename(file_path)}")
        return None
    except subprocess.TimeoutExpired:
        logger.error(f"ffprobe timed out getting duration for {os.path.basename(file_path)}")
        return None
    except subprocess.CalledProcessError as e:
        logger.error(f"ffprobe failed to get duration for {os.path.basename(file_path)}: {e.stderr}")
        return None
    except Exception as e:
        logger.error(f"Error getting duration for {os.path.basename(file_path)}: {e}", exc_info=False)
        return None


def find_visual_program_bounds(video_path, minimum_black_seconds=0.2):
    """Return the first and last non-black video timestamps, when detectable.

    This deliberately does not assume that audio silence matches video black:
    dubbed releases can announce the episode title while the picture is still
    black. ``None`` is returned for either boundary when the video has no
    qualifying black lead-in/trailer, so callers can safely retain audio bounds.
    """
    duration = get_file_duration(video_path, media_type='video')
    if duration is None:
        return None, None
    try:
        command = [
            FFMPEG_EXEC, "-hide_banner", "-v", "info", "-i", video_path,
            "-vf", f"blackdetect=d={minimum_black_seconds}:pix_th=0.10",
            "-an", "-f", "null", "-",
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
    except Exception as error:
        logger.warning(f"  Visual program-boundary detection skipped for {os.path.basename(video_path)}: {error}")
        return None, None

    black_ranges = [
        (float(match.group(1)), float(match.group(2)))
        for match in re.finditer(r"black_start:([0-9.]+)\s+black_end:([0-9.]+)", result.stderr)
    ]
    visual_start = next((end for start, end in black_ranges if start <= 0.1), None)
    visual_end = next((start for start, end in reversed(black_ranges) if end >= duration - 0.1), None)
    return visual_start, visual_end

# --- Image Pairing Stage Functions ---
def extract_frames_ffmpeg(video_path, output_folder, scene_threshold):
    """Extracts scene change frames using FFmpeg's scene detection."""
    os.makedirs(output_folder, exist_ok=True)
    parsed_pts_times = []
    vid_name = os.path.basename(video_path)

    # Command using scene detection filter
    ffmpeg_command = [
        "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info",
        "-i", video_path,
        "-vf", f"select='gt(scene,{scene_threshold})',showinfo", # Select frames where scene score > threshold, showinfo logs PTS
        "-vsync", "vfr", # Variable frame rate to capture exact frames
        "-q:v", "2", # High quality PNG output
        os.path.join(output_folder, "frame_%06d.png")
    ]

    logger.info(f"--- Extracting Scene Frames (threshold={scene_threshold}) from {vid_name} ---")
    success, stderr_output = run_ffmpeg(ffmpeg_command, f"Extract Frames ({vid_name})", verbose_success=False, capture_stderr=True)

    if success and stderr_output:
        # Scan the raw text directly (not split by '\n' first): splitting first requires the
        # whole "n: ... pts: ... pts_time:..." triple to land intact on one line-break-derived
        # line, which silently drops most entries if anything reflows/interleaves that text.
        pts_time_re = re.compile(r'\[Parsed_showinfo[^\]]*\][^\n]*?pts_time:([-\d.]+)')
        for match in pts_time_re.finditer(stderr_output):
            try:
                parsed_pts_times.append(float(match.group(1)))
            except (ValueError, IndexError):
                logger.warning(f"Could not parse pts_time from match: {match.group(0)}")
    elif not success:
        logger.error(f"  Frame extraction command failed for {vid_name}.")
        return False, [], []

    # Verify extracted frames match timestamps
    frame_files = sorted(glob.glob(os.path.join(output_folder, "frame_*.png")))
    frame_count = len(frame_files)
    timestamp_count = len(parsed_pts_times)

    if frame_count == 0 or timestamp_count == 0:
        logger.error(f"  Frame extraction yielded zero frames or timestamps for {vid_name}.")
        return False, [], []

    final_count = 0
    if frame_count != timestamp_count:
         # A large gap almost always means the pts_time parser dropped valid entries, not that
         # ffmpeg actually produced fewer timestamps than frames - flag it loudly, not as a warning.
         log_fn = logger.error if timestamp_count < frame_count * 0.9 else logger.warning
         log_fn(f"  Frame/Timestamp count mismatch ({frame_count} frames vs {timestamp_count} timestamps) for {vid_name}. Using minimum.")
         final_count = min(frame_count, timestamp_count)
         # Trim lists to the minimum count to maintain correspondence
         parsed_pts_times = parsed_pts_times[:final_count]
         frame_files = frame_files[:final_count]
    else:
         final_count = frame_count

    logger.info(f"> Extracted {final_count} scene frames from {vid_name}")
    return True, [os.path.basename(f) for f in frame_files], parsed_pts_times

def _prepare_frame_for_matching(gray_frame):
    """Letterbox-resize to RESIZE_WIDTH x RESIZE_HEIGHT preserving aspect ratio, then mild blur.

    Stretching to a fixed size (ignoring the source's real aspect ratio) and comparing
    raw pixels directly is fragile when one source has a much lower resolution/heavier
    compression than the other (e.g. non-16:9 SD source vs sharp 1080p reference) -
    template matching scores collapse even for genuinely matching scenes.
    """
    if gray_frame is None or gray_frame.size == 0:
        return None
    h, w = gray_frame.shape[:2]
    if h == 0 or w == 0:
        return None
    scale = min(RESIZE_WIDTH / w, RESIZE_HEIGHT / h)
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    resized = cv2.resize(gray_frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((RESIZE_HEIGHT, RESIZE_WIDTH), dtype=resized.dtype)
    y_off = (RESIZE_HEIGHT - new_h) // 2
    x_off = (RESIZE_WIDTH - new_w) // 2
    canvas[y_off:y_off + new_h, x_off:x_off + new_w] = resized
    return cv2.GaussianBlur(canvas, (3, 3), 0)

def process_image_pair_for_match(ref_img_name, foreign_image_list, ref_extract_folder, foreign_extract_folder, match_threshold):
    """Compares one reference image against a list of foreign images using template matching."""
    ref_img_path = os.path.join(ref_extract_folder, ref_img_name)
    try:
        # Read reference image, convert to grayscale, and resize for consistent comparison
        ref_frame_orig = cv2.imread(ref_img_path, cv2.IMREAD_GRAYSCALE)
        if ref_frame_orig is None:
            logger.warning(f"Could not read reference image: {ref_img_name}"); return None
        ref_frame_comp = _prepare_frame_for_matching(ref_frame_orig)
        if ref_frame_comp is None or ref_frame_comp.size == 0:
             logger.warning(f"Failed to resize reference image: {ref_img_name}"); return None
    except Exception as e:
        logger.error(f"Error processing reference image {ref_img_name}: {e}", exc_info=False); return None

    best_match_foreign_name = None
    best_score = -1.0 # Initialize score below any possible match

    # Iterate through potential foreign matches (this list might be pre-filtered)
    for foreign_img_name in foreign_image_list:
        foreign_img_path = os.path.join(foreign_extract_folder, foreign_img_name)
        try:
            # Read, grayscale, and resize foreign image
            foreign_frame_orig = cv2.imread(foreign_img_path, cv2.IMREAD_GRAYSCALE)
            if foreign_frame_orig is None: continue # Skip if image can't be read
            foreign_frame_comp = _prepare_frame_for_matching(foreign_frame_orig)
            if foreign_frame_comp is None or foreign_frame_comp.size == 0: continue # Skip if resize fails

            # Perform template matching
            # TM_CCOEFF_NORMED gives a score between -1 and 1, where 1 is a perfect match
            result = cv2.matchTemplate(foreign_frame_comp, ref_frame_comp, cv2.TM_CCOEFF_NORMED)
            _minVal, maxVal, _minLoc, _maxLoc = cv2.minMaxLoc(result) # We only need the max value

            # Update best match if current score is higher
            if maxVal > best_score:
                best_score = maxVal
                best_match_foreign_name = foreign_img_name
        except Exception as e:
            # Log error but continue checking other foreign images
            logger.debug(f"Error comparing {ref_img_name} with {foreign_img_name}: {e}", exc_info=False)
            continue

    # Return the best match only if its score meets the threshold
    if best_match_foreign_name is not None and best_score >= match_threshold:
        return (best_match_foreign_name, best_score)
    else:
        return None # No match found above the threshold

def filter_similar_ref_images(initial_matches_with_times, ref_extract_folder, similarity_threshold):
    """Filters out reference frames that are too visually similar using perceptual hashing."""
    global similarity_libs_available
    if not similarity_libs_available:
        logger.info("  Skipping similarity filtering: imagehash/Pillow libraries not available.")
        return initial_matches_with_times
    if similarity_threshold < 0:
        logger.info(f"  Skipping similarity filtering: threshold ({similarity_threshold}) is negative.")
        return initial_matches_with_times
    if not initial_matches_with_times:
        return {} # Return empty if no initial matches

    logger.info(f"--- Filtering Similar Reference Frames (pHash Threshold: {similarity_threshold}) ---")
    start_time = time.time()

    # Sort reference frame names numerically based on frame index (e.g., frame_000001.png)
    frame_num_re = re.compile(r'frame_(\d+).png')
    try:
        ref_names_sorted = sorted(
            initial_matches_with_times.keys(),
            key=lambda name: int(frame_num_re.search(name).group(1))
        )
    except Exception as e:
        logger.warning(f"Could not sort reference frames numerically, using default sort. Error: {e}")
        ref_names_sorted = sorted(initial_matches_with_times.keys())


    hashes = {} # Store phash -> (ref_name, file_size)
    to_remove_ref_names = set() # Keep track of reference frames to discard

    for ref_name in tqdm(ref_names_sorted, desc="  Filtering Similar Refs", unit="frame", ncols=100, leave=False):
        if ref_name in to_remove_ref_names:
            continue # Skip if already marked for removal

        ref_path = os.path.join(ref_extract_folder, ref_name)
        if not os.path.exists(ref_path):
             logger.warning(f"Reference frame {ref_name} not found, skipping similarity check.")
             to_remove_ref_names.add(ref_name)
             continue

        try:
            # Get file size and compute perceptual hash
            current_size = os.path.getsize(ref_path)
            with Image.open(ref_path) as img_file:
                img_hash = imagehash.phash(img_file)
        except Exception as e:
            logger.warning(f"Could not process {ref_name} for similarity hashing: {e}")
            continue # Skip this frame if hashing fails

        found_similar = False
        hashes_to_update = {} # Store updates for the current hash if it replaces an existing one
        hashes_to_delete = [] # Hashes to remove if replaced by a larger frame

        # Compare current hash with existing stored hashes
        # Create a copy of items to allow modification during iteration
        for existing_hash, (existing_ref_name, existing_size) in list(hashes.items()):
             # Skip comparison if the existing frame was already marked for removal
             if existing_ref_name in to_remove_ref_names:
                 hashes_to_delete.append(existing_hash) # Mark the old hash for deletion
                 continue

             # Calculate Hamming distance between hashes (lower means more similar)
             hash_diff = img_hash - existing_hash

             if hash_diff < similarity_threshold:
                 # Found a similar frame
                 found_similar = True
                 # Decide which frame to keep: prefer the one with larger file size (potentially higher quality/detail)
                 if current_size >= existing_size:
                     # Current frame is better or equal, mark existing for removal and update hash mapping
                     logger.debug(f"    '{ref_name}' ({current_size}b) replacing similar '{existing_ref_name}' ({existing_size}b), diff={hash_diff}")
                     to_remove_ref_names.add(existing_ref_name)
                     hashes_to_update[img_hash] = (ref_name, current_size) # Map new hash to this frame
                     hashes_to_delete.append(existing_hash) # Mark old hash for deletion
                 else:
                     # Existing frame is better, mark current frame for removal
                     logger.debug(f"    '{ref_name}' ({current_size}b) removed due to similarity with '{existing_ref_name}' ({existing_size}b), diff={hash_diff}")
                     to_remove_ref_names.add(ref_name)
                 break # Stop comparing once a similar frame is found

        # Clean up hashes map after comparisons
        for h_del in hashes_to_delete:
             if h_del in hashes:
                 del hashes[h_del]
        hashes.update(hashes_to_update) # Apply updates

        # If no similar frame was found and this frame wasn't marked for removal, add its hash
        if not found_similar and ref_name not in to_remove_ref_names:
            hashes[img_hash] = (ref_name, current_size)

    # Create the final dictionary excluding the removed frames
    filtered_matches = {
        ref_name: data
        for ref_name, data in initial_matches_with_times.items()
        if ref_name not in to_remove_ref_names
    }

    removed_count = len(initial_matches_with_times) - len(filtered_matches)
    elapsed_time = time.time() - start_time
    logger.info(f"  -> Similarity filtering complete. Removed {removed_count} potentially redundant pairs ({elapsed_time:.2f}s).")
    return filtered_matches

def filter_temporal_inconsistency(matches_after_similarity):
    """Filters matches where the foreign frame order doesn't match the reference frame order."""
    if not matches_after_similarity:
        return [] # Return empty list if no matches
    logger.info("--- Filtering Temporal Inconsistencies ---")
    start_time = time.time()

    # Extract items and sort them based on the reference frame number
    match_items = list(matches_after_similarity.items())
    frame_num_re = re.compile(r'frame_(\d+).png')
    try:
        # Sort by reference frame number extracted from filename
        match_items.sort(key=lambda item: int(frame_num_re.search(item[0]).group(1)))
    except Exception as e:
        logger.warning(f"Could not sort matches numerically by reference frame, using timestamp sort. Error: {e}")
        # Fallback sort by reference timestamp if filename parsing fails
        match_items.sort(key=lambda item: item[1][1]) # Sort by ref_time (index 1 of tuple value)

    filtered_list = []
    last_accepted_foreign_num = -1 # Track the frame number of the last accepted foreign match
    removed_count = 0

    for ref_name, (foreign_name, ref_time, foreign_time) in match_items:
        # Extract frame number from the foreign filename
        foreign_match = frame_num_re.search(foreign_name)
        if not foreign_match:
            # If filename format is unexpected, keep the match but log a warning
            logger.warning(f"Could not parse frame number from foreign image '{foreign_name}'. Keeping match.")
            filtered_list.append((ref_name, foreign_name, ref_time, foreign_time))
            continue

        try:
            current_foreign_num = int(foreign_match.group(1))
        except ValueError:
             logger.warning(f"Could not convert foreign frame number to int for '{foreign_name}'. Keeping match.")
             filtered_list.append((ref_name, foreign_name, ref_time, foreign_time))
             continue

        # Core logic: Check if the current foreign frame number is >= the last accepted one
        # This ensures that the sequence of matched foreign frames is monotonically increasing
        if current_foreign_num >= last_accepted_foreign_num:
            filtered_list.append((ref_name, foreign_name, ref_time, foreign_time))
            last_accepted_foreign_num = current_foreign_num # Update the last accepted number
        else:
            # Temporal inconsistency detected (e.g., Ref frame 5 matches Foreign 10, Ref 6 matches Foreign 8)
            logger.debug(f"    Temporal inconsistency: Ref '{ref_name}' -> Foreign '{foreign_name}' ({current_foreign_num}) is earlier than last accepted ({last_accepted_foreign_num}). Removing.")
            removed_count += 1

    elapsed_time = time.time() - start_time
    logger.info(f"  -> Temporal filtering complete. Removed {removed_count} inconsistent pairs ({elapsed_time:.2f}s).")
    # Return a list of tuples: [(ref_filename, foreign_filename, ref_time, foreign_time)]
    return filtered_list


def run_image_pairing_stage(ref_video_path, foreign_video_path, temp_dir, scene_threshold, match_threshold, similarity_threshold):
    """Orchestrates the entire image pairing stage."""
    logger.info("\n===== Image Pairing Stage =====")
    stage_start_time = time.time()

    # Define paths for extracted frames
    ref_extract_path = os.path.join(temp_dir, "Extracted_Reference")
    foreign_extract_path = os.path.join(temp_dir, "Extracted_Foreign")

    # --- Step 1: Extract Frames ---
    ref_extract_ok, ref_filenames, ref_timestamps_list = extract_frames_ffmpeg(ref_video_path, ref_extract_path, scene_threshold)
    if not ref_extract_ok:
        logger.error("Failed to extract reference frames.")
        return None

    foreign_extract_ok, foreign_filenames, foreign_timestamps_list = extract_frames_ffmpeg(foreign_video_path, foreign_extract_path, scene_threshold)
    if not foreign_extract_ok:
        logger.error("Failed to extract foreign frames.")
        return None

    # --- Step 1.5: Calculate Initial Search Window ---
    logger.info("--- Calculating Initial Anchor Search Window ---")
    ref_duration = get_file_duration(ref_video_path, media_type='video')
    if ref_duration is None or ref_duration <= 0:
        logger.warning("Could not determine reference video duration or duration is zero. Initial search will use all foreign frames.")
        initial_search_window_s = float('inf')
    else:
        initial_search_window_s = ref_duration * MATCH_WINDOW_PERCENT
        logger.info(f"  Reference duration: {ref_duration:.2f}s")
        logger.info(f"  Initial anchor search window: +/- {initial_search_window_s:.2f}s ({MATCH_WINDOW_PERCENT*100}%)")
        logger.info(f"  Subsequent match window: 0 to +{ANCHOR_FOLLOW_FORWARD_WINDOW_S:.1f}s forward from estimate")

    # --- Step 2: Map filenames to timestamps ---
    logger.info("--- Mapping Timestamps to Extracted Frames ---")
    ref_timestamps_dict = {name: ts for name, ts in zip(ref_filenames, ref_timestamps_list)}
    foreign_timestamps_dict = {name: ts for name, ts in zip(foreign_filenames, foreign_timestamps_list)}
    if not ref_timestamps_dict or not foreign_timestamps_dict:
        logger.error("  ERROR: Failed to create timestamp dictionaries.")
        return None
    logger.info(f"  -> Mapped {len(ref_timestamps_dict)} reference and {len(foreign_timestamps_dict)} foreign timestamps.")

    # --- Step 2.5: Pre-cache all foreign frames (read + resize once) ---
    logger.info("--- Pre-caching Foreign Frames ---")
    foreign_frame_cache = {}  # {filename: resized_grayscale_ndarray}
    cache_failures = 0
    for f_name in tqdm(foreign_filenames, desc="  Caching Foreign Frames", unit="frame", ncols=100, leave=False):
        f_path = os.path.join(foreign_extract_path, f_name)
        try:
            img = cv2.imread(f_path, cv2.IMREAD_GRAYSCALE)
            if img is not None:
                resized = _prepare_frame_for_matching(img)
                if resized is not None and resized.size > 0:
                    foreign_frame_cache[f_name] = resized
                else:
                    cache_failures += 1
            else:
                cache_failures += 1
        except Exception as e:
            logger.debug(f"Failed to cache foreign frame {f_name}: {e}")
            cache_failures += 1
    logger.info(f"  -> Cached {len(foreign_frame_cache)} foreign frames ({cache_failures} failures)")

    # Build sorted foreign timestamps array for fast windowing
    foreign_ts_array = np.array([foreign_timestamps_dict[fn] for fn in foreign_filenames])
    foreign_names_array = np.array(foreign_filenames)

    # --- Step 3: Anchor-and-Follow Frame Matching ---
    logger.info(f"--- Anchor-and-Follow Frame Matching (Threshold: {match_threshold}) ---")
    match_start_time = time.time()
    initial_matches_dict = {}  # Stores {ref_name: (foreign_name, ref_time, foreign_time)}
    skipped_count = 0

    # Sort reference frames by timestamp for sequential processing
    ref_sorted = sorted(
        [(name, ref_timestamps_dict[name]) for name in ref_filenames if name in ref_timestamps_dict],
        key=lambda x: x[1]
    )

    # Anchor state
    anchor_ref_ts = None
    anchor_foreign_ts = None

    progress_bar = tqdm(total=len(ref_sorted), desc="  Matching Frames", unit="frame", ncols=100, leave=False)

    for ref_name, ref_time in ref_sorted:
        progress_bar.update(1)

        # Read and resize reference frame
        try:
            ref_img = cv2.imread(os.path.join(ref_extract_path, ref_name), cv2.IMREAD_GRAYSCALE)
            if ref_img is None:
                skipped_count += 1
                continue
            ref_frame_comp = _prepare_frame_for_matching(ref_img)
            if ref_frame_comp is None or ref_frame_comp.size == 0:
                skipped_count += 1
                continue
        except Exception as e:
            logger.debug(f"Error reading ref frame {ref_name}: {e}")
            skipped_count += 1
            continue

        # Determine search window
        if anchor_ref_ts is None:
            # No anchor yet: wide initial window centered on ref_time
            min_t = ref_time - initial_search_window_s
            max_t = ref_time + initial_search_window_s
        else:
            # Anchor established: estimate foreign position, search forward only
            time_since_anchor = ref_time - anchor_ref_ts
            estimated_foreign_ts = anchor_foreign_ts + time_since_anchor
            min_t = estimated_foreign_ts  # start from estimate (no backward search)
            max_t = estimated_foreign_ts + ANCHOR_FOLLOW_FORWARD_WINDOW_S

        # Find candidate foreign frames within the window
        mask = (foreign_ts_array >= min_t) & (foreign_ts_array <= max_t)
        candidate_indices = np.where(mask)[0]

        if len(candidate_indices) == 0:
            logger.debug(f"No candidates for ref frame {ref_name} (time {ref_time:.3f}s)")
            continue

        # Get candidate names and timestamps
        candidate_names = foreign_names_array[candidate_indices]
        candidate_ts = foreign_ts_array[candidate_indices]

        # Sort candidates by proximity to estimated position (closest first)
        if anchor_ref_ts is not None:
            estimated = anchor_foreign_ts + (ref_time - anchor_ref_ts)
        else:
            estimated = ref_time
        proximity_order = np.argsort(np.abs(candidate_ts - estimated))
        candidate_names = candidate_names[proximity_order]
        candidate_ts = candidate_ts[proximity_order]

        # Compare against candidates, early-stop on first above-threshold match
        best_score = -1.0
        best_foreign_name = None
        best_foreign_ts = None

        for c_name, c_ts in zip(candidate_names, candidate_ts):
            c_name_str = str(c_name)
            if c_name_str not in foreign_frame_cache:
                continue

            foreign_frame_comp = foreign_frame_cache[c_name_str]
            try:
                result = cv2.matchTemplate(foreign_frame_comp, ref_frame_comp, cv2.TM_CCOEFF_NORMED)
                _, maxVal, _, _ = cv2.minMaxLoc(result)
            except Exception as e:
                logger.debug(f"matchTemplate error {ref_name} vs {c_name_str}: {e}")
                continue

            if maxVal > best_score:
                best_score = maxVal
                best_foreign_name = c_name_str
                best_foreign_ts = float(c_ts)
                # Early stop: good enough match found near estimated position
                if best_score >= match_threshold:
                    break

        # Record match if above threshold
        if best_foreign_name is not None and best_score >= match_threshold:
            initial_matches_dict[ref_name] = (best_foreign_name, ref_time, best_foreign_ts)

            # Set anchor on first match
            if anchor_ref_ts is None:
                anchor_ref_ts = ref_time
                anchor_foreign_ts = best_foreign_ts
                offset = best_foreign_ts - ref_time
                logger.info(f"  Anchor established: ref {ref_time:.1f}s -> foreign {best_foreign_ts:.1f}s (offset {offset:+.1f}s, score {best_score:.3f})")

    progress_bar.close()
    del foreign_frame_cache  # Free memory

    match_elapsed_time = time.time() - match_start_time
    logger.info(f"  -> Anchor-and-follow matching complete. Found {len(initial_matches_dict)} potential pairs ({skipped_count} skipped). ({match_elapsed_time:.2f}s).")

    if not initial_matches_dict:
        logger.error("No initial matches found between reference and foreign frames. Cannot proceed.")
        return None

    # --- Step 4: Filter Similar Reference Images ---
    matches_after_sim_filter = filter_similar_ref_images(initial_matches_dict, ref_extract_path, similarity_threshold)
    if not matches_after_sim_filter:
        logger.error("No matches remaining after similarity filtering.")
        return None

    # --- Step 5: Filter Temporal Inconsistencies ---
    # Result is a list: [(ref_filename, foreign_filename, ref_time, foreign_time), ...]
    visual_anchors_details = filter_temporal_inconsistency(matches_after_sim_filter)
    if not visual_anchors_details:
        logger.error("No matches remaining after temporal filtering.")
        return None

    final_anchor_count = len(visual_anchors_details)
    stage_elapsed_time = time.time() - stage_start_time
    logger.info(f"---=== Image Pairing Stage Finished ({stage_elapsed_time:.2f}s). Generated {final_anchor_count} visual anchors ===---")
    return visual_anchors_details # Return list of detailed anchor tuples


# --- Audio Anchor Pairing (fallback for video pairs where template matching is unreliable) ---

def get_video_fps(video_path):
    """Return the primary video stream's frame rate as a float using ffprobe.

    Uses avg_frame_rate rather than r_frame_rate: for VFR-flagged AVI/Xvid content,
    r_frame_rate can report a bogus "least common multiple" value (e.g. 21845/911)
    instead of the actual nominal rate, while avg_frame_rate reflects real playback speed.
    """
    global FFPROBE_EXEC
    try:
        result = subprocess.run(
            [FFPROBE_EXEC, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate,r_frame_rate",
             "-of", "default=noprint_wrappers=1", video_path],
            capture_output=True, text=True, check=True, timeout=30)
        rates = {}
        for line in result.stdout.strip().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                rates[key.strip()] = value.strip()

        def _parse(rate_str):
            if not rate_str or rate_str == "0/0":
                return None
            if "/" in rate_str:
                num, den = rate_str.split("/")
                den_f = float(den)
                return float(num) / den_f if den_f != 0 else None
            return float(rate_str)

        return _parse(rates.get("avg_frame_rate")) or _parse(rates.get("r_frame_rate"))
    except Exception as e:
        logger.warning(f"Could not determine fps for {os.path.basename(video_path)}: {e}")
        return None


def compute_auto_source_tempo(ref_video, foreign_video):
    """Return the global source-to-reference speed factor from effective video FPS.

    ``avg_frame_rate`` is preferred by ``get_video_fps`` because some AVI/Xvid
    files expose an unusable ``r_frame_rate`` value. The returned factor is
    used only to normalize the source timeline before audio comparison.
    """
    ref_fps = get_video_fps(ref_video)
    foreign_fps = get_video_fps(foreign_video)
    if not ref_fps or not foreign_fps:
        logger.warning("  Could not auto-detect effective FPS for global normalization; using factor=1.0.")
        return 1.0
    tempo = ref_fps / foreign_fps
    logger.info(f"  Global FPS normalization: source {foreign_fps:.6f} -> reference {ref_fps:.6f} (factor={tempo:.9f})")
    return tempo


def resolve_anchor_stream_indices(args):
    """Resolve the original-audio stream indices used for anchor detection."""
    ref_idx = args.ref_stream_idx
    if ref_idx is None:
        ref_idx = find_audio_stream_index_by_lang(get_stream_info(args.ref_video), args.ref_lang)
    foreign_idx = getattr(args, 'foreign_anchor_stream_idx', None)
    if foreign_idx is None:
        # Backward compatibility: before the roles were separated, the primary
        # foreign track was also used as the audio anchor stream.
        foreign_idx = args.foreign_stream_idx
    if foreign_idx is None:
        foreign_idx = find_audio_stream_index_by_lang(get_stream_info(args.foreign_video), args.foreign_lang)
    return ref_idx, foreign_idx


def _import_audio_alignment():
    """Import the sibling audio_alignment module regardless of the current working directory."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    import audio_alignment as aa
    return aa


def _direct_similarity(a, b):
    """Zero-lag normalized similarity between two equal-length audio arrays."""
    a = a.astype(np.float64); a = a - a.mean()
    b = b.astype(np.float64); b = b - b.mean()
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom < 1e-9:
        return -1.0
    return float(np.dot(a, b) / denom)


def _locate_transition_point(reference, source, aa, sample_rate, ref_lo, ref_hi,
                              offset_before, offset_after,
                              probe_window_seconds=1.0, precision_seconds=0.02, max_iterations=25):
    """Binary-search the exact reference time where content switches from offset_before
    to offset_after, so only that instant needs correcting instead of stretching the
    whole (often 30-120s) window between two coarse audio anchors.

    The probe window shrinks as the search narrows: a wide window is more robust for the
    first few iterations (far from the boundary), but a window comparable to or larger than
    the remaining [lo, hi] gap biases the result by up to half the window size, since it then
    straddles content from both sides of the true transition.
    """
    def _measure_state(probe_ref_time, expected_offset, window_seconds=4.0):
        window_samples = int(window_seconds * sample_rate)
        ref_start = int(probe_ref_time * sample_rate)
        ref_end = ref_start + window_samples
        source_start = int((probe_ref_time + expected_offset) * sample_rate)
        source_end = source_start + window_samples
        if (ref_start < 0 or ref_end > len(reference)
                or source_start < 0 or source_end > len(source)):
            return None
        reference_window = reference[ref_start:ref_end]
        source_window = source[source_start:source_end]
        waveform_similarity = _direct_similarity(reference_window, source_window)
        reference_envelope, envelope_rate = aa._envelope(reference_window, sample_rate)
        source_envelope, _ = aa._envelope(source_window, sample_rate)
        envelope_similarity = (_direct_similarity(reference_envelope, source_envelope)
                               if len(reference_envelope) and len(source_envelope) else -1.0)
        return {
            "matches_expected": max(waveform_similarity, envelope_similarity) >= 0.15,
            "waveform_similarity": waveform_similarity,
            "envelope_similarity": envelope_similarity,
        }

    # A binary search is unreliable across a quiet/ambiguous interval: it can select an
    # arbitrary midpoint rather than the natural pause between two otherwise stable states.
    # First find consecutive one-second probes that independently favor each endpoint
    # offset, then place the edit at the lowest-energy point between those two runs.
    state_samples = []
    for probe_time in np.arange(ref_lo, ref_hi + 1e-6, 1.0):
        before_measurement = _measure_state(probe_time, offset_before)
        after_measurement = _measure_state(probe_time, offset_after)
        if before_measurement is None or after_measurement is None:
            continue
        before_matches = before_measurement["matches_expected"]
        after_matches = after_measurement["matches_expected"]
        if before_matches and not after_matches:
            state = "before"
        elif after_matches and not before_matches:
            state = "after"
        else:
            state = "ambiguous"
        logger.debug(
            f"    Transition probe ref {probe_time:.3f}s: {state}; "
            f"before W={before_measurement['waveform_similarity']:+.3f}, "
            f"E={before_measurement['envelope_similarity']:+.3f}; "
            f"after W={after_measurement['waveform_similarity']:+.3f}, "
            f"E={after_measurement['envelope_similarity']:+.3f}"
        )
        state_samples.append((probe_time, state))

    def _runs_for(state):
        runs = []
        current = []
        for sample in state_samples:
            if sample[1] == state:
                if current and sample[0] - current[-1][0] > 1.01:
                    current = []
                current.append(sample)
            elif current:
                if len(current) >= 2:
                    runs.append(current)
                current = []
        if len(current) >= 2:
            runs.append(current)
        return runs

    before_runs = _runs_for("before")
    after_runs = _runs_for("after")
    for before_run in reversed(before_runs):
        for after_run in after_runs:
            band_start = before_run[-1][0]
            band_end = after_run[0][0]
            if 0 < band_end - band_start <= 45.0:
                start_sample = int(band_start * sample_rate)
                end_sample = int(band_end * sample_rate)
                frame_samples = max(1, int(0.05 * sample_rate))
                samples = reference[start_sample:end_sample]
                frame_count = len(samples) // frame_samples
                if frame_count:
                    frames = samples[:frame_count * frame_samples].reshape(frame_count, frame_samples)
                    rms = np.sqrt(np.mean(np.square(frames.astype(np.float64)), axis=1))
                    return band_start + int(np.argmin(rms)) * 0.05, True

    def _matches_before(probe_ref_time, window_samples):
        ref_start = int(probe_ref_time * sample_rate)
        ref_end = ref_start + window_samples
        if ref_start < 0 or ref_end > len(reference):
            return None
        probe = reference[ref_start:ref_end]

        def _score(offset):
            src_start = int((probe_ref_time + offset) * sample_rate)
            src_end = src_start + window_samples
            if src_start < 0 or src_end > len(source):
                return -1.0
            return _direct_similarity(probe, source[src_start:src_end])

        score_before = _score(offset_before)
        score_after = _score(offset_after)
        if score_before < 0.15 and score_after < 0.15:
            return None  # inconclusive (e.g. silence at this probe point)
        return score_before >= score_after

    lo, hi = ref_lo, ref_hi
    for _ in range(max_iterations):
        gap = hi - lo
        if gap <= precision_seconds:
            break
        window_seconds = max(0.05, min(probe_window_seconds, gap / 3.0))
        window_samples = int(window_seconds * sample_rate)
        mid = (lo + hi) / 2.0
        matches_before = _matches_before(mid, window_samples)
        if matches_before is None:
            break  # inconclusive at this probe; keep current [lo, hi] bounds
        if matches_before:
            lo = mid
        else:
            hi = mid

    return (lo + hi) / 2.0, False


def _measure_local_audio_offset(reference, source, aa, sample_rate, ref_time,
                                expected_offset, window_seconds=8.0,
                                search_radius_seconds=8.0):
    """Measure one short local offset without relying on coarse anchor spacing."""
    window_samples = int(window_seconds * sample_rate)
    ref_start = max(0, int(ref_time * sample_rate))
    ref_end = min(len(reference), ref_start + window_samples)
    reference_window = reference[ref_start:ref_end]
    if len(reference_window) < window_samples // 2:
        return None

    expected_source_start = ref_time + expected_offset
    search_start = max(0, int((expected_source_start - search_radius_seconds) * sample_rate))
    search_end = min(len(source), int((expected_source_start + window_seconds + search_radius_seconds) * sample_rate))
    source_search = source[search_start:search_end]
    if len(source_search) < len(reference_window):
        return None

    try:
        waveform_offset, waveform_confidence = aa.correlate_offset(
            reference_window, source_search, sample_rate)
        envelope_offset, envelope_confidence = aa.correlate_envelope_offset(
            reference_window, source_search, sample_rate)
    except ValueError:
        return None

    search_start_seconds = search_start / sample_rate
    waveform_offset += search_start_seconds - ref_time
    envelope_offset += search_start_seconds - ref_time
    if max(waveform_confidence, envelope_confidence) < 2.0:
        return None
    if (waveform_confidence >= 2.0 and envelope_confidence >= 2.0
            and abs(waveform_offset - envelope_offset) > 0.15):
        return None
    return (envelope_offset, envelope_confidence) if envelope_confidence >= waveform_confidence else (waveform_offset, waveform_confidence)


def _write_transition_report_csv(path, reference, source, aa, sample_rate, anchor_offsets,
                                 anchors, jump_tolerance_seconds):
    """Write local one-second measurements for coarse anchor intervals up to one minute.

    This includes intervals whose endpoint offsets look stable: an editorial
    transition can occur inside a 30-second segment and be hidden when both
    coarse anchors happen to land on the same side of it.
    """
    rows = []
    pairs = sorted(zip(anchors, anchor_offsets), key=lambda pair: pair[0][2])
    for (left_anchor, left_offset), (right_anchor, right_offset) in zip(pairs, pairs[1:]):
        ref_start = left_anchor[2]
        ref_end = right_anchor[2]
        if ref_end - ref_start > 60.0:
            continue
        # Probe through the right anchor. The 4s window then crosses into the
        # post-transition material instead of ending four seconds before it.
        for ref_time in np.arange(ref_start, ref_end + 1e-6, 1.0):
            fraction = min(1.0, (ref_time - ref_start) / max(1e-9, ref_end - ref_start))
            expected_offset = left_offset + fraction * (right_offset - left_offset)
            window_start = int(ref_time * sample_rate)
            window_end = window_start + int(4.0 * sample_rate)
            reference_window = reference[window_start:window_end]
            if len(reference_window) < int(4.0 * sample_rate):
                continue
            source_start = max(0, int((ref_time + expected_offset - 5.0) * sample_rate))
            source_end = min(len(source), int((ref_time + expected_offset + 9.0) * sample_rate))
            source_window = source[source_start:source_end]
            if len(source_window) < len(reference_window):
                continue
            try:
                waveform_offset, waveform_confidence = aa.correlate_offset(
                    reference_window, source_window, sample_rate)
                envelope_offset, envelope_confidence = aa.correlate_envelope_offset(
                    reference_window, source_window, sample_rate)
            except ValueError:
                continue
            source_start_seconds = source_start / sample_rate
            waveform_offset += source_start_seconds - ref_time
            envelope_offset += source_start_seconds - ref_time
            reference_rms = np.sqrt(np.mean(np.square(reference_window.astype(np.float64))))
            rows.append([
                f"{ref_start:.3f}-{ref_end:.3f}", f"{left_offset:+.3f}",
                f"{right_offset:+.3f}", f"{ref_time:.3f}",
                f"{expected_offset:+.3f}", f"{waveform_offset:+.3f}",
                f"{envelope_offset:+.3f}", f"{waveform_confidence:.2f}",
                f"{envelope_confidence:.2f}",
                f"{20.0 * np.log10(reference_rms + 1e-9):.1f}",
            ])
    try:
        with open(path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow([
                "anchor_interval", "left_offset", "right_offset", "ref_time",
                "interpolated_offset", "waveform_offset", "envelope_offset",
                "waveform_confidence", "envelope_confidence", "reference_rms_db",
            ])
            writer.writerows(rows)
        logger.info(f"  -> Wrote {len(rows)} local transition measurements to {path}")
    except Exception as e:
        logger.warning(f"  Failed to write transition report CSV: {e}")


def _progressive_offset_probes(reference, source, aa, sample_rate, ref_lo, ref_hi,
                               offset_lo, offset_hi, jump_tolerance_seconds,
                               min_interval_seconds=2.0, max_depth=8):
    """Recursively probe a suspect interval until an offset change is localized."""
    probes = [(ref_lo, offset_lo), (ref_hi, offset_hi)]

    def subdivide(lo, hi, left_offset, right_offset, depth):
        if depth >= max_depth or hi - lo <= min_interval_seconds:
            return
        mid = (lo + hi) / 2.0
        expected_offset = (left_offset + right_offset) / 2.0
        measurement = _measure_local_audio_offset(
            reference, source, aa, sample_rate, mid, expected_offset)
        if measurement is None:
            return
        mid_offset, _ = measurement
        probes.append((mid, mid_offset))
        if abs(mid_offset - left_offset) > jump_tolerance_seconds:
            subdivide(lo, mid, left_offset, mid_offset, depth + 1)
        if abs(right_offset - mid_offset) > jump_tolerance_seconds:
            subdivide(mid, hi, mid_offset, right_offset, depth + 1)

    subdivide(ref_lo, ref_hi, offset_lo, offset_hi, 0)
    return sorted(probes)


def _has_two_sided_transition_evidence(reference, source, sample_rate, transition_time,
                                       offset_before, offset_after, window_seconds=2.0):
    """Confirm that a proposed cut separates its two endpoint offset states.

    A negative offset jump creates a silence/reference fill only when a short
    window before the cut prefers the old offset and a short window after it
    prefers the new one. This prevents a coarse-window offset change from
    creating a fill at an arbitrary point inside still-matching content.
    """
    window_samples = int(window_seconds * sample_rate)

    def score_at(start_time, offset):
        ref_start = int(start_time * sample_rate)
        ref_end = ref_start + window_samples
        source_start = int((start_time + offset) * sample_rate)
        source_end = source_start + window_samples
        if (ref_start < 0 or ref_end > len(reference)
                or source_start < 0 or source_end > len(source)):
            return None
        return _direct_similarity(reference[ref_start:ref_end], source[source_start:source_end])

    before_start = transition_time - window_seconds
    after_start = transition_time + 0.05
    before_old = score_at(before_start, offset_before)
    before_new = score_at(before_start, offset_after)
    after_old = score_at(after_start, offset_before)
    after_new = score_at(after_start, offset_after)
    if None in (before_old, before_new, after_old, after_new):
        return False
    margin = 0.06
    return before_old >= 0.15 and before_old - before_new >= margin and after_new >= 0.15 and after_new - after_old >= margin


def run_audio_pairing_stage(ref_video_path, foreign_video_path, ref_stream_idx, foreign_stream_idx,
                            source_tempo, window_seconds, step_seconds, min_confidence,
                            search_radius_seconds=20.0, agreement_seconds=0.15, jump_tolerance_seconds=0.15,
                            anchor_report_csv=None, transition_report_csv=None):
    """Generate sync anchors via audio cross-correlation instead of visual frame matching.

    Intended for pairs where resolution/compression/aspect-ratio mismatch makes template
    matching unreliable (e.g. an SD Xvid source vs an HEVC 1080p reference). Returns anchors
    in the same (ref_name, foreign_name, ref_time, foreign_time) tuple format produced by
    run_image_pairing_stage, so they flow through the existing filtering/sync pipeline unchanged.
    """
    global AUDIO_EDITORIAL_SOURCE_TEMPO
    aa = _import_audio_alignment()
    sample_rate = aa.DEFAULT_SAMPLE_RATE
    AUDIO_REPLACEMENT_RANGES.clear()
    AUDIO_HARD_CUT_RANGES.clear()
    AUDIO_EDITORIAL_EDITS.clear()

    logger.info("\n===== Audio Anchor Pairing Stage =====")
    stage_start_time = time.time()
    logger.info(f"  Ref stream: #{ref_stream_idx}, Foreign stream: #{foreign_stream_idx}, Source tempo: {source_tempo:.6f}")
    logger.info(f"  Window: {window_seconds:.1f}s, Step: {step_seconds:.1f}s, Min confidence: {min_confidence:.1f}, Search radius: {search_radius_seconds:.1f}s")

    logger.info("  -> Extracting full reference audio for anchor analysis (with loudness normalization)...")
    reference = aa.extract_mono_audio(ref_video_path, ref_stream_idx, sample_rate, normalize_loudness=True)
    logger.info("  -> Extracting full foreign audio (tempo-corrected) for anchor analysis (with loudness normalization)...")
    source = aa.extract_mono_audio(foreign_video_path, foreign_stream_idx, sample_rate, tempo=source_tempo, normalize_loudness=True)
    logger.info("  -> Extracting linear-gain analysis copies for correlation fallback...")
    reference_linear = aa.normalize_analysis_level(
        aa.extract_mono_audio(ref_video_path, ref_stream_idx, sample_rate))
    source_linear = aa.normalize_analysis_level(
        aa.extract_mono_audio(foreign_video_path, foreign_stream_idx, sample_rate, tempo=source_tempo))

    window_samples = int(window_seconds * sample_rate)
    step_samples = max(1, int(step_seconds * sample_rate))
    if len(reference) < window_samples:
        logger.error("  Reference audio shorter than one analysis window; cannot generate audio anchors.")
        return None

    anchors = []
    anchor_offsets = []  # tempo-corrected offset used for each accepted anchor, parallel to `anchors`
    candidate_measurements = []
    expected_offset = 0.0
    have_confirmed_baseline = False
    num_windows = max(1, (len(reference) - window_samples) // step_samples + 1)
    progress_bar = tqdm(total=num_windows, desc="  Scanning Audio Anchors", unit="window", ncols=100, leave=False)

    for ref_start in range(0, len(reference) - window_samples + 1, step_samples):
        progress_bar.update(1)
        ref_end = ref_start + window_samples
        reference_window = reference[ref_start:ref_end]
        ref_time = ref_start / sample_rate

        expected_source_start = ref_time + expected_offset
        search_start = max(0, int((expected_source_start - search_radius_seconds) * sample_rate))
        search_end = min(len(source), int((expected_source_start + window_seconds + search_radius_seconds) * sample_rate))
        source_search = source[search_start:search_end]
        if len(source_search) < window_samples:
            continue

        try:
            waveform_offset, waveform_confidence = aa.correlate_offset(reference_window, source_search, sample_rate)
            envelope_offset, envelope_confidence = aa.correlate_envelope_offset(reference_window, source_search, sample_rate)
        except ValueError:
            continue

        search_start_seconds = search_start / sample_rate
        waveform_offset += search_start_seconds - ref_time
        envelope_offset += search_start_seconds - ref_time

        candidate_measurements.append((
            ref_time,
            envelope_offset if envelope_confidence >= waveform_confidence else waveform_offset,
            waveform_offset,
            envelope_offset,
            waveform_confidence,
            envelope_confidence,
        ))

        best_confidence = max(waveform_confidence, envelope_confidence)
        candidate_offset = envelope_offset if envelope_confidence >= waveform_confidence else waveform_offset
        used_linear_gain = False
        both_reliable = waveform_confidence >= min_confidence and envelope_confidence >= min_confidence
        needs_linear_retry = (
            best_confidence < min_confidence
            or (both_reliable and abs(waveform_offset - envelope_offset) > agreement_seconds)
        )
        if needs_linear_retry:
            linear_reference_window = reference_linear[ref_start:ref_end]
            linear_source_search = source_linear[search_start:search_end]
            try:
                linear_waveform_offset, linear_waveform_confidence = aa.correlate_offset(
                    linear_reference_window, linear_source_search, sample_rate)
                linear_envelope_offset, linear_envelope_confidence = aa.correlate_envelope_offset(
                    linear_reference_window, linear_source_search, sample_rate)
                linear_waveform_offset += search_start_seconds - ref_time
                linear_envelope_offset += search_start_seconds - ref_time
                linear_best_confidence = max(linear_waveform_confidence, linear_envelope_confidence)
                linear_both_reliable = (linear_waveform_confidence >= min_confidence
                                        and linear_envelope_confidence >= min_confidence)
                if (linear_best_confidence >= min_confidence
                        and (not linear_both_reliable
                             or abs(linear_waveform_offset - linear_envelope_offset) <= agreement_seconds)):
                    waveform_offset, waveform_confidence = linear_waveform_offset, linear_waveform_confidence
                    envelope_offset, envelope_confidence = linear_envelope_offset, linear_envelope_confidence
                    best_confidence = linear_best_confidence
                    candidate_offset = (envelope_offset if envelope_confidence >= waveform_confidence
                                        else waveform_offset)
                    both_reliable = linear_both_reliable
                    used_linear_gain = True
            except ValueError:
                pass
        # A window too weak to accept on its own correlation strength can still be trusted if
        # its offset lands almost exactly where the already-confirmed constant (FPS-corrected)
        # speed predicts it should be - that coincidence is itself strong corroborating evidence,
        # and it's exactly the kind of low-confidence window that otherwise leaves large gaps
        # between accepted anchors (e.g. a quiet passage within an otherwise matching stretch).
        offset_matches_baseline = (
            have_confirmed_baseline
            and abs(candidate_offset - expected_offset) <= agreement_seconds
        )
        relaxed_min_confidence = max(1.0, min_confidence * 0.5)
        if best_confidence < min_confidence:
            if not (offset_matches_baseline and best_confidence >= relaxed_min_confidence):
                continue
        if best_confidence >= min_confidence and both_reliable and abs(waveform_offset - envelope_offset) > agreement_seconds:
            logger.debug(f"    Window {ref_time:.1f}s: waveform/envelope disagree ({waveform_offset:.3f}s vs {envelope_offset:.3f}s), skipping.")
            continue

        if envelope_confidence >= waveform_confidence:
            offset, method = envelope_offset, "envelope"
        else:
            offset, method = waveform_offset, "waveform"
        if used_linear_gain:
            method += " linear-gain"
        if best_confidence < min_confidence:
            method += " offset-consistency"
        else:
            have_confirmed_baseline = True

        # offset/source_time are in the tempo-corrected timeline; map back to the real foreign audio timeline.
        # offset is positive when the foreign audio lags the reference, so the matching
        # foreign position is LATER by that amount: source_time = ref_time + offset.
        tempo_corrected_source_time = ref_time + offset
        if tempo_corrected_source_time < 0:
            continue
        foreign_time = tempo_corrected_source_time * source_tempo

        anchor_name = f"AUDIO_ANCHOR_{len(anchors)+1:04d}"
        anchors.append((f"{anchor_name}_ref", f"{anchor_name}_foreign", ref_time, foreign_time))
        anchor_offsets.append(offset)
        expected_offset = offset
        logger.info(f"  Anchor: ref {ref_time:.1f}s -> foreign {foreign_time:.1f}s (offset {offset:+.3f}s, {method}, conf {best_confidence:.2f})")

    progress_bar.close()

    # A long editorially different opening can make individual 30-60 second
    # windows look ambiguous even though several later windows agree on the
    # same offset. Recover that stable consensus without accepting isolated
    # correlation peaks as anchors.
    consensus_candidates = [
        item for item in candidate_measurements
        if max(item[4], item[5]) >= 1.1
        and abs(item[2] - item[3]) <= agreement_seconds
    ]
    if len(consensus_candidates) >= 3:
        consensus_offset = float(np.median([item[1] for item in consensus_candidates]))
        consensus_anchors = []
        for item in consensus_candidates:
            if abs(item[1] - consensus_offset) > 0.35:
                continue
            ref_time, offset = item[0], item[1]
            foreign_time = (ref_time + offset) * source_tempo
            if foreign_time < 0:
                continue
            consensus_anchors.append((
                f"AUDIO_CONSENSUS_{len(consensus_anchors)+1:04d}_ref",
                f"AUDIO_CONSENSUS_{len(consensus_anchors)+1:04d}_foreign",
                ref_time,
                foreign_time,
            ))
        if len(consensus_anchors) > len(anchors):
            anchors = consensus_anchors
            anchor_offsets = [item[1] for item in consensus_candidates
                              if abs(item[1] - consensus_offset) <= 0.35
                              and (item[0] + item[1]) * source_tempo >= 0]
            logger.info(
                f"  Consensus recovery: retained {len(anchors)} anchors around "
                f"offset {consensus_offset:+.3f}s from {len(consensus_candidates)} "
                "concordant windows"
            )

    stage_elapsed_time = time.time() - stage_start_time
    if not anchors:
        logger.error("  No audio anchors passed confidence/agreement thresholds.")
        return None

    first_full_anchor = min(anchors, key=lambda item: item[2])
    last_full_anchor = anchors[-1]
    last_full_anchor_offset = anchor_offsets[-1]
    if first_full_anchor[2] > window_seconds * 0.5:
        partial_anchor = aa.find_partial_anchor(
            reference,
            source,
            sample_rate,
            reference_end=first_full_anchor[2],
            source_end=first_full_anchor[3] / source_tempo,
            window_seconds=min(2.0, window_seconds / 10.0),
            step_seconds=0.5,
            search_radius_seconds=8.0,
            min_confidence=1.5,
            agreement_seconds=0.2,
            min_consistent_matches=2,
        )
        if partial_anchor is not None and partial_anchor.reference_time < first_full_anchor[2]:
            partial_foreign_time = partial_anchor.source_time * source_tempo
            anchors.insert(0, (
                "AUDIO_PARTIAL_0001_ref",
                "AUDIO_PARTIAL_0001_foreign",
                partial_anchor.reference_time,
                partial_foreign_time,
            ))
            anchor_offsets.insert(0, partial_anchor.offset_seconds)
            logger.info(
                f"  Partial anchor recovered: ref {partial_anchor.reference_time:.3f}s -> "
                f"foreign {partial_foreign_time:.3f}s (offset {partial_anchor.offset_seconds:+.3f}s, "
                f"confidence {partial_anchor.confidence:.2f})"
            )

    # Symmetric recovery at the tail end: the last accepted full-window anchor can sit
    # well before the true end of the content (e.g. an editorially different closing
    # stretch), leaving the final segment stretched across a much larger gap than any
    # other segment and prone to audible drift in its last seconds. Search the interval
    # after the last anchor for the LATEST short fragment that still matches, using the
    # last anchor's offset (not zero) as the search center since drift may have accumulated.
    reference_total_duration = len(reference) / sample_rate
    source_total_duration = len(source) / sample_rate
    if reference_total_duration - last_full_anchor[2] > window_seconds * 0.5:
        partial_end_anchor = aa.find_partial_anchor(
            reference,
            source,
            sample_rate,
            reference_start=last_full_anchor[2],
            reference_end=reference_total_duration,
            source_end=source_total_duration,
            expected_offset=last_full_anchor_offset,
            window_seconds=min(2.0, window_seconds / 10.0),
            step_seconds=0.5,
            search_radius_seconds=8.0,
            min_confidence=1.5,
            agreement_seconds=0.2,
            min_consistent_matches=2,
            prefer='latest',
        )
        if partial_end_anchor is not None and partial_end_anchor.reference_time > last_full_anchor[2]:
            partial_end_foreign_time = partial_end_anchor.source_time * source_tempo
            anchors.append((
                "AUDIO_PARTIAL_END_0001_ref",
                "AUDIO_PARTIAL_END_0001_foreign",
                partial_end_anchor.reference_time,
                partial_end_foreign_time,
            ))
            anchor_offsets.append(partial_end_anchor.offset_seconds)
            logger.info(
                f"  Partial end-anchor recovered: ref {partial_end_anchor.reference_time:.3f}s -> "
                f"foreign {partial_end_foreign_time:.3f}s (offset {partial_end_anchor.offset_seconds:+.3f}s, "
                f"confidence {partial_end_anchor.confidence:.2f})"
            )

    # A 60-second window can hide a clean short match inside a mixed dialogue/music/silence
    # section. Rescan only the resulting large gaps at 10-second windows every 5 seconds.
    # Each dense measurement must agree with a direct neighbour before becoming an anchor;
    # this rejects an isolated correlation peak while retaining continuous matching runs.
    dense_gap_threshold = step_seconds
    dense_window_seconds = 10.0
    dense_step_seconds = 5.0
    dense_min_confidence = 1.5
    dense_candidates = []
    anchor_pairs = sorted(zip(anchors, anchor_offsets), key=lambda pair: pair[0][2])
    for (left_anchor, left_offset), (right_anchor, right_offset) in zip(anchor_pairs, anchor_pairs[1:]):
        gap_start, gap_end = left_anchor[2], right_anchor[2]
        if gap_end - gap_start <= dense_gap_threshold:
            continue
        gap_candidates = []
        for dense_time in np.arange(gap_start + dense_step_seconds, gap_end - dense_window_seconds + 1e-6, dense_step_seconds):
            fraction = (dense_time - gap_start) / (gap_end - gap_start)
            expected_dense_offset = left_offset + fraction * (right_offset - left_offset)
            source_start = max(0, int((dense_time + expected_dense_offset - 5.0) * sample_rate))
            source_end = min(len(source), int((dense_time + expected_dense_offset + dense_window_seconds + 5.0) * sample_rate))
            ref_start = int(dense_time * sample_rate)
            ref_end = ref_start + int(dense_window_seconds * sample_rate)
            if ref_end > len(reference) or source_end - source_start < ref_end - ref_start:
                continue
            try:
                wave_offset, wave_confidence = aa.correlate_offset(
                    reference[ref_start:ref_end], source[source_start:source_end], sample_rate)
                envelope_offset, envelope_confidence = aa.correlate_envelope_offset(
                    reference[ref_start:ref_end], source[source_start:source_end], sample_rate)
            except ValueError:
                continue
            source_start_seconds = source_start / sample_rate
            wave_offset += source_start_seconds - dense_time
            envelope_offset += source_start_seconds - dense_time
            best_confidence = max(wave_confidence, envelope_confidence)
            if (best_confidence < dense_min_confidence
                    or (wave_confidence >= dense_min_confidence
                        and envelope_confidence >= dense_min_confidence
                        and abs(wave_offset - envelope_offset) > agreement_seconds)):
                continue
            offset = envelope_offset if envelope_confidence >= wave_confidence else wave_offset
            method = "envelope" if envelope_confidence >= wave_confidence else "waveform"
            gap_candidates.append((dense_time, offset, best_confidence, method))

        for candidate_index, candidate in enumerate(gap_candidates):
            dense_time, offset, confidence, method = candidate
            neighbours = gap_candidates[max(0, candidate_index - 1):candidate_index] + gap_candidates[candidate_index + 1:candidate_index + 2]
            if not any(abs(offset - neighbour[1]) <= agreement_seconds for neighbour in neighbours):
                continue
            dense_candidates.append(candidate)

    for dense_index, (dense_time, offset, confidence, method) in enumerate(dense_candidates, start=1):
        foreign_time = (dense_time + offset) * source_tempo
        anchors.append((f"AUDIO_DENSE_{dense_index:04d}_ref", f"AUDIO_DENSE_{dense_index:04d}_foreign", dense_time, foreign_time))
        anchor_offsets.append(offset)
        logger.info(f"  Dense anchor: ref {dense_time:.3f}s -> foreign {foreign_time:.3f}s "
                    f"(offset {offset:+.3f}s, {method}, conf {confidence:.2f})")
    if dense_candidates:
        ordered_anchors = sorted(zip(anchors, anchor_offsets), key=lambda pair: pair[0][2])
        anchors[:] = [item[0] for item in ordered_anchors]
        anchor_offsets[:] = [item[1] for item in ordered_anchors]

    # Densify large stable gaps: a wide span between two anchors that broadly agree on offset
    # is usually safe to stretch smoothly, but relying on a single 60-120s atempo pass over it
    # is fragile if correlation confidence was only marginal throughout. Recover extra confirmed
    # short-window anchors near both edges of any such gap, using the same run-based scan
    # already used to reach the head/tail.
    densify_gap_threshold = step_seconds * 2.0
    short_window = min(2.0, window_seconds / 10.0)
    new_densify_anchors = []
    for i in range(len(anchors) - 1):
        ref_lo, offset_lo = anchors[i][2], anchor_offsets[i]
        ref_hi, offset_hi = anchors[i + 1][2], anchor_offsets[i + 1]
        if ref_hi - ref_lo <= densify_gap_threshold or abs(offset_hi - offset_lo) > jump_tolerance_seconds:
            continue
        found_candidates = []
        for label, prefer, expected in (("a", "earliest", offset_lo), ("b", "latest", offset_hi)):
            found = aa.find_partial_anchor(
                reference, source, sample_rate,
                reference_start=ref_lo, reference_end=ref_hi,
                source_end=source_total_duration,
                expected_offset=expected,
                window_seconds=short_window, step_seconds=0.5,
                search_radius_seconds=8.0, min_confidence=1.5,
                agreement_seconds=0.2, min_consistent_matches=2,
                prefer=prefer,
            )
            if found is None or found.reference_time <= ref_lo + 1.0 or found.reference_time >= ref_hi - 1.0:
                continue
            if found_candidates and abs(found.reference_time - found_candidates[0][1].reference_time) <= short_window * 2.0:
                continue  # same run already found from the other edge
            found_candidates.append((label, found))
        for label, found in found_candidates:
            foreign_time = found.source_time * source_tempo
            new_densify_anchors.append((found.reference_time, f"AUDIO_DENSIFY_{i+1:04d}{label}", foreign_time, found.offset_seconds))
            logger.info(
                f"  Densified gap {ref_lo:.1f}s-{ref_hi:.1f}s: recovered anchor at ref {found.reference_time:.3f}s -> "
                f"foreign {foreign_time:.3f}s (offset {found.offset_seconds:+.3f}s, confidence {found.confidence:.2f})"
            )
    for ref_time, name, foreign_time, offset in sorted(new_densify_anchors, key=lambda item: item[0]):
        insert_at = next((idx for idx, a in enumerate(anchors) if a[2] > ref_time), len(anchors))
        anchors.insert(insert_at, (f"{name}_ref", f"{name}_foreign", ref_time, foreign_time))
        anchor_offsets.insert(insert_at, offset)

    if transition_report_csv:
        _write_transition_report_csv(
            transition_report_csv, reference, source, aa, sample_rate,
            anchor_offsets, anchors, jump_tolerance_seconds,
        )

    silence_differences = aa.compare_silence_profiles(
        reference,
        source,
        sample_rate,
        # Piecewise offset per anchor, not just the first one - offset drifts across the file,
        # and a single global value misses real silence differences wherever it's stale.
        offset=list(zip((anchor[2] for anchor in anchors), anchor_offsets)),
        threshold_db=-40.0,
        min_duration=0.2,
        tolerance_seconds=0.25,
    )
    if silence_differences:
        logger.info(f"  Detected {len(silence_differences)} candidate silence difference(s) after FPS normalization.")
        editorial_edits = aa.build_editorial_edit_recipe(silence_differences, source_tempo=source_tempo)
        AUDIO_EDITORIAL_EDITS.extend(editorial_edits)
        AUDIO_EDITORIAL_SOURCE_TEMPO = source_tempo
        logger.info(f"  Built {len(editorial_edits)} editorial edit(s) for Source-Audio recipe application.")
        for difference in silence_differences:
            logger.info(
                f"  Silence candidate: Reference {difference.reference_start:.3f}s-"
                f"{difference.reference_end:.3f}s, Source "
                f"{difference.source_start:.3f}s-{difference.source_end:.3f}s, "
                f"Source silence is {difference.kind.replace('_', ' ')} by "
                f"{abs(difference.duration_difference):.3f}s"
            )
        for edit in editorial_edits:
            logger.info(
                f"  Editorial edit: {edit.operation} @ Source {edit.source_start:.3f}s-"
                f"{edit.source_end:.3f}s, Reference {edit.reference_position:.3f}s, "
                f"shift={edit.cumulative_shift_seconds:+.3f}s, reason={edit.reason}"
            )
        corrected_source = aa.apply_editorial_edit_recipe(
            source_audio=source,
            reference_audio=reference,
            edits=editorial_edits,
            sample_rate=sample_rate,
        )
        logger.info(
            f"  Applied the editorial recipe to the Source signal: "
            f"{len(source)/sample_rate:.3f}s -> {len(corrected_source)/sample_rate:.3f}s"
        )
    else:
        logger.info("  No significant matched silence-duration differences detected.")

    logger.info(f"---=== Audio Anchor Pairing Stage Finished ({stage_elapsed_time:.2f}s). Generated {len(anchors)} audio anchors ===---")

    # --- Refinement: locate precise transition points for abrupt offset jumps ---
    # A large, sudden offset change between consecutive anchors usually means a real editorial
    # difference (extra/missing shot), not gradual drift. Left alone, the generic segment
    # stretcher smears that jump across the whole 30-120s window between anchors, audibly
    # changing pace/pitch throughout. Instead, pinpoint the exact instant and bracket it with
    # a pair of anchors a few tens of milliseconds apart so only that near-instant absorbs the jump.
    refined_anchors = list(anchors)
    transition_count = 0

    # The first accepted window may be well after the first editorial cut. Use
    # the beginning of the extracted timelines as the initial offset instead of
    # stretching the entire pre-anchor interval.
    initial_offset = anchors[0][3] / source_tempo - anchors[0][2]
    initial_jump = anchor_offsets[0] - initial_offset
    first_ref_time = anchors[0][2]
    if initial_offset < -jump_tolerance_seconds:
        # The first anchor can be correct while the reference has a short prefix
        # that is absent from the foreign recording. Preserve that prefix instead
        # of stretching the whole first stable interval to reach the anchor.
        content_start_index = np.flatnonzero(np.abs(reference) > 1e-4)
        reference_content_start = (content_start_index[0] / sample_rate
                                   if len(content_start_index) else 0.0)
        replacement_start = reference_content_start
        replacement_end = min(first_ref_time, replacement_start - initial_offset)
        if replacement_end - replacement_start > jump_tolerance_seconds:
            transition_count += 1
            replacement_id = f"AUDIO_REPLACEMENT_{transition_count:04d}"
            replacement_foreign_time = 0.0
            # Copying HQ reference content only makes sense if the cut point is a
            # natural pause; otherwise it would hard-cut mid-note/mid-phrase.
            use_silence = not aa.is_safe_splice_point(reference, sample_rate, replacement_end)
            AUDIO_REPLACEMENT_RANGES.append({
                "id": replacement_id,
                "ref_start": replacement_start,
                "ref_end": replacement_end,
                "foreign_splice_time": replacement_foreign_time,
                "use_silence": use_silence,
            })
            refined_anchors.append((f"{replacement_id}_a_ref", f"{replacement_id}_a_foreign",
                                     replacement_start, replacement_foreign_time))
            refined_anchors.append((f"{replacement_id}_b_ref", f"{replacement_id}_b_foreign",
                                     replacement_end, replacement_foreign_time))
            fill_desc = "silence (unsafe mid-content splice point)" if use_silence else "reference audio"
            logger.info(f"  Located early missing foreign prefix at ref {replacement_start:.3f}s-"
                        f"{replacement_end:.3f}s (initial offset {initial_offset:+.3f}s) -> "
                        f"filling it with {fill_desc} instead of stretching the first segment")
    elif abs(initial_jump) > jump_tolerance_seconds:
        transition_ref_time, transition_validated = _locate_transition_point(
            reference, source, aa, sample_rate, 0.0, first_ref_time,
            initial_offset, anchor_offsets[0])
        if initial_jump < 0 and transition_validated:
            missing_ref_duration = abs(initial_jump)
            replacement_start = transition_ref_time
            replacement_end = replacement_start + missing_ref_duration
            if replacement_start > 0.0 and replacement_end < first_ref_time:
                transition_count += 1
                replacement_id = f"AUDIO_REPLACEMENT_{transition_count:04d}"
                use_silence = not aa.is_safe_splice_point(reference, sample_rate, replacement_end)
                AUDIO_REPLACEMENT_RANGES.append({
                    "id": replacement_id,
                    "ref_start": replacement_start,
                    "ref_end": replacement_end,
                    "foreign_splice_time": replacement_foreign_time,
                    "use_silence": use_silence,
                })
                replacement_foreign_time = (replacement_start + initial_offset) * source_tempo
                refined_anchors.append((f"{replacement_id}_a_ref", f"{replacement_id}_a_foreign",
                                         replacement_start, replacement_foreign_time))
                refined_anchors.append((f"{replacement_id}_b_ref", f"{replacement_id}_b_foreign",
                                         replacement_end, replacement_foreign_time))
                fill_desc = "silence (unsafe mid-content splice point)" if use_silence else "reference audio"
                logger.info(f"  Located early missing foreign interval at ref {replacement_start:.3f}s-"
                            f"{replacement_end:.3f}s (jump {initial_jump:+.3f}s) -> "
                            f"filling it with {fill_desc} instead of stretching the first segment")

    progressive_candidates = []
    progressive_intervals = [(0.0, anchors[0][2], initial_offset, anchor_offsets[0])]
    progressive_intervals.extend(
        (anchors[i][2], anchors[i + 1][2], anchor_offsets[i], anchor_offsets[i + 1])
        for i in range(len(anchors) - 1)
    )
    # Mirror the head interval: the tail beyond the last anchor was never probed at all,
    # so an offset drift/jump in the closing stretch (e.g. a differently-edited ending)
    # silently got smeared across the whole final segment instead of being localized.
    progressive_intervals.append(
        (anchors[-1][2], reference_total_duration, anchor_offsets[-1], anchor_offsets[-1])
    )
    for interval_start, interval_end, interval_offset_start, interval_offset_end in progressive_intervals:
        probes = _progressive_offset_probes(
            reference, source, aa, sample_rate,
            interval_start, interval_end,
            interval_offset_start, interval_offset_end,
            jump_tolerance_seconds,
        )
        if any(abs(right_offset - left_offset) > jump_tolerance_seconds
               for (_, left_offset), (_, right_offset) in zip(probes, probes[1:])):
            progressive_candidates.append(
                (interval_start, interval_end, interval_offset_start, interval_offset_end))

    for ref_time_i, ref_time_j, offset_i, offset_j in progressive_candidates:

        # A coarse anchor is evidence for its following window, not proof that a
        # transition happened at its timestamp. When silence lies near that boundary,
        # inspect into the next confirmed anchor (capped at 30s) to establish the
        # post-transition state before choosing a cut point in the ambiguous band.
        following_anchor_time = next(
            (anchor[2] for anchor in anchors if anchor[2] > ref_time_j + 0.001),
            ref_time_j + 30.0,
        )
        search_hi = min(following_anchor_time, ref_time_j + 30.0, len(reference) / sample_rate)
        if search_hi > ref_time_j:
            logger.debug(f"    Extending transition evidence from ref {ref_time_j:.3f}s to {search_hi:.3f}s")
        transition_ref_time, transition_validated = _locate_transition_point(
            reference, source, aa, sample_rate, ref_time_i, search_hi, offset_i, offset_j)

        delta = (offset_j - offset_i) * source_tempo
        before_foreign_time = (transition_ref_time + offset_i) * source_tempo

        # A negative jump means the foreign timeline moves backwards: the foreign
        # edit is missing a piece that exists in the reference. Represent that
        # missing piece as a real segment, to be filled from the reference audio.
        if delta < 0:
            missing_ref_duration = abs(delta) / source_tempo
            replacement_start = transition_ref_time
            replacement_end = transition_ref_time + missing_ref_duration
            if not transition_validated:
                logger.info(f"  Skipping unsupported missing-foreign fill near ref {transition_ref_time:.3f}s: "
                            "the local transition classifier found no two-sided evidence")
                continue
            if (replacement_start > ref_time_i and replacement_end < search_hi
                    and before_foreign_time >= 0):
                transition_count += 1
                replacement_id = f"AUDIO_REPLACEMENT_{transition_count:04d}"
                # This gap is spliced in on both sides, so both boundaries must be safe cut points.
                use_silence = not (aa.is_safe_splice_point(reference, sample_rate, replacement_start)
                                    and aa.is_safe_splice_point(reference, sample_rate, replacement_end))
                AUDIO_REPLACEMENT_RANGES.append({
                    "id": replacement_id,
                    "ref_start": replacement_start,
                    "ref_end": replacement_end,
                    "foreign_splice_time": before_foreign_time,
                    "use_silence": use_silence,
                })
                refined_anchors.append((f"{replacement_id}_a_ref", f"{replacement_id}_a_foreign",
                                         replacement_start, before_foreign_time))
                refined_anchors.append((f"{replacement_id}_b_ref", f"{replacement_id}_b_foreign",
                                         replacement_end, before_foreign_time))
                fill_desc = "silence (unsafe mid-content splice point)" if use_silence else "reference audio"
                logger.info(f"  Located missing foreign interval at ref {replacement_start:.3f}s-"
                            f"{replacement_end:.3f}s (jump {delta:+.3f}s) -> "
                            f"filling it with {fill_desc} instead of silence/stretching")
            else:
                logger.debug(f"    Skipping audio replacement near ref {transition_ref_time:.3f}s: "
                             f"not enough room in [{ref_time_i:.3f}, {search_hi:.3f}]")
            continue

        epsilon = max(0.05, min(0.5, abs(delta) / 50.0))
        before_ref_time = transition_ref_time - epsilon
        after_ref_time = transition_ref_time + epsilon
        if before_ref_time <= ref_time_i or after_ref_time >= search_hi:
            logger.debug(f"    Skipping transition refinement near ref {transition_ref_time:.3f}s: not enough room in [{ref_time_i:.3f}, {search_hi:.3f}]")
            continue

        before_foreign_time = (before_ref_time + offset_i) * source_tempo
        after_foreign_time = (after_ref_time + offset_j) * source_tempo
        if after_foreign_time <= before_foreign_time or before_foreign_time < 0:
            logger.debug(f"    Skipping transition refinement near ref {transition_ref_time:.3f}s: non-monotonic foreign times")
            continue

        previous_foreign_time = max(
            (anchor[3] for anchor in refined_anchors if anchor[2] <= before_ref_time),
            default=float("-inf"),
        )
        next_foreign_time = min(
            (anchor[3] for anchor in refined_anchors if anchor[2] >= after_ref_time),
            default=float("inf"),
        )
        if before_foreign_time < previous_foreign_time or after_foreign_time > next_foreign_time:
            logger.debug(f"    Skipping transition refinement near ref {transition_ref_time:.3f}s: "
                         "would reverse time relative to neighboring anchors")
            continue

        transition_count += 1
        transition_id = f"AUDIO_TRANSITION_{transition_count:04d}"
        AUDIO_HARD_CUT_RANGES.append({
            "id": transition_id,
            "ref_start": before_ref_time,
            "ref_end": after_ref_time,
            "foreign_start": before_foreign_time,
            "foreign_end": after_foreign_time,
        })
        refined_anchors.append((f"{transition_id}a_ref", f"{transition_id}a_foreign",
                                 before_ref_time, before_foreign_time))
        refined_anchors.append((f"{transition_id}b_ref", f"{transition_id}b_foreign",
                                 after_ref_time, after_foreign_time))
        logger.info(f"  Located precise transition at ref {transition_ref_time:.3f}s (jump {delta:+.3f}s) -> "
                    f"hard-cut anchors at {before_ref_time:.3f}s/{after_ref_time:.3f}s instead of stretching the "
                    f"surrounding {ref_time_j - ref_time_i:.1f}s window")

    if transition_count > 0:
        refined_anchors.sort(key=lambda a: a[2])
        logger.info(f"  -> Refined {transition_count} abrupt offset jump(s) into near-instant hard cuts.")
        final_anchors = refined_anchors
    else:
        final_anchors = anchors

    if anchor_report_csv:
        _write_anchor_report_csv(anchor_report_csv, candidate_measurements, anchors, final_anchors, min_confidence)

    return final_anchors


def _write_anchor_report_csv(path, candidate_measurements, accepted_anchors, final_anchors, min_confidence):
    """Dump every scanned correlation window plus the final anchor list for manual review.

    ``candidate_measurements`` covers every coarse window tried (accepted or not), so gaps
    between accepted anchors (e.g. rejected windows in a stretch with low correlation
    confidence) are directly visible instead of only inferred from log timestamps.
    """
    accepted_ref_times = {round(anchor[2], 3) for anchor in accepted_anchors}
    try:
        with open(path, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["source", "ref_time", "foreign_time", "offset", "waveform_offset",
                              "envelope_offset", "waveform_confidence", "envelope_confidence", "accepted"])
            for (ref_time, offset, waveform_offset, envelope_offset,
                 waveform_confidence, envelope_confidence) in candidate_measurements:
                accepted = round(ref_time, 3) in accepted_ref_times
                best_confidence = max(waveform_confidence, envelope_confidence)
                writer.writerow([
                    "window_scan", f"{ref_time:.3f}", "", f"{offset:+.3f}",
                    f"{waveform_offset:+.3f}", f"{envelope_offset:+.3f}",
                    f"{waveform_confidence:.2f}", f"{envelope_confidence:.2f}",
                    "yes" if accepted else ("no (low confidence)" if best_confidence < min_confidence
                                             else "no (disagreement)"),
                ])
            for ref_name, _, ref_time, foreign_time in final_anchors:
                writer.writerow([
                    ref_name.rsplit('_ref', 1)[0], f"{ref_time:.3f}", f"{foreign_time:.3f}",
                    "", "", "", "", "", "final_anchor",
                ])
        logger.info(f"  -> Wrote anchor report CSV to {path}")
    except Exception as e:
        logger.warning(f"  Failed to write anchor report CSV: {e}")


# --- Audio Syncing Stage Functions ---

def find_audio_start_end(wav_path, db_threshold):
    """Finds the start and end times of audio content above a dB threshold in a WAV file."""
    logger.debug(f"Analyzing audio boundaries for: {os.path.basename(wav_path)} (Threshold: {db_threshold} dB)")
    try:
        sample_rate, audio_data = wavfile.read(wav_path)
        if audio_data.size == 0:
            logger.warning(f"Audio data is empty for {os.path.basename(wav_path)}")
            return 0.0, 0.0 # Return 0 duration if empty

        # Normalize audio data to float range [-1.0, 1.0] for consistent thresholding
        if np.issubdtype(audio_data.dtype, np.integer):
            dtype_info = np.iinfo(audio_data.dtype)
            max_val = float(dtype_info.max)
            min_val = float(dtype_info.min)
            # Avoid division by zero if audio is silent
            norm_factor = max(abs(max_val), abs(min_val))
            if norm_factor == 0: return 0.0, 0.0
            audio_float = audio_data.astype(np.float64) / norm_factor
        elif np.issubdtype(audio_data.dtype, np.floating):
             audio_float = audio_data.astype(np.float64)
             # Handle potential clipping in float audio > 1.0
             abs_max = np.max(np.abs(audio_float)) if audio_float.size > 0 else 0.0
             if abs_max > 1.0 and abs_max > 0:
                 audio_float /= abs_max
             elif abs_max == 0: # Silent float audio
                  return 0.0, 0.0
        else:
             logger.error(f"Unsupported audio data type: {audio_data.dtype} in {os.path.basename(wav_path)}")
             return None, None # Indicate error

        # Convert to mono by taking the max absolute amplitude across channels if stereo
        if audio_float.ndim > 1 and audio_float.shape[1] > 1:
            amplitude = np.max(np.abs(audio_float), axis=1)
        else:
            amplitude = np.abs(audio_float.flatten())

        if amplitude.size == 0: return 0.0, 0.0 # Check again after potential flattening

        # Boundary detection is level-independent; the exported audio is not
        # modified. This handles quiet AC3 tracks without changing their sound.
        analysis_peak = np.percentile(amplitude, 99.5)
        if analysis_peak > 1e-12:
            amplitude = amplitude * (0.9 / analysis_peak)

        # Convert dB threshold to linear amplitude threshold
        # threshold = 10^(dB/20)
        threshold_amplitude = 10.0**(db_threshold / 20.0)

        # Find indices where amplitude exceeds the threshold
        indices_above_thresh = np.where(amplitude >= threshold_amplitude)[0]

        if len(indices_above_thresh) > 0:
            start_index = indices_above_thresh[0]
            end_index = indices_above_thresh[-1]
            # Calculate times in seconds
            start_time_sec = start_index / sample_rate
            # Add 1 sample duration to end time to include the last sample's duration
            end_time_sec = (end_index + 1) / sample_rate

            # Ensure end time is strictly after start time (handle edge cases)
            if end_time_sec <= start_time_sec:
                 # If difference is less than half a sample, treat as single point
                 if abs(end_time_sec - start_time_sec) < (0.5 / sample_rate):
                     end_time_sec = start_time_sec
                 else: # Otherwise, force end time to be slightly after start
                     end_time_sec = start_time_sec + (1.0 / sample_rate)

            logger.debug(f"  -> Detected boundaries: {start_time_sec:.3f}s - {end_time_sec:.3f}s")
            return start_time_sec, end_time_sec
        else:
            # No audio above threshold found
            logger.warning(f"  No audio found above {db_threshold:.1f} dB threshold in {os.path.basename(wav_path)}. Returning full duration or zero.")
            # Optionally return full duration: return 0.0, audio_data.shape[0] / sample_rate
            # Returning zero duration seems safer if threshold is meaningful
            return 0.0, 0.0

    except FileNotFoundError:
        logger.error(f"WAV file not found: {wav_path}")
        return None, None
    except Exception as e:
        logger.error(f"ERROR processing WAV {os.path.basename(wav_path)}: {e}", exc_info=True)
        return None, None


def measure_mean_volume_db(wav_path, start_time, end_time):
    """Return the mean volume (dBFS) of a time window in a WAV file via ffmpeg's volumedetect, or None on failure."""
    duration = end_time - start_time
    if duration <= 0:
        return None
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-ss", f"{max(0.0, start_time):.8f}", "-t", f"{duration:.8f}",
        "-i", wav_path,
        "-af", "volumedetect", "-f", "null", "-"
    ]
    success, stderr = run_ffmpeg(cmd, "Measure Level (volumedetect)", capture_stderr=True)
    if not success or not stderr:
        return None
    match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", stderr)
    return float(match.group(1)) if match else None


def _window_rms_db(samples, sample_rate, center_time, before, window_seconds=0.15):
    """Measure normalized analysis energy immediately before or after a splice point."""
    center = int(round(center_time * sample_rate))
    window = max(1, int(round(window_seconds * sample_rate)))
    start = max(0, center - window if before else center)
    end = min(len(samples), center if before else center + window)
    if end <= start:
        return None
    rms = np.sqrt(np.mean(np.square(samples[start:end].astype(np.float64))))
    return 20.0 * np.log10(rms + 1e-9)


def _find_low_energy_splice(samples, sample_rate, center_time, search_seconds=1.0):
    """Return the least-energetic splice candidate near ``center_time`` for reporting only."""
    start_time = max(0.0, center_time - search_seconds)
    end_time = min(len(samples) / sample_rate, center_time + search_seconds)
    candidates = np.arange(start_time, end_time + 1e-6, 0.02)
    scored = []
    for candidate_time in candidates:
        before_db = _window_rms_db(samples, sample_rate, candidate_time, before=True)
        after_db = _window_rms_db(samples, sample_rate, candidate_time, before=False)
        if before_db is not None and after_db is not None:
            scored.append((max(before_db, after_db), candidate_time, before_db, after_db))
    return min(scored, default=(None, None, None, None))


def _find_nearby_quiet_interval(samples, sample_rate, center_time, search_seconds=3.0, threshold_db=-35.0):
    """Return the quietest sustained interval near a splice point.

    A dub can carry audio (e.g. announcing the episode title) exactly where the original
    track is silent, so the current splice point may sit on real foreign-track content.
    Candidates are nearby low-energy intervals on either side; the quietest one wins, and
    duration only breaks ties between similarly-quiet options - a longer but less silent
    interval is not preferred over a shorter, genuinely quieter one.
    """
    aa = _import_audio_alignment()
    intervals = aa.detect_silence_intervals(
        samples, sample_rate, threshold_db=threshold_db, min_duration=0.2)
    nearby = [
        interval for interval in intervals
        if interval[1] >= center_time - search_seconds and interval[0] <= center_time + search_seconds
    ]
    if not nearby:
        return None

    def _interval_rank(interval):
        start, end = interval
        start_idx = max(0, int(round(start * sample_rate)))
        end_idx = min(len(samples), int(round(end * sample_rate)))
        segment = samples[start_idx:end_idx]
        if segment.size == 0:
            energy_db = 0.0
        else:
            rms = np.sqrt(np.mean(np.square(segment.astype(np.float64))))
            energy_db = 20.0 * np.log10(rms + 1e-9)
        duration = end - start
        proximity = abs((start + end) / 2.0 - center_time)
        return (energy_db, -duration, proximity)

    return min(nearby, key=_interval_rank)


def _record_threshold_calibration(label, noise_floor_db, threshold_db):
    """Record one --auto_silence_threshold measurement for the optional CSV export."""
    THRESHOLD_CALIBRATION_LOG.append((label, noise_floor_db, threshold_db))


def write_threshold_calibration_csv(path):
    """Write every recorded noise-floor/threshold calibration to a CSV for manual review."""
    if not THRESHOLD_CALIBRATION_LOG:
        logger.info("  No threshold calibration data recorded; skipping --threshold_calibration_csv.")
        return
    try:
        with open(path, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["label", "noise_floor_db", "threshold_db"])
            for label, noise_floor_db, threshold_db in THRESHOLD_CALIBRATION_LOG:
                writer.writerow([label, f"{noise_floor_db:.1f}", f"{threshold_db:.1f}"])
        logger.info(f"  -> Wrote {len(THRESHOLD_CALIBRATION_LOG)} threshold calibration row(s) to {path}")
    except Exception as error:
        logger.error(f"-> Failed to write threshold calibration CSV: {error}")


def _move_replacements_to_quiet_primary_splices(args, anchors, reference_wav, foreign_wav):
    """Optionally relocate replacement ranges inside an existing reference silence.

    The replacement duration and subsequent timeline shift remain unchanged. Only
    the boundary is moved, and only when the primary source track supplies a
    quieter pause whose corresponding reference interval stays inside the same
    reference silence.
    """
    if not args.per_track_splice_placement or not AUDIO_REPLACEMENT_RANGES:
        return anchors
    try:
        sample_rate, reference_audio = wavfile.read(reference_wav)
        _, foreign_audio = wavfile.read(foreign_wav)
    except Exception as error:
        logger.warning(f"  Per-track splice placement skipped: {error}")
        return anchors
    aa = _import_audio_alignment()
    reference_mono = reference_audio.mean(axis=1) if reference_audio.ndim == 2 else reference_audio
    foreign_mono = foreign_audio.mean(axis=1) if foreign_audio.ndim == 2 else foreign_audio
    reference_analysis = aa.normalize_analysis_level(reference_mono)
    foreign_analysis = aa.normalize_analysis_level(foreign_mono)
    reference_silence_threshold_db = -35.0
    foreign_quiet_threshold_db = -35.0
    if args.auto_silence_threshold:
        reference_silence_threshold_db, reference_noise_floor_db = aa.calibrate_silence_threshold_db(
            reference_analysis, sample_rate)
        foreign_quiet_threshold_db, foreign_noise_floor_db = aa.calibrate_silence_threshold_db(
            foreign_analysis, sample_rate)
        logger.info(f"  Auto-calibrated splice threshold: reference {reference_silence_threshold_db:.1f} dB "
                    f"(noise floor {reference_noise_floor_db:.1f} dB), primary foreign "
                    f"{foreign_quiet_threshold_db:.1f} dB (noise floor {foreign_noise_floor_db:.1f} dB)")
        _record_threshold_calibration("reference (primary splice)", reference_noise_floor_db, reference_silence_threshold_db)
        _record_threshold_calibration("primary foreign (splice)", foreign_noise_floor_db, foreign_quiet_threshold_db)
    reference_silences = aa.detect_silence_intervals(
        reference_analysis, sample_rate, threshold_db=reference_silence_threshold_db, min_duration=0.2)
    tempo = AUDIO_EDITORIAL_SOURCE_TEMPO if AUDIO_EDITORIAL_SOURCE_TEMPO > 0 else 1.0
    moved = []
    for replacement in AUDIO_REPLACEMENT_RANGES:
        source_time = replacement.get("foreign_splice_time")
        if source_time is None:
            continue
        # Remember the pre-move position so additional tracks can each search for their own
        # quiet splice independently, instead of chaining off the primary track's choice.
        replacement.setdefault("original_foreign_splice_time", source_time)
        replacement.setdefault("original_ref_start", replacement["ref_start"])
        replacement.setdefault("original_ref_end", replacement["ref_end"])
        quiet_interval = _find_nearby_quiet_interval(
            foreign_analysis, sample_rate, source_time, threshold_db=foreign_quiet_threshold_db)
        if quiet_interval is None:
            continue
        candidate_source = (quiet_interval[0] + quiet_interval[1]) / 2.0
        source_delta = candidate_source - source_time
        reference_delta = source_delta / tempo
        candidate_start = replacement["ref_start"] + reference_delta
        candidate_end = replacement["ref_end"] + reference_delta
        containing_silence = next((interval for interval in reference_silences
                                   if interval[0] <= candidate_start and candidate_end <= interval[1]), None)
        if containing_silence is None:
            continue
        moved.append((replacement["id"], candidate_start, candidate_end, candidate_source, source_delta))

    if not moved:
        logger.info("  Per-track splice placement found no safely movable replacement ranges.")
        return anchors
    adjusted = []
    for ref_name, foreign_name, ref_time, foreign_time in anchors:
        match = next((item for item in moved if ref_name.startswith(item[0])), None)
        if match:
            _, start, end, source_time, _ = match
            ref_time = start if ref_name.endswith("a_ref") else end
            foreign_time = source_time
        adjusted.append((ref_name, foreign_name, ref_time, foreign_time))
    adjusted.sort(key=lambda item: item[2])
    if any(right[3] < left[3] for left, right in zip(adjusted, adjusted[1:])):
        logger.warning("  Per-track splice placement discarded: adjusted anchors would be non-monotonic.")
        return anchors
    for replacement_id, start, end, source_time, source_delta in moved:
        replacement = next(item for item in AUDIO_REPLACEMENT_RANGES if item["id"] == replacement_id)
        replacement.update({
            "ref_start": start,
            "ref_end": end,
            "foreign_splice_time": source_time,
        })
        logger.info(f"  Per-track splice placement: {replacement_id} -> ref {start:.3f}s-{end:.3f}s, "
                    f"source {source_time:.3f}s (shift {source_delta:+.3f}s)")
    return adjusted


def _localize_replacements_for_track(args, final_segment_anchors, ref_wav_analysis, track_wav_analysis, track_label):
    """Give one additional foreign track its own quiet splice point for each relocated
    replacement, instead of reusing the primary track's chosen boundary.

    A pause that is safe in the primary language may still contain dialogue in another
    dub, so each track searches near the replacement's original (pre-move) position for
    its own nearby quiet interval. Only the two anchor points bracketing that replacement
    are changed, and only in the list returned for this track; the shared
    ``final_segment_anchors`` used by the primary track, other tracks, and subtitles is
    left untouched.
    """
    if not args.per_track_splice_placement or not AUDIO_REPLACEMENT_RANGES:
        return final_segment_anchors
    try:
        sample_rate, reference_audio = wavfile.read(ref_wav_analysis)
        _, track_audio = wavfile.read(track_wav_analysis)
    except Exception as error:
        logger.warning(f"  Per-track splice placement skipped for {track_label}: {error}")
        return final_segment_anchors
    aa = _import_audio_alignment()
    reference_mono = reference_audio.mean(axis=1) if reference_audio.ndim == 2 else reference_audio
    track_mono = track_audio.mean(axis=1) if track_audio.ndim == 2 else track_audio
    reference_analysis = aa.normalize_analysis_level(reference_mono)
    track_analysis = aa.normalize_analysis_level(track_mono)
    reference_silence_threshold_db = -35.0
    track_quiet_threshold_db = -35.0
    if args.auto_silence_threshold:
        reference_silence_threshold_db, reference_noise_floor_db = aa.calibrate_silence_threshold_db(
            reference_analysis, sample_rate)
        track_quiet_threshold_db, track_noise_floor_db = aa.calibrate_silence_threshold_db(
            track_analysis, sample_rate)
        logger.info(f"  Auto-calibrated splice threshold ({track_label}): reference "
                    f"{reference_silence_threshold_db:.1f} dB (noise floor {reference_noise_floor_db:.1f} dB), "
                    f"track {track_quiet_threshold_db:.1f} dB (noise floor {track_noise_floor_db:.1f} dB)")
        _record_threshold_calibration(f"reference (splice, {track_label})", reference_noise_floor_db, reference_silence_threshold_db)
        _record_threshold_calibration(f"{track_label} (splice)", track_noise_floor_db, track_quiet_threshold_db)
    reference_silences = aa.detect_silence_intervals(
        reference_analysis, sample_rate, threshold_db=reference_silence_threshold_db, min_duration=0.2)
    tempo = AUDIO_EDITORIAL_SOURCE_TEMPO if AUDIO_EDITORIAL_SOURCE_TEMPO > 0 else 1.0

    adjusted = list(final_segment_anchors)
    moved_count = 0
    for replacement in AUDIO_REPLACEMENT_RANGES:
        base_source_time = replacement.get("original_foreign_splice_time", replacement.get("foreign_splice_time"))
        base_ref_start = replacement.get("original_ref_start", replacement["ref_start"])
        base_ref_end = replacement.get("original_ref_end", replacement["ref_end"])
        if base_source_time is None:
            continue
        quiet_interval = _find_nearby_quiet_interval(
            track_analysis, sample_rate, base_source_time, threshold_db=track_quiet_threshold_db)
        if quiet_interval is None:
            continue
        candidate_source = (quiet_interval[0] + quiet_interval[1]) / 2.0
        source_delta = candidate_source - base_source_time
        reference_delta = source_delta / tempo
        candidate_start = base_ref_start + reference_delta
        candidate_end = base_ref_end + reference_delta
        containing_silence = next((interval for interval in reference_silences
                                   if interval[0] <= candidate_start and candidate_end <= interval[1]), None)
        if containing_silence is None:
            continue
        # Find this replacement's current boundary anchors (post primary placement) in the
        # shared list, and nudge only those two points for this track's own copy.
        start_idx = next((i for i, (ref_t, _) in enumerate(adjusted)
                          if abs(ref_t - replacement["ref_start"]) < 0.001), None)
        end_idx = next((i for i, (ref_t, _) in enumerate(adjusted)
                        if abs(ref_t - replacement["ref_end"]) < 0.001), None)
        if start_idx is None or end_idx is None:
            continue
        trial = list(adjusted)
        trial[start_idx] = (candidate_start, candidate_source)
        trial[end_idx] = (candidate_end, candidate_source)
        if any(right[0] < left[0] or right[1] < left[1] for left, right in zip(trial, trial[1:])):
            logger.debug(f"  Per-track splice placement for {track_label}: discarded {replacement['id']} (non-monotonic).")
            continue
        adjusted = trial
        moved_count += 1
        logger.info(f"  Per-track splice placement ({track_label}): {replacement['id']} -> "
                    f"ref {candidate_start:.3f}s-{candidate_end:.3f}s, source {candidate_source:.3f}s "
                    f"(shift {source_delta:+.3f}s from original position)")
    if moved_count == 0:
        logger.info(f"  Per-track splice placement found no independently movable replacement ranges for {track_label}.")
    return adjusted


def write_splice_safety_report(path, args, final_segment_anchors):
    """Report whether every selected source track is quiet at planned splice edges.

    Audio is loudness-normalized only for this analysis. The report never changes
    the recipe or the delivered tracks, and supports per-track decisions later.
    """
    if not AUDIO_REPLACEMENT_RANGES:
        logger.info("  No reference replacement ranges to include in splice-safety report.")
        return
    aa = _import_audio_alignment()
    sample_rate = aa.DEFAULT_SAMPLE_RATE
    rows = []
    for track in args.selected_foreign_tracks:
        stream_idx = track["stream_idx"]
        try:
            analysis_audio = aa.extract_mono_audio(
                args.foreign_video, stream_idx, sample_rate, normalize_loudness=True)
        except RuntimeError as error:
            logger.warning(f"  Splice-safety analysis skipped for stream #{stream_idx}: {error}")
            continue
        for replacement in AUDIO_REPLACEMENT_RANGES:
            ref_start = replacement["ref_start"]
            ref_end = replacement["ref_end"]
            source_splice_time = replacement.get("foreign_splice_time")
            if source_splice_time is None:
                nearest_anchor = min(final_segment_anchors, key=lambda anchor: abs(anchor[0] - ref_start))
                source_splice_time = nearest_anchor[1]
            before_db = _window_rms_db(analysis_audio, sample_rate, source_splice_time, before=True)
            after_db = _window_rms_db(analysis_audio, sample_rate, source_splice_time, before=False)
            edge_cost = max(value for value in (before_db, after_db) if value is not None)
            candidate_cost, candidate_time, candidate_before_db, candidate_after_db = _find_low_energy_splice(
                analysis_audio, sample_rate, source_splice_time)
            quiet_interval = _find_nearby_quiet_interval(
                analysis_audio, sample_rate, source_splice_time)
            quiet_start, quiet_end = quiet_interval if quiet_interval else (None, None)
            quiet_duration = quiet_end - quiet_start if quiet_interval else None
            rows.append([
                replacement["id"], stream_idx, track.get("language") or "und",
                f"{ref_start:.3f}", f"{ref_end:.3f}",
                f"{source_splice_time:.3f}",
                f"{before_db:.1f}" if before_db is not None else "",
                f"{after_db:.1f}" if after_db is not None else "",
                f"{edge_cost:.1f}",
                "quiet" if edge_cost <= -35.0 else "content-present",
                f"{candidate_time:.3f}" if candidate_time is not None else "",
                f"{candidate_time - source_splice_time:+.3f}" if candidate_time is not None else "",
                f"{candidate_before_db:.1f}" if candidate_before_db is not None else "",
                f"{candidate_after_db:.1f}" if candidate_after_db is not None else "",
                f"{candidate_cost:.1f}" if candidate_cost is not None else "",
                "quiet" if candidate_cost is not None and candidate_cost <= -35.0 else "content-present",
                f"{quiet_start:.3f}" if quiet_start is not None else "",
                f"{quiet_end:.3f}" if quiet_end is not None else "",
                f"{quiet_duration:.3f}" if quiet_duration is not None else "",
                "yes" if quiet_duration is not None and quiet_duration >= ref_end - ref_start else "no",
            ])
    try:
        with open(path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow([
                "replacement_id", "stream_idx", "language", "reference_start",
                "reference_end", "source_splice_time", "source_before_db",
                "source_after_db", "edge_cost_db", "assessment",
                "best_source_splice_time", "best_offset_seconds",
                "best_before_db", "best_after_db", "best_edge_cost_db",
                "best_assessment",
                "nearby_quiet_start", "nearby_quiet_end", "nearby_quiet_duration",
                "quiet_interval_covers_replacement",
            ])
            writer.writerows(rows)
        logger.info(f"  -> Wrote {len(rows)} per-track splice-safety measurements to {path}")
    except OSError as error:
        logger.warning(f"  Failed to write splice-safety report: {error}")


def process_segment_iteratively(foreign_wav_full, foreign_start, foreign_end, ref_duration, segment_num, temp_dir, max_iterations=3, target_precision_ms=5, is_first_segment=False, is_last_segment=False, first_adjust_ms=0.0, last_adjust_ms=0.0, gain_db=0.0, fixed_speed=None):
    """
    Processes an audio segment, iteratively adjusting 'atempo' to match a target duration precisely.

    Args:
        foreign_wav_full (str): Path to the full foreign audio WAV file.
        foreign_start (float): Start time (seconds) of the segment in the foreign audio.
        foreign_end (float): End time (seconds) of the segment in the foreign audio.
        ref_duration (float): The target duration (seconds) for the processed segment.
        segment_num (int): The segment number (for logging).
        temp_dir (str): Path to the temporary directory for intermediate files.
        max_iterations (int): Maximum number of refinement iterations.
        target_precision_ms (int): Desired duration precision in milliseconds.
        is_first_segment (bool): Whether this is the first segment (for adjustment).
        is_last_segment (bool): Whether this is the last segment (for adjustment).
        first_adjust_ms (float): Milliseconds to adjust first segment (+ pad, - trim).
        last_adjust_ms (float): Milliseconds to adjust last segment (+ pad, - trim).

    Returns:
        str: Path to the final processed segment file meeting the target duration, or None on failure.
    """
    target_precision_s = target_precision_ms / 1000.0
    
    # --- Apply Manual Adjustments for First/Last Segments ---
    # Strategy:
    # - For TRIM adjustments (negative values): adjust the extraction boundaries
    # - For PAD adjustments (positive values): add silence via FFmpeg filter, then stretch the combined audio
    
    adjusted_foreign_start = foreign_start
    adjusted_foreign_end = foreign_end
    prepend_silence_s = 0.0  # Silence to add BEFORE the extracted segment
    append_silence_s = 0.0   # Silence to add AFTER the extracted segment
    
    if is_first_segment and first_adjust_ms != 0.0:
        adjust_s = first_adjust_ms / 1000.0
        if adjust_s < 0:
            # NEGATIVE = TRIM from start (skip ahead in audio)
            logger.info(f"  -> First segment: Trimming {abs(adjust_s):.3f}s from start")
            adjusted_foreign_start -= adjust_s  # Move start forward (adjust_s is negative, so this subtracts negative = adds)
        else:
            # POSITIVE = PAD with silence at start
            logger.info(f"  -> First segment: Adding {adjust_s:.3f}s silence padding at start")
            prepend_silence_s = adjust_s
    
    if is_last_segment and last_adjust_ms != 0.0:
        adjust_s = last_adjust_ms / 1000.0
        if adjust_s < 0:
            # NEGATIVE = TRIM from end
            logger.info(f"  -> Last segment: Trimming {abs(adjust_s):.3f}s from end")
            adjusted_foreign_end += adjust_s  # Move end backward (adjust_s is negative)
        else:
            # POSITIVE = PAD with silence at end
            logger.info(f"  -> Last segment: Adding {adjust_s:.3f}s silence padding at end")
            append_silence_s = adjust_s
    
    # Calculate the base extracted duration (before padding)
    base_foreign_duration = adjusted_foreign_end - adjusted_foreign_start
    
    # Total foreign duration including any padding
    foreign_duration = base_foreign_duration + prepend_silence_s + append_silence_s
    
    # Basic validation
    if ref_duration <= 0 or base_foreign_duration <= 0 or foreign_duration <= 0:
        logger.warning(f"  -> Segment {segment_num}: Invalid duration (Ref={ref_duration:.3f}s, Base={base_foreign_duration:.3f}s, Total={foreign_duration:.3f}s)")
        return None

    # --- Initial setup ---
    # Initial speed factor estimate
    # IMPORTANT: `atempo` filter works inversely: tempo < 1 slows down, tempo > 1 speeds up.
    # So, we need foreign_duration / ref_duration
    initial_speed_factor = fixed_speed if fixed_speed is not None else foreign_duration / ref_duration
    clamped_speed = max(MIN_ATEMPO, min(MAX_ATEMPO, initial_speed_factor))

    segment_output_path = os.path.join(temp_dir, f"segment_{segment_num:04d}_final.wav")
    best_segment_path = None
    best_duration_diff = float('inf')
    last_processed_duration = None

    logger.info(f"  -> Segment {segment_num}: Target={ref_duration:.3f}s, Base={base_foreign_duration:.3f}s, +Padding={foreign_duration:.3f}s. Initial speed={clamped_speed:.5f}x")

    # --- Iterative Refinement Loop ---
    for iteration in range(max_iterations):
        iteration_path = os.path.join(temp_dir, f"segment_{segment_num:04d}_iter{iteration}.wav")

        # Build FFmpeg filter chain with padding if needed
        if prepend_silence_s > 0 or append_silence_s > 0:
            # Need to add silence - use complex filter chain
            filter_parts = []
            input_labels = []
            
            # Generate prepend silence if needed
            if prepend_silence_s > 0:
                filter_parts.append(f"aevalsrc=0:d={prepend_silence_s:.8f}:s={DEFAULT_SAMPLE_RATE}:c={DEFAULT_CHANNELS}[pre_silence]")
                input_labels.append("[pre_silence]")
            
            # Extract and label the main audio segment
            filter_parts.append(f"[0:a]atrim=start={adjusted_foreign_start:.8f}:end={adjusted_foreign_end:.8f},asetpts=PTS-STARTPTS[main_audio]")
            input_labels.append("[main_audio]")
            
            # Generate append silence if needed
            if append_silence_s > 0:
                filter_parts.append(f"aevalsrc=0:d={append_silence_s:.8f}:s={DEFAULT_SAMPLE_RATE}:c={DEFAULT_CHANNELS}[post_silence]")
                input_labels.append("[post_silence]")
            
            # Concatenate all parts
            n_inputs = len(input_labels)
            concat_inputs = "".join(input_labels)
            filter_parts.append(f"{concat_inputs}concat=n={n_inputs}:v=0:a=1[padded]")
            
            # Apply atempo to the padded audio
            gain_stage = f",volume={gain_db:.3f}dB" if gain_db else ""
            filter_parts.append(f"[padded]atempo={clamped_speed:.8f}{gain_stage}")
            
            filter_complex = ";".join(filter_parts)
        else:
            # No padding needed - simple filter
            gain_stage = f",volume={gain_db:.3f}dB" if gain_db else ""
            filter_complex = f"atrim=start={adjusted_foreign_start:.8f}:end={adjusted_foreign_end:.8f},asetpts=PTS-STARTPTS,atempo={clamped_speed:.8f}{gain_stage}"
        
        process_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
            "-i", foreign_wav_full,
            "-filter_complex", filter_complex,
            "-c:a", "pcm_s16le", "-ar", str(DEFAULT_SAMPLE_RATE), "-ac", str(DEFAULT_CHANNELS),
            "-y", iteration_path
        ]

        if not run_ffmpeg(process_cmd, f"Process Segment {segment_num} (Iter {iteration+1}, Speed {clamped_speed:.5f}x)")[0]:
            logger.error(f"  -> Segment {segment_num}: Processing failed on iteration {iteration+1}")
            continue

        # Measure the actual duration of the processed segment
        processed_duration = get_file_duration(iteration_path, media_type='audio')
        if processed_duration is None:
            logger.warning(f"  -> Segment {segment_num}: Could not get duration for iteration {iteration+1}")
            continue

        duration_diff = processed_duration - ref_duration
        abs_duration_diff = abs(duration_diff)
        logger.info(f"    Iter {iteration+1}: Speed={clamped_speed:.5f}x -> Duration={processed_duration:.3f}s (Diff={duration_diff*1000:+.1f}ms)")

        # Keep track of the best result so far
        if abs_duration_diff < best_duration_diff:
            best_duration_diff = abs_duration_diff
            best_segment_path = iteration_path

        if fixed_speed is not None:
            logger.info(f"   Segment {segment_num}: Preserved fixed FPS tempo {clamped_speed:.6f}x.")
            break

        # Check if we've reached the target precision
        if abs_duration_diff <= target_precision_s:
            logger.info(f"   Segment {segment_num}: Achieved target precision ({abs_duration_diff*1000:.1f}ms <= {target_precision_ms}ms) on iteration {iteration+1}")
            break

        # --- Adjust speed factor for the next iteration ---
        if iteration < max_iterations - 1:
            if processed_duration <= 0:
                logger.warning(f"    Skipping speed adjustment for iter {iteration+1}: Processed duration is zero or negative.")
                continue

            ideal_correction = processed_duration / ref_duration
            dampening = 1.0 - min(0.7, abs(ideal_correction - 1.0) * 1.5)
            dampened_correction = (ideal_correction - 1.0) * dampening + 1.0
            next_speed = clamped_speed * dampened_correction
            clamped_speed = max(MIN_ATEMPO, min(MAX_ATEMPO, next_speed))
            logger.debug(f"    Adjusting speed: IdealCorr={ideal_correction:.6f}x, DampenedCorr={dampened_correction:.6f}x -> NextSpeed={clamped_speed:.6f}x")
            last_processed_duration = processed_duration

    # --- Post-Iteration Handling ---
    if best_segment_path is None:
         logger.error(f" Segment {segment_num}: No successful iteration completed.")
         return None

    # Check the duration of the best segment found
    final_processed_duration = get_file_duration(best_segment_path, media_type='audio')
    if final_processed_duration is None:
        logger.error(f" Segment {segment_num}: Could not get duration of best segment '{os.path.basename(best_segment_path)}'.")
        return None

    if fixed_speed is not None:
        try:
            shutil.copy2(best_segment_path, segment_output_path)
            return segment_output_path
        except Exception as error:
            logger.error(f" Segment {segment_num}: Failed to preserve fixed-tempo segment: {error}")
            return None

    final_duration_gap = ref_duration - final_processed_duration

    # If the best result is still outside precision, perform final trim/pad
    if abs(final_duration_gap) > target_precision_s:
        logger.warning(f"  -> Segment {segment_num}: Best iteration ({final_processed_duration:.3f}s) still {final_duration_gap*1000:+.1f}ms off target. Applying final correction.")

        if final_duration_gap > 0: # Segment is too short, need to pad with silence
            silence_path = os.path.join(temp_dir, f"silence_{segment_num:04d}.wav")
            silence_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
                "-f", "lavfi", "-i", f"anullsrc=r={DEFAULT_SAMPLE_RATE}:cl={'stereo' if DEFAULT_CHANNELS == 2 else 'mono'}",
                "-t", f"{final_duration_gap:.8f}",
                "-c:a", "pcm_s16le", "-y", silence_path
            ]
            if not run_ffmpeg(silence_cmd, f"Create Silence Pad for Segment {segment_num} ({final_duration_gap:.3f}s)")[0]:
                 logger.error(f" Segment {segment_num}: Failed to create silence pad.")
                 return None

            concat_list_path = os.path.join(temp_dir, f"concat_list_{segment_num:04d}.txt")
            try:
                with open(concat_list_path, 'w', encoding='utf-8') as f_concat:
                    abs_best = os.path.abspath(best_segment_path).replace("\\", "/")
                    abs_silence = os.path.abspath(silence_path).replace("\\", "/")
                    f_concat.write(f"file '{abs_best}'\n")
                    f_concat.write(f"file '{abs_silence}'\n")
            except IOError as e:
                 logger.error(f" Segment {segment_num}: Failed to write concat list: {e}")
                 return None

            concat_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
                "-f", "concat", "-safe", "0",
                "-i", concat_list_path,
                "-c", "copy",
                "-y", segment_output_path
            ]
            if not run_ffmpeg(concat_cmd, f"Add Silence Pad to Segment {segment_num}")[0]:
                logger.error(f" Segment {segment_num}: Failed to concatenate silence pad.")
                return None
            logger.info(f"   Segment {segment_num}: Added {final_duration_gap*1000:.1f}ms silence pad for final correction.")
            return segment_output_path

        elif final_duration_gap < 0: # Segment is too long, need to trim
            trim_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
                "-i", best_segment_path,
                "-t", f"{ref_duration:.8f}",
                "-c", "copy",
                "-y", segment_output_path
            ]
            if not run_ffmpeg(trim_cmd, f"Trim Segment {segment_num} to {ref_duration:.3f}s")[0]:
                logger.error(f" Segment {segment_num}: Failed to trim segment.")
                return None
            logger.info(f"   Segment {segment_num}: Trimmed by {abs(final_duration_gap)*1000:.1f}ms for final correction.")
            return segment_output_path
    else:
        # Best iteration was already within precision
        logger.info(f"   Segment {segment_num}: Best iteration duration ({final_processed_duration:.3f}s) already within {target_precision_ms}ms of target.")
        try:
            shutil.copy2(best_segment_path, segment_output_path)
            return segment_output_path
        except Exception as e:
            logger.error(f" Segment {segment_num}: Failed to copy best segment to final path: {e}")
            return None

    logger.error(f" Segment {segment_num}: Failed to produce final segment after iterations and correction.")
    return None


def fallback_direct_segment(source_wav, source_start, source_end, out_path, segment_num, label="segment", gain_db=0.0):
    """Directly extract a time-slice as a last-resort fallback when iterative processing fails."""
    segment_duration = source_end - source_start
    if segment_duration <= 0:
        logger.warning(f"  -> {label} {segment_num}: invalid fallback range ({source_start:.3f}s -> {source_end:.3f}s)")
        return False

    fallback_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
        "-i", source_wav,
        "-ss", f"{source_start:.8f}",
        "-t", f"{segment_duration:.8f}",
    ]
    if gain_db:
        fallback_cmd.extend(["-af", f"volume={gain_db:.3f}dB"])
    fallback_cmd.extend([
        "-c:a", "pcm_s16le",
        "-ar", str(DEFAULT_SAMPLE_RATE),
        "-ac", str(DEFAULT_CHANNELS),
        "-y", out_path
    ])

    if not run_ffmpeg(fallback_cmd, f"Fallback {label.title()} {segment_num} Slice")[0]:
        logger.error(f"  -> {label.title()} {segment_num}: fallback extraction failed. The iterative segment processing was unsuccessful and the direct slice recovery also failed.")
        return False

    duration = get_file_duration(out_path, media_type='audio')
    if duration is None:
        logger.warning(f"  -> {label.title()} {segment_num}: direct slice fallback created a file, but its duration could not be verified; continuing with caution.")
        return False

    if duration <= 0:
        logger.warning(f"  -> {label.title()} {segment_num}: direct slice fallback produced a non-positive duration ({duration:.3f}s); this segment will not be used.")
        return False

    logger.warning(f"  -> {label.title()} {segment_num}: FALLBACK ACTIVE - direct slice recovery was used because iterative segment processing failed.")
    return True

def run_progressive_sync_iterative(args, visual_anchors_details, output_audio_path, temp_dir, db_threshold, min_segment_duration):
    """
    Audio sync stage using iterative refinement for precise segment durations.
    Filters anchors, processes each segment iteratively, concatenates, and applies delay.
    """
    logger.info("\n===== Audio Synchronization Stage =====")
    stage_start_time = time.time()

    # Define full paths for extracted audio
    ref_wav_full = os.path.join(temp_dir, "ref_audio_full.wav")
    foreign_wav_full = os.path.join(temp_dir, "foreign_audio_full.wav")
    ref_wav_analysis = os.path.join(temp_dir, "ref_audio_analysis.wav")
    foreign_wav_analysis = os.path.join(temp_dir, "foreign_audio_analysis.wav")

    # --- Step 1: Determine Audio Stream Indices ---
    logger.info(f"--- Finding Audio Streams (Ref: {args.ref_lang}, Foreign: {args.foreign_lang}) ---")
    ref_stream_idx = args.ref_stream_idx
    if ref_stream_idx is None:
        ref_streams = get_stream_info(args.ref_video)
        ref_stream_idx = find_audio_stream_index_by_lang(ref_streams, args.ref_lang)
        if ref_stream_idx is not None: logger.info(f"  -> Auto-detected Reference Stream Index: {ref_stream_idx}")
    else: logger.info(f"  -> Using Forced Reference Stream Index: {ref_stream_idx}")

    foreign_stream_idx = args.foreign_stream_idx
    if foreign_stream_idx is None:
        foreign_streams = get_stream_info(args.foreign_video)
        foreign_stream_idx = find_audio_stream_index_by_lang(foreign_streams, args.foreign_lang)
        if foreign_stream_idx is not None: logger.info(f"  -> Auto-detected Foreign Stream Index: {foreign_stream_idx}")
    else: logger.info(f"  -> Using Forced Foreign Stream Index: {foreign_stream_idx}")

    # Validate indices
    if ref_stream_idx is None:
        logger.error("-> Could not determine reference audio stream index. Cannot proceed.")
        return None, None
    if foreign_stream_idx is None:
        logger.error("-> Could not determine foreign audio stream index. Cannot proceed.")
        return None, None

    # --- Step 2: Extract Full Audio Tracks ---
    logger.info(f"--- Extracting Audio Tracks (Ref Index: {ref_stream_idx}, Foreign Index: {foreign_stream_idx}) ---")
    # Pristine extraction: only a lossless container/PCM conversion, no loudness or level changes.
    # This is what final segments are actually cut from, so the output preserves the source's
    # original dynamics/volume instead of permanently baking in analysis-only normalization.
    extract_cmd_ref = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
                       "-i", args.ref_video,
                       # Use absolute stream index mapping:
                       "-map", f"0:{ref_stream_idx}", # <<< CORRECTED MAPPING
                       "-vn",
                       "-c:a", "pcm_s16le", "-ar", str(DEFAULT_SAMPLE_RATE), "-ac", str(DEFAULT_CHANNELS),
                       "-y", "-f", "wav", ref_wav_full]
    if not run_ffmpeg(extract_cmd_ref, f"Extract Reference Audio (Index {ref_stream_idx})")[0]:
        return None, None # Abort if extraction fails


    extract_cmd_foreign = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
                           "-i", args.foreign_video,
                           # Use absolute stream index mapping:
                           "-map", f"0:{foreign_stream_idx}", # <<< CORRECTED MAPPING
                           "-vn",
                           "-c:a", "pcm_s16le", "-ar", str(DEFAULT_SAMPLE_RATE), "-ac", str(DEFAULT_CHANNELS),
                           "-y", "-f", "wav", foreign_wav_full]
    if not run_ffmpeg(extract_cmd_foreign, f"Extract Foreign Audio (Index {foreign_stream_idx})")[0]:
        return None, None # Abort if extraction fails

    # Analysis-only copies: resampled + loudness-normalized so boundary/silence detection is
    # reliable even on quiet source streams (e.g. low-level AC3 tracks). Never used as output content.
    aresample_filter = f'aresample=resampler=soxr:precision=28:cutoff={0.99 if DEFAULT_SAMPLE_RATE >= 44100 else 0.90}'
    loudness_filter = 'loudnorm=I=-23:TP=-1.5:LRA=11'
    audio_filter_chain = f'{aresample_filter},{loudness_filter}'

    extract_cmd_ref_analysis = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
                       "-i", args.ref_video,
                       "-map", f"0:{ref_stream_idx}",
                       "-vn",
                       "-c:a", "pcm_s16le", "-ar", str(DEFAULT_SAMPLE_RATE), "-ac", str(DEFAULT_CHANNELS),
                       "-af", audio_filter_chain, "-y", "-f", "wav", ref_wav_analysis]
    if not run_ffmpeg(extract_cmd_ref_analysis, "Extract Reference Audio (Analysis Copy)")[0]:
        return None, None

    extract_cmd_foreign_analysis = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
                           "-i", args.foreign_video,
                           "-map", f"0:{foreign_stream_idx}",
                           "-vn",
                           "-c:a", "pcm_s16le", "-ar", str(DEFAULT_SAMPLE_RATE), "-ac", str(DEFAULT_CHANNELS),
                           "-af", audio_filter_chain, "-y", "-f", "wav", foreign_wav_analysis]
    if not run_ffmpeg(extract_cmd_foreign_analysis, "Extract Foreign Audio (Analysis Copy)")[0]:
        return None, None

    # --- Step 3: Detect Audio Boundaries ---
    visual_anchors_details = _move_replacements_to_quiet_primary_splices(
        args, visual_anchors_details, ref_wav_analysis, foreign_wav_analysis)
    ref_boundary_threshold = db_threshold
    foreign_boundary_threshold = db_threshold
    if args.auto_silence_threshold:
        aa = _import_audio_alignment()
        try:
            ref_sr, ref_boundary_audio = wavfile.read(ref_wav_analysis)
            foreign_sr, foreign_boundary_audio = wavfile.read(foreign_wav_analysis)
            ref_boundary_mono = ref_boundary_audio.mean(axis=1) if ref_boundary_audio.ndim == 2 else ref_boundary_audio
            foreign_boundary_mono = foreign_boundary_audio.mean(axis=1) if foreign_boundary_audio.ndim == 2 else foreign_boundary_audio
            # calibrate_silence_threshold_db expects a level-normalized signal (like the other
            # two calibration call sites), not raw PCM - otherwise the RMS is measured against
            # int16 full-scale instead of 1.0 and produces a nonsensical positive "dB" floor.
            ref_boundary_analysis = aa.normalize_analysis_level(ref_boundary_mono)
            foreign_boundary_analysis = aa.normalize_analysis_level(foreign_boundary_mono)
            ref_boundary_threshold, ref_noise_floor_db = aa.calibrate_silence_threshold_db(ref_boundary_analysis, ref_sr)
            foreign_boundary_threshold, foreign_noise_floor_db = aa.calibrate_silence_threshold_db(foreign_boundary_analysis, foreign_sr)
            logger.info(f"  Auto-calibrated boundary threshold: reference {ref_boundary_threshold:.1f} dB "
                        f"(noise floor {ref_noise_floor_db:.1f} dB), foreign {foreign_boundary_threshold:.1f} dB "
                        f"(noise floor {foreign_noise_floor_db:.1f} dB)")
            _record_threshold_calibration("reference (boundary)", ref_noise_floor_db, ref_boundary_threshold)
            _record_threshold_calibration("foreign (boundary)", foreign_noise_floor_db, foreign_boundary_threshold)
        except Exception as error:
            logger.warning(f"  Auto silence-threshold calibration skipped, using fixed {db_threshold} dB: {error}")
            ref_boundary_threshold = db_threshold
            foreign_boundary_threshold = db_threshold
    logger.info(f"--- Detecting Audio Content Boundaries (Reference: {ref_boundary_threshold:.1f} dB, "
                f"Foreign: {foreign_boundary_threshold:.1f} dB) ---")
    ref_start_s, ref_end_s = find_audio_start_end(ref_wav_analysis, ref_boundary_threshold)
    foreign_start_s, foreign_end_s = find_audio_start_end(foreign_wav_analysis, foreign_boundary_threshold)

    if ref_start_s is None or foreign_start_s is None:
        logger.error("-> Failed to detect audio boundaries. Cannot proceed.")
        return None, None

    visual_prefix = None
    if args.visual_program_bounds:
        ref_visual_start, _ = find_visual_program_bounds(args.ref_video)
        foreign_visual_start, _ = find_visual_program_bounds(args.foreign_video)
        source_tempo = AUDIO_EDITORIAL_SOURCE_TEMPO if AUDIO_EDITORIAL_SOURCE_TEMPO > 0 else 1.0
        if (ref_visual_start is not None and foreign_visual_start is not None
                and ref_visual_start > 0 and foreign_visual_start > 0):
            prefix_duration = foreign_visual_start / source_tempo
            padding_duration = ref_visual_start - prefix_duration
            if padding_duration >= 0:
                visual_prefix = {
                    "ref_start": ref_visual_start,
                    "foreign_end": foreign_visual_start,
                    "source_tempo": source_tempo,
                    "padding_duration": padding_duration,
                }
                logger.info(f"  Visual program start: reference {ref_visual_start:.3f}s, "
                            f"foreign {foreign_visual_start:.3f}s -> preserving foreign preamble "
                            f"at {source_tempo:.6f}x plus {padding_duration:.3f}s padding")
            else:
                logger.warning(f"  Visual program start ignored: foreign preamble at FPS tempo "
                               f"({prefix_duration:.3f}s) exceeds reference black lead-in ({ref_visual_start:.3f}s).")
        else:
            logger.warning("  Visual program start not found in both videos; using audio boundaries.")

    ref_delay_s = 0.0 if visual_prefix else ref_start_s
    ref_content_duration = ref_end_s if visual_prefix else ref_end_s - ref_start_s
    foreign_content_duration = foreign_end_s - foreign_start_s

    logger.info(f"  -> Reference Audio Content: {format_time(ref_start_s)} -> {format_time(ref_end_s)} (Duration: {ref_content_duration:.3f}s)")
    logger.info(f"  -> Foreign Audio Content:   {format_time(foreign_start_s)} -> {format_time(foreign_end_s)} (Duration: {foreign_content_duration:.3f}s)")
    logger.info(f"  -> Calculated Reference Delay (Padding): {ref_delay_s:.3f} seconds")

    # --- Step 4: Combine and Filter Anchor Points ---
    logger.info(f"--- Filtering Anchor Points (Min Ref Segment Duration: {min_segment_duration}s, Max Duration Diff: {MAX_ALLOWED_DURATION_PERCENT_DIFF}%) ---")
    # Start with audio boundaries as the absolute first and last anchors
    all_anchors = ([(visual_prefix["ref_start"], visual_prefix["foreign_end"])]
                   if visual_prefix else [(ref_start_s, foreign_start_s)])
    added_image_count = 0
    added_forced_count = 0
    skipped_outside = 0
    
    # Track forced sync point reference times - these bypass all filtering
    forced_ref_times = set()

    # Add visual anchors from Stage 1, ensuring they fall within the detected audio content times
    # EXCEPTION: Forced sync points (starting with 'FORCED_SYNC_') bypass boundary checks
    for ref_name, _, ref_img_time, foreign_img_time in visual_anchors_details:
        # AUDIO_TRANSITION_ anchors bracket a precisely-located hard-cut (see _locate_transition_point)
        # and must bypass filtering just like FORCED_SYNC_ points, or the tiny segment they define
        # gets stripped out and the jump gets smeared back across the surrounding window.
        is_forced = (ref_name.startswith('FORCED_SYNC_')
                 or ref_name.startswith('AUDIO_TRANSITION_')
                 or ref_name.startswith('AUDIO_REPLACEMENT_'))

        replacement = next(
            (item for item in AUDIO_REPLACEMENT_RANGES if ref_name.startswith(item["id"])),
            None,
        )
        if (visual_prefix and replacement is not None
                and replacement["ref_start"] < visual_prefix["ref_start"] < replacement["ref_end"]):
            logger.info(f"  Skipping {replacement['id']}: visual preamble already covers its "
                        f"overlapping reference prefix ({replacement['ref_start']:.3f}s-"
                        f"{replacement['ref_end']:.3f}s).")
            continue
        
        if is_forced:
            # Forced sync points are always included (that's the point!)
            all_anchors.append((ref_img_time, foreign_img_time))
            forced_ref_times.add(ref_img_time)  # Track this as a forced anchor
            added_forced_count += 1
            logger.info(f"  > FORCED sync point added: Ref={format_time(ref_img_time)} -> Foreign={format_time(foreign_img_time)}")
        else:
            # Check if anchor falls within the content boundaries of BOTH reference and foreign audio
            anchor_ref_start = visual_prefix["ref_start"] if visual_prefix else ref_start_s
            anchor_foreign_start = visual_prefix["foreign_end"] if visual_prefix else foreign_start_s
            is_within_ref = (anchor_ref_start <= ref_img_time <= ref_end_s)
            is_within_foreign = (anchor_foreign_start <= foreign_img_time <= foreign_end_s)
            if is_within_ref and is_within_foreign:
                all_anchors.append((ref_img_time, foreign_img_time))
                added_image_count += 1
            else:
                logger.debug(f"    Skipping visual anchor RefT={ref_img_time:.3f}/ForeignT={foreign_img_time:.3f} - outside audio bounds ({is_within_ref=}, {is_within_foreign=})")
                skipped_outside += 1

    all_anchors.append((ref_end_s, foreign_end_s)) # Add audio end boundary
    all_anchors.sort() # Sort chronologically by reference time

    logger.info(f"  > Started with {len(all_anchors)} total anchors (2 audio boundaries + {added_image_count} visual + {added_forced_count} forced).")
    if skipped_outside > 0: logger.info(f"  > Skipped {skipped_outside} visual anchors falling outside audio content boundaries.")

    # --- Filter 1: Minimum Reference Segment Duration ---
    # NOTE: Forced sync points (in forced_ref_times) bypass this filter
    min_ref_dur_filtered_anchors = []
    skipped_short_ref = 0
    if all_anchors:
        min_ref_dur_filtered_anchors.append(all_anchors[0]) # Always keep the first anchor (audio start)
        for i in range(1, len(all_anchors)):
            last_kept_ref_time, _ = min_ref_dur_filtered_anchors[-1]
            current_ref_time, _ = all_anchors[i]
            segment_ref_duration = current_ref_time - last_kept_ref_time
            
            # ALWAYS keep forced sync points regardless of segment duration
            is_forced = current_ref_time in forced_ref_times

            # Keep the current anchor if it's forced OR if the segment meets minimum duration
            if is_forced:
                min_ref_dur_filtered_anchors.append(all_anchors[i])
                logger.debug(f"    MinRefDur Filter: KEEPING forced anchor (RefT={current_ref_time:.3f}) despite segment duration ({segment_ref_duration:.3f}s)")
            elif segment_ref_duration >= min_segment_duration:
                min_ref_dur_filtered_anchors.append(all_anchors[i])
            elif i < len(all_anchors) - 1: # Don't count removal if it's the segment before the very last anchor
                logger.debug(f"    MinRefDur Filter: Removing anchor {i} (RefT={current_ref_time:.3f}) because segment duration ({segment_ref_duration:.3f}s) < {min_segment_duration}s")
                skipped_short_ref += 1
            # else: Anchor is the last one, but segment is too short - keep it anyway to preserve the endpoint

        # Ensure the final anchor point (audio end) is always included, even if the last segment is short
        if len(all_anchors) > 1 and min_ref_dur_filtered_anchors[-1] != all_anchors[-1]:
            last_kept_ref, _ = min_ref_dur_filtered_anchors[-1]
            actual_last_ref, _ = all_anchors[-1]
            last_segment_dur = actual_last_ref - last_kept_ref
            logger.warning(f"  -> Final segment ({last_segment_dur:.2f}s) is shorter than minimum ({min_segment_duration}s), but keeping final endpoint.") # Use argument here
            min_ref_dur_filtered_anchors.append(all_anchors[-1]) # Re-add the true last anchor

    if skipped_short_ref > 0: logger.info(f"  -> Filtered out {skipped_short_ref} anchors creating reference segments shorter than {min_segment_duration}s.") # Use argument here

    # --- Filter 2: Maximum Segment Duration Percentage Difference ---
    fully_filtered_anchors = []
    skipped_percent_diff = 0
    if min_ref_dur_filtered_anchors:
        fully_filtered_anchors.append(min_ref_dur_filtered_anchors[0]) # Always keep the start anchor
        for i in range(len(min_ref_dur_filtered_anchors) - 1):
            ref_start, foreign_start = min_ref_dur_filtered_anchors[i]
            ref_end, foreign_end = min_ref_dur_filtered_anchors[i+1] # Look ahead to the next anchor

            ref_seg_duration = ref_end - ref_start
            foreign_seg_duration = foreign_end - foreign_start
            
            # ALWAYS keep forced sync points regardless of duration difference
            is_end_forced = ref_end in forced_ref_times
            
            if is_end_forced:
                # Forced sync point - always keep
                fully_filtered_anchors.append(min_ref_dur_filtered_anchors[i+1])
                logger.debug(f"    MaxDiff Filter: KEEPING forced anchor (RefT={ref_end:.3f}) despite duration diff")
            # Avoid division by zero for zero-duration segments
            elif ref_seg_duration > 1e-6: # Use a small epsilon
                duration_diff_percent = abs(ref_seg_duration - foreign_seg_duration) / ref_seg_duration * 100
                if duration_diff_percent <= MAX_ALLOWED_DURATION_PERCENT_DIFF:
                    # If difference is acceptable, keep the *end* anchor of this valid segment
                    fully_filtered_anchors.append(min_ref_dur_filtered_anchors[i+1])
                else:
                    logger.debug(f"    MaxDiff Filter: Removing anchor {i+1} (RefT={ref_end:.3f}) - segment duration diff ({duration_diff_percent:.2f}%) > {MAX_ALLOWED_DURATION_PERCENT_DIFF}%")
                    skipped_percent_diff += 1
            elif abs(foreign_seg_duration) < 1e-6:
                 # Both ref and foreign durations are near zero, keep the anchor
                 fully_filtered_anchors.append(min_ref_dur_filtered_anchors[i+1])
            else:
                 # Reference duration is zero/negative, foreign is not. This indicates a problem. Remove.
                 logger.warning(f"    MaxDiff Filter: Removing anchor {i+1} (RefT={ref_end:.3f}) due to zero/negative ref duration ({ref_seg_duration:.3f}s) vs non-zero foreign duration ({foreign_seg_duration:.3f}s).")
                 skipped_percent_diff += 1

        # Ensure the very last anchor point is always present after filtering
        if min_ref_dur_filtered_anchors and fully_filtered_anchors[-1] != min_ref_dur_filtered_anchors[-1]:
            logger.info("  -> Re-adding final audio endpoint anchor after duration difference filtering.")
            fully_filtered_anchors.append(min_ref_dur_filtered_anchors[-1])
            # Remove potential duplicate if the last was already added and identical to second-last
            if len(fully_filtered_anchors) > 1 and fully_filtered_anchors[-1] == fully_filtered_anchors[-2]:
                 fully_filtered_anchors.pop()

    if skipped_percent_diff > 0: logger.info(f"  -> Filtered out {skipped_percent_diff} anchors creating segments with duration difference > {MAX_ALLOWED_DURATION_PERCENT_DIFF}%.")


    final_segment_anchors = fully_filtered_anchors # Use the fully filtered list
    num_segments = len(final_segment_anchors) - 1 # Number of segments is number of anchors - 1

    logger.info(f"  -> Using {len(final_segment_anchors)} final anchors, defining {num_segments} segments for processing.")
    if num_segments <= 0:
        logger.error("-> Need at least 2 final anchors (start and end) to define segments. Cannot proceed.")
        return None, None

    if args.splice_safety_report_csv:
        write_splice_safety_report(args.splice_safety_report_csv, args, final_segment_anchors)

    # --- Step 5: Write Segment Info to CSV (Optional) ---
    if args.output_csv: # Check if CSV output is requested
        logger.info(f"--- Writing segment information to CSV: {args.output_csv} ---")
        try:
            with open(args.output_csv, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                # Define headers
                header = [
                    "Segment", "Ref Start Time", "Ref End Time", "Foreign Start Time", "Foreign End Time",
                    "Ref Duration (s)", "Foreign Duration (s)", "Duration Diff (%)",
                    "Initial Speed Factor" # Speed factor before iterative adjustment
                ]
                writer.writerow(header)

                # Write data for each segment
                for i in range(num_segments):
                    ref_start, foreign_start = final_segment_anchors[i]
                    ref_end, foreign_end = final_segment_anchors[i+1]
                    ref_dur = ref_end - ref_start
                    foreign_dur = foreign_end - foreign_start

                    # Calculate percentage difference and initial speed
                    percent_diff = float('inf')
                    initial_speed = 1.0
                    if ref_dur > 1e-6: # Avoid division by zero
                        percent_diff = abs(ref_dur - foreign_dur) / ref_dur * 100
                        initial_speed = foreign_dur / ref_dur
                    elif abs(foreign_dur) < 1e-6: # Both near zero
                         percent_diff = 0.0

                    writer.writerow([
                        i + 1,
                        format_time(ref_start), format_time(ref_end),
                        format_time(foreign_start), format_time(foreign_end),
                        f"{ref_dur:.3f}", f"{foreign_dur:.3f}",
                        f"{percent_diff:.2f}" if percent_diff != float('inf') else "N/A",
                        f"{initial_speed:.5f}"
                    ])
            logger.info(f"  -> Successfully wrote segment data to {args.output_csv}")
        except Exception as e:
            logger.error(f"-> Failed to write segment CSV file: {e}", exc_info=True)
            # Continue processing even if CSV writing fails
    else:
        logger.debug("Skipping CSV output (not requested)")

    # --- Step 6: Process Segments Iteratively ---
    logger.info(f"--- Processing {num_segments} Segments ---")
    process_start_time = time.time()
    processed_segment_files = [] # List to store paths of successfully processed segments
    total_processed_ref_duration = 0.0 # Sum of target durations for processed segments

    if visual_prefix:
        prefix_target_duration = visual_prefix["foreign_end"] / visual_prefix["source_tempo"]
        prefix_path = process_segment_iteratively(
            foreign_wav_full=foreign_wav_full,
            foreign_start=0.0,
            foreign_end=visual_prefix["foreign_end"],
            ref_duration=prefix_target_duration,
            segment_num=0,
            temp_dir=temp_dir,
            max_iterations=3,
            target_precision_ms=5,
            fixed_speed=visual_prefix["source_tempo"],
        )
        if prefix_path is None:
            logger.error("-> Failed to preserve foreign preamble before visual program start.")
            return None, None
        processed_segment_files.append(prefix_path)
        actual_prefix_duration = get_file_duration(prefix_path, media_type='audio')
        if actual_prefix_duration is None:
            logger.error("-> Failed to measure preserved foreign preamble duration.")
            return None, None
        total_processed_ref_duration += actual_prefix_duration
        visual_padding_duration = visual_prefix["ref_start"] - actual_prefix_duration
        if visual_padding_duration < -0.005:
            logger.error(f"-> Fixed-FPS visual preamble ({actual_prefix_duration:.3f}s) exceeds "
                         f"the reference visual start ({visual_prefix['ref_start']:.3f}s).")
            return None, None
        if visual_padding_duration > 0.001:
            padding_path = os.path.join(temp_dir, "visual_start_padding.wav")
            padding_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
                "-f", "lavfi", "-i", f"anullsrc=r={DEFAULT_SAMPLE_RATE}:cl={'stereo' if DEFAULT_CHANNELS == 2 else 'mono'}",
                "-t", f"{visual_padding_duration:.8f}",
                "-c:a", "pcm_s16le", "-y", padding_path,
            ]
            if not run_ffmpeg(padding_cmd, "Create Visual Start Padding")[0]:
                return None, None
            processed_segment_files.append(padding_path)
            total_processed_ref_duration += visual_padding_duration
        logger.info(f"  Visual program start aligned: fixed-FPS preamble {actual_prefix_duration:.3f}s + "
                    f"padding {max(0.0, visual_padding_duration):.3f}s = {visual_prefix['ref_start']:.3f}s")

    # Configure iterative processing parameters
    max_iterations = 3       # Max attempts per segment
    target_precision_ms = 5  # Target accuracy in milliseconds
    
    # Track corrections for summary
    segments_with_corrections = 0

    # Use progress bar for segment processing (visible on console)
    # Dynamic description shows current segment being processed
    pbar = tqdm(
        total=num_segments,
        desc="Processing",
        unit="seg",
        ncols=80,
        bar_format='{desc}: {bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]',
        file=sys.stdout,
        dynamic_ncols=False
    )

    for i in range(num_segments):
        segment_num = i + 1
        pbar.set_description(f"Segment {segment_num:3d}/{num_segments}")
        ref_start, foreign_start = final_segment_anchors[i]
        ref_end, foreign_end = final_segment_anchors[i+1]
        target_ref_duration = ref_end - ref_start # This is the target duration for the output segment

        replacement_range = next(
            (item for item in AUDIO_REPLACEMENT_RANGES
             if abs(item["ref_start"] - ref_start) < 0.001
             and abs(item["ref_end"] - ref_end) < 0.001),
            None,
        )
        hard_cut_range = next(
            (item for item in AUDIO_HARD_CUT_RANGES
             if abs(item["ref_start"] - ref_start) < 0.001
             and abs(item["ref_end"] - ref_end) < 0.001),
            None,
        )
        segment_source_wav = foreign_wav_full
        segment_source_start = foreign_start
        segment_source_end = foreign_end
        if hard_cut_range is not None or (replacement_range is not None and replacement_range.get("use_silence")):
            # Copying reference audio here would hard-cut mid-note/mid-phrase content,
            # which sounds worse than a silent gap of the same duration.
            if hard_cut_range is not None:
                logger.info(f"  -> Segment {segment_num}: Applying editorial hard cut "
                            f"({foreign_start:.3f}s-{foreign_end:.3f}s source removed; "
                            f"{target_ref_duration:.3f}s neutral transition)")
            else:
                logger.info(f"  -> Segment {segment_num}: Filling missing foreign content "
                            f"({ref_start:.3f}s-{ref_end:.3f}s) with silence "
                            f"(reference audio here is not a natural cut point)")
            silence_path = os.path.join(temp_dir, f"segment_{segment_num:04d}_silence.wav")
            silence_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
                "-f", "lavfi", "-i", f"anullsrc=r={DEFAULT_SAMPLE_RATE}:cl={'stereo' if DEFAULT_CHANNELS == 2 else 'mono'}",
                "-t", f"{target_ref_duration:.8f}",
                "-c:a", "pcm_s16le", "-y", silence_path
            ]
            if run_ffmpeg(silence_cmd, f"Create Silence Fill for Segment {segment_num} ({target_ref_duration:.3f}s)")[0]:
                processed_segment_files.append(silence_path)
                total_processed_ref_duration += target_ref_duration
                pbar.update(1)
                continue
            else:
                logger.error(f" Segment {segment_num}: Failed to create silence fill; falling back to copying reference audio.")
        if replacement_range is not None:
            segment_source_wav = ref_wav_full
            segment_source_start = ref_start
            segment_source_end = ref_end
            logger.info(f"  -> Segment {segment_num}: Filling missing foreign content "
                        f"({ref_start:.3f}s-{ref_end:.3f}s) with reference audio")

        logger.debug(f"Processing Segment {segment_num}/{num_segments}")

        # Level-match inserted reference audio to its surrounding foreign content, so the
        # splice doesn't suddenly sound much louder/quieter than the rest of the track.
        gain_db = 0.0
        if replacement_range is not None:
            context_window = 1.5
            context_levels = []
            if i > 0:
                level_before = measure_mean_volume_db(foreign_wav_full, max(0.0, foreign_start - context_window), foreign_start)
                if level_before is not None and level_before > -60.0:
                    context_levels.append(level_before)
            if i < num_segments - 1:
                level_after = measure_mean_volume_db(foreign_wav_full, foreign_end, foreign_end + context_window)
                if level_after is not None and level_after > -60.0:
                    context_levels.append(level_after)
            ref_level = measure_mean_volume_db(ref_wav_full, ref_start, ref_end)
            if context_levels and ref_level is not None and ref_level > -60.0:
                target_level = sum(context_levels) / len(context_levels)
                gain_db = max(-15.0, min(15.0, target_level - ref_level))
                if abs(gain_db) > 0.5:
                    logger.info(f"  -> Segment {segment_num}: Matching inserted audio level to surrounding "
                                f"foreign content ({ref_level:+.1f}dB -> {target_level:+.1f}dB, gain {gain_db:+.1f}dB)")
                else:
                    gain_db = 0.0

        # Call the iterative processing function for this segment
        fixed_speed = None
        if visual_prefix and i == 0:
            fixed_speed = visual_prefix["source_tempo"]
            expected_foreign_end = foreign_start + target_ref_duration * fixed_speed
            logger.info(f"  -> Segment {segment_num}: Visual-start anchor fixes tempo at {fixed_speed:.6f}x; "
                        f"next audio-anchor residual {foreign_end - expected_foreign_end:+.3f}s.")
        segment_path = process_segment_iteratively(
            foreign_wav_full=segment_source_wav,
            foreign_start=segment_source_start,
            foreign_end=segment_source_end,
            ref_duration=target_ref_duration, # Pass the target duration
            segment_num=segment_num,
            temp_dir=temp_dir,
            max_iterations=max_iterations,
            target_precision_ms=target_precision_ms,
            is_first_segment=(segment_num == 1),
            is_last_segment=(segment_num == num_segments),
            first_adjust_ms=args.first_segment_adjust,
            last_adjust_ms=args.last_segment_adjust,
            gain_db=gain_db,
            fixed_speed=fixed_speed,
        )

        if segment_path and os.path.exists(segment_path):
            processed_segment_files.append(segment_path)
            total_processed_ref_duration += target_ref_duration # Add target duration to total
        else:
            fallback_path = os.path.join(temp_dir, f"segment_{segment_num:04d}_fallback.wav")
            logger.warning(f"Segment {segment_num}: iterative processing failed. Activating direct segment fallback recovery.")
            if fallback_direct_segment(
                source_wav=segment_source_wav,
                source_start=segment_source_start,
                source_end=segment_source_end,
                out_path=fallback_path,
                segment_num=segment_num,
                label="segment",
                gain_db=gain_db
            ):
                processed_segment_files.append(fallback_path)
                total_processed_ref_duration += target_ref_duration
            else:
                pbar.close()
                logger.error(f"Failed to recover segment {segment_num}. Both iterative processing and direct fallback failed; aborting audio synchronization.")
                return None, None # Critical failure, stop processing
        
        pbar.update(1)

    pbar.close()
    
    process_elapsed_time = time.time() - process_start_time
    if not processed_segment_files:
         logger.error("No audio segments were successfully processed.")
         return None, None
    logger.info(f"  [OK] Processed {len(processed_segment_files)} segments in {process_elapsed_time:.1f}s")


    # --- Step 7: Concatenate Processed Segments ---
    logger.info("--- Concatenating Precisely-Timed Segments ---")
    concatenated_foreign_temp = os.path.join(temp_dir, "foreign_concatenated_temp.wav")
    concat_list_path = os.path.join(temp_dir, "concat_list_final.txt")

    try:
        with open(concat_list_path, 'w', encoding='utf-8') as f_concat:
            for seg_path in processed_segment_files:
                # Absolute path required: concat demuxer resolves relative to process CWD, not list location
                abs_path = os.path.abspath(seg_path).replace("\\", "/")
                f_concat.write(f"file '{abs_path}'\n")
    except IOError as e:
        logger.error(f"-> Failed to create final concat list: {e}")
        return None, None

    # FFmpeg command for concatenation
    concat_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
        "-f", "concat", "-safe", "0", # Allow relative paths from list file
        "-i", concat_list_path,
        "-c", "copy", # Copy streams without re-encoding
        "-y", concatenated_foreign_temp
    ]

    if not run_ffmpeg(concat_cmd, "Concatenate All Processed Segments")[0]:
        logger.error("-> Failed to concatenate processed segments.")
        return None, None

    # --- Step 8: Verify Final Concatenated Duration ---
    final_duration = get_file_duration(concatenated_foreign_temp, media_type='audio')
    expected_total_duration = ref_content_duration # Should match the total duration of the reference content

    if final_duration is not None:
        duration_diff = final_duration - expected_total_duration
        logger.info(f"  -> Final Concatenated Audio Duration: {final_duration:.3f}s (Expected Reference Content Duration: {expected_total_duration:.3f}s)")
        if abs(duration_diff) > 0.1: # Check if difference is > 100ms
            logger.warning(f"  -> WARNING: Final duration differs from expected reference duration by {duration_diff:+.3f}s. Check segment processing logs.")
        else:
            logger.info(f"  -> SUCCESS: Final duration matches expected reference duration within {abs(duration_diff):.3f}s.")
    else:
        logger.warning("  -> Could not verify final concatenated audio duration using ffprobe.")


    # --- Step 9: Apply Start Delay Padding ---
    logger.info("--- Applying Start Delay Padding ---")
    if ref_delay_s >= MIN_DELAY_S: # Only pad if delay is significant
        delay_ms = int(ref_delay_s * 1000)
        logger.info(f"  Applying {delay_ms}ms start delay padding...")
        pad_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostats",
            "-i", concatenated_foreign_temp, # Input is the concatenated audio
            "-af", f"adelay={delay_ms}|{delay_ms}", # Apply delay to all channels
            "-c:a", "pcm_s16le", # Keep WAV format for output
            "-ar", str(DEFAULT_SAMPLE_RATE), "-ac", str(DEFAULT_CHANNELS), # Maintain audio spec
            "-y", output_audio_path # Final output file path
        ]

        if not run_ffmpeg(pad_cmd, f"Apply {delay_ms}ms Padding")[0]:
            logger.error(f"-> Failed to apply start delay padding to final audio.")
            # Output without padding might still exist at concatenated_foreign_temp
            # Consider copying it to output_audio_path as a fallback?
            try:
                shutil.copy2(concatenated_foreign_temp, output_audio_path)
                logger.warning(f"  -> Copied unpadded audio to {output_audio_path} as padding failed.")
            except Exception as copy_err:
                logger.error(f"  -> Failed to copy unpadded audio as fallback: {copy_err}")
                return None, None # Indicate failure if padding fails and copy fails
        else:
             logger.info(f"  -> Successfully added {delay_ms}ms start delay padding.")
    else:
        # If delay is too small, just copy the concatenated file to the final output path
        logger.info(f"  -> Skipping delay padding (Reference delay {ref_delay_s:.3f}s < {MIN_DELAY_S:.3f}s).")
        try:
            shutil.copy2(concatenated_foreign_temp, output_audio_path)
            logger.info(f"  -> Copied concatenated audio directly to {output_audio_path}.")
        except Exception as e:
            logger.error(f"-> Failed to copy final audio (without padding): {e}")
            return None, None # Indicate failure if copy fails

    # --- Stage Completion ---
    stage_elapsed_time = time.time() - stage_start_time
    logger.info(f"---=== Audio Synchronization Stage Finished ({stage_elapsed_time:.2f}s) ===---")
    # Return the calculated delay and the final list of anchors used (for QC)
    return ref_delay_s, final_segment_anchors


# --- QC Image Generation ---
def _create_single_qc(ref_frame_path, foreign_frame_path, qc_output_path):
    """Creates a side-by-side comparison image from two frame paths."""
    try:
        img_ref = cv2.imread(ref_frame_path)
        img_foreign = cv2.imread(foreign_frame_path)

        if img_ref is None or img_foreign is None:
            logger.warning(f"QC Skip: Could not read images for QC: Ref='{os.path.basename(ref_frame_path)}', Foreign='{os.path.basename(foreign_frame_path)}'")
            return False

        h_ref, w_ref = img_ref.shape[:2]
        h_foreign, w_foreign = img_foreign.shape[:2]

        # Ensure images have valid dimensions
        if h_ref == 0 or w_ref == 0 or h_foreign == 0 or w_foreign == 0:
            logger.warning(f"QC Skip: Invalid image dimensions for Ref='{os.path.basename(ref_frame_path)}' or Foreign='{os.path.basename(foreign_frame_path)}'")
            return False

        target_h = QC_IMAGE_HEIGHT # Use constant for target height

        # Resize reference image maintaining aspect ratio
        ref_ratio = target_h / h_ref
        new_w_ref = int(w_ref * ref_ratio)
        if new_w_ref <= 0: return False # Check for invalid width
        img_ref_resized = cv2.resize(img_ref, (new_w_ref, target_h), interpolation=cv2.INTER_AREA)

        # Resize foreign image maintaining aspect ratio
        foreign_ratio = target_h / h_foreign
        new_w_foreign = int(w_foreign * foreign_ratio)
        if new_w_foreign <= 0: return False # Check for invalid width
        img_foreign_resized = cv2.resize(img_foreign, (new_w_foreign, target_h), interpolation=cv2.INTER_AREA)

        # Concatenate horizontally
        qc_image = cv2.hconcat([img_ref_resized, img_foreign_resized])

        # Save the QC image (use PNG for lossless quality)
        # Add compression level for PNG to potentially reduce size
        cv2.imwrite(qc_output_path, qc_image, [cv2.IMWRITE_PNG_COMPRESSION, 3]) # Compression level 0-9
        return True

    except Exception as e:
        logger.error(f"Error creating QC image '{os.path.basename(qc_output_path)}': {e}", exc_info=True)
        return False

def create_qc_images(visual_anchors_details, final_segment_anchors,
                     ref_extract_path, foreign_extract_path, qc_output_dir):
    """Generates side-by-side QC images for the anchor points used in the final segments."""
    # qc_output_dir check is done in main() before calling this
    if not visual_anchors_details:
         logger.warning("QC image generation skipped: No visual anchor details available.")
         return
    # Need at least start, one intermediate, and end anchor (3 total) to have intermediate points
    if not final_segment_anchors or len(final_segment_anchors) < 3:
        logger.warning("QC image generation skipped: Not enough final segment anchors (need >= 3) to generate QC for intermediate points.")
        return

    logger.info("\n===== QC Image Generation =====")
    logger.info(f"Saving QC images to: {qc_output_dir}")
    os.makedirs(qc_output_dir, exist_ok=True)
    start_time = time.time()
    qc_count = 0

    # Create a lookup map from the original visual anchors list: (ref_time, foreign_time) -> (ref_filename, foreign_filename)
    visual_lookup = {(r_t, f_t): (r_fn, f_fn) for r_fn, f_fn, r_t, f_t in visual_anchors_details}

    # We want QC images for the *internal* anchor points that defined the final segments.
    # Exclude the very first (audio start) and very last (audio end) anchors from QC generation,
    # as these might not correspond directly to visually matched frames.
    internal_segment_anchors = final_segment_anchors[1:-1]

    if not internal_segment_anchors:
         logger.warning("No internal anchor points found after filtering; cannot generate intermediate QC images.")
         return

    logger.info(f"Attempting to generate QC images for {len(internal_segment_anchors)} internal anchor points used in final segments.")

    for ref_ts, foreign_ts in tqdm(internal_segment_anchors, desc="  Generating QC", unit="image", ncols=100, leave=False):
        anchor_tuple = (ref_ts, foreign_ts)

        # Find the corresponding filenames using the lookup map
        if anchor_tuple in visual_lookup:
            ref_filename, foreign_filename = visual_lookup[anchor_tuple]
            ref_frame_path = os.path.join(ref_extract_path, ref_filename)
            foreign_frame_path = os.path.join(foreign_extract_path, foreign_filename)

            # Check if the actual frame image files exist
            if os.path.exists(ref_frame_path) and os.path.exists(foreign_frame_path):
                # Create a descriptive filename for the QC image
                ref_filename_base = os.path.splitext(ref_filename)[0] # e.g., "frame_000123"
                # Include reference timestamp in filename for easy identification
                qc_filename = f"qc_{ref_filename_base}_reft{ref_ts:.3f}s.png"
                qc_output_path = os.path.join(qc_output_dir, qc_filename)

                # Create the single QC image
                if _create_single_qc(ref_frame_path, foreign_frame_path, qc_output_path):
                    qc_count += 1
            else:
                logger.warning(f"QC Skip: Frame file missing for anchor (RefT={ref_ts:.3f}, ForeignT={foreign_ts:.3f}): Ref='{ref_filename}' or Foreign='{foreign_filename}'")
        else:
            # This might happen if an audio boundary point coincides exactly with a visual anchor time,
            # but generally internal points should come from the visual_anchors_details list.
            logger.warning(f"QC Skip: Could not find original filenames in visual anchor details for final segment anchor point (RefT={ref_ts:.3f}, ForeignT={foreign_ts:.3f})")

    elapsed_time = time.time() - start_time
    logger.info(f"  -> QC image generation complete. Created {qc_count} images ({elapsed_time:.2f}s).")


# --- Muxing Function ---

def run_muxing(args, ref_stream_idx, synced_subtitles=None, synced_foreign_tracks=None):
    """
    Muxes synced foreign audio track(s) into the reference video while preserving ALL original
    streams, metadata, attachments (fonts), and chapters from the reference video.
    
    For MKV output: Uses mkvmerge which natively preserves everything.
    For non-MKV output: Uses ffmpeg with full stream mapping.
    
    Each foreign audio track is tagged with proper language metadata.
    
    Args:
        args: parsed arguments
        ref_stream_idx: absolute stream index of reference audio (for ffmpeg fallback)
        synced_subtitles: list of subtitle dicts (optional)
        synced_foreign_tracks: list of dicts [{'wav_path': str, 'language': str, 'stream_idx': int}, ...]
            If None, falls back to single-track mode using args.output_audio and args.foreign_lang
    """
    if not args.output_video:
        logger.error("No output video path specified. Cannot mux.")
        return False

    # Build track list (backward compatible: single track if synced_foreign_tracks not provided)
    if synced_foreign_tracks is None:
        synced_foreign_tracks = [{
            'wav_path': args.output_audio,
            'language': args.foreign_lang,
            'stream_idx': args.foreign_stream_idx or 0,
        }]

    # Validate all WAV files exist
    for track in synced_foreign_tracks:
        if not os.path.exists(track['wav_path']):
            logger.error(f"Muxing failed: Synced audio file '{track['wav_path']}' not found.")
            return False

    logger.info("\n===== Muxing Stage =====")
    logger.info(f"  Reference Video Source: {os.path.basename(args.ref_video)}")
    for i, track in enumerate(synced_foreign_tracks):
        logger.info(f"  Foreign Audio Track {i+1}:  Stream #{track['stream_idx']} ({track['language']}) -> {os.path.basename(track['wav_path'])}")
    logger.info(f"  Output Muxed Video:     {os.path.basename(args.output_video)}")

    is_mkv_output = args.output_video.lower().endswith(('.mkv', '.mka', '.mks'))

    if is_mkv_output and MKVMERGE_EXEC:
        success = _mux_with_mkvmerge(args, synced_foreign_tracks, synced_subtitles)
    else:
        if is_mkv_output and not MKVMERGE_EXEC:
            logger.warning("mkvmerge not found. Falling back to ffmpeg for MKV muxing.")
            logger.warning("  Note: Some attachments/chapters may not be preserved. Install mkvtoolnix for best results.")
        success = _mux_with_ffmpeg(args, ref_stream_idx, synced_foreign_tracks, synced_subtitles)

    # Clean up temporary WAV files
    is_temp_wav = args.output_audio_original is None
    for track in synced_foreign_tracks:
        wav_path = track['wav_path']
        # Delete temp WAVs (the primary one if temp, and always additional ones)
        is_additional = wav_path != args.output_audio
        should_delete = is_additional or is_temp_wav
        if os.path.exists(wav_path) and should_delete:
            try:
                os.remove(wav_path)
                logger.info(f"  -> Deleted temporary audio file: {os.path.basename(wav_path)}")
            except Exception as e:
                logger.warning(f"  Note: Could not delete temporary audio file '{os.path.basename(wav_path)}': {e}")
    
    if not is_temp_wav:
        logger.info(f"  Keeping user-specified synchronized audio file: {args.output_audio}")

    if not success:
        logger.error(f"Muxing failed.")
        return False

    logger.info(f"---=== Muxing Stage Finished Successfully ===---")
    return True


# Maps a source audio codec_name to the ffmpeg encoder used to re-encode it "in kind".
AUDIO_CODEC_ENCODER_MAP = {
    'aac': 'aac', 'mp3': 'libmp3lame', 'ac3': 'ac3', 'eac3': 'eac3',
    'opus': 'libopus', 'vorbis': 'libvorbis', 'flac': 'flac', 'alac': 'alac',
    'pcm_s16le': 'pcm_s16le', 'pcm_s24le': 'pcm_s24le', 'pcm_s32le': 'pcm_s32le',
}
LOSSLESS_AUDIO_ENCODERS = {'flac', 'alac', 'pcm_s16le', 'pcm_s24le', 'pcm_s32le'}


def resolve_output_audio_settings(source_stream_info, requested_codec, requested_bitrate):
    """Resolve 'auto' codec/bitrate to match the foreign source, so the output neither
    gains nor loses quality/format by default unless the user explicitly overrides it."""
    source_codec = (source_stream_info or {}).get('codec_name', '').lower()
    source_bitrate = (source_stream_info or {}).get('bit_rate')

    codec = requested_codec
    if codec == 'auto':
        codec = AUDIO_CODEC_ENCODER_MAP.get(source_codec)
        if not codec:
            logger.warning(f"  No suitable encoder mapping for source codec '{source_codec}'; "
                           f"falling back to lossless FLAC to avoid any quality loss.")
            codec = 'flac'
        else:
            logger.info(f"  Auto-selected output codec '{codec}' to match source codec '{source_codec}'.")

    bitrate = requested_bitrate
    if bitrate == 'auto':
        if codec in LOSSLESS_AUDIO_ENCODERS:
            bitrate = None  # Not applicable for lossless encoders
        elif source_bitrate and str(source_bitrate).isdigit():
            bitrate = f"{max(1, round(int(source_bitrate) / 1000))}k"
            logger.info(f"  Auto-selected output bitrate '{bitrate}' to match source bitrate.")
        else:
            bitrate = DEFAULT_MUX_ABITRATE_FALLBACK
            logger.warning(f"  Could not detect source bitrate; using fallback {bitrate}.")
    return codec, bitrate


def get_foreign_audio_stream_info(video_path, stream_idx):
    """Looks up the ffprobe stream dict for a specific absolute audio stream index."""
    streams = get_stream_info(video_path)
    if not streams:
        return None
    return next((s for s in streams if s.get('index') == stream_idx), None)


def _mux_with_mkvmerge(args, synced_foreign_tracks, synced_subtitles=None):
    """
    Mux using mkvmerge - preserves ALL original content from reference video
    and adds synced foreign audio track(s) with proper language tagging.
    """
    logger.info("  Using mkvmerge (preserves all original content)")

    # Pre-encode foreign audio tracks if needed (mkvmerge can't transcode)
    encoded_tracks = []
    temp_encoded_files = []

    for track in synced_foreign_tracks:
        foreign_audio_input = track['wav_path']

        source_stream_info = get_foreign_audio_stream_info(args.foreign_video, track['stream_idx'])
        codec, bitrate = resolve_output_audio_settings(
            source_stream_info, args.mux_foreign_codec, args.mux_foreign_bitrate)

        if codec != 'copy' and codec != 'pcm_s16le':
            ext_map = {'aac': '.m4a', 'ac3': '.ac3', 'flac': '.flac', 'opus': '.opus'}
            ext = ext_map.get(codec, '.mka')
            temp_encoded = track['wav_path'] + ext

            encode_cmd = [
                'ffmpeg', '-hide_banner', '-loglevel', 'warning',
                '-i', track['wav_path'],
            ]
            if args.audio_filters:
                encode_cmd.extend(['-af', args.audio_filters])
            encode_cmd.extend(['-c:a', codec])
            if bitrate:
                encode_cmd.extend(['-b:a', bitrate])
            encode_cmd.extend(['-y', temp_encoded])
            encode_success, _ = run_ffmpeg(encode_cmd, f"Pre-encode track #{track['stream_idx']}")
            if not encode_success:
                logger.error(f"Failed to pre-encode track #{track['stream_idx']}")
                # Clean up already encoded files
                for f in temp_encoded_files:
                    try: os.remove(f)
                    except: pass
                return False
            foreign_audio_input = temp_encoded
            temp_encoded_files.append(temp_encoded)
        
        encoded_tracks.append({**track, 'encoded_path': foreign_audio_input})

    # Build mkvmerge command
    mkvmerge_cmd = [
        'mkvmerge',
        '--output', args.output_video,
        # Reference video: include everything as-is
        args.ref_video,
    ]

    # Add each foreign audio track with language/title options BEFORE the file
    for i, track in enumerate(encoded_tracks):
        mkvmerge_cmd.extend([
            '--language', f'0:{track["language"]}',
            track['encoded_path'],
        ])

    # Add synced subtitle files if available
    if synced_subtitles:
        for sub_info in synced_subtitles:
            mkvmerge_cmd.extend([
                '--language', f"0:{sub_info['language']}",
                sub_info['path'],
            ])
        logger.info(f"  Including {len(synced_subtitles)} synced subtitle stream(s)")

    success = run_mkvmerge(mkvmerge_cmd, "Mux Final Video (mkvmerge)")

    # Clean up temp encoded files
    for f in temp_encoded_files:
        try: os.remove(f)
        except: pass

    return success


def _mux_with_ffmpeg(args, ref_stream_idx, synced_foreign_tracks, synced_subtitles=None):
    """
    Mux using ffmpeg - maps ALL streams from reference video and adds
    synced foreign audio track(s) with proper language metadata.
    """
    logger.info("  Using ffmpeg (mapping all original streams)")

    if ref_stream_idx is None:
        logger.warning("Reference stream index not provided to muxing stage, attempting to find again.")
        ref_streams = get_stream_info(args.ref_video)
        ref_stream_idx = find_audio_stream_index_by_lang(ref_streams, args.ref_lang)
        if ref_stream_idx is None:
            logger.error("Could not determine reference audio stream index for muxing. Aborting mux.")
            return False
    logger.info(f"  Using Reference Audio Stream Index: {ref_stream_idx}")

    # Probe the reference video to count audio streams for metadata indexing
    probe_cmd = [
        'ffprobe', '-v', 'error',
        '-select_streams', 'a',
        '-show_entries', 'stream=index',
        '-of', 'json',
        args.ref_video
    ]
    try:
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        ref_audio_streams = json.loads(result.stdout).get('streams', [])
        num_ref_audio = len(ref_audio_streams)
    except Exception:
        num_ref_audio = 1

    # Build ffmpeg command
    ffmpeg_cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'warning', '-stats',
        '-i', args.ref_video,       # Input 0: Reference video (ALL streams)
    ]

    # Add each foreign audio track as an input
    for i, track in enumerate(synced_foreign_tracks):
        ffmpeg_cmd.extend(['-i', track['wav_path']])  # Input 1, 2, 3...

    # Add subtitle inputs after audio inputs
    subtitle_input_start = len(synced_foreign_tracks) + 1  # After ref video + foreign tracks
    if synced_subtitles:
        for sub_info in synced_subtitles:
            ffmpeg_cmd.extend(['-i', sub_info['path']])
        logger.info(f"  Including {len(synced_subtitles)} synced subtitle stream(s)")

    # Map ALL streams from reference video
    ffmpeg_cmd.extend(['-map', '0'])

    # Map each foreign audio track
    for i in range(len(synced_foreign_tracks)):
        ffmpeg_cmd.extend(['-map', f'{i+1}:a:0'])

    # Map subtitle streams
    if synced_subtitles:
        for i in range(len(synced_subtitles)):
            ffmpeg_cmd.extend(['-map', f'{subtitle_input_start + i}:s'])

    # Codec settings - copy everything from reference
    ffmpeg_cmd.extend(['-c', 'copy'])

    # Encode each new foreign audio track, resolving 'auto' codec/bitrate to match its source
    for i, track in enumerate(synced_foreign_tracks):
        audio_idx = num_ref_audio + i
        source_stream_info = get_foreign_audio_stream_info(args.foreign_video, track['stream_idx'])
        codec, bitrate = resolve_output_audio_settings(
            source_stream_info, args.mux_foreign_codec, args.mux_foreign_bitrate)
        ffmpeg_cmd.extend([f'-c:a:{audio_idx}', codec])
        if bitrate:
            ffmpeg_cmd.extend([f'-b:a:{audio_idx}', bitrate])
        if args.audio_filters:
            ffmpeg_cmd.extend([f'-filter:a:{audio_idx}', args.audio_filters])

    # Subtitle codec for new subtitles
    if synced_subtitles:
        ffmpeg_cmd.extend(['-c:s', 'copy'])

    # Metadata for each new foreign audio track
    for i, track in enumerate(synced_foreign_tracks):
        audio_idx = num_ref_audio + i
        ffmpeg_cmd.extend([
            f'-metadata:s:a:{audio_idx}', f'language={track["language"]}',
        ])

    # Subtitle metadata
    if synced_subtitles:
        for i, sub_info in enumerate(synced_subtitles):
            ffmpeg_cmd.extend([f'-metadata:s:s:{i}', f"language={sub_info['language']}"])

    # Preserve chapters and global metadata
    ffmpeg_cmd.extend([
        '-map_metadata', '0',
        '-map_chapters', '0',
        '-y',
        args.output_video
    ])

    success, _ = run_ffmpeg(ffmpeg_cmd, "Mux Final Video (ffmpeg)")
    return success


# --- Main Execution ---
# --- SUBTITLE SYNCHRONIZATION FUNCTIONS ---

# ---- SRT helpers ----

def seconds_to_srt_time(seconds):
    """Convert seconds to SRT timestamp format (HH:MM:SS,mmm)"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def srt_time_to_seconds(srt_time):
    """Convert SRT timestamp format (HH:MM:SS,mmm) to seconds"""
    time_parts, millis = srt_time.split(',')
    h, m, s = map(int, time_parts.split(':'))
    return h * 3600 + m * 60 + s + int(millis) / 1000.0


def parse_srt_file(srt_path):
    """Parse SRT file into list of subtitle entries"""
    subtitles = []
    
    try:
        with open(srt_path, 'r', encoding='utf-8-sig') as f:
            content = f.read()
    except UnicodeDecodeError:
        try:
            with open(srt_path, 'r', encoding='latin-1') as f:
                content = f.read()
        except Exception as e:
            logger.error(f"Failed to read subtitle file {srt_path}: {e}")
            return None
    
    blocks = re.split(r'\n\s*\n', content.strip())
    
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue
        
        timing_line = lines[1]
        timing_match = re.match(r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})', timing_line)
        
        if not timing_match:
            continue
        
        start_time = srt_time_to_seconds(timing_match.group(1))
        end_time = srt_time_to_seconds(timing_match.group(2))
        text = '\n'.join(lines[2:])
        
        subtitles.append({
            'start': start_time,
            'end': end_time,
            'text': text
        })
    
    return subtitles


def write_srt_file(srt_path, subtitles):
    """Write subtitles to SRT file"""
    try:
        with open(srt_path, 'w', encoding='utf-8') as f:
            for idx, sub in enumerate(subtitles, 1):
                f.write(f"{idx}\n")
                f.write(f"{seconds_to_srt_time(sub['start'])} --> {seconds_to_srt_time(sub['end'])}\n")
                f.write(f"{sub['text']}\n\n")
        return True
    except Exception as e:
        logger.error(f"Failed to write subtitle file {srt_path}: {e}")
        return False


# ---- ASS/SSA helpers ----

def seconds_to_ass_time(seconds):
    """Convert seconds to ASS timestamp format (H:MM:SS.cc) — centisecond precision"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centisecs = int(round((seconds - int(seconds)) * 100))
    if centisecs >= 100:  # handle rounding edge
        centisecs = 99
    return f"{hours}:{minutes:02d}:{secs:02d}.{centisecs:02d}"


def ass_time_to_seconds(ass_time):
    """Convert ASS timestamp format (H:MM:SS.cc) to seconds"""
    try:
        parts = ass_time.strip().split(':')
        h = int(parts[0])
        m = int(parts[1])
        rest = parts[2].split('.')
        s = int(rest[0])
        cs = int(rest[1]) if len(rest) > 1 else 0
        return h * 3600 + m * 60 + s + cs / 100.0
    except (ValueError, IndexError):
        return 0.0


def parse_ass_file(ass_path):
    """Parse ASS/SSA file preserving all header/style data.
    
    Returns:
        dict with keys:
            'header_lines': list of all lines before [Events] Dialogue entries
            'format_line': the Format: line from [Events] section
            'dialogues': list of dicts with 'start', 'end', 'raw_fields' (everything after End timestamp)
            'tail_lines': any lines after the last Dialogue entry
        or None on failure
    """
    try:
        with open(ass_path, 'r', encoding='utf-8-sig') as f:
            content = f.read()
    except UnicodeDecodeError:
        try:
            with open(ass_path, 'r', encoding='latin-1') as f:
                content = f.read()
        except Exception as e:
            logger.error(f"Failed to read ASS file {ass_path}: {e}")
            return None
    
    lines = content.split('\n')
    
    header_lines = []
    format_line = None
    dialogues = []
    tail_lines = []
    in_events = False
    past_dialogues = False
    
    # ASS Dialogue line regex: Dialogue: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
    # We only need to parse Start and End; everything else is preserved verbatim
    dialogue_re = re.compile(
        r'^(Dialogue:\s*)(\d+),\s*'              # "Dialogue: " + Layer + ","
        r'(\d+:\d{2}:\d{2}\.\d{2}),\s*'          # Start timestamp
        r'(\d+:\d{2}:\d{2}\.\d{2}),\s*'          # End timestamp
        r'(.*)$',                                  # Everything else (Style,Name,...,Text)
        re.DOTALL
    )
    
    for line in lines:
        stripped = line.strip()
        
        # Detect [Events] section
        if stripped.lower() == '[events]':
            in_events = True
            header_lines.append(line)
            continue
        
        # Detect Format line within Events
        if in_events and stripped.lower().startswith('format:') and format_line is None:
            format_line = line
            continue
        
        # Parse Dialogue lines
        if in_events and stripped.startswith('Dialogue:'):
            m = dialogue_re.match(stripped)
            if m:
                dialogues.append({
                    'start': ass_time_to_seconds(m.group(3)),
                    'end': ass_time_to_seconds(m.group(4)),
                    'prefix': m.group(1),        # "Dialogue: "
                    'layer': m.group(2),          # Layer number
                    'rest': m.group(5),           # Style,Name,...,Text — all preserved
                })
            else:
                # Malformed dialogue line, keep as-is in tail
                tail_lines.append(line)
            continue
        
        # Detect new section after Events (e.g. [Fonts], [Graphics])
        if in_events and stripped.startswith('[') and len(dialogues) > 0:
            in_events = False
            past_dialogues = True
            tail_lines.append(line)
            continue
        
        # Sort into header vs tail
        if past_dialogues or (in_events and len(dialogues) > 0 and not stripped.startswith('Dialogue:')):
            tail_lines.append(line)
        else:
            header_lines.append(line)
    
    if not dialogues:
        logger.warning(f"No Dialogue lines found in ASS file: {ass_path}")
        return None
    
    return {
        'header_lines': header_lines,
        'format_line': format_line,
        'dialogues': dialogues,
        'tail_lines': tail_lines,
    }


def write_ass_file(ass_path, ass_data, adjusted_dialogues):
    """Write ASS/SSA file with adjusted dialogue timings, preserving all header/style data.
    
    Args:
        ass_path: output file path
        ass_data: the dict returned by parse_ass_file (header, format, tail)
        adjusted_dialogues: list of dicts with 'start', 'end', 'prefix', 'layer', 'rest'
    """
    try:
        with open(ass_path, 'w', encoding='utf-8') as f:
            # Write header (includes [Script Info], [V4+ Styles], [Events] section header)
            for line in ass_data['header_lines']:
                f.write(line + '\n')
            
            # Write Format line
            if ass_data['format_line']:
                f.write(ass_data['format_line'] + '\n')
            
            # Write adjusted Dialogue lines
            for dlg in adjusted_dialogues:
                start_ts = seconds_to_ass_time(dlg['start'])
                end_ts = seconds_to_ass_time(dlg['end'])
                f.write(f"{dlg['prefix']}{dlg['layer']},{start_ts},{end_ts},{dlg['rest']}\n")
            
            # Write tail (any sections after Events like [Fonts])
            for line in ass_data['tail_lines']:
                f.write(line + '\n')
        
        return True
    except Exception as e:
        logger.error(f"Failed to write ASS file {ass_path}: {e}")
        return False


# ---- Shared timing adjustment (works for both SRT and ASS dialogue entries) ----

def adjust_subtitle_timing(subtitles, segment_anchors, ref_delay,
                           editorial_edits=None, source_tempo=1.0):
    """Adjust subtitle timing based on audio segments.
    
    Each subtitle entry must have 'start' and 'end' keys (in seconds).
    All other keys are preserved (e.g. 'text' for SRT, 'prefix'/'layer'/'rest' for ASS).
    
    Returns:
        tuple: (adjusted_subtitles list, dropped_subtitles list)
        Each dropped entry is a dict with 'index', 'start', 'end', and 'reason'.
    """
    adjusted_subtitles = []
    dropped_subtitles = []
    num_segments = len(segment_anchors) - 1
    aa = _import_audio_alignment() if editorial_edits else None
    
    logger.debug(f"\nSubtitle timing adjustment:")
    logger.debug(f"  Total subtitles: {len(subtitles)}")
    logger.debug(f"  Segments: {num_segments}")
    logger.debug(f"  Reference delay: {ref_delay:.3f}s (NOT added to subtitles - they sync to video)")
    logger.debug(f"  Segment ranges:")
    
    # Compute segment boundaries for logging
    foreign_min = segment_anchors[0][1] if num_segments > 0 else 0
    foreign_max = segment_anchors[-1][1] if num_segments > 0 else 0
    
    for i in range(num_segments):
        ref_s, foreign_s = segment_anchors[i]
        ref_e, foreign_e = segment_anchors[i + 1]
        logger.debug(f"    Seg {i+1}: foreign=[{foreign_s:.2f}-{foreign_e:.2f}s], ref=[{ref_s:.2f}-{ref_e:.2f}s]")
    
    for idx, sub in enumerate(subtitles):
        if editorial_edits:
            new_start = aa.map_source_time_to_reference(
                sub['start'], editorial_edits, source_tempo)
            new_end = aa.map_source_time_to_reference(
                sub['end'], editorial_edits, source_tempo)
            if new_end <= new_start:
                dropped_subtitles.append({
                    'index': idx + 1,
                    'start': sub['start'],
                    'end': sub['end'],
                    'text_preview': sub.get('text', sub.get('rest', ''))[:60],
                    'reason': 'falls entirely inside a deleted source interval',
                })
                continue
            adjusted = dict(sub)
            adjusted['start'] = max(0, new_start)
            adjusted['end'] = max(0, new_end)
            adjusted_subtitles.append(adjusted)
            continue

        segment_idx = None
        for i in range(num_segments):
            foreign_start = segment_anchors[i][1]
            foreign_end = segment_anchors[i + 1][1]
            
            if foreign_start <= sub['start'] < foreign_end:
                segment_idx = i
                break
        
        if segment_idx is None:
            # Determine reason for drop
            if sub['start'] < foreign_min:
                reason = f"before first segment boundary ({foreign_min:.2f}s)"
            elif sub['start'] >= foreign_max:
                reason = f"after last segment boundary ({foreign_max:.2f}s)"
            else:
                reason = "falls in gap between segments"
            
            dropped_subtitles.append({
                'index': idx + 1,
                'start': sub['start'],
                'end': sub['end'],
                'text_preview': sub.get('text', sub.get('rest', ''))[:60],
                'reason': reason,
            })
            continue
        
        ref_seg_start = segment_anchors[segment_idx][0]
        ref_seg_end = segment_anchors[segment_idx + 1][0]
        foreign_seg_start = segment_anchors[segment_idx][1]
        foreign_seg_end = segment_anchors[segment_idx + 1][1]
        
        ref_seg_duration = ref_seg_end - ref_seg_start
        foreign_seg_duration = foreign_seg_end - foreign_seg_start
        
        stretch_factor = ref_seg_duration / foreign_seg_duration if foreign_seg_duration > 0 else 1.0
        
        relative_start = sub['start'] - foreign_seg_start
        relative_end = sub['end'] - foreign_seg_start
        
        new_start = ref_seg_start + (relative_start * stretch_factor)
        new_end = ref_seg_start + (relative_end * stretch_factor)
        
        # NOTE: Do NOT add ref_delay here!
        # Subtitles sync to video timeline, which doesn't have the audio padding
        # Only the foreign audio gets ref_delay added to match reference audio
        
        if idx < 3:  # Log first few for debugging
            logger.debug(f"  Sub {idx+1}: {sub['start']:.2f}s -> {new_start:.2f}s (seg {segment_idx+1}, stretch {stretch_factor:.4f}x)")
        
        # Build adjusted entry preserving all original keys
        adjusted = dict(sub)  # shallow copy all fields
        adjusted['start'] = max(0, new_start)
        adjusted['end'] = max(0, new_end)
        adjusted_subtitles.append(adjusted)
    
    return adjusted_subtitles, dropped_subtitles


# ---- Stream detection and extraction ----

def get_subtitle_streams(video_path):
    """Get list of subtitle streams from video file, including codec info."""
    cmd = [
        'ffprobe', '-v', 'error',
        '-select_streams', 's',
        '-show_entries', 'stream=index,codec_name:stream_tags=language',
        '-of', 'json',
        video_path
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        streams = data.get('streams', [])
        
        subtitle_streams = []
        for stream in streams:
            stream_idx = stream.get('index')
            codec = stream.get('codec_name', 'unknown').lower()
            tags = stream.get('tags', {})
            lang = tags.get('language', 'unknown')
            subtitle_streams.append({
                'index': stream_idx,
                'language': lang,
                'codec': codec,  # e.g. 'ass', 'ssa', 'subrip', 'srt', 'mov_text'
            })
        
        return subtitle_streams
    except Exception as e:
        logger.debug(f"Failed to get subtitle streams: {e}")
        return []


def extract_subtitle_stream(video_path, stream_idx, output_path, native_codec=False):
    """Extract subtitle stream, optionally preserving native format.
    
    Args:
        native_codec: if True, use '-c:s copy' to preserve ASS/SSA format.
                      if False, convert to SRT via '-c:s srt'.
    """
    codec_args = ['-c:s', 'copy'] if native_codec else ['-c:s', 'srt']
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'warning',
        '-i', video_path,
        '-map', f'0:{stream_idx}',
        *codec_args,
        '-y', output_path
    ]
    
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.debug(f"Subtitle extraction failed (stream {stream_idx}): {e.stderr[:200] if e.stderr else 'no stderr'}")
        return False


# ---- Main subtitle sync orchestrator ----

def sync_subtitles(foreign_video, segment_anchors, ref_delay, temp_dir, foreign_lang,
                   editorial_edits=None, source_tempo=1.0):
    """Synchronize subtitle streams from foreign video.
    
    Preserves ASS/SSA format natively (fonts, styles, positioning).
    Falls back to SRT conversion for unsupported codecs.
    Logs all dropped subtitles with reasons.
    """
    logger.info("\n--- Subtitle Synchronization Stage ---")
    
    subtitle_streams = get_subtitle_streams(foreign_video)
    
    if not subtitle_streams:
        logger.info("> No subtitle streams found")
        return []
    
    logger.info(f"Found {len(subtitle_streams)} subtitle stream(s)")
    
    synced_subtitle_files = []
    total_dropped = 0
    
    # Bitmap (image-based) subtitle codecs cannot be text-parsed or timing-adjusted.
    # They are passed through untouched so they remain present in the output.
    BITMAP_CODECS = {
        'hdmv_pgs_subtitle': '.sup',   # Blu-ray PGS
        'pgssub': '.sup',
        'dvd_subtitle': '.sub',        # DVD VobSub
        'dvdsub': '.sub',
        'xsub': '.sub',
    }
    
    for stream in subtitle_streams:
        stream_idx = stream['index']
        stream_lang = stream['language']
        stream_codec = stream.get('codec', 'unknown')
        
        # --- Bitmap subtitle pass-through (PGS, VobSub) ---
        if stream_codec in BITMAP_CODECS:
            bitmap_ext = BITMAP_CODECS[stream_codec]
            logger.info(f"Processing subtitle stream {stream_idx} (lang: {stream_lang}, codec: {stream_codec}, format: BITMAP)")
            logger.warning(f"  Stream {stream_idx} is a bitmap format ({stream_codec}) and CANNOT be timing-adjusted.")
            logger.warning(f"  -> Passing it through UNCHANGED. Timing will match the foreign video, not the adjusted audio.")
            
            passthrough_path = os.path.join(temp_dir, f"subtitle_passthrough_{stream_idx}{bitmap_ext}")
            if extract_subtitle_stream(foreign_video, stream_idx, passthrough_path, native_codec=True):
                synced_subtitle_files.append({
                    'path': passthrough_path,
                    'language': stream_lang,
                    'original_index': stream_idx,
                    'format': f'BITMAP ({stream_codec}, pass-through)',
                    'passthrough': True,
                })
                logger.info(f"  [OK] Bitmap subtitle passed through unchanged")
            else:
                logger.warning(f"  Failed to extract bitmap subtitle stream {stream_idx}")
            continue
        
        # Determine if this is an ASS/SSA stream
        is_ass = stream_codec in ('ass', 'ssa')
        format_label = 'ASS/SSA' if is_ass else 'SRT'
        ext = '.ass' if is_ass else '.srt'
        
        logger.info(f"Processing subtitle stream {stream_idx} (lang: {stream_lang}, codec: {stream_codec}, format: {format_label})")
        
        # Extract in native format for ASS, convert to SRT for others
        original_path = os.path.join(temp_dir, f"subtitle_original_{stream_idx}{ext}")
        if not extract_subtitle_stream(foreign_video, stream_idx, original_path, native_codec=is_ass):
            # If native extraction failed for ASS, try SRT fallback
            if is_ass:
                logger.warning(f"  Native ASS extraction failed, trying SRT conversion fallback...")
                original_path = os.path.join(temp_dir, f"subtitle_original_{stream_idx}.srt")
                if not extract_subtitle_stream(foreign_video, stream_idx, original_path, native_codec=False):
                    logger.warning(f"  Failed to extract subtitle stream {stream_idx}")
                    continue
                is_ass = False
                ext = '.srt'
                format_label = 'SRT (converted from ASS)'
            else:
                logger.warning(f"  Failed to extract subtitle stream {stream_idx}")
                continue
        
        # Parse based on format
        if is_ass:
            ass_data = parse_ass_file(original_path)
            if not ass_data:
                logger.warning(f"  No dialogues parsed from ASS stream {stream_idx}")
                continue
            
            subtitle_entries = ass_data['dialogues']
            logger.info(f"  Parsed {len(subtitle_entries)} ASS dialogue entries (styles/fonts preserved)")
        else:
            subtitle_entries = parse_srt_file(original_path)
            if not subtitle_entries:
                logger.warning(f"  No subtitles parsed from stream {stream_idx}")
                continue
            ass_data = None
        
        logger.info(f"  Parsed {len(subtitle_entries)} subtitle entries")
        
        # Adjust timing (same logic for both formats)
        adjusted_entries, dropped_entries = adjust_subtitle_timing(
            subtitle_entries,
            segment_anchors,
            ref_delay,
            editorial_edits=editorial_edits,
            source_tempo=source_tempo,
        )
        logger.info(f"  Adjusted {len(adjusted_entries)} subtitle entries")
        
        # Log dropped subtitles
        if dropped_entries:
            total_dropped += len(dropped_entries)
            logger.warning(f"  DROPPED {len(dropped_entries)} subtitle(s) outside segment boundaries:")
            # Log all dropped entries (not just first/last)
            for drop in dropped_entries:
                preview = drop['text_preview'].replace('\n', ' ').strip()
                if preview:
                    logger.warning(f"    Sub #{drop['index']} [{drop['start']:.2f}s - {drop['end']:.2f}s] "
                                   f"Reason: {drop['reason']} | \"{preview}...\"")
                else:
                    logger.warning(f"    Sub #{drop['index']} [{drop['start']:.2f}s - {drop['end']:.2f}s] "
                                   f"Reason: {drop['reason']}")
        
        # Write synced file in original format
        synced_path = os.path.join(temp_dir, f"subtitle_synced_{stream_idx}{ext}")
        
        if is_ass:
            write_ok = write_ass_file(synced_path, ass_data, adjusted_entries)
        else:
            write_ok = write_srt_file(synced_path, adjusted_entries)
        
        if write_ok:
            synced_subtitle_files.append({
                'path': synced_path,
                'language': stream_lang,
                'original_index': stream_idx,
                'format': format_label,
            })
            logger.info(f"  [OK] Synced subtitle saved ({format_label})")
    
    if synced_subtitle_files:
        logger.info(f"[OK] Synced {len(synced_subtitle_files)} subtitle stream(s)")
    if total_dropped > 0:
        logger.warning(f"[WARN] Total dropped subtitles across all streams: {total_dropped}")
    
    return synced_subtitle_files


def main():
    global FFMPEG_EXEC, FFPROBE_EXEC, MKVMERGE_EXEC, AUDIO_EDITORIAL_SOURCE_TEMPO
    parser = argparse.ArgumentParser(
        description="AVSync: Aligns foreign audio and subtitles to a reference timeline using audio-to-audio anchors and precise timing.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=f"""
Example Usage:
  # Basic usage (muxed video output is mandatory)
  python gs.py ref_video.mkv foreign_video.mkv output_video.mkv --ref_lang eng --foreign_lang spa

  # Specify audio streams by index instead of language (Use absolute stream indices shown)
    python gs.py ref_video.mkv foreign_video.mkv output_video.mkv --ref_stream_idx 1 --foreign_stream_idx 2

  # Set minimum segment duration for audio filtering to 10 seconds (default is 5)
  python gs.py ref_video.mkv foreign_video.mkv output_video.mkv --min_segment_duration 10

  # Keep the synchronized WAV file separately
  python gs.py ref_video.mkv foreign_video.mkv output_video.mkv --output_audio synced_audio.wav

  # Generate QC images and segment CSV along with the video
  python gs.py ref_video.mkv foreign_video.mkv output_video.mkv --qc_output_dir ./qc_images --output_csv segments.csv

Workflow:
1. Generates timeline anchors from matching original audio, or optionally from scene-change frames.
2. For audio anchors, correlates waveform and energy envelopes and locates abrupt editorial transitions.
3. Extracts audio tracks based on language tags or specified absolute indices.
4. Determines audio content boundaries and filters anchor points based on minimum segment duration and duration difference.
5. Processes audio segments iteratively to match reference timing precisely.
6. Concatenates processed segments and applies start delay.
7. (Optional) Retimes text subtitles and generates QC images or a segment CSV.
8. Muxes the reference video, original audio, and synchronized foreign tracks into the final output video.
"""
    )
    # --- Input/Output Arguments ---
    parser.add_argument("ref_video", help="Path to the Reference video file (e.g., original language version).")
    parser.add_argument("foreign_video", help="Path to the Foreign video file (e.g., translated language version to be synced).")
    parser.add_argument("output_video", help="Path for the final muxed video file including reference video, reference audio, and synced foreign audio.")
    parser.add_argument("--output_audio", metavar="WAV_PATH", default=None, help="Optional: Path to save the synchronized audio as WAV file. If not specified, a temporary file will be used and deleted after muxing.")
    parser.add_argument("--output_csv", metavar="CSV_PATH", default=None, help="Optional: Path to save segment timing information in a CSV file. By default, no CSV is generated.")
    parser.add_argument("--splice_safety_report_csv", metavar="CSV_PATH", default=None,
        help="Optional: Write normalized per-track energy at every planned splice edge. Analysis only; does not change cuts or audio output.")
    parser.add_argument("--no_per_track_splice_placement", dest="per_track_splice_placement",
        action="store_false", default=True,
        help="Disable automatic primary-track splice placement inside verified reference silences.")
    parser.add_argument("--auto_silence_threshold", action="store_true",
        help="Experimental: derive the silence/boundary threshold per track from its own measured "
             "noise floor instead of a fixed -40/-35 dBFS value. Disabled by default.")
    parser.add_argument("--threshold_calibration_csv", metavar="CSV_PATH", default=None,
        help="Optional: write the noise floor and derived threshold computed for each analyzed "
             "track when --auto_silence_threshold is enabled.")
    parser.add_argument("--visual_program_bounds", action="store_true",
        help="Experimental: use matching first non-black video frames to preserve a foreign audio "
             "preamble at FPS tempo and anchor normal synchronization after the visual program start.")
    parser.add_argument("--no_subtitles", action='store_true', help="Skip subtitle synchronization (enabled by default)")

    parser.add_argument("--qc_output_dir", metavar="QC_DIR", default=None, help="Optional: Directory to save side-by-side QC images. By default, no QC images are generated.")

    # --- Image Pairing Arguments ---
    img_group = parser.add_argument_group('Image Pairing Parameters')
    img_group.add_argument("--scene_threshold", type=float, default=0.25, help="FFmpeg scene change detection threshold (0.0-1.0). Lower values detect more changes. (Default: 0.25)")
    img_group.add_argument("--match_threshold", type=float, default=0.7, help="OpenCV template matching score threshold (0.0-1.0) for considering frames a match. (Default: 0.7)")
    img_group.add_argument("--similarity_threshold", type=int, default=4, help="Perceptual hash (pHash) difference threshold for filtering similar reference frames. Lower values mean stricter filtering. Use -1 to disable. (Default: 4)")
    img_group.add_argument("--force_sync_points", type=str, default=None, metavar="SYNC_SPEC",
        help="Force sync points at specific timestamps where auto-detection fails. "
             "Format: 'HH:MM:SS:MS>HH:MM:SS:MS,HH:MM:SS:MS>HH:MM:SS:MS,...' "
             "Example: '00:00:10:500>00:00:10:800,00:02:00:300>00:02:01:000' creates sync points "
             "at ref 10.5s->foreign 10.8s and ref 120.3s->foreign 121.0s. "
             "These bypass scene detection and are guaranteed sync points.")
    # Note: Match search window is now calculated automatically, not a direct argument

    # --- Anchor Source Selection ---
    anchor_group = parser.add_argument_group('Anchor Source Parameters')
    anchor_group.add_argument("--anchor_source", choices=["visual", "audio"], default="visual",
        help="How to generate sync anchor points. 'visual' (default) uses scene-change frame "
             "matching. 'audio' uses audio cross-correlation instead - use this when the video "
             "pair has such a large resolution/compression/aspect-ratio mismatch that template "
             "matching gives few or inconsistent anchors. Requires the ref/foreign audio streams "
             "to contain matching content (e.g. same-language dialogue).")
    anchor_group.add_argument("--audio_anchor_window", type=float, default=60.0,
        help="Audio anchor analysis window size in seconds (Default: 60.0)")
    anchor_group.add_argument("--audio_anchor_step", type=float, default=30.0,
        help="Step between consecutive audio anchor windows in seconds (Default: 30.0)")
    anchor_group.add_argument("--audio_anchor_min_confidence", type=float, default=2.0,
        help="Minimum peak/secondary-peak confidence ratio required to accept an audio anchor (Default: 2.0)")
    anchor_group.add_argument("--audio_anchor_search_radius", type=float, default=20.0,
        help="Seconds around the expected position to search in the foreign audio for each window (Default: 20.0)")
    anchor_group.add_argument("--source_tempo", type=float, default=None,
        help="Manual atempo factor applied to foreign audio before correlation (e.g. 0.959 for 25fps->23.976fps). "
             "If omitted, it is auto-detected from each video's frame rate.")
    anchor_group.add_argument("--audio_jump_tolerance", type=float, default=0.15,
        help="Offset change (seconds) between consecutive audio anchors above which it's treated as a "
             "discrete editorial cut rather than gradual drift, triggering precise transition-point "
             "detection instead of stretching the whole window (Default: 0.15)")
    anchor_group.add_argument("--anchor_report_csv", default=None, metavar="PATH",
        help="Write a CSV dump of every audio-anchor correlation window scanned (accepted or not, "
             "with confidence/offset) plus the final anchor list actually used, for manual inspection "
             "of anchor density and confidence in a specific episode. Only applies to --anchor_source audio.")
    anchor_group.add_argument("--transition_report_csv", default=None, metavar="PATH",
        help="Write one-second local waveform/envelope/energy measurements around every material "
             "audio-offset transition, before any automatic cut or fill is applied. Only applies to "
             "--anchor_source audio.")

    # --- Audio Processing Arguments ---
    audio_group = parser.add_argument_group('Audio Processing Parameters')
    audio_group.add_argument("--ref_lang", default=DEFAULT_REF_LANG, help=f"Reference audio language code (3-letter ISO 639-2/T) for stream selection. (Default: {DEFAULT_REF_LANG})")
    audio_group.add_argument("--foreign_lang", default=DEFAULT_FOREIGN_LANG, help=f"Foreign audio language code (3-letter ISO 639-2/T, e.g., hin, jpn, spa). REQUIRED for proper metadata tagging. If not provided or invalid, you will be prompted. (Default: {DEFAULT_FOREIGN_LANG})")
    audio_group.add_argument("--db_threshold", type=float, default=DEFAULT_DB_THRESHOLD, help=f"Audio detection threshold (dBFS) to find start/end of content. (Default: {DEFAULT_DB_THRESHOLD:.1f})")
    audio_group.add_argument("--min_segment_duration", type=float, default=DEFAULT_MIN_SEGMENT_DURATION, help=f"Minimum duration (seconds) for a reference audio segment to be kept during anchor filtering. (Default: {DEFAULT_MIN_SEGMENT_DURATION:.1f})") # New Argument
    audio_group.add_argument("--first_segment_adjust", type=float, default=0.0, help="Adjustment in milliseconds for the FIRST segment. Positive = add padding at start, Negative = trim from start. Applied BEFORE atempo processing (Default: 0.0ms).")
    audio_group.add_argument("--last_segment_adjust", type=float, default=0.0, help="Adjustment in milliseconds for the LAST segment. Positive = add padding at end, Negative = trim from end. Applied BEFORE atempo processing (Default: 0.0ms).")
    audio_group.add_argument("--ref_stream_idx", type=int, default=None, help="Force specific *absolute* audio stream index for reference video (e.g., 1, 2, ...). Overrides --ref_lang.")
    audio_group.add_argument("--foreign_stream_idx", type=int, default=None, help="Force specific *absolute* audio stream index for foreign video. Overrides --foreign_lang.")
    audio_group.add_argument("--foreign_anchor_stream_idx", type=int, default=None,
        help="Absolute audio stream index in the foreign video containing the original audio used for audio-to-audio comparison. "
             "If omitted, --foreign_stream_idx is used for backward compatibility.")
    audio_group.add_argument("--foreign_tracks", type=str, default=None, metavar="TRACKS",
        help="Which foreign audio tracks to sync and include in output. "
             "Options: 'primary' (just the main track, default behavior), "
             "'all' (sync all audio tracks from foreign video), "
             "or comma-separated absolute stream indices (e.g., '1,2,3'). "
             "In interactive mode without this flag, you will be prompted to choose.")
    audio_group.add_argument("--auto_detect", action="store_true", help="Skip audio stream selection prompts and use auto-detection.")

    # --- Muxing Arguments ---
    mux_group = parser.add_argument_group('Muxing Parameters')
    mux_group.add_argument("--mux_foreign_codec", default=DEFAULT_MUX_ACODEC,
        help="Audio codec for the synced foreign track in the muxed output (e.g., 'aac', 'ac3', 'flac', 'copy'). "
             "'auto' matches the foreign source's own codec so quality/format is neither gained nor lost "
             f"(falls back to lossless FLAC if the source codec has no suitable encoder). (Default: {DEFAULT_MUX_ACODEC})")
    mux_group.add_argument("--mux_foreign_bitrate", default=DEFAULT_MUX_ABITRATE,
        help="Audio bitrate for the synced foreign track if re-encoding (e.g., '192k', '320k'). "
             f"'auto' matches the foreign source's own bitrate (ignored for lossless codecs). (Default: {DEFAULT_MUX_ABITRATE})")
    mux_group.add_argument("--audio_filters", default=None,
        help="Optional ffmpeg -af filter chain applied to the synced foreign track before final encoding "
             "(e.g., 'loudnorm=I=-16:TP=-1.5:LRA=11' or 'highpass=f=80,adeclick'). Opt-in only: by default "
             "no enhancement filters are applied, to avoid altering the source's original character.")


    # --- Caching Arguments ---
    cache_group = parser.add_argument_group('Caching Parameters')
    cache_group.add_argument("--use-cache", action="store_true", default=True, help="Use checkpoint cache for faster iterations (default: enabled)")
    cache_group.add_argument("--no-cache", dest="use_cache", action="store_false", help="Disable checkpoint caching")

    # --- Logging Arguments ---
    log_group = parser.add_argument_group('Logging Parameters')
    log_group.add_argument("--verbose", "-v", action="store_true", help="Show detailed output on console (default: minimal with progress bar)")
    log_group.add_argument("--show-warnings", action="store_true", help="Show warning messages on console (default: suppressed for clean progress)")
    log_group.add_argument("--log-file", type=str, default=None, metavar="PATH", help="Custom log file path. Default: <output_name>_avsync.log")
    log_group.add_argument("--no-log", action="store_true", help="Disable file logging (console only)")

    args = parser.parse_args()

    # --- Initialize Logging ---
    log_file_path = None
    # --- Set warning display preference before logging setup ---
    ConsoleFilter.show_warnings = args.show_warnings
    
    if not args.no_log:
        log_file_path = args.log_file if args.log_file else get_log_path(args.output_video)
    
    global logger
    logger, log_file_path = setup_logging(log_file=log_file_path, verbose=args.verbose)

    # --- Handle Temporary WAV File ---
    args.output_audio_original = args.output_audio # Store if user specified a path
    if args.output_audio is None:
        # Create a temporary file for the synchronized audio
        temp_audio_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        args.output_audio = temp_audio_file.name
        temp_audio_file.close()  # Close but don't delete yet
        logger.info(f"Using temporary WAV file for processing: {args.output_audio}")
        # This file will be deleted by run_muxing

    # Assign output_video to mux_output for compatibility with any remaining internal refs if needed
    args.mux_output = args.output_video

    overall_start_time = time.time()
    
    # Professional startup banner
    logger.info("")
    logger.info("=" * 70)
    logger.info(" AVSync v14 - Audio/Video Synchronization Engine")
    logger.info("=" * 70)
    logger.info(f"Reference video : {args.ref_video}")
    logger.info(f"Foreign video   : {args.foreign_video}")
    logger.info(f"Output video    : {args.output_video}")
    if log_file_path:
        logger.info(f"Log file        : {log_file_path}")
    if args.output_audio_original:
        logger.info(f"Output audio    : {args.output_audio_original}")
    if args.output_csv:
        logger.info(f"Segment CSV     : {args.output_csv}")
    if args.qc_output_dir:
        logger.info(f"QC images       : {args.qc_output_dir}")
    logger.info(f"Caching         : {'Enabled' if args.use_cache else 'Disabled'}")
    logger.info("-"*70)


    # --- Find Executables ---
    FFMPEG_EXEC = find_executable("ffmpeg")
    FFPROBE_EXEC = find_executable("ffprobe")
    MKVMERGE_EXEC = find_executable("mkvmerge")
    if not FFMPEG_EXEC or not FFPROBE_EXEC:
        logger.error("FATAL: Required 'ffmpeg' and/or 'ffprobe' executable not found in system PATH.")
        sys.exit(1)
    logger.info(f"Using ffmpeg: {FFMPEG_EXEC}")
    logger.info(f"Using ffprobe: {FFPROBE_EXEC}")
    if MKVMERGE_EXEC:
        logger.info(f"Using mkvmerge: {MKVMERGE_EXEC}")
    else:
        logger.warning("mkvmerge not found. MKV muxing will fall back to ffmpeg (attachments/chapters may not be preserved).")
        logger.warning("  Install mkvtoolnix for best results: https://mkvtoolnix.download/")

    # --- Validate Input/Output Paths ---
    if not os.path.isfile(args.ref_video): logger.error(f"Reference video not found: {args.ref_video}"); sys.exit(1)
    if not os.path.isfile(args.foreign_video): logger.error(f"Foreign video not found: {args.foreign_video}"); sys.exit(1)

    # --- Validate Foreign Language Code (for primary track / --foreign_lang default) ---
    # This validates the --foreign_lang argument. Per-track language validation happens later.
    primary_lang_from_arg = args.foreign_lang
    primary_lang_valid = validate_language_code(args.foreign_lang) and args.foreign_lang.lower() not in ('foreign', 'und', 'unk')

    # --- Handle Audio Stream Selection ---
    # For reference video streams
    if args.ref_stream_idx is None and not args.auto_detect:
        # Prompt user, returns the absolute stream index if selected
        args.ref_stream_idx = prompt_user_for_audio_stream(args.ref_video, "reference")
        if args.ref_stream_idx is not None:
            logger.info(f"User selected Reference Stream Index: {args.ref_stream_idx}")

    # For foreign video streams (primary track for sync timing)
    if args.foreign_stream_idx is None and not args.auto_detect:
        # Prompt user, returns the absolute stream index if selected
        args.foreign_stream_idx = prompt_user_for_audio_stream(args.foreign_video, "foreign (primary for sync)")
        if args.foreign_stream_idx is not None:
            logger.info(f"User selected Foreign Stream Index: {args.foreign_stream_idx}")

    # --- Multi-Track Foreign Audio Selection ---
    # Determine which foreign audio tracks to sync and include in the output
    if args.foreign_tracks is not None:
        # Explicit track selection via CLI
        selected_foreign_tracks = parse_foreign_tracks_arg(
            args.foreign_tracks, args.foreign_video, args.foreign_stream_idx)
        if not selected_foreign_tracks:
            logger.error("No valid foreign audio tracks selected via --foreign_tracks.")
            sys.exit(1)
        logger.info(f"Foreign tracks selected via --foreign_tracks: {[t['stream_idx'] for t in selected_foreign_tracks]}")
    elif not args.auto_detect:
        # Interactive mode: prompt user to select tracks
        selected_foreign_tracks = prompt_user_for_foreign_tracks(
            args.foreign_video, args.foreign_stream_idx)
        if not selected_foreign_tracks:
            # Fallback to primary only
            selected_foreign_tracks = [{'stream_idx': args.foreign_stream_idx, 'language': None}]
    else:
        # Auto-detect mode: just the primary track
        selected_foreign_tracks = [{'stream_idx': args.foreign_stream_idx, 'language': None}]

    # Ensure the primary track is first and set its stream_idx if not yet determined
    if selected_foreign_tracks and args.foreign_stream_idx is not None:
        # Make sure primary is first
        selected_foreign_tracks.sort(key=lambda x: 0 if x['stream_idx'] == args.foreign_stream_idx else 1)
    
    # If foreign_stream_idx not yet set, use first selected track
    if args.foreign_stream_idx is None and selected_foreign_tracks:
        args.foreign_stream_idx = selected_foreign_tracks[0]['stream_idx']

    # --- Per-Track Language Validation ---
    # Apply --foreign_lang to tracks that don't have metadata language
    for track in selected_foreign_tracks:
        if track.get('language') is None or track['language'].lower() in ('und', 'unk', ''):
            if primary_lang_valid:
                track['language'] = args.foreign_lang
            # else: will be prompted below

    # Validate all track languages
    if not resolve_track_languages(selected_foreign_tracks, auto_detect=args.auto_detect):
        logger.error("Cannot proceed without valid language codes for all foreign audio tracks.")
        sys.exit(1)

    # Update args.foreign_lang from primary track (for backward compatibility)
    args.foreign_lang = selected_foreign_tracks[0]['language']
    
    # Store selected tracks in args for later use
    args.selected_foreign_tracks = selected_foreign_tracks

    # Log the final stream selection choices
    if args.ref_stream_idx is not None:
        logger.info(f"Ref Stream Index (Absolute): {args.ref_stream_idx} (User selected or forced)")
    else:
        logger.info(f"Ref Lang: {args.ref_lang} (Will auto-detect)")

    if len(selected_foreign_tracks) == 1:
        logger.info(f"Foreign Track: Stream #{selected_foreign_tracks[0]['stream_idx']} ({selected_foreign_tracks[0]['language']})")
    else:
        logger.info(f"Foreign Tracks ({len(selected_foreign_tracks)}):")
        for i, t in enumerate(selected_foreign_tracks):
            role = "Primary" if i == 0 else "Additional"
            logger.info(f"  [{role}] Stream #{t['stream_idx']} ({t['language']})")

    logger.info(f"Audio Threshold: {args.db_threshold} dB")
    logger.info(f"Min Segment Duration: {args.min_segment_duration}s") # Log new parameter
    if args.first_segment_adjust != 0.0 or args.last_segment_adjust != 0.0:
        logger.info(f"First Segment Adjust: {args.first_segment_adjust:+.1f}ms, Last Segment Adjust: {args.last_segment_adjust:+.1f}ms")
    logger.info(f"Scene Threshold: {args.scene_threshold}, Match Threshold: {args.match_threshold}, Similarity Threshold: {args.similarity_threshold}")
    if args.force_sync_points:
        logger.info(f"Forced Sync Points: {args.force_sync_points}")
    logger.info(f"Frame Match: Anchor-and-follow (initial: +/- {MATCH_WINDOW_PERCENT*100}% of ref duration, subsequent: +{ANCHOR_FOLLOW_FORWARD_WINDOW_S}s forward)")

    # Validate output directories are writable for all specified outputs
    output_paths_to_check = [args.output_video, args.output_audio, args.output_csv]
    for path in output_paths_to_check:
        if path: # Only check paths that are actually set
            try:
                out_dir = os.path.dirname(os.path.abspath(path))
                if not out_dir: # Handle case where path is just a filename in cwd
                    out_dir = '.'
                os.makedirs(out_dir, exist_ok=True) # Create dir if it doesn't exist
                if not os.access(out_dir, os.W_OK):
                    raise OSError(f"Output directory is not writable: {out_dir}")
                # Warn about overwriting files (except for the temp audio)
                if path != args.output_audio or args.output_audio_original: # Don't warn for default temp audio path
                    if os.path.exists(path) and os.path.isfile(path):
                        logger.warning(f"Output file '{os.path.basename(path)}' exists and will be overwritten.")
            except Exception as e:
                logger.error(f"Output path validation failed for '{path}': {e}")
                # Clean up temporary audio file if created and validation fails early
                if args.output_audio_original is None and args.output_audio and os.path.exists(args.output_audio):
                    try: os.remove(args.output_audio)
                    except Exception: pass
                sys.exit(1)
    # Validate QC dir separately if specified
    if args.qc_output_dir:
         try:
             qc_dir_abs = os.path.abspath(args.qc_output_dir)
             os.makedirs(qc_dir_abs, exist_ok=True)
             if not os.access(qc_dir_abs, os.W_OK):
                 raise OSError(f"QC output directory is not writable: {qc_dir_abs}")
             if os.path.exists(qc_dir_abs) and not os.path.isdir(qc_dir_abs):
                  raise OSError(f"QC output path exists but is not a directory: {qc_dir_abs}")
         except Exception as e:
             logger.error(f"QC output directory validation failed for '{args.qc_output_dir}': {e}")
             # Clean up temporary audio file if created
             if args.output_audio_original is None and args.output_audio and os.path.exists(args.output_audio):
                 try: os.remove(args.output_audio)
                 except Exception: pass
             sys.exit(1)


    # --- Main Process ---
    final_ref_delay = None
    audio_sync_success = False
    muxing_success = False
    final_ref_stream_idx = None # Store the absolute index used for sync/mux
    final_segment_anchors = None # Store anchors for QC
    temp_dir_obj = None # To hold the TemporaryDirectory object

    try:
        # Use a temporary directory for intermediate files (frames, wav segments)
        temp_dir_obj = tempfile.TemporaryDirectory(prefix="gsync_")
        temp_dir = temp_dir_obj.name # Get the path string
        logger.info(f"\nUsing temporary directory: {temp_dir}")

        # === Stage 1: Anchor Pairing (Visual or Audio) ===
        # Try to load from cache if enabled
        visual_anchors_details = None
        if args.use_cache:
            cache_path = get_cache_path(args)
            checkpoint = load_checkpoint(cache_path)
            if checkpoint:
                visual_anchors_details = checkpoint.get('visual_anchors_details')
                if visual_anchors_details:
                    AUDIO_REPLACEMENT_RANGES[:] = checkpoint.get('audio_replacement_ranges') or []
                    AUDIO_HARD_CUT_RANGES[:] = checkpoint.get('audio_hard_cut_ranges') or []
                    AUDIO_EDITORIAL_EDITS[:] = checkpoint.get('audio_editorial_edits') or []
                    AUDIO_EDITORIAL_SOURCE_TEMPO = checkpoint.get('audio_editorial_source_tempo', 1.0)
                    logger.info("[CACHE] Using cached anchors, skipping frame/audio anchor detection")

        # Run anchor detection if not loaded from cache
        if visual_anchors_details is None:
            if args.anchor_source == "audio":
                anchor_ref_stream_idx, anchor_foreign_stream_idx = resolve_anchor_stream_indices(args)
                if anchor_ref_stream_idx is None or anchor_foreign_stream_idx is None:
                    raise RuntimeError("Audio Anchor Pairing Stage Failed: Could not resolve ref/foreign audio stream indices.")
                source_tempo = args.source_tempo
                if source_tempo is None:
                    source_tempo = compute_auto_source_tempo(args.ref_video, args.foreign_video)
                else:
                    logger.info(f"  Global FPS normalization: using manual factor={source_tempo:.9f}")
                visual_anchors_details = run_audio_pairing_stage(
                    ref_video_path=args.ref_video,
                    foreign_video_path=args.foreign_video,
                    ref_stream_idx=anchor_ref_stream_idx,
                    foreign_stream_idx=anchor_foreign_stream_idx,
                    source_tempo=source_tempo,
                    window_seconds=args.audio_anchor_window,
                    step_seconds=args.audio_anchor_step,
                    min_confidence=args.audio_anchor_min_confidence,
                    search_radius_seconds=args.audio_anchor_search_radius,
                    jump_tolerance_seconds=args.audio_jump_tolerance,
                    anchor_report_csv=args.anchor_report_csv,
                    transition_report_csv=args.transition_report_csv,
                )
                if visual_anchors_details is None:
                    raise RuntimeError("Audio Anchor Pairing Stage Failed: No audio anchors generated.")
            else:
                visual_anchors_details = run_image_pairing_stage(
                    ref_video_path=args.ref_video,
                    foreign_video_path=args.foreign_video,
                    temp_dir=temp_dir,
                    scene_threshold=args.scene_threshold,
                    match_threshold=args.match_threshold,
                    similarity_threshold=args.similarity_threshold
                )
                if visual_anchors_details is None:
                    raise RuntimeError("Image Pairing Stage Failed: No visual anchors generated.")

            # Save checkpoint if caching is enabled
            if args.use_cache:
                cache_path = get_cache_path(args)
                save_checkpoint(cache_path, visual_anchors_details)

        # === Stage 1.5: Inject Forced Sync Points (if specified) ===
        forced_sync_points = parse_force_sync_points(args.force_sync_points)
        if forced_sync_points:
            visual_anchors_details = inject_forced_sync_points(visual_anchors_details, forced_sync_points)

        # === Stage 2: Audio Synchronization (Iterative Method) ===
        # This function now handles finding streams, filtering anchors, processing, concatenating, and padding.
        # It outputs to args.output_audio (which might be temporary)
        # It internally uses args.ref_stream_idx/foreign_stream_idx if set, or detects by lang.
        # It returns the delay and the final anchors used.
        final_ref_delay, final_segment_anchors = run_progressive_sync_iterative(
            args=args, # Pass all args
            visual_anchors_details=visual_anchors_details,
            output_audio_path=args.output_audio,
            temp_dir=temp_dir,
            db_threshold=args.db_threshold,
            min_segment_duration=args.min_segment_duration # Pass new argument
        )

        if final_ref_delay is None or final_segment_anchors is None:
            raise RuntimeError("Audio Synchronization Stage Failed.")

        audio_sync_success = True

        # Determine the *final* reference stream index used for muxing (needed for ffmpeg fallback).
        # mkvmerge-based muxing doesn't need this since it copies ALL streams from reference.
        final_ref_stream_idx = args.ref_stream_idx # Use forced/selected index if available
        if final_ref_stream_idx is None:
            # If it was auto-detected, find it again (necessary for ffmpeg muxing)
            temp_ref_streams = get_stream_info(args.ref_video)
            final_ref_stream_idx = find_audio_stream_index_by_lang(temp_ref_streams, args.ref_lang)

        is_mkv_output = args.output_video.lower().endswith(('.mkv', '.mka', '.mks'))
        if final_ref_stream_idx is None:
            if is_mkv_output and MKVMERGE_EXEC:
                logger.info("Reference stream index not determined, but mkvmerge will preserve all streams automatically.")
            else:
                raise RuntimeError("Could not determine *final* reference audio stream index for muxing.")
        else:
             logger.info(f"Determined final absolute reference stream index for muxing: {final_ref_stream_idx}")


        # === Stage 2.5: Generate QC Images (Optional & Conditional) ===
        if args.qc_output_dir: # Check if requested
            if visual_anchors_details and final_segment_anchors:
                ref_extract_path = os.path.join(temp_dir, "Extracted_Reference")
                foreign_extract_path = os.path.join(temp_dir, "Extracted_Foreign")
                # Check if extract paths exist before calling QC function
                if os.path.isdir(ref_extract_path) and os.path.isdir(foreign_extract_path):
                    create_qc_images(
                        visual_anchors_details=visual_anchors_details,
                        final_segment_anchors=final_segment_anchors,
                        ref_extract_path=ref_extract_path,
                        foreign_extract_path=foreign_extract_path,
                        qc_output_dir=args.qc_output_dir
                    )
                else:
                    logger.warning("Skipping QC image generation: Frame extraction directories not found in temp dir.")
            else:
                 logger.warning("Skipping QC image generation: Missing anchor details or final anchors.")

        # === Stage 2.6: Subtitle Synchronization (Optional) ===
        synced_subtitles = []
        if not args.no_subtitles:
            synced_subtitles = sync_subtitles(
                foreign_video=args.foreign_video,
                segment_anchors=final_segment_anchors,
                ref_delay=final_ref_delay,
                temp_dir=temp_dir,
                foreign_lang=args.foreign_lang,
                editorial_edits=AUDIO_EDITORIAL_EDITS,
                source_tempo=AUDIO_EDITORIAL_SOURCE_TEMPO,
            )

        # === Stage 2.7: Sync Additional Foreign Audio Tracks ===
        # Build the list of synced foreign tracks for muxing
        synced_foreign_tracks_for_mux = []
        
        # Primary track (already synced)
        primary_track = args.selected_foreign_tracks[0]
        synced_foreign_tracks_for_mux.append({
            'wav_path': args.output_audio,
            'language': primary_track['language'],
            'stream_idx': primary_track['stream_idx'],
        })

        # Additional tracks (if any)
        additional_tracks = args.selected_foreign_tracks[1:]  # Everything after the primary
        if additional_tracks and final_segment_anchors:
            logger.info(f"\n===== Syncing {len(additional_tracks)} Additional Foreign Audio Track(s) =====")
            for track in additional_tracks:
                synced_wav = sync_additional_track(
                    args=args,
                    foreign_video=args.foreign_video,
                    stream_idx=track['stream_idx'],
                    final_segment_anchors=final_segment_anchors,
                    ref_delay_s=final_ref_delay,
                    temp_dir=temp_dir,
                    track_label=f"{track['language']} (stream #{track['stream_idx']})"
                )
                if synced_wav:
                    synced_foreign_tracks_for_mux.append({
                        'wav_path': synced_wav,
                        'language': track['language'],
                        'stream_idx': track['stream_idx'],
                    })
                else:
                    logger.warning(f"Failed to sync additional track #{track['stream_idx']} ({track['language']}). "
                                 f"It will be excluded from the output.")

        if args.threshold_calibration_csv:
            write_threshold_calibration_csv(args.threshold_calibration_csv)

        # === Stage 3: Muxing (Now the default final step) ===
        if audio_sync_success:
            # Pass all synced foreign tracks to the muxing function
            muxing_success = run_muxing(args, final_ref_stream_idx, synced_subtitles, synced_foreign_tracks_for_mux)
            if not muxing_success:
                raise RuntimeError("Muxing Stage Failed.")
        else:
             raise RuntimeError("Audio synchronization did not complete successfully, cannot mux.")

    except RuntimeError as e:
        logger.error(f"Process aborted due to error: {e}")
        # Clean up temporary audio file if it exists and wasn't user specified
        if args.output_audio_original is None and args.output_audio and os.path.exists(args.output_audio):
            try:
                os.remove(args.output_audio)
                logger.info(f"Cleaned up temporary audio file: {args.output_audio}")
            except Exception as del_e:
                logger.warning(f"Could not clean up temp audio file {args.output_audio}: {del_e}")
        if temp_dir_obj:
            try: temp_dir_obj.cleanup()
            except Exception as clean_e: logger.warning(f"Error cleaning up temp dir: {clean_e}")
            else: logger.info("Attempted temporary directory cleanup.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"An unexpected critical error occurred:", exc_info=True)
        # Clean up temporary audio file if it exists and wasn't user specified
        if args.output_audio_original is None and args.output_audio and os.path.exists(args.output_audio):
            try:
                os.remove(args.output_audio)
                logger.info(f"Cleaned up temporary audio file: {args.output_audio}")
            except Exception as del_e:
                 logger.warning(f"Could not clean up temp audio file {args.output_audio}: {del_e}")
        if temp_dir_obj:
            try: temp_dir_obj.cleanup()
            except Exception as clean_e: logger.warning(f"Error cleaning up temp dir: {clean_e}")
            else: logger.info("Attempted temporary directory cleanup.")
        sys.exit(1)
    finally:
        # Ensure temporary directory is cleaned up even if errors occur after its creation but before muxing/final cleanup
        if temp_dir_obj:
            try: temp_dir_obj.cleanup()
            except Exception as clean_e: logger.warning(f"Final attempt to clean up temp dir failed: {clean_e}")
            # else: logger.info("Temporary directory cleaned up.") # Avoid duplicate message if already logged


    # --- Final Summary ---
    overall_elapsed_time = time.time() - overall_start_time
    logger.info("\n===== PROCESS SUMMARY =====")
    final_exit_code = 1 # Default to error

    # Core success means audio sync AND muxing worked
    if audio_sync_success and muxing_success:
        logger.info(f"-> Muxed Video Created:      {args.output_video}")
        logger.info(f"-> Calculated Reference Delay: {final_ref_delay:.3f} seconds")

        # Log optional outputs only if they were requested and presumably created
        if args.output_audio_original:
             if os.path.exists(args.output_audio_original):
                 logger.info(f"-> Synchronized Audio Saved: {args.output_audio_original}")
             else:
                  logger.warning(f"-> Expected audio file not found: {args.output_audio_original}") # Should not happen if sync succeeded

        if args.output_csv:
            if os.path.exists(args.output_csv):
                 logger.info(f"-> Segment CSV File Written: {args.output_csv}")
            else:
                 logger.warning(f"-> Expected CSV file not found: {args.output_csv}") # Might happen if writing failed but process continued

        if args.qc_output_dir:
             if os.path.isdir(args.qc_output_dir):
                 logger.info(f"-> QC Image Directory:       {args.qc_output_dir}")
             else:
                 logger.warning(f"-> Expected QC directory not found: {args.qc_output_dir}")

        logger.info(f"Total time elapsed  : {overall_elapsed_time:.1f}s")
        logger.info("===== AVSync Completed Successfully =====")
        final_exit_code = 0 # Success

    else:
        # Handle various failure modes
        if not audio_sync_success:
            logger.error("-> Audio Synchronization Stage Failed.")
        elif not muxing_success:
             logger.error("-> Muxing Stage Failed.") # Muxing is now essential

        logger.error("\nScript failed during processing.")
        logger.info(f"Total Elapsed Time: {overall_elapsed_time:.2f} seconds")
        logger.info("===== AVSync Failed =====")
        final_exit_code = 1 # Error

    sys.exit(final_exit_code)


if __name__ == "__main__":
    main()