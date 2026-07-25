from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from services.coding.config import CodingPolicy, get_coding_policy
from services.coding.contracts import CommandStatus


_SAFE_ENVIRONMENT_KEYS = {
    "ALLUSERSPROFILE",
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "WINDIR",
}
_FORBIDDEN_ENV_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "COOKIE",
    "AUTHORIZATION",
    "TELEGRAM",
    "GATEWAY",
)


class ProcessPolicyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    status: CommandStatus
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int


class _WindowsJob:
    """Best-effort native job ownership for race-free descendant cleanup."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._handle: int | None = None
        self._kernel32 = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class _BasicLimitInformation(ctypes.Structure):
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

            class _IoCounters(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class _ExtendedLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _BasicLimitInformation),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            information = _ExtendedLimitInformation()
            information.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            configured = kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(information), ctypes.sizeof(information)
            )
            assigned = configured and kernel32.AssignProcessToJobObject(
                handle, wintypes.HANDLE(process._handle)  # type: ignore[attr-defined]
            )
            if not assigned:
                kernel32.CloseHandle(handle)
                return
            self._handle = int(handle)
            self._kernel32 = kernel32
        except (AttributeError, OSError, TypeError, ValueError):
            self._handle = None
            self._kernel32 = None

    def terminate(self) -> None:
        handle = self._handle
        kernel32 = self._kernel32
        if handle is None or kernel32 is None:
            return
        try:
            kernel32.TerminateJobObject(handle, 1)
        finally:
            kernel32.CloseHandle(handle)
            self._handle = None


class _ProcessTreeGuard:
    """Own the launched process group and remember observed descendants."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.windows_job = _WindowsJob(process)
        self._termination_lock = threading.Lock()
        self._termination_complete = False
        self.posix_pgid: int | None = None
        if os.name != "nt":
            try:
                pgid = os.getpgid(process.pid)
                session_id = os.getsid(process.pid)
                if pgid == process.pid and session_id == process.pid:
                    self.posix_pgid = pgid
            except (ProcessLookupError, PermissionError, OSError):
                self.posix_pgid = None
        self.descendants: dict[tuple[int, float], psutil.Process] = {}
        self.refresh()

    def refresh(self) -> None:
        try:
            parent = psutil.Process(self.process.pid)
            members = parent.children(recursive=True)
        except (psutil.Error, OSError):
            return
        for member in members:
            try:
                self.descendants[(member.pid, member.create_time())] = member
            except psutil.Error:
                continue

    def terminate(self, *, include_parent: bool) -> None:
        with self._termination_lock:
            if self._termination_complete:
                return
            try:
                try:
                    self._terminate_once(include_parent=include_parent)
                except (OSError, ValueError, psutil.Error, subprocess.SubprocessError):
                    # Cleanup is best-effort but must never expose a stale group
                    # identity to a retry.  The Job/group signal was attempted;
                    # make one final exact-parent kill before retiring ownership.
                    try:
                        if include_parent and self.process.poll() is None:
                            self.process.kill()
                    except (OSError, subprocess.SubprocessError):
                        pass
            finally:
                # Neither a closed Job handle nor a former PGID may be reused by
                # a later/concurrent cleanup call.
                try:
                    self.windows_job.terminate()
                finally:
                    self.posix_pgid = None
                    self._termination_complete = True

    def _terminate_once(self, *, include_parent: bool) -> None:
        self.refresh()
        # A Windows job catches descendants that were spawned between psutil
        # samples, even if the direct parent has already exited.
        self.windows_job.terminate()
        if self.posix_pgid is not None:
            try:
                os.killpg(self.posix_pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        targets: list[psutil.Process] = []
        for member in self.descendants.values():
            try:
                if member.is_running():
                    targets.append(member)
            except psutil.Error:
                continue
        if include_parent:
            try:
                parent = psutil.Process(self.process.pid)
                if parent.is_running():
                    targets.append(parent)
            except psutil.Error:
                pass
        for member in reversed(targets):
            try:
                member.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(targets, timeout=2.0) if targets else ([], [])
        posix_group_alive = False
        if self.posix_pgid is not None:
            try:
                os.killpg(self.posix_pgid, 0)
                posix_group_alive = True
            except PermissionError:
                posix_group_alive = True
            except (ProcessLookupError, OSError):
                pass
        if posix_group_alive:
            pgid = self.posix_pgid
            assert pgid is not None
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    break
                except (PermissionError, OSError):
                    break
                time.sleep(0.05)
        for member in alive:
            try:
                member.kill()
            except psutil.Error:
                pass
        if include_parent and self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()


# Shared lifecycle users may own only processes they launched themselves.  The
# public alias keeps the exact Stage 005 Windows Job Object semantics without
# duplicating a weaker MCP-specific process killer.
ProcessTreeGuard = _ProcessTreeGuard


def safe_child_environment(overrides: dict[str, str] | None = None) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _SAFE_ENVIRONMENT_KEYS
        and not any(marker in key.upper() for marker in _FORBIDDEN_ENV_MARKERS)
    }
    environment.update(
        {
            "NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "QWEN_CODE_SUPPRESS_YOLO_WARNING": "1",
        }
    )
    for key, value in (overrides or {}).items():
        upper = key.upper()
        if any(marker in upper for marker in _FORBIDDEN_ENV_MARKERS):
            raise ProcessPolicyError("secret-shaped environment override is forbidden")
        if "\x00" in key or "=" in key or "\x00" in value:
            raise ProcessPolicyError("invalid environment override")
        environment[key] = value
    return environment


def terminate_process_tree(
    process: subprocess.Popen[bytes], guard: _ProcessTreeGuard | None = None
) -> None:
    (guard or _ProcessTreeGuard(process)).terminate(include_parent=True)


class ProcessRunner:
    def __init__(self, policy: CodingPolicy | None = None) -> None:
        self.policy = policy or get_coding_policy()

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        input_text: str | None = None,
        environment: dict[str, str] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ProcessOutcome:
        if (
            not argv
            or not isinstance(argv[0], str)
            or not argv[0]
            or any(not isinstance(item, str) or "\x00" in item for item in argv)
        ):
            raise ProcessPolicyError("process argv must be a non-empty string list")
        try:
            canonical_cwd = cwd.resolve(strict=True)
        except OSError as exc:
            raise ProcessPolicyError("process cwd is unavailable") from exc
        started = time.monotonic()
        encoded_input = input_text.encode("utf-8") if input_text is not None else None
        if (
            encoded_input is not None
            and len(encoded_input) > self.policy.max_artifact_bytes
        ):
            raise ProcessPolicyError("process stdin exceeds the configured limit")
        if cancel_event is not None and cancel_event.is_set():
            return ProcessOutcome(
                status=CommandStatus.CANCELLED,
                exit_code=None,
                stdout="",
                stderr="",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        process = subprocess.Popen(
            argv,
            cwd=canonical_cwd,
            stdin=subprocess.PIPE if encoded_input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=safe_child_environment(environment),
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            start_new_session=(os.name != "nt"),
        )
        tree_guard = _ProcessTreeGuard(process)
        output: dict[str, bytes] = {"stdout": b"", "stderr": b""}
        overflow = threading.Event()
        stdin_done = threading.Event()
        stdin_complete = threading.Event()
        stdin_failed = threading.Event()
        stdin_written = 0
        stdin_lock = threading.Lock()

        def reader(name: str, stream) -> None:
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = stream.read(8_192)
                if not chunk:
                    break
                size += len(chunk)
                if size <= self.policy.max_output_chars * 4:
                    chunks.append(chunk)
                else:
                    overflow.set()
                    break
            output[name] = b"".join(chunks)

        reader_threads = [
            threading.Thread(target=reader, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=reader, args=("stderr", process.stderr), daemon=True),
        ]
        for thread in reader_threads:
            thread.start()

        stdin_thread: threading.Thread | None = None
        if encoded_input is None:
            stdin_complete.set()
            stdin_done.set()
        else:
            assert process.stdin is not None
            stdin_stream = process.stdin

            def write_stdin() -> None:
                nonlocal stdin_written
                view = memoryview(encoded_input)
                try:
                    descriptor = stdin_stream.fileno()
                    while stdin_written < len(view):
                        written = os.write(descriptor, view[stdin_written:])
                        if not isinstance(written, int) or written <= 0:
                            raise OSError("process stdin write made no progress")
                        with stdin_lock:
                            stdin_written += written
                    stdin_complete.set()
                except (BrokenPipeError, OSError, ValueError):
                    stdin_failed.set()
                finally:
                    try:
                        stdin_stream.close()
                    except OSError:
                        pass
                    stdin_done.set()

            stdin_thread = threading.Thread(target=write_stdin, daemon=True)
            stdin_thread.start()
        deadline = started + timeout_seconds
        status = CommandStatus.FAILED
        exit_code: int | None = None
        stdin_delivery_failed = False
        while True:
            tree_guard.refresh()
            if overflow.is_set():
                terminate_process_tree(process, tree_guard)
                status = CommandStatus.FAILED
                exit_code = process.poll()
                break
            if cancel_event is not None and cancel_event.is_set():
                terminate_process_tree(process, tree_guard)
                status = CommandStatus.CANCELLED
                exit_code = None
                break
            if time.monotonic() >= deadline:
                terminate_process_tree(process, tree_guard)
                status = CommandStatus.TIMED_OUT
                exit_code = None
                break
            if stdin_failed.is_set():
                terminate_process_tree(process, tree_guard)
                status = CommandStatus.FAILED
                exit_code = process.poll()
                stdin_delivery_failed = True
                break
            exit_code = process.poll()
            if exit_code is not None:
                if not stdin_done.is_set():
                    stdin_done.wait(timeout=self.policy.process_poll_seconds)
                with stdin_lock:
                    delivered = stdin_written
                if (
                    not stdin_done.is_set()
                    or not stdin_complete.is_set()
                    or stdin_failed.is_set()
                    or encoded_input is not None
                    and delivered != len(encoded_input)
                ):
                    status = CommandStatus.FAILED
                    stdin_delivery_failed = True
                else:
                    status = (
                        CommandStatus.PASSED
                        if exit_code == 0
                        else CommandStatus.FAILED
                    )
                break
            time.sleep(self.policy.process_poll_seconds)
        # Successful/failed parents may intentionally or accidentally detach a
        # background worker.  No descendant may outlive the bounded executor
        # and continue changing its worktree after evidence is captured.
        if status in {CommandStatus.PASSED, CommandStatus.FAILED}:
            tree_guard.terminate(include_parent=False)
        if stdin_thread is not None:
            stdin_thread.join(timeout=5)
            if stdin_thread.is_alive():
                terminate_process_tree(process, tree_guard)
                status = CommandStatus.FAILED
                exit_code = process.poll()
                stdin_delivery_failed = True
        for thread in reader_threads:
            thread.join(timeout=5)
        duration = int((time.monotonic() - started) * 1000)
        stdout = output["stdout"].decode("utf-8", errors="replace")[-self.policy.max_output_chars :]
        stderr = output["stderr"].decode("utf-8", errors="replace")[-self.policy.max_output_chars :]
        if overflow.is_set():
            stderr = (stderr + "\nProcess output exceeded the coding policy limit.").strip()
        if stdin_delivery_failed:
            stderr = (
                stderr + "\nProcess stdin was not delivered completely."
            ).strip()
        return ProcessOutcome(
            status=status,
            exit_code=exit_code if status in {CommandStatus.PASSED, CommandStatus.FAILED} else None,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
        )
