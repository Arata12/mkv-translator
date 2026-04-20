"""Utilities for experimental burned-in subtitle OCR workflows."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import md5
from pathlib import Path

import pysubs2

from tools.process_utils import run_tracked_subprocess, track_process, untrack_process


@dataclass
class OCRFrameSample:
    """One sampled frame prepared for OCR."""

    index: int
    timestamp_s: float
    image_path: Path | None = None
    signature_path: Path | None = None
    image_bytes: bytes | None = None
    image_hash: str | None = None

    def get_image_bytes(self):
        """Return the sample image payload, loading it lazily if needed."""
        if self.image_bytes is None:
            if self.image_path is None:
                raise ValueError("OCR sample has no image bytes or image path")
            self.image_bytes = self.image_path.read_bytes()
        return self.image_bytes

    def get_image_hash(self):
        """Return a stable hash for OCR image dedupe and caching."""
        if self.image_hash is None:
            self.image_hash = md5(self.get_image_bytes()).hexdigest()
        return self.image_hash


# Cache version for persistent OCR extraction caching
OCR_EXTRACT_CACHE_VERSION = 1


def get_ocr_extract_cache_dir(mkv_path: Path) -> Path:
    """Return the directory path for OCR extraction cache."""
    return Path("tmp") / f"{mkv_path.stem}.ocr-extract"


def _save_ocr_extract_cache(
    mkv_path: Path,
    crop_filter: str,
    ocr_mode: str,
    ocr_fps: float | None,
    subtitle_stream_index: int | None,
    frame_samples: list[OCRFrameSample],
):
    """Save OCR extraction cache to disk for later reuse."""
    cache_dir = get_ocr_extract_cache_dir(mkv_path)
    cache_dir.mkdir(parents=True, exist_ok=True)

    images_dir = cache_dir / "images"
    images_dir.mkdir(exist_ok=True)

    signatures_dir = cache_dir / "signatures"
    signatures_dir.mkdir(exist_ok=True)

    frame_metadata = []
    for sample in frame_samples:
        image_hash = sample.get_image_hash()
        suffix = ".png"
        if sample.image_bytes:
            if sample.image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                suffix = ".png"
            elif sample.image_bytes.startswith(b"\xff\xd8\xff"):
                suffix = ".jpg"
        else:
            suffix = Path(sample.image_path or "frame.png").suffix or ".png"

        image_filename = f"{sample.index:06d}_{image_hash[:12]}{suffix}"
        image_path = images_dir / image_filename

        if sample.image_bytes and not image_path.exists():
            image_path.write_bytes(sample.image_bytes)
        elif sample.image_path and sample.image_path.exists() and not image_path.exists():
            shutil.copy2(sample.image_path, image_path)

        signature_filename = None
        signature_path_local = None
        if sample.signature_path and sample.signature_path.exists():
            signature_filename = f"{sample.index:06d}_{image_hash[:12]}.pgm"
            signature_path_local = signatures_dir / signature_filename
            if not signature_path_local.exists():
                shutil.copy2(sample.signature_path, signature_path_local)

        frame_metadata.append(
            {
                "index": sample.index,
                "timestamp_s": sample.timestamp_s,
                "image_hash": image_hash,
                "image_filename": image_filename,
                "signature_filename": signature_filename,
            }
        )

    metadata = {
        "version": OCR_EXTRACT_CACHE_VERSION,
        "input_file": str(mkv_path),
        "crop_filter": crop_filter,
        "ocr_mode": ocr_mode,
        "ocr_fps": ocr_fps,
        "subtitle_stream_index": subtitle_stream_index,
        "frame_count": len(frame_samples),
        "frames": frame_metadata,
    }

    metadata_file = cache_dir / "metadata.json"
    with open(metadata_file, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)


def _load_ocr_extract_cache(
    mkv_path: Path,
    crop_filter: str,
    ocr_mode: str,
    ocr_fps: float | None,
    subtitle_stream_index: int | None,
) -> list[OCRFrameSample] | None:
    """Load OCR extraction cache from disk if compatible. Returns None if not compatible."""
    cache_dir = get_ocr_extract_cache_dir(mkv_path)
    metadata_file = cache_dir / "metadata.json"

    if not metadata_file.exists():
        return None

    try:
        with open(metadata_file, "r", encoding="utf-8") as f:
            metadata = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    if metadata.get("version") != OCR_EXTRACT_CACHE_VERSION:
        return None
    if metadata.get("input_file") != str(mkv_path):
        return None
    if metadata.get("crop_filter") != crop_filter:
        return None
    if metadata.get("ocr_mode") != ocr_mode:
        return None
    if metadata.get("ocr_fps") != ocr_fps:
        return None
    if metadata.get("subtitle_stream_index") != subtitle_stream_index:
        return None

    images_dir = cache_dir / "images"
    signatures_dir = cache_dir / "signatures"

    if not images_dir.exists():
        return None

    frame_samples = []
    for frame_info in metadata.get("frames", []):
        image_filename = frame_info.get("image_filename")
        if not image_filename:
            return None

        image_path = images_dir / image_filename
        if not image_path.exists():
            return None

        signature_path = None
        signature_filename = frame_info.get("signature_filename")
        if signature_filename and signatures_dir.exists():
            signature_path = signatures_dir / signature_filename
            if not signature_path.exists():
                signature_path = None

        frame_samples.append(
            OCRFrameSample(
                index=frame_info.get("index", 0),
                timestamp_s=frame_info.get("timestamp_s", 0.0),
                image_path=image_path,
                signature_path=signature_path,
            )
        )

    if not frame_samples:
        return None

    return frame_samples


def get_subtitle_packet_timestamps(video_path: Path, subtitle_stream_index):
    """Read raw subtitle packet timestamps for one subtitle stream."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        f"s:{subtitle_stream_index}",
        "-show_entries",
        "packet=pts_time",
        "-of",
        "csv=p=0",
        str(video_path),
    ]
    result = run_tracked_subprocess(
        command, capture_output=True, text=True, encoding="utf-8", check=True
    )

    timestamps = []
    for raw_line in result.stdout.splitlines():
        value = raw_line.strip()
        if not value or value == "N/A":
            continue
        try:
            timestamp = float(value)
        except ValueError:
            continue

        timestamps.append(timestamp)

    return timestamps


def filter_timestamps_by_gap(timestamps, min_gap_s=0.75):
    """Reduce subtitle timestamps by requiring a minimum gap between kept entries."""
    filtered = []
    last_kept = None
    for timestamp in timestamps:
        if last_kept is None or timestamp - last_kept >= min_gap_s:
            filtered.append(timestamp)
            last_kept = timestamp
    return filtered


def get_subtitle_event_timestamps(video_path: Path, subtitle_stream_index, min_gap_s=0.75):
    """Read sparse subtitle packet timestamps for one subtitle stream."""
    return filter_timestamps_by_gap(
        get_subtitle_packet_timestamps(video_path, subtitle_stream_index), min_gap_s
    )


def choose_sparse_subtitle_event_timestamps(
    video_path: Path,
    subtitle_stream_index,
    target_count=600,
    candidate_gaps=(1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0),
):
    """Pick a subtitle timestamp spacing that keeps OCR workload reasonable."""
    raw_timestamps = get_subtitle_packet_timestamps(video_path, subtitle_stream_index)
    if len(raw_timestamps) <= target_count:
        return raw_timestamps, 0.0

    chosen = raw_timestamps
    chosen_gap = candidate_gaps[-1]
    for gap in candidate_gaps:
        filtered = filter_timestamps_by_gap(raw_timestamps, gap)
        chosen = filtered
        chosen_gap = gap
        if len(filtered) <= target_count:
            break

    if len(chosen) > target_count:
        stride = max(1, math.ceil(len(chosen) / target_count))
        chosen = chosen[::stride]

    return chosen, chosen_gap


def check_ffmpeg_tools():
    """Return True when ffmpeg and ffprobe are available in PATH."""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def get_default_ocr_extract_workers():
    """Choose a conservative default for parallel OCR frame extraction."""
    cpu_count = os.cpu_count() or 4
    return max(1, min(2, cpu_count))


def get_video_info(video_path: Path):
    """Read width, height, and duration for a video."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:format=duration",
        "-of",
        "json",
        str(video_path),
    ]
    result = run_tracked_subprocess(
        command, capture_output=True, text=True, encoding="utf-8", check=True
    )
    data = json.loads(result.stdout)
    stream = (data.get("streams") or [{}])[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    duration = float((data.get("format") or {}).get("duration") or 0.0)
    if width <= 0 or height <= 0:
        raise ValueError("Could not determine video dimensions")
    return width, height, duration


def resolve_ocr_crop_filter(width, height, crop_spec=None, full_frame=False):
    """Build an ffmpeg crop filter for OCR frame extraction."""
    if full_frame:
        return "scale=iw:ih"

    if crop_spec:
        try:
            x_str, y_str, w_str, h_str = crop_spec.split(":")
            x = int(x_str)
            y = int(y_str)
            w = int(w_str)
            h = int(h_str)
        except ValueError as exc:
            raise ValueError("ocr-crop must use x:y:w:h pixel format") from exc

        if x < 0 or y < 0 or w <= 0 or h <= 0:
            raise ValueError("ocr-crop values must be positive")
        if x + w > width or y + h > height:
            raise ValueError("ocr-crop extends outside the video frame")
        return f"crop={w}:{h}:{x}:{y}"

    # Default: bottom third of the frame, where hard subtitles usually live.
    crop_y = (height * 2) // 3
    crop_h = height - crop_y
    return f"crop={width}:{crop_h}:0:{crop_y}"


def _clear_directory(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    for entry in directory.iterdir():
        if entry.is_file() or entry.is_symlink():
            entry.unlink()


def get_ram_temp_dir(prefix: str):
    """Create a temporary working directory backed by RAM when possible."""
    base_dir = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None
    return Path(tempfile.mkdtemp(prefix=prefix, dir=base_dir))


def _count_matching_files(directory: Path, suffix: str):
    if not directory.exists():
        return 0
    return sum(1 for entry in directory.iterdir() if entry.is_file() and entry.name.endswith(suffix))


def _run_ffmpeg_sequence_with_progress(
    command,
    output_dir: Path,
    suffix: str,
    progress_callback=None,
    progress_offset=0,
    progress_total=None,
):
    """Run an ffmpeg image-sequence job while reporting extraction progress."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    track_process(process)
    last_reported = -1
    stop_event = threading.Event()

    def report_progress(frame_count):
        nonlocal last_reported
        if not progress_callback or frame_count == last_reported:
            return
        last_reported = frame_count
        total = max(progress_total or 0, progress_offset + frame_count, 1)
        progress_callback(progress_offset + frame_count, total)

    def poll_output_files():
        while not stop_event.wait(0.1):
            report_progress(_count_matching_files(output_dir, suffix))

    poll_thread = threading.Thread(target=poll_output_files, daemon=True)
    poll_thread.start()

    try:
        while True:
            line = process.stdout.readline()
            if not line:
                break
            line = line.strip()
            if line.startswith("frame="):
                try:
                    report_progress(int(line.split("=", 1)[1]))
                except ValueError:
                    continue
        process.wait()
    finally:
        stop_event.set()
        poll_thread.join(timeout=1)
        untrack_process(process)

    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command)

    final_count = _count_matching_files(output_dir, suffix)
    report_progress(final_count)
    return final_count


def extract_ocr_frames(
    video_path: Path,
    working_dir: Path,
    fps: float,
    crop_filter: str,
    subtitle_stream_index=None,
    expected_total=None,
    progress_callback=None,
):
    """Extract OCR frames plus tiny grayscale signatures for dedupe."""
    if fps <= 0:
        raise ValueError("ocr-fps must be greater than 0")

    image_dir = working_dir / "images"
    signature_dir = working_dir / "signatures"
    _clear_directory(image_dir)
    _clear_directory(signature_dir)

    image_pattern = image_dir / "frame_%06d.jpg"
    signature_pattern = signature_dir / "frame_%06d.pgm"

    image_filter = f"fps={fps},{crop_filter}"
    signature_filter = f"fps={fps},{crop_filter},scale=64:32:flags=area,format=gray"

    if subtitle_stream_index is None:
        image_command = [
            "ffmpeg",
            "-v",
            "error",
            "-nostats",
            "-progress",
            "pipe:1",
            "-y",
            "-i",
            str(video_path),
            "-vf",
            image_filter,
            "-vsync",
            "0",
            "-q:v",
            "2",
            str(image_pattern),
        ]
        signature_command = [
            "ffmpeg",
            "-v",
            "error",
            "-nostats",
            "-progress",
            "pipe:1",
            "-y",
            "-i",
            str(video_path),
            "-vf",
            signature_filter,
            "-vsync",
            "0",
            str(signature_pattern),
        ]
    else:
        overlay_source = f"[0:v][0:s:{subtitle_stream_index}]overlay"
        image_complex = f"{overlay_source},{image_filter}[vout]"
        signature_complex = f"{overlay_source},{signature_filter}[vout]"
        image_command = [
            "ffmpeg",
            "-v",
            "error",
            "-nostats",
            "-progress",
            "pipe:1",
            "-y",
            "-i",
            str(video_path),
            "-filter_complex",
            image_complex,
            "-map",
            "[vout]",
            "-vsync",
            "0",
            "-q:v",
            "2",
            str(image_pattern),
        ]
        signature_command = [
            "ffmpeg",
            "-v",
            "error",
            "-nostats",
            "-progress",
            "pipe:1",
            "-y",
            "-i",
            str(video_path),
            "-filter_complex",
            signature_complex,
            "-map",
            "[vout]",
            "-vsync",
            "0",
            str(signature_pattern),
        ]

    combined_total = expected_total * 2 if expected_total else None
    image_count = _run_ffmpeg_sequence_with_progress(
        image_command,
        image_dir,
        ".jpg",
        progress_callback=progress_callback,
        progress_offset=0,
        progress_total=combined_total,
    )
    signature_offset = expected_total or image_count
    _run_ffmpeg_sequence_with_progress(
        signature_command,
        signature_dir,
        ".pgm",
        progress_callback=progress_callback,
        progress_offset=signature_offset,
        progress_total=combined_total,
    )

    image_files = sorted(image_dir.glob("frame_*.jpg"))
    signature_files = sorted(signature_dir.glob("frame_*.pgm"))
    if not image_files:
        raise RuntimeError("No OCR frames were extracted from the video")

    count = min(len(image_files), len(signature_files))
    interval = 1.0 / fps
    return [
        OCRFrameSample(
            index=i,
            timestamp_s=i * interval,
            image_path=image_files[i],
            signature_path=signature_files[i],
        )
        for i in range(count)
    ]


def extract_subtitle_bitmap_frames_at_timestamps(
    video_path: Path,
    working_dir: Path,
    timestamps,
    crop_filter: str,
    subtitle_stream_index,
    workers=None,
    progress_callback=None,
):
    """Render sparse subtitle bitmap frames directly from the subtitle stream into RAM."""

    worker_count = workers or get_default_ocr_extract_workers()

    def render_one(index_and_timestamp):
        i, timestamp_s = index_and_timestamp
        filter_complex = f"[0:s:{subtitle_stream_index}]format=rgba,{crop_filter}[vout]"
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-ss",
            f"{timestamp_s:.3f}",
            "-i",
            str(video_path),
            "-filter_complex",
            filter_complex,
            "-map",
            "[vout]",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
        ]
        result = run_tracked_subprocess(command, check=True, capture_output=True)
        image_bytes = result.stdout or b""
        if not image_bytes:
            raise RuntimeError(f"No OCR image bytes were extracted at timestamp {timestamp_s:.3f}")
        return OCRFrameSample(
            index=i,
            timestamp_s=timestamp_s,
            image_bytes=image_bytes,
            image_hash=md5(image_bytes).hexdigest(),
        )

    samples = []
    total = len(timestamps)
    next_index = 0
    started = 0
    running = set()

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        while next_index < total and len(running) < worker_count:
            running.add(executor.submit(render_one, (next_index, timestamps[next_index])))
            next_index += 1
            started += 1
            if progress_callback:
                progress_callback(started, total)

        while running:
            done, running = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                samples.append(future.result())

            while next_index < total and len(running) < worker_count:
                running.add(executor.submit(render_one, (next_index, timestamps[next_index])))
                next_index += 1
                started += 1
                if progress_callback:
                    progress_callback(started, total)

    if not samples:
        raise RuntimeError("No subtitle-bitmap OCR frames were extracted")

    return sorted(samples, key=lambda sample: sample.index)


def extract_subtitle_bitmap_frames_full_stream(
    video_path: Path,
    crop_filter: str,
    subtitle_stream_index,
    expected_total=None,
    progress_callback=None,
):
    """Extract displayed subtitle frames in one pass to a RAM-backed temp dir, then load them into memory."""
    image_dir = get_ram_temp_dir("mkv_ocr_")
    image_pattern = image_dir / "frame_%010d.png"
    filter_complex = f"[0:s:{subtitle_stream_index}]format=rgba,{crop_filter}[vout]"
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-y",
        "-i",
        str(video_path),
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        "-vsync",
        "0",
        "-frame_pts",
        "1",
        str(image_pattern),
    ]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    track_process(process)
    last_reported = -1
    stop_event = threading.Event()

    def poll_ram_frames():
        nonlocal last_reported
        while not stop_event.wait(0.1):
            frame_count = _count_matching_files(image_dir, ".png")
            if progress_callback and frame_count > last_reported:
                last_reported = frame_count
                progress_callback(frame_count, max(expected_total or frame_count, 1))

    poll_thread = threading.Thread(target=poll_ram_frames, daemon=True)
    poll_thread.start()

    try:
        while True:
            line = process.stdout.readline()
            if not line:
                break
            line = line.strip()
            if line.startswith("frame=") and progress_callback:
                try:
                    frame_count = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if frame_count != last_reported:
                    last_reported = frame_count
                    progress_callback(frame_count, max(expected_total or frame_count, 1))
        process.wait()
    finally:
        stop_event.set()
        poll_thread.join(timeout=1)
        untrack_process(process)

    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command)

    image_files = sorted(image_dir.glob("frame_*.png"))
    if not image_files:
        raise RuntimeError("No subtitle bitmap frames were extracted from the subtitle stream")

    samples = []
    for idx, image_path in enumerate(image_files):
        image_bytes = image_path.read_bytes()
        pts_value = int(image_path.stem.split("_")[-1])
        timestamp_s = pts_value / 1000000.0
        samples.append(
            OCRFrameSample(
                index=idx,
                timestamp_s=timestamp_s,
                image_bytes=image_bytes,
                image_hash=md5(image_bytes).hexdigest(),
            )
        )

    shutil.rmtree(image_dir, ignore_errors=True)
    if progress_callback and len(samples) != last_reported:
        progress_callback(len(samples), max(expected_total or len(samples), 1))

    return samples


def count_subtitle_bitmap_frames_full_stream(
    video_path: Path,
    crop_filter: str,
    subtitle_stream_index,
):
    """Count the real number of displayed subtitle frames via a fast ffmpeg null pass."""
    filter_complex = f"[0:s:{subtitle_stream_index}]format=rgba,{crop_filter}[vout]"
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-y",
        "-i",
        str(video_path),
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        "-vsync",
        "0",
        "-f",
        "null",
        "-",
    ]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        start_new_session=True,
    )
    track_process(process)

    last_frame = 0
    try:
        while True:
            line = process.stdout.readline()
            if not line:
                break
            line = line.strip()
            if line.startswith("frame="):
                try:
                    last_frame = int(line.split("=", 1)[1])
                except ValueError:
                    continue
        process.wait()
    finally:
        untrack_process(process)

    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command)
    if last_frame <= 0:
        raise RuntimeError("Could not determine OCR frame count from subtitle stream")
    return last_frame


def _read_pgm_payload(file_path: Path):
    """Read raw grayscale payload bytes from a simple PGM file."""
    with open(file_path, "rb") as handle:
        magic = handle.readline().strip()
        if magic != b"P5":
            raise ValueError(f"Unsupported signature format: {file_path.name}")

        def next_non_comment_line():
            line = handle.readline()
            while line.startswith(b"#"):
                line = handle.readline()
            return line

        dimensions = next_non_comment_line().strip().split()
        if len(dimensions) != 2:
            raise ValueError(f"Invalid PGM dimensions in {file_path.name}")
        next_non_comment_line()  # max value
        return handle.read()


def _mean_abs_diff(left: bytes, right: bytes):
    if not left or not right:
        return 255.0
    length = min(len(left), len(right))
    if length == 0:
        return 255.0
    total = 0
    for i in range(length):
        total += abs(left[i] - right[i])
    return total / length


def select_distinct_frame_samples(samples, diff_threshold=5.0, recheck_every=3):
    """Keep frames whose subtitle region changed meaningfully."""
    if not samples:
        return []

    distinct = [samples[0]]
    previous_payload = _read_pgm_payload(samples[0].signature_path)
    skipped_since_keep = 0

    for sample in samples[1:]:
        current_payload = _read_pgm_payload(sample.signature_path)
        diff = _mean_abs_diff(previous_payload, current_payload)
        should_keep = diff >= diff_threshold
        if recheck_every and skipped_since_keep >= recheck_every:
            should_keep = True

        if should_keep:
            distinct.append(sample)
            previous_payload = current_payload
            skipped_since_keep = 0
        else:
            skipped_since_keep += 1

    return distinct


def select_distinct_image_samples(samples, recheck_every=0):
    """Keep only samples whose rendered subtitle bitmap changed."""
    if not samples:
        return []

    distinct = []
    previous_hash = None
    skipped_since_keep = 0

    for sample in samples:
        image_hash = sample.get_image_hash()
        should_keep = image_hash != previous_hash
        if recheck_every and skipped_since_keep >= recheck_every:
            should_keep = True

        if should_keep:
            distinct.append(sample)
            previous_hash = image_hash
            skipped_since_keep = 0
        else:
            skipped_since_keep += 1

    return distinct


def normalize_ocr_text(text):
    """Normalize model OCR output into subtitle-friendly text."""
    if not text:
        return ""

    cleaned = text.strip().strip("`")
    lowered = cleaned.lower()
    if lowered in {
        "none",
        "n/a",
        "no subtitle",
        "no subtitles",
        "[no subtitle]",
        "(no subtitle)",
        "[none]",
    }:
        return ""

    cleaned = cleaned.replace("\\N", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in cleaned.split("\n")]
    lines = [line.strip() for line in lines if line.strip()]
    return "\n".join(lines)


def texts_similar(left, right, threshold=0.92):
    """Compare OCR text samples with a soft similarity threshold."""
    if left == right:
        return True
    if not left or not right:
        return False
    return SequenceMatcher(None, left, right).ratio() >= threshold


def choose_representative_ocr_text(texts):
    """Pick the strongest OCR text variant from one subtitle run."""
    if not texts:
        return ""

    def text_score(text):
        compact = "".join(text.split())
        return (len(compact), len(text))

    return max(texts, key=text_score)


def prune_and_merge_adjacent_identical_subs(subs, max_gap_s=1.0):
    """Drop empty subtitle events and merge adjacent identical ones with small gaps."""
    if not subs:
        return subs

    max_gap_ms = int(max_gap_s * 1000)
    merged_events = []

    for event in subs:
        normalized_text = normalize_ocr_text(event.text)
        if not normalized_text:
            continue

        event.text = normalized_text
        if not merged_events:
            merged_events.append(event)
            continue

        previous_event = merged_events[-1]
        gap_ms = max(0, event.start - previous_event.end)
        if normalize_ocr_text(previous_event.text) == normalized_text and gap_ms <= max_gap_ms:
            previous_event.end = max(previous_event.end, event.end)
            continue

        merged_events.append(event)

    subs.events = merged_events
    return subs


def build_srt_from_ocr_results(
    samples_with_text,
    output_path: Path,
    sample_interval_s: float,
    similarity_threshold=0.82,
    min_duration_s=0.6,
    bridge_gap_s=2.0,
):
    """Convert sampled OCR text observations into an SRT subtitle file."""
    subs = pysubs2.SSAFile()
    observations = [
        (timestamp_s, normalize_ocr_text(raw_text))
        for timestamp_s, raw_text in samples_with_text
    ]
    observations.sort(key=lambda item: item[0])

    current_text = ""
    current_variants = []
    current_start = None
    previous_time = 0.0
    current_end = None

    def finalize_current_run():
        nonlocal current_text, current_variants, current_start, current_end
        if current_text and current_start is not None and current_end is not None:
            end_time = max(current_end, current_start + min_duration_s)
            subs.append(
                pysubs2.SSAEvent(
                    start=int(current_start * 1000),
                    end=int(end_time * 1000),
                    text=choose_representative_ocr_text(current_variants or [current_text]),
                )
            )
        current_text = ""
        current_variants = []
        current_start = None
        current_end = None

    for index, (timestamp_s, text) in enumerate(observations):
        next_timestamp = (
            observations[index + 1][0] if index + 1 < len(observations) else timestamp_s + sample_interval_s
        )
        packet_end = max(next_timestamp, timestamp_s + sample_interval_s)

        if current_text:
            if text and texts_similar(current_text, text, similarity_threshold):
                current_variants.append(text)
                current_end = packet_end
                previous_time = timestamp_s
                continue

            if not text:
                lookahead_index = index + 1
                bridged = False
                while lookahead_index < len(observations):
                    future_timestamp, future_text = observations[lookahead_index]
                    if future_timestamp - previous_time > bridge_gap_s:
                        break
                    if future_text and texts_similar(current_text, future_text, similarity_threshold):
                        current_end = max(current_end or packet_end, packet_end)
                        previous_time = timestamp_s
                        bridged = True
                        break
                    if future_text:
                        break
                    lookahead_index += 1
                if bridged:
                    continue

            finalize_current_run()

        if text:
            current_text = text
            current_variants = [text]
            current_start = timestamp_s
            current_end = packet_end
            previous_time = timestamp_s
        else:
            previous_time = timestamp_s

    finalize_current_run()

    if not subs:
        raise RuntimeError("OCR did not produce any readable subtitle lines")

    prune_and_merge_adjacent_identical_subs(subs, max_gap_s=1.0)

    subs.save(str(output_path))
    return output_path
