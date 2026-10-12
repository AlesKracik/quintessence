#!/usr/bin/env python3
"""
proc.py — run a child process whose timeout kills everything it started.

`subprocess.run(..., timeout=)` kills only the DIRECT child. `quint` is a node
process that starts its own children — the Rust evaluator
(`~/.quint/rust-evaluator-*/quint_evaluator simulate-from-stdin`) for `quint
run`, Apalache's JVM for a one-shot `quint verify` — and when node is killed
those are re-parented to pid 1 and keep running. A timed-out simulation then
goes on burning a core for hours, competing with every check after it and
outliving the run that started it. Every timing a run reports is distorted
by its own orphans.

So the child is started in a NEW SESSION (its own process group, POSIX) or a
new process group (Windows), and on timeout — or on any interruption,
Ctrl-C included — the whole group is terminated, then killed after a short
grace period. The new session matters for Ctrl-C too: the terminal's SIGINT
goes to the foreground group, which the child is no longer in, so it is
forwarded here instead of being lost.

`run_group()` keeps the shape of `subprocess.run(capture_output=True, text=True)`:
it returns a CompletedProcess and raises subprocess.TimeoutExpired, so a call
site changes its function name and nothing else.
"""

import os
import signal
import subprocess
import sys
import threading

GRACE_SECONDS = 3

# Every child run_group has started and not yet reaped. A run that checks in
# parallel interrupts its workers from the main thread: Ctrl-C lands there,
# and the workers' children are in sessions of their own that the terminal's
# SIGINT no longer reaches.
_live = set()
_live_lock = threading.Lock()


def kill_all():
    """Take down every child (and its group) still running."""
    with _live_lock:
        procs = list(_live)
    for p in procs:
        _kill_group(p)


def _kill_group(proc):
    """SIGTERM the child's whole group, then SIGKILL whatever survived."""
    if proc.poll() is not None and sys.platform == "win32":
        return
    if sys.platform == "win32":
        # taskkill /T walks the tree; there is no group signal to send.
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        # The leader is gone; its group id is its pid (start_new_session).
        pgid = proc.pid
    for sig, wait in ((signal.SIGTERM, GRACE_SECONDS), (signal.SIGKILL, None)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        if wait is None:
            break
        try:
            proc.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            continue
        # The leader exited; grandchildren in its group may not have. One
        # more SIGKILL to the group is harmless if it is already empty.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return


def run_group(cmd, timeout=None, cwd=None, shell=False, env=None, input=None):
    """subprocess.run(cmd, capture_output=True, text=True, timeout=timeout),
    except that a timeout or an interruption takes the child's whole process
    group down with it."""
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, cwd=cwd, shell=shell, env=env, text=True,
                            stdin=subprocess.PIPE if input is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            **kwargs)
    with _live_lock:
        _live.add(proc)
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        out, err = proc.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
    except BaseException:
        # Ctrl-C, or the caller's own exception: nothing this started may
        # outlive it.
        _kill_group(proc)
        proc.wait()
        raise
    finally:
        with _live_lock:
            _live.discard(proc)
    # NOT killed after a normal exit: `quint verify` may have started the
    # persistent Apalache server in this group, and it is meant to outlive
    # the call so the next check skips JVM start. A timeout takes it down
    # (above) — which is the point: the timed-out query dies with it instead
    # of congesting every check after it.
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
