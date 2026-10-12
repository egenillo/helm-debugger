"""
Watch mode: re-run the debugger whenever chart or values files change.
Uses mtime polling so it needs no extra dependencies and works on every OS.
"""

import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

IGNORED_DIRS = {".git", "__pycache__", ".idea", ".vscode"}


def snapshot(paths: Iterable[Path]) -> dict[str, float]:
    """Map every file under `paths` to its modification time."""
    state: dict[str, float] = {}
    for root in paths:
        root = Path(root)
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = (
                p for p in root.rglob("*")
                if p.is_file() and not IGNORED_DIRS.intersection(p.relative_to(root).parts)
            )
        else:
            continue
        for file_path in candidates:
            try:
                state[str(file_path)] = file_path.stat().st_mtime
            except OSError:
                pass
    return state


def changed_files(before: dict[str, float], after: dict[str, float]) -> list[str]:
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


def watch(
    run_once: Callable[[], int],
    paths: list[Path],
    interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    out: Callable[[str], None] = print,
    max_runs: Optional[int] = None,
) -> int:
    """
    Run `run_once`, then poll `paths` and run it again after every change.
    Returns the exit code of the last run when interrupted (Ctrl+C) or after `max_runs`.
    """
    runs = 0
    last_code = run_once()
    runs += 1
    state = snapshot(paths)

    try:
        while max_runs is None or runs < max_runs:
            out(f"\nWatching {len(state)} files for changes... (Ctrl+C to stop)")
            while True:
                sleep(interval)
                new_state = snapshot(paths)
                changes = changed_files(state, new_state)
                if changes:
                    state = new_state
                    break
            stamp = datetime.now().strftime("%H:%M:%S")
            names = ", ".join(Path(c).name for c in changes[:3]) + (" ..." if len(changes) > 3 else "")
            out(f"\n{'=' * 55}\n[{stamp}] Change detected: {names}\n{'=' * 55}")
            last_code = run_once()
            runs += 1
    except KeyboardInterrupt:
        out("\nStopped watching.")

    return last_code
