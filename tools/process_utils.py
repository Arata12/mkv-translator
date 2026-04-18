"""Tracked subprocess helpers so external tools do not outlive the app."""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import threading


_tracked_processes = set()
_tracked_lock = threading.RLock()


def track_process(process):
    """Register an already-started subprocess for cleanup."""
    with _tracked_lock:
        _tracked_processes.add(process)


def untrack_process(process):
    """Remove a subprocess from cleanup tracking."""
    with _tracked_lock:
        _tracked_processes.discard(process)


def _terminate_process(process, force=False):
    if process.poll() is not None:
        return

    try:
        if os.name == "posix":
            sig = signal.SIGKILL if force else signal.SIGTERM
            os.killpg(process.pid, sig)
        else:
            if force:
                process.kill()
            else:
                process.terminate()
    except ProcessLookupError:
        pass
    except Exception:
        if force:
            process.kill()
        else:
            process.terminate()


def cleanup_tracked_processes():
    """Terminate any still-running tracked subprocesses."""
    with _tracked_lock:
        processes = list(_tracked_processes)

    for process in processes:
        _terminate_process(process, force=False)

    for process in processes:
        try:
            process.wait(timeout=1)
        except Exception:
            _terminate_process(process, force=True)
            try:
                process.wait(timeout=1)
            except Exception:
                pass

    with _tracked_lock:
        _tracked_processes.clear()


def run_tracked_subprocess(command, check=False, **kwargs):
    """Run a subprocess while ensuring it is cleaned up on abort."""
    popen_kwargs = dict(kwargs)
    if popen_kwargs.pop("capture_output", False):
        popen_kwargs.setdefault("stdout", subprocess.PIPE)
        popen_kwargs.setdefault("stderr", subprocess.PIPE)
    if os.name == "posix":
        popen_kwargs.setdefault("start_new_session", True)

    process = subprocess.Popen(command, **popen_kwargs)
    track_process(process)

    try:
        stdout, stderr = process.communicate()
    except BaseException:
        _terminate_process(process, force=False)
        try:
            process.wait(timeout=1)
        except Exception:
            _terminate_process(process, force=True)
        raise
    finally:
        untrack_process(process)

    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            command,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed


atexit.register(cleanup_tracked_processes)
