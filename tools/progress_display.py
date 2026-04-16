#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Progress display with animated progress bar.
Simplified from gemini-translator-srt's logger.py
"""

import os
import sys
import shutil
import threading

# Loading animation frames
_LOADING_BARS = ["—", "\\", "|", "/"]
_loading_bar_index = 0

# Store last progress state for updates
_last_progress = None
_render_lock = threading.RLock()

# Track if progress bar has been displayed before
_has_started = False


def _log_hidden_message(message: str) -> None:
    """Write progress-only messages to the optional file log."""
    try:
        from tools import logger

        logger.log_only(message)
    except Exception:
        pass


def supports_color():
    """Check if terminal supports color output"""
    # If NO_COLOR env var is set, disable color
    if os.environ.get("NO_COLOR"):
        return False

    # If FORCE_COLOR env var is set, enable color
    if os.environ.get("FORCE_COLOR"):
        return True

    # Check if stdout is a TTY
    is_a_tty = hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()

    return (
        is_a_tty
        or "ANSICON" in os.environ
        or "WT_SESSION" in os.environ
        or os.environ.get("TERM_PROGRAM") == "vscode"
    )


def supports_cursor_control():
    """Check if the terminal likely supports cursor movement for redraws."""
    return (
        hasattr(sys.stdout, "isatty")
        and sys.stdout.isatty()
        or "ANSICON" in os.environ
        or "WT_SESSION" in os.environ
        or os.environ.get("TERM_PROGRAM") == "vscode"
    )


def clear_lines(num_lines):
    """Clear specified number of lines using ANSI codes"""
    if not supports_cursor_control():
        return

    for _ in range(num_lines):
        sys.stdout.write("\033[F")  # Move cursor up one line
        sys.stdout.write("\033[K")  # Clear the line


def _truncate_text(text, max_length):
    """Truncate text with ellipsis to fit a maximum visible length."""
    if max_length <= 0:
        return ""
    if len(text) <= max_length:
        return text
    if max_length <= 3:
        return text[:max_length]
    return text[: max_length - 3] + "..."


def progress_bar(current, total, model_name, chunk_size=0,
                 is_loading=False, is_sending=False, is_thinking=False,
                 is_retrying=False, retry_countdown=0, bar_length=30,
                 thinking_time="", message="", message_color=None,
                 status_detail="", status_color=None):
    """
    Display animated progress bar with real-time updates and optional message below.

    Args:
        current: Current line number in the file
        total: Total lines to translate
        model_name: Name of model being used
        chunk_size: Number of partial lines translated in current batch (for real-time updates)
        is_loading: Show loading spinner
        is_sending: Show sending indicator
        is_thinking: Show thinking indicator (for Gemini 2.5+ thinking mode)
        is_retrying: Show retry countdown
        retry_countdown: Seconds remaining for retry
        bar_length: Length of progress bar in characters
        thinking_time: Elapsed thinking time string (e.g., "2m 34s") - shown in status line
        message: Optional message to display below progress bar
        message_color: Color code for message (e.g., "\033[36m" for cyan)
    """
    global _loading_bar_index, _last_progress, _has_started

    with _render_lock:
        # Save state for message updates
        _last_progress = {
            "current": current,
            "total": total,
            "model_name": model_name,
            "chunk_size": chunk_size,
            "is_loading": is_loading,
            "is_sending": is_sending,
            "is_thinking": is_thinking,
            "is_retrying": is_retrying,
            "retry_countdown": retry_countdown,
            "bar_length": bar_length,
            "thinking_time": thinking_time,
            "status_detail": status_detail,
            "status_color": status_color,
        }

        term_width = shutil.get_terminal_size(fallback=(140, 20)).columns

        # Calculate progress (add chunk_size for real-time progress within batch)
        progress_ratio = (current + chunk_size) / total if total > 0 else 0
        percentage = int(100 * progress_ratio)
        display_current = current + chunk_size

        prefix_without_bar = f"Translating: || {percentage}% ({display_current}/{total})"
        max_bar_length = max(10, term_width // 4)
        bar_length = min(bar_length, max_bar_length)
        filled_length = int(bar_length * progress_ratio)

        # Build progress bar (plain text initially)
        bar = '█' * filled_length + '░' * (bar_length - filled_length)

        # Build status indicator
        status = ""
        if is_retrying:
            status = f"| Retrying ({retry_countdown}s)"
        elif is_sending:
            status = "| Sending batch ↑↑↑"
        elif is_thinking:
            status = f"| Thinking {_LOADING_BARS[_loading_bar_index]}"
            if thinking_time:
                status += f" {thinking_time}"
            _loading_bar_index = (_loading_bar_index + 1) % len(_LOADING_BARS)
        elif is_loading:
            status = f"| Processing {_LOADING_BARS[_loading_bar_index]}"
            _loading_bar_index = (_loading_bar_index + 1) % len(_LOADING_BARS)

        prefix = f"Translating: |{bar}| {percentage}% ({display_current}/{total})"
        suffix_parts = [model_name]
        if status:
            suffix_parts.append(status)
        if status_detail:
            suffix_parts.append(status_detail)
        suffix = " ".join(part for part in suffix_parts if part).strip()

        if suffix:
            available_suffix = max(0, term_width - len(prefix) - 1)
            suffix = _truncate_text(suffix, available_suffix)
            progress_text = f"{prefix} {suffix}".rstrip()
        else:
            progress_text = prefix

        # Apply colors if supported (matching gemini-translator-srt style)
        if supports_color():
            # Highlight filled blocks in green, then wrap everything in blue
            progress_text = progress_text.replace("█", f"\033[32m█\033[34m")
            # Highlight upload arrows in green
            progress_text = progress_text.replace("↑", f"\033[32m↑\033[34m")
            # Highlight loading animation characters in green
            for char in _LOADING_BARS:
                progress_text = progress_text.replace(char, f"\033[32m{char}\033[34m")
            # Wrap entire progress text in blue
            progress_text = f"\033[34m{progress_text}\033[0m"

        if supports_cursor_control():
            _has_started = True
            sys.stdout.write("\r\033[2K" + progress_text)
            if message:
                if supports_color() and message_color:
                    sys.stdout.write(f"\n{message_color}{message}\033[0m")
                else:
                    sys.stdout.write(f"\n{message}")
                sys.stdout.write("\n\r\033[2K" + progress_text)
        else:
            print(progress_text)
            if message:
                print(message)

        sys.stdout.flush()


def progress_status(message: str, color: str = None) -> None:
    """Update the progress bar with a transient inline status message."""
    if _last_progress:
        _log_hidden_message(message)
        if not supports_cursor_control():
            return
        progress_state = dict(_last_progress)
        progress_state["status_detail"] = message
        progress_state["status_color"] = color
        progress_bar(**progress_state)


def info_with_progress(message: str) -> None:
    """Display an info message below the progress bar (cyan)"""
    if _last_progress:
        _log_hidden_message(message)
        progress_bar(**_last_progress, message=message, message_color="\033[36m")


def warning_with_progress(message: str) -> None:
    """Display a warning message below the progress bar (yellow)"""
    if _last_progress:
        _log_hidden_message(message)
        progress_bar(**_last_progress, message=message, message_color="\033[33m")


def error_with_progress(message: str) -> None:
    """Display an error message below the progress bar (red)"""
    if _last_progress:
        _log_hidden_message(message)
        progress_bar(**_last_progress, message=message, message_color="\033[31m")


def success_with_progress(message: str) -> None:
    """Display a success message below the progress bar (green)"""
    if _last_progress:
        _log_hidden_message(message)
        progress_bar(**_last_progress, message=message, message_color="\033[32m")


def progress_complete(current, total, model_name):
    """
    Show completion message for translation.
    """
    global _has_started

    if supports_color():
        message = f"\033[32m✓ Translation complete ({current}/{total} lines) - {model_name}\033[0m"
    else:
        message = f"✓ Translation complete ({current}/{total} lines) - {model_name}"

    with _render_lock:
        if supports_cursor_control() and _has_started:
            sys.stdout.write("\r\033[2K")

        print(message)
        sys.stdout.flush()

        # Reset state for next file
        _has_started = False


def clear_progress():
    """Clear the current progress line and messages"""
    global _has_started

    with _render_lock:
        if supports_cursor_control() and _has_started:
            sys.stdout.write("\r\033[2K")
            sys.stdout.flush()

        _has_started = False


def reset_progress_state():
    """
    Reset progress bar state between files.
    Call this when starting translation of a new file in batch processing.
    """
    global _has_started, _last_progress
    _has_started = False
    _last_progress = None
