"""Local web UI for reviewing OCR subtitle transcripts before translation."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tools.ocr_utils import normalize_ocr_text


REVIEW_SESSION_VERSION = 4


def _detect_browser_host():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        host = sock.getsockname()[0]
        return host or "127.0.0.1"
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _format_timestamp(seconds: float):
    total_ms = max(0, int(round(float(seconds or 0) * 1000)))
    hours, remainder = divmod(total_ms, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _infer_image_suffix(sample):
    image_path = getattr(sample, "image_path", None)
    if image_path and Path(image_path).suffix:
        return Path(image_path).suffix.lower()

    image_bytes = sample.get_image_bytes()
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    return ".bin"


def _count_completed_items(items):
    return sum(1 for item in items if item.get("reviewed"))


def _session_file_name(session_data):
    input_file = session_data.get("input_file") or ""
    return Path(input_file).name if input_file else "OCR review"


def _read_session_file(session_file: Path):
    with open(session_file, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_session_file(session_file: Path, payload):
    payload["updated_at"] = time.time()
    with open(session_file, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def _session_matches_expected(session_data, mkv_path: Path, expected_source_groups):
    if not isinstance(session_data, dict):
        return False
    if session_data.get("version") != REVIEW_SESSION_VERSION:
        return False
    if session_data.get("input_file") != str(mkv_path):
        return False

    saved_source_groups = [
        item.get("source_indexes") or [item.get("source_index")]
        for item in session_data.get("items", [])
    ]
    return saved_source_groups == expected_source_groups


def _format_timestamp_range(start_s: float, end_s: float):
    return f"{_format_timestamp(start_s)} → {_format_timestamp(end_s)}"


def _build_review_groups(samples, initial_text_by_hash):
    prepared = []
    for position, sample in enumerate(samples):
        image_hash = sample.get_image_hash()
        source_index = getattr(sample, "index", position)
        raw_text = initial_text_by_hash.get(source_index, initial_text_by_hash.get(image_hash, ""))
        prepared.append(
            {
                "sample": sample,
                "source_index": source_index,
                "image_hash": image_hash,
                "timestamp_s": sample.timestamp_s,
                "text": normalize_ocr_text(raw_text),
            }
        )

    groups = []
    index = 0
    while index < len(prepared):
        entry = prepared[index]
        if not entry["text"]:
            index += 1
            continue

        group_text = entry["text"]
        group_start = entry["timestamp_s"]
        last_seen_time = entry["timestamp_s"]
        group_source_indexes = [entry["source_index"]]
        first_sample = entry["sample"]
        pending_blank_indexes = []
        cursor = index + 1

        while cursor < len(prepared):
            candidate = prepared[cursor]
            if not candidate["text"]:
                pending_blank_indexes.append(candidate["source_index"])
                last_seen_time = candidate["timestamp_s"]
                cursor += 1
                continue

            if candidate["text"] != group_text:
                break

            group_source_indexes.extend(pending_blank_indexes)
            pending_blank_indexes = []
            group_source_indexes.append(candidate["source_index"])
            last_seen_time = candidate["timestamp_s"]
            cursor += 1

        if cursor < len(prepared):
            group_end = prepared[cursor]["timestamp_s"]
        else:
            group_end = last_seen_time

        groups.append(
            {
                "first_sample": first_sample,
                "source_indexes": group_source_indexes,
                "image_hash": entry["image_hash"],
                "start_s": group_start,
                "end_s": group_end,
                "text": group_text,
            }
        )
        index = cursor

    return groups


def _build_review_items(distinct_samples, initial_text_by_hash, images_dir: Path):
    images_dir.mkdir(parents=True, exist_ok=True)
    items = []

    for group in _build_review_groups(distinct_samples, initial_text_by_hash):
        sample = group["first_sample"]
        image_hash = group["image_hash"]

        suffix = _infer_image_suffix(sample)
        filename = f"{len(items) + 1:05d}_{image_hash[:12]}{suffix}"
        image_path = images_dir / filename
        if not image_path.exists():
            image_path.write_bytes(sample.get_image_bytes())

        items.append(
            {
                "review_index": len(items),
                "source_index": group["source_indexes"][0],
                "source_indexes": group["source_indexes"],
                "timestamp_s": group["start_s"],
                "timestamp_label": _format_timestamp_range(group["start_s"], group["end_s"]),
                "image_hash": image_hash,
                "image_filename": filename,
                "original_text": group["text"],
                "text": group["text"],
                "reviewed": False,
                "flagged": False,
            }
        )

    return items


def _build_new_session(mkv_path: Path, distinct_samples, initial_text_by_hash, session_dir: Path):
    images_dir = session_dir / "images"
    items = _build_review_items(distinct_samples, initial_text_by_hash, images_dir)
    return {
        "version": REVIEW_SESSION_VERSION,
        "input_file": str(mkv_path),
        "created_at": time.time(),
        "updated_at": time.time(),
        "completed": False,
        "current_index": 0,
        "items": items,
    }


def _upgrade_session_data(session_data, initial_text_by_hash):
    if not isinstance(session_data, dict):
        return session_data

    changed = False
    for item in session_data.get("items", []):
        if "source_indexes" not in item:
            item["source_indexes"] = [item.get("source_index", item.get("review_index", 0))]
            changed = True
        original_text = item.get("original_text")
        if original_text is None:
            source_indexes = item.get("source_indexes") or [item.get("source_index")]
            source_index = source_indexes[0] if source_indexes else item.get("source_index")
            original_text = initial_text_by_hash.get(
                source_index,
                initial_text_by_hash.get(item.get("image_hash"), item.get("text", "")),
            )
            item["original_text"] = original_text
            changed = True
        if "reviewed" not in item:
            item["reviewed"] = bool((item.get("text") or "").strip())
            changed = True
        if "flagged" not in item:
            item["flagged"] = False
            changed = True

    if session_data.get("version") != REVIEW_SESSION_VERSION:
        session_data["version"] = REVIEW_SESSION_VERSION
        changed = True

    return session_data, changed


def prompt_resume_ocr_review(saved_index, total_items):
    percentage = (saved_index / total_items) * 100 if total_items > 0 else 0
    print(f"\n{'=' * 60}")
    print("Previous OCR review was interrupted")
    print(
        f"Progress: {saved_index}/{total_items} reviewed items ({percentage:.1f}% complete)"
    )
    print(f"{'=' * 60}")

    while True:
        response = (
            input("Resume OCR review where you left off? (y/n) [default: y]: ")
            .strip()
            .lower()
        )
        if response in {"", "y", "yes"}:
            return True
        if response in {"n", "no"}:
            return False
        print("Please enter 'y' or 'n'")


class OCRReviewController:
    def __init__(self, session_dir: Path, session_file: Path, session_data):
        self.session_dir = session_dir
        self.session_file = session_file
        self.session_data = session_data
        self.lock = threading.Lock()
        self.finished_event = threading.Event()
        self.httpd = None

    def session_payload(self):
        with self.lock:
            items = []
            for item in self.session_data["items"]:
                items.append(
                    {
                        "review_index": item["review_index"],
                        "timestamp_label": item["timestamp_label"],
                        "timestamp_s": item["timestamp_s"],
                        "image_filename": item["image_filename"],
                        "original_text": item.get("original_text", ""),
                        "text": item.get("text", ""),
                        "reviewed": bool(item.get("reviewed", False)),
                        "flagged": bool(item.get("flagged", False)),
                    }
                )

            return {
                "completed": self.session_data.get("completed", False),
                "current_index": self.session_data.get("current_index", 0),
                "completed_count": _count_completed_items(self.session_data.get("items", [])),
                "total_items": len(items),
                "file_name": _session_file_name(self.session_data),
                "items": items,
            }

    def update_item(self, index, text, reviewed=None, flagged=None):
        with self.lock:
            items = self.session_data["items"]
            if index < 0 or index >= len(items):
                raise IndexError("OCR review item index out of range")
            items[index]["text"] = text
            if reviewed is not None:
                items[index]["reviewed"] = bool(reviewed)
            if flagged is not None:
                items[index]["flagged"] = bool(flagged)

    def revert_item_to_ocr(self, index):
        with self.lock:
            items = self.session_data["items"]
            if index < 0 or index >= len(items):
                raise IndexError("OCR review item index out of range")
            items[index]["text"] = items[index].get("original_text", "")
            return items[index]["text"]

    def save_progress(self, current_index):
        with self.lock:
            total_items = len(self.session_data["items"])
            bounded_index = max(0, min(int(current_index), max(total_items - 1, 0)))
            self.session_data["current_index"] = bounded_index
            _write_session_file(self.session_file, self.session_data)
        return self.session_payload()

    def finish(self, current_index):
        with self.lock:
            total_items = len(self.session_data["items"])
            bounded_index = max(0, min(int(current_index), max(total_items - 1, 0)))
            self.session_data["current_index"] = bounded_index
            self.session_data["completed"] = True
            _write_session_file(self.session_file, self.session_data)

        self.finished_event.set()
        if self.httpd is not None:
            threading.Thread(target=self.httpd.shutdown, daemon=True).start()
        return self.session_payload()

    def get_corrected_text_by_hash(self):
        with self.lock:
            corrected = {}
            for item in self.session_data.get("items", []):
                for source_index in item.get("source_indexes") or [
                    item.get("source_index", item["review_index"])
                ]:
                    corrected[source_index] = item.get("text", "")
            return corrected


def _build_page_html():
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OCR Review</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #111315;
      --bg-soft: #171a1d;
      --panel: #171a1d;
      --panel-2: #1e2328;
      --panel-3: #0f1215;
      --border: #2c333a;
      --border-strong: #3a444d;
      --text: #e7ebef;
      --muted: #97a2ac;
      --accent: #9eb8d9;
      --accent-strong: #d5e5f8;
      --ok: #9fcca8;
      --warn: #e5b589;
      --flag: #e19a9a;
    }

    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0;
      min-height: 100vh;
      background: #111315;
      color: var(--text);
      font: 14px/1.45 sans-serif;
    }

    button, input, textarea { font: inherit; }

    .app {
      height: 100vh;
      display: grid;
      grid-template-rows: 48px 1fr;
    }

    .topbar {
      display: grid;
      grid-template-columns: auto 1fr auto;
      align-items: center;
      gap: 18px;
      padding: 8px 16px;
      border-bottom: 1px solid var(--border);
      background: #15181b;
    }

    .file-meta {
      display: flex;
      align-items: center;
      gap: 10px;
      white-space: nowrap;
      min-width: 0;
    }

    .file-name {
      font-weight: 600;
      color: var(--accent-strong);
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .file-label,
    .hint,
    .panel-title,
    .list-empty,
    .editor-note,
    .source-label {
      color: var(--muted);
    }

    .top-meta,
    .top-actions,
    .editor-actions,
    .state-actions,
    .editor-footer,
    .list-row-meta,
    .list-row-text {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }

    .top-meta {
      justify-self: center;
      min-width: 0;
      justify-content: center;
    }
    .top-actions { justify-self: end; }

    .meta-item {
      font: 500 12px/1.2 "Iosevka", "JetBrains Mono", monospace;
      color: var(--muted);
    }

    .meta-item strong {
      color: var(--text);
    }

    .shell {
      min-height: 0;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 330px;
      gap: 14px;
      padding: 14px;
    }

    .workspace,
    .sidebar,
    .card {
      min-height: 0;
    }

    .workspace {
      display: grid;
      grid-template-rows: minmax(260px, 52%) minmax(260px, 48%);
      gap: 14px;
    }

    .card,
    .sidebar {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
    }

    .card-header,
    .sidebar-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--border);
      background: #1a1e22;
    }

    .panel-title {
      font-weight: 600;
    }

    .preview-wrap {
      position: relative;
      height: 100%;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 18px;
      background: var(--panel-3);
    }

    .preview-stage {
      width: 100%;
      height: 100%;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #090b0d;
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
    }

    .preview-stage img {
      max-width: 100%;
      max-height: 100%;
      object-fit: contain;
      background: #000;
    }

    .editor-card {
      display: grid;
      grid-template-rows: auto 1fr auto;
    }

    .editor-main {
      min-height: 0;
      display: grid;
      grid-template-rows: auto 1fr;
      gap: 10px;
      padding: 14px;
    }

    .source-block {
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #14181c;
    }

    .source-text {
      margin-top: 6px;
      white-space: pre-wrap;
      color: #c4ccd4;
      font: 13px/1.45 "Iosevka", "JetBrains Mono", monospace;
    }

    textarea {
      width: 100%;
      height: 100%;
      min-height: 0;
      resize: none;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
      background: #101317;
      color: var(--text);
      outline: none;
      font: 15px/1.55 "Iosevka", "JetBrains Mono", monospace;
    }

    textarea:focus {
      border-color: #6982a2;
      box-shadow: 0 0 0 1px rgba(158, 184, 217, 0.35);
    }

    .editor-footer {
      justify-content: space-between;
      padding: 0 14px 14px;
    }

    .editor-note {
      font-size: 12px;
    }

    .sidebar {
      display: grid;
      grid-template-rows: auto auto 1fr;
    }

    .sidebar-tools {
      padding: 12px 14px;
      border-bottom: 1px solid var(--border);
      background: #1a1e22;
    }

    .search-input {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: #101317;
      color: var(--text);
      padding: 10px 12px;
      outline: none;
    }

    .search-input:focus {
      border-color: #6982a2;
    }

    .list-wrap {
      min-height: 0;
      overflow: auto;
      padding: 8px;
    }

    .list-empty {
      padding: 14px;
      text-align: center;
    }

    .list-row {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: transparent;
      padding: 10px 10px 11px;
      text-align: left;
      display: grid;
      gap: 6px;
      transition: background 120ms ease, border-color 120ms ease;
    }

    .list-row:hover {
      background: #1b1f23;
      border-color: #38424c;
    }

    .list-row.is-active {
      background: #20262d;
      border-color: #5b6d82;
    }

    .list-row-meta {
      justify-content: space-between;
      font: 500 12px/1.2 "Iosevka", "JetBrains Mono", monospace;
      color: var(--muted);
    }

    .list-row-text {
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
    }

    .list-row-snippet {
      flex: 1;
      color: #d8dee5;
      line-height: 1.35;
      overflow: hidden;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
    }

    .badges {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }

    .badge {
      width: 9px;
      height: 9px;
      border-radius: 99px;
      background: #3d4650;
    }

    .badge.reviewed { background: var(--ok); }
    .badge.flagged { background: var(--flag); }
    .badge.edited { background: var(--warn); }

    button {
      appearance: none;
      border: 1px solid var(--border);
      background: var(--panel-2);
      color: var(--text);
      border-radius: 8px;
      padding: 9px 12px;
      cursor: pointer;
      transition: border-color 120ms ease, background 120ms ease, color 120ms ease;
    }

    button:hover:not(:disabled) {
      border-color: #5d6f85;
      color: var(--accent-strong);
    }

    button:disabled {
      opacity: 0.45;
      cursor: not-allowed;
    }

    button.primary {
      background: #243041;
      border-color: #53677f;
      color: #edf4fb;
    }

    button.ghost-active {
      border-color: #6e8a70;
      color: #d9eadc;
      background: rgba(159, 204, 168, 0.09);
    }

    button.warn-active {
      border-color: #845353;
      color: #f3d6d6;
      background: rgba(225, 154, 154, 0.1);
    }

    .status-ok { color: var(--accent-strong); }
    .status-warn { color: #f3d1ac; }

    @media (max-width: 1180px) {
      .app { height: auto; min-height: 100vh; }
      .shell { grid-template-columns: 1fr; }
      .sidebar { min-height: 320px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <div class="topbar">
      <div class="file-meta">
        <div class="file-label">File</div>
        <div class="file-name" id="file-name">OCR review</div>
      </div>

      <div class="top-meta">
        <div class="meta-item" id="counter">Line <strong>0 / 0</strong></div>
        <div class="meta-item" id="timestamp"><strong>00:00:00.000</strong></div>
        <div class="meta-item" id="completion">Reviewed <strong>0 / 0</strong></div>
        <div class="meta-item" id="dirty-indicator">Saved</div>
      </div>

      <div class="top-actions">
        <button id="prev-button">Previous</button>
        <button id="next-button">Next</button>
        <button id="save-button">Save</button>
        <button id="save-next-button">Save + Next</button>
        <button id="finish-button" class="primary">Finish &amp; Continue</button>
      </div>
    </div>

    <div class="shell">
      <main class="workspace">
        <section class="card">
          <div class="card-header">
            <div class="panel-title">PGS / OCR frame</div>
            <div class="hint" id="save-status">Loading…</div>
          </div>
          <div class="preview-wrap">
            <div class="preview-stage">
              <img id="preview-image" alt="OCR preview">
              <div id="preview-empty" class="list-empty" hidden>No subtitle image available</div>
            </div>
          </div>
        </section>

        <section class="card editor-card">
          <div class="card-header">
            <div class="panel-title">Transcript editor</div>
            <div class="state-actions">
              <button id="reviewed-button">Mark reviewed</button>
              <button id="flag-button">Flag</button>
              <button id="revert-button">Revert to OCR</button>
            </div>
          </div>

          <div class="editor-main">
            <div class="source-block">
              <div class="source-label">OCR text</div>
              <div class="source-text" id="ocr-source"></div>
            </div>
            <textarea id="transcript" spellcheck="false" autocomplete="off"></textarea>
          </div>

          <div class="editor-footer">
            <div class="editor-note">Enter or Ctrl/Cmd+Enter saves and moves next · Shift+Enter inserts newline · Ctrl/Cmd+S saves · Ctrl/Cmd+F focuses search</div>
            <div class="editor-actions">
              <div class="hint">Select any item at right to jump directly.</div>
            </div>
          </div>
        </section>
      </main>

      <aside class="sidebar">
        <div class="sidebar-header">
          <div class="panel-title">All OCR lines</div>
          <div class="hint" id="sidebar-count">0 items</div>
        </div>
        <div class="sidebar-tools">
          <input id="search-input" class="search-input" type="search" placeholder="Search line number, transcript, or OCR text">
        </div>
        <div class="list-wrap" id="list-wrap"></div>
      </aside>
    </div>
  </div>

  <script>
    let session = null;
    let currentIndex = 0;
    let dirty = false;
    let searchTerm = '';

    const counterEl = document.getElementById('counter');
    const timestampEl = document.getElementById('timestamp');
    const completionEl = document.getElementById('completion');
    const fileNameEl = document.getElementById('file-name');
    const dirtyIndicatorEl = document.getElementById('dirty-indicator');
    const saveStatusEl = document.getElementById('save-status');
    const sidebarCountEl = document.getElementById('sidebar-count');
    const previewImageEl = document.getElementById('preview-image');
    const previewEmptyEl = document.getElementById('preview-empty');
    const transcriptEl = document.getElementById('transcript');
    const ocrSourceEl = document.getElementById('ocr-source');
    const prevButtonEl = document.getElementById('prev-button');
    const nextButtonEl = document.getElementById('next-button');
    const saveButtonEl = document.getElementById('save-button');
    const saveNextButtonEl = document.getElementById('save-next-button');
    const finishButtonEl = document.getElementById('finish-button');
    const reviewedButtonEl = document.getElementById('reviewed-button');
    const flagButtonEl = document.getElementById('flag-button');
    const revertButtonEl = document.getElementById('revert-button');
    const searchInputEl = document.getElementById('search-input');
    const listWrapEl = document.getElementById('list-wrap');

    function currentItem() {
      return session && session.items ? session.items[currentIndex] : null;
    }

    function normalizeText(text) {
      return (text || '').trim();
    }

    function setStatus(message, className = 'status-ok') {
      saveStatusEl.textContent = message;
      saveStatusEl.className = `hint ${className}`;
    }

    function setDirtyState(isDirty) {
      dirty = isDirty;
      dirtyIndicatorEl.textContent = dirty ? 'Unsaved' : 'Saved';
      dirtyIndicatorEl.style.color = dirty ? '#f3d1ac' : 'var(--muted)';
    }

    function updateButtons() {
      const total = session ? session.total_items : 0;
      const item = currentItem();
      const hasItem = !!item;

      prevButtonEl.disabled = !hasItem || currentIndex <= 0;
      nextButtonEl.disabled = !hasItem || currentIndex >= total - 1;
      saveButtonEl.disabled = !hasItem;
      saveNextButtonEl.disabled = !hasItem;
      finishButtonEl.disabled = !hasItem;
      reviewedButtonEl.disabled = !hasItem;
      flagButtonEl.disabled = !hasItem;
      revertButtonEl.disabled = !hasItem;

      reviewedButtonEl.textContent = item && item.reviewed ? 'Reviewed' : 'Mark reviewed';
      reviewedButtonEl.className = item && item.reviewed ? 'ghost-active' : '';
      flagButtonEl.textContent = item && item.flagged ? 'Flagged' : 'Flag';
      flagButtonEl.className = item && item.flagged ? 'warn-active' : '';
    }

    function itemSnippet(item) {
      return normalizeText(item.text) || normalizeText(item.original_text) || '—';
    }

    function filteredItems() {
      if (!session || !session.items) {
        return [];
      }
      const term = searchTerm.trim().toLowerCase();
      return session.items.filter((item) => {
        if (!term) return true;
        return `${item.review_index + 1}\n${item.text || ''}\n${item.original_text || ''}\n${item.timestamp_label || ''}`.toLowerCase().includes(term);
      });
    }

    function renderList() {
      const items = filteredItems();
      sidebarCountEl.textContent = `${items.length} item${items.length === 1 ? '' : 's'}`;

      if (!items.length) {
        listWrapEl.innerHTML = '<div class="list-empty">No matching OCR lines.</div>';
        return;
      }

      listWrapEl.innerHTML = items.map((item) => {
        const isEdited = normalizeText(item.text) !== normalizeText(item.original_text);
        const activeClass = item.review_index === currentIndex ? ' is-active' : '';
        const badges = [
          item.reviewed ? '<span class="badge reviewed" title="Reviewed"></span>' : '',
          item.flagged ? '<span class="badge flagged" title="Flagged"></span>' : '',
          isEdited ? '<span class="badge edited" title="Edited"></span>' : ''
        ].join('');

        return `
          <button class="list-row${activeClass}" data-index="${item.review_index}">
            <div class="list-row-meta">
              <span>#${item.review_index + 1}</span>
              <span>${item.timestamp_label}</span>
            </div>
            <div class="list-row-text">
              <div class="list-row-snippet">${escapeHtml(itemSnippet(item)).replace(/\\n/g, '<br>')}</div>
              <div class="badges">${badges}</div>
            </div>
          </button>`;
      }).join('');
    }

    function render() {
      if (!session || !session.items || session.items.length === 0) {
        counterEl.textContent = 'Line 0 / 0';
        timestampEl.textContent = '00:00:00.000';
        completionEl.textContent = 'Reviewed 0 / 0';
        fileNameEl.textContent = 'OCR review';
        transcriptEl.value = '';
        ocrSourceEl.textContent = '';
        previewImageEl.hidden = true;
        previewEmptyEl.hidden = false;
        renderList();
        updateButtons();
        return;
      }

      const item = currentItem();
      fileNameEl.textContent = session.file_name || 'OCR review';
      counterEl.innerHTML = `Line <strong>${currentIndex + 1} / ${session.total_items}</strong>`;
      timestampEl.innerHTML = `<strong>${item.timestamp_label}</strong>`;
      completionEl.innerHTML = `Reviewed <strong>${session.completed_count} / ${session.total_items}</strong>`;
      transcriptEl.value = item.text || '';
      ocrSourceEl.textContent = item.original_text || 'No OCR text available';
      previewImageEl.src = `/api/image/${encodeURIComponent(item.image_filename)}`;
      previewImageEl.hidden = false;
      previewEmptyEl.hidden = true;
      setDirtyState(false);
      renderList();
      updateButtons();

      requestAnimationFrame(() => {
        transcriptEl.focus();
        transcriptEl.setSelectionRange(transcriptEl.value.length, transcriptEl.value.length);
        const activeRow = listWrapEl.querySelector('.list-row.is-active');
        if (activeRow) {
          activeRow.scrollIntoView({block: 'nearest'});
        }
      });
    }

    function escapeHtml(text) {
      return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function editorPayload() {
      const item = currentItem();
      return {
        index: currentIndex,
        text: transcriptEl.value,
        reviewed: item ? !!item.reviewed : false,
        flagged: item ? !!item.flagged : false,
      };
    }

    async function request(url, method, payload) {
      const response = await fetch(url, {
        method,
        headers: {'Content-Type': 'application/json'},
        body: payload ? JSON.stringify(payload) : undefined,
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error || 'Request failed');
      }
      return data;
    }

    async function loadSession() {
      session = await request('/api/session', 'GET');
      currentIndex = Math.min(session.current_index || 0, Math.max(session.total_items - 1, 0));
      render();
      setStatus('Ready');
    }

    async function saveProgress(message = 'Progress saved') {
      const data = await request('/api/save-progress', 'POST', editorPayload());
      session = data.session;
      currentIndex = data.current_index;
      render();
      setStatus(message);
    }

    async function navigateTo(targetIndex) {
      const data = await request('/api/navigate', 'POST', {
        ...editorPayload(),
        target_index: targetIndex,
      });
      session = data.session;
      currentIndex = data.current_index;
      render();
      setStatus(data.message || 'Saved');
    }

    async function patchCurrentItem(updates, message = 'Updated') {
      if (!session || !currentItem()) return;
      const data = await request('/api/update-item', 'POST', {
        ...editorPayload(),
        ...updates,
      });
      session = data.session;
      currentIndex = data.current_index;
      render();
      setStatus(message);
    }

    async function revertToOCR() {
      const item = currentItem();
      if (!item) return;
      const data = await request('/api/update-item', 'POST', {
        index: currentIndex,
        text: item.original_text || '',
        reviewed: item.reviewed,
        flagged: item.flagged,
      });
      session = data.session;
      currentIndex = data.current_index;
      render();
      setStatus('Reverted to OCR text');
    }

    async function finishReview() {
      const data = await request('/api/finish', 'POST', editorPayload());
      setStatus(data.message || 'Review completed');
      document.body.innerHTML = '<div style="padding:32px;font:14px/1.5 sans-serif;color:#e7ebef;background:#111315;min-height:100vh"><div style="max-width:620px;margin:0 auto;padding:24px;border:1px solid #2c333a;border-radius:8px;background:#171a1d"><div style="font-weight:600;color:#e7ebef;margin-bottom:8px">OCR review saved</div><div style="color:#97a2ac">Translation is continuing in the terminal. You can close this tab.</div></div></div>';
    }

    transcriptEl.addEventListener('input', () => {
      setDirtyState(true);
      setStatus('Unsaved changes', 'status-warn');
    });

    transcriptEl.addEventListener('keydown', async (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        if (!session || session.total_items === 0) return;
        if (currentIndex >= session.total_items - 1) {
          await saveProgress('Saved. Last line reached. Use Finish & Continue when ready.');
          return;
        }
        await navigateTo(currentIndex + 1);
      }

      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault();
        await saveProgress('Progress saved');
      }

      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'enter') {
        event.preventDefault();
        if (!session || session.total_items === 0) return;
        if (currentIndex >= session.total_items - 1) {
          await saveProgress('Saved. Last line reached. Use Finish & Continue when ready.');
          return;
        }
        await navigateTo(currentIndex + 1);
      }
    });

    prevButtonEl.addEventListener('click', async () => {
      if (currentIndex > 0) await navigateTo(currentIndex - 1);
    });

    nextButtonEl.addEventListener('click', async () => {
      if (session && currentIndex < session.total_items - 1) await navigateTo(currentIndex + 1);
    });

    saveButtonEl.addEventListener('click', async () => {
      await saveProgress('Progress saved. You can stop and resume later.');
    });

    saveNextButtonEl.addEventListener('click', async () => {
      if (!session || currentIndex >= session.total_items - 1) {
        await saveProgress('Saved. Last line reached.');
        return;
      }
      await navigateTo(currentIndex + 1);
    });

    reviewedButtonEl.addEventListener('click', async () => {
      const item = currentItem();
      if (!item) return;
      await patchCurrentItem({reviewed: !item.reviewed}, item.reviewed ? 'Review mark removed' : 'Marked reviewed');
    });

    flagButtonEl.addEventListener('click', async () => {
      const item = currentItem();
      if (!item) return;
      await patchCurrentItem({flagged: !item.flagged}, item.flagged ? 'Flag removed' : 'Line flagged for follow-up');
    });

    revertButtonEl.addEventListener('click', async () => {
      await revertToOCR();
    });

    finishButtonEl.addEventListener('click', async () => {
      await finishReview();
    });

    searchInputEl.addEventListener('input', () => {
      searchTerm = searchInputEl.value || '';
      renderList();
    });

    window.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
        event.preventDefault();
        searchInputEl.focus();
        searchInputEl.select();
      }
    });

    listWrapEl.addEventListener('click', async (event) => {
      const row = event.target.closest('.list-row');
      if (!row) return;
      const targetIndex = Number(row.dataset.index);
      if (Number.isNaN(targetIndex) || targetIndex === currentIndex) return;
      await navigateTo(targetIndex);
    });

    window.addEventListener('beforeunload', (event) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = '';
    });

    loadSession().catch((error) => {
      setStatus(error.message || 'Failed to load session', 'status-warn');
    });
  </script>
</body>
</html>
"""


def _make_handler(controller: OCRReviewController):
    class OCRReviewHandler(BaseHTTPRequestHandler):
        def _send_json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self):
            content_length = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            return json.loads(raw_body.decode("utf-8") or "{}")

        def do_GET(self):
            if self.path == "/":
                body = _build_page_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if self.path == "/api/session":
                self._send_json(controller.session_payload())
                return

            if self.path.startswith("/api/image/"):
                filename = self.path.split("/api/image/", 1)[1]
                image_path = controller.session_dir / "images" / Path(filename).name
                if not image_path.exists():
                    self._send_json({"error": "Image not found"}, status=404)
                    return

                body = image_path.read_bytes()
                content_type = "application/octet-stream"
                suffix = image_path.suffix.lower()
                if suffix == ".png":
                    content_type = "image/png"
                elif suffix in {".jpg", ".jpeg"}:
                    content_type = "image/jpeg"

                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self._send_json({"error": "Not found"}, status=404)

        def do_POST(self):
            try:
                payload = self._read_json_body()
                current_index = int(payload.get("index", 0))
                text = payload.get("text", "")
                reviewed = payload.get("reviewed")
                flagged = payload.get("flagged")
                if not isinstance(text, str):
                    raise ValueError("text must be a string")

                if reviewed is not None and not isinstance(reviewed, bool):
                    raise ValueError("reviewed must be a boolean")
                if flagged is not None and not isinstance(flagged, bool):
                    raise ValueError("flagged must be a boolean")

                controller.update_item(current_index, text, reviewed=reviewed, flagged=flagged)

                if self.path == "/api/save-progress":
                    session = controller.save_progress(current_index)
                    self._send_json(
                        {
                            "message": "Progress saved",
                            "current_index": session["current_index"],
                            "session": session,
                        }
                    )
                    return

                if self.path == "/api/update-item":
                    session = controller.save_progress(current_index)
                    self._send_json(
                        {
                            "message": "Updated",
                            "current_index": session["current_index"],
                            "session": session,
                        }
                    )
                    return

                if self.path == "/api/navigate":
                    target_index = int(payload.get("target_index", current_index))
                    session = controller.save_progress(target_index)
                    self._send_json(
                        {
                            "message": "Saved",
                            "current_index": session["current_index"],
                            "session": session,
                        }
                    )
                    return

                if self.path == "/api/finish":
                    session = controller.finish(current_index)
                    self._send_json(
                        {
                            "message": "Review completed. Translation is continuing.",
                            "finished": True,
                            "current_index": session["current_index"],
                            "session": session,
                        }
                    )
                    return

                self._send_json({"error": "Not found"}, status=404)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=400)

        def log_message(self, format, *args):
            return

    return OCRReviewHandler


def review_ocr_text_in_webui(mkv_path: Path, distinct_samples, initial_text_by_hash, logger):
    session_dir = Path("tmp") / f"{mkv_path.stem}.ocr-review"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / "session.json"
    expected_source_groups = [
        group["source_indexes"]
        for group in _build_review_groups(distinct_samples, initial_text_by_hash)
    ]

    session_data = None
    if session_file.exists():
        try:
            loaded = _read_session_file(session_file)
            if _session_matches_expected(loaded, mkv_path, expected_source_groups):
                session_data, upgraded = _upgrade_session_data(loaded, initial_text_by_hash)
                if upgraded:
                    _write_session_file(session_file, session_data)
        except Exception as exc:
            logger.warning(f"Failed to read saved OCR review session: {exc}")

    if session_data and session_data.get("completed"):
        logger.info("Found completed OCR review session. Reusing corrected OCR text.")
        controller = OCRReviewController(session_dir, session_file, session_data)
        return controller.get_corrected_text_by_hash()

    if session_data and session_data.get("current_index", 0) > 0:
        if not prompt_resume_ocr_review(
            session_data.get("current_index", 0), len(session_data.get("items", []))
        ):
            session_data = None

    if session_data is None:
        session_data = _build_new_session(
            mkv_path=mkv_path,
            distinct_samples=distinct_samples,
            initial_text_by_hash=initial_text_by_hash,
            session_dir=session_dir,
        )
        _write_session_file(session_file, session_data)

    if not session_data.get("items"):
        raise RuntimeError("OCR review session has no items to edit")

    controller = OCRReviewController(session_dir, session_file, session_data)
    server = ThreadingHTTPServer(("0.0.0.0", 0), _make_handler(controller))
    controller.httpd = server
    _, port = server.server_address
    review_host = _detect_browser_host()
    review_url = f"http://{review_host}:{port}/"

    logger.highlight("Open the OCR review UI in your browser:")
    logger.highlight(review_url)
    logger.info(
        "OCR review controls: Enter = save and next, Shift+Enter = newline, Ctrl/Cmd+S = save, Finish & Continue = resume pipeline."
    )

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        logger.info("Open that link in your browser. Use Save Progress to keep your place for a later resume.")
        controller.finished_event.wait()
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=1)

    return controller.get_corrected_text_by_hash()
