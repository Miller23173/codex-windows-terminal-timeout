#!/usr/bin/env python3
r"""Run a PowerShell command on Windows with a hard timeout.

Canonical usage from PowerShell:

@'
rg -n "needle" .
'@ | python "$env:USERPROFILE\.codex\skills\windows-terminal-timeout\scripts\run_with_timeout.py" --seconds 15 --ps-stdin

The command body is read from stdin so quoting, pipes, redirects, and newlines
arrive unchanged. File mode exists only for rare cases where a here-string is
not suitable.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Optional

EXIT_BAD_USAGE = 2
EXIT_COMMAND_NOT_FOUND = 127
EXIT_TIMEOUT = 124
DEFAULT_SECONDS = 10.0
MAX_SECONDS = 86400.0
CLEANUP_GRACE_SECONDS = 5.0

# Windows constants used by the whole-process-tree cleanup path.
CREATE_NEW_PROCESS_GROUP = 0x00000200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsJob:
    """Best-effort process-tree containment with taskkill fallback."""

    def __init__(self) -> None:
        self.handle: Optional[object] = None

    def create(self) -> bool:
        if os.name != "nt":
            return False

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return False

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(handle)
            return False

        self.handle = handle
        return True

    def assign(self, proc: subprocess.Popen[bytes] | subprocess.Popen[str]) -> bool:
        if os.name != "nt" or self.handle is None:
            return False

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        process_handle = wintypes.HANDLE(proc._handle)  # type: ignore[attr-defined]
        return bool(kernel32.AssignProcessToJobObject(self.handle, process_handle))

    def terminate(self, exit_code: int = 1) -> bool:
        if os.name != "nt" or self.handle is None:
            return False

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        return bool(kernel32.TerminateJobObject(self.handle, exit_code))

    def close(self) -> None:
        if os.name != "nt" or self.handle is None:
            return

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(self.handle)
        self.handle = None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a PowerShell command on Windows with a hard timeout.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Canonical usage from PowerShell:\n"
            "  @'\n"
            "  rg -n \"needle\" .\n"
            "  '@ | python run_with_timeout.py --seconds 15 --ps-stdin\n"
        ),
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=DEFAULT_SECONDS,
        help=f"Timeout in seconds. Default: {DEFAULT_SECONDS:g}. Maximum: {MAX_SECONDS:g}.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--ps-stdin",
        action="store_true",
        help="Read the complete PowerShell command from stdin.",
    )
    source.add_argument(
        "--ps-file",
        type=Path,
        help="Read the complete PowerShell command from a UTF-8 text file.",
    )
    return parser.parse_args(argv)


def validate_seconds(seconds: float) -> Optional[str]:
    if not (seconds > 0):
        return "--seconds must be greater than 0."
    if seconds > MAX_SECONDS:
        return f"--seconds cannot exceed {MAX_SECONDS:g}."
    return None


def read_command(args: argparse.Namespace) -> tuple[Optional[str], Optional[str]]:
    if args.ps_stdin:
        try:
            command = sys.stdin.read()
        except Exception as exc:  # pragma: no cover - defensive path
            return None, f"failed to read PowerShell command from stdin: {exc}"
    else:
        path: Path = args.ps_file
        try:
            command = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            return None, f"failed to read PowerShell command file {path}: {exc}"
        except UnicodeError as exc:
            return None, f"failed to decode PowerShell command file {path} as UTF-8: {exc}"

    if not command.strip():
        return None, "PowerShell command is empty."
    return command, None


def find_powershell() -> Optional[str]:
    # Prefer the modern host when installed, but support the built-in host too.
    return shutil.which("pwsh.exe") or shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")


def taskkill_tree(pid: int) -> None:
    if os.name != "nt":
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=CLEANUP_GRACE_SECONDS,
        )
    except Exception:
        # Fallback cleanup is best effort; a direct kill is attempted later too.
        pass


def direct_kill(proc: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
    try:
        proc.kill()
    except Exception:
        pass


def wait_briefly(proc: subprocess.Popen[bytes] | subprocess.Popen[str], seconds: float) -> None:
    try:
        proc.wait(timeout=seconds)
    except Exception:
        pass


def run_command(command: str, seconds: float) -> int:
    powershell = find_powershell()
    if not powershell:
        print("[windows-terminal-timeout] PowerShell executable not found.", file=sys.stderr)
        return EXIT_COMMAND_NOT_FOUND

    creationflags = 0
    if os.name == "nt":
        creationflags |= CREATE_NEW_PROCESS_GROUP

    proc: subprocess.Popen[str]
    try:
        proc = subprocess.Popen(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            text=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        print(f"[windows-terminal-timeout] Failed to start PowerShell: {exc}", file=sys.stderr)
        return EXIT_COMMAND_NOT_FOUND

    job = WindowsJob()
    job_assigned = False
    try:
        if job.create():
            job_assigned = job.assign(proc)

        try:
            return int(proc.wait(timeout=seconds))
        except subprocess.TimeoutExpired:
            print(
                f"[windows-terminal-timeout] Timed out after {seconds:g}s; terminating command.",
                file=sys.stderr,
            )

            if job_assigned:
                job.terminate(exit_code=EXIT_TIMEOUT)
            else:
                taskkill_tree(proc.pid)

            wait_briefly(proc, CLEANUP_GRACE_SECONDS)
            if proc.poll() is None:
                taskkill_tree(proc.pid)
                direct_kill(proc)
                wait_briefly(proc, CLEANUP_GRACE_SECONDS)

            return EXIT_TIMEOUT
        except KeyboardInterrupt:
            print("[windows-terminal-timeout] Interrupted; terminating command.", file=sys.stderr)
            if job_assigned:
                job.terminate(exit_code=130)
            else:
                taskkill_tree(proc.pid)
            wait_briefly(proc, CLEANUP_GRACE_SECONDS)
            if proc.poll() is None:
                direct_kill(proc)
            return 130
    finally:
        job.close()


def main(argv: list[str]) -> int:
    if os.name != "nt":
        print("[windows-terminal-timeout] This runner is intended for native Windows.", file=sys.stderr)
        return EXIT_BAD_USAGE

    args = parse_args(argv)
    seconds_error = validate_seconds(args.seconds)
    if seconds_error:
        print(f"[windows-terminal-timeout] {seconds_error}", file=sys.stderr)
        return EXIT_BAD_USAGE

    command, command_error = read_command(args)
    if command_error:
        print(f"[windows-terminal-timeout] {command_error}", file=sys.stderr)
        return EXIT_BAD_USAGE

    assert command is not None
    return run_command(command, args.seconds)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
