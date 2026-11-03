"""The fallback test lock is reclaimed from a holder that is gone, and only then.

scripts/test-lock.sh is what `poe test` uses where flock is missing (macOS). A
run killed with SIGKILL leaves its lock directory behind, and without reclaim
every later run waits five minutes and fails.
"""

import os
import subprocess
from pathlib import Path

LOCK_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "test-lock.sh"


def _try_acquire(lock_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; try_acquire_lock_dir "$2" && echo "acquired by $$"',
            "bash",
            str(LOCK_SCRIPT),
            str(lock_dir),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _start_time(pid: int) -> str:
    """The start time as a holder records it."""
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; process_start_time "$2"',
            "bash",
            str(LOCK_SCRIPT),
            str(pid),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.rstrip("\n")


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def _held_by(lock_dir: Path, *lines: str) -> Path:
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("".join(f"{line}\n" for line in lines))
    return lock_dir


def test_a_free_lock_is_taken(tmp_path: Path) -> None:
    result = _try_acquire(tmp_path / "lock")

    assert result.returncode == 0
    assert "acquired by" in result.stdout


def test_a_lock_whose_holder_was_killed_is_reclaimed(tmp_path: Path) -> None:
    lock_dir = _held_by(tmp_path / "lock", str(_dead_pid()), "Mon Nov  2 12:58:00 2026")

    result = _try_acquire(lock_dir)

    assert result.returncode == 0
    assert "Reclaimed a test lock" in result.stdout
    holder = (lock_dir / "pid").read_text().splitlines()[0]
    assert result.stdout.rstrip().endswith(f"acquired by {holder}")


def test_a_lock_held_by_a_running_process_is_left_alone(tmp_path: Path) -> None:
    lock_dir = _held_by(tmp_path / "lock", str(os.getpid()), _start_time(os.getpid()))

    result = _try_acquire(lock_dir)

    assert result.returncode == 1
    assert (lock_dir / "pid").read_text().splitlines()[0] == str(os.getpid())


def test_a_recycled_pid_does_not_keep_a_dead_holders_lock(tmp_path: Path) -> None:
    # The pid is running, but it is not the process that took the lock.
    lock_dir = _held_by(tmp_path / "lock", str(os.getpid()), "Thu Jan  1 00:00:00 1970")

    result = _try_acquire(lock_dir)

    assert result.returncode == 0
    assert "Reclaimed a test lock" in result.stdout


def test_a_lock_written_before_start_times_were_recorded_is_judged_by_pid(
    tmp_path: Path,
) -> None:
    live = _held_by(tmp_path / "live", str(os.getpid()))
    dead = _held_by(tmp_path / "dead", str(_dead_pid()))

    assert _try_acquire(live).returncode == 1
    assert _try_acquire(dead).returncode == 0


def test_a_lock_still_being_written_is_not_taken(tmp_path: Path) -> None:
    # Taken a moment ago, before its holder wrote the pid file.
    lock_dir = tmp_path / "lock"
    lock_dir.mkdir()

    result = _try_acquire(lock_dir)

    assert result.returncode == 1
    assert lock_dir.is_dir()
