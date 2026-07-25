from __future__ import annotations

import os
import shutil
import stat
import threading
import time
from pathlib import Path


class WritableResourceLimitError(RuntimeError):
    """A writable Docker bind crossed a host resource boundary."""


class CompositeCancellation:
    """Read two cancellation sources without mutating either caller state."""

    def __init__(
        self,
        caller: threading.Event | None,
        resource_event: threading.Event,
    ) -> None:
        self._caller = caller
        self._resource_event = resource_event

    def is_set(self) -> bool:
        return self._resource_event.is_set() or (
            self._caller is not None and self._caller.is_set()
        )


def _disk_usage(path: Path):
    return shutil.disk_usage(path)


class WritableMountWatchdog:
    """Live, fail-closed growth and free-space guard for host bind mounts."""

    def __init__(
        self,
        roots: tuple[Path, ...],
        *,
        max_growth_bytes: int,
        free_space_reserve_bytes: int,
        max_entries: int,
        scan_timeout_seconds: float,
        scan_poll_seconds: float,
        free_space_poll_seconds: float,
        caller_cancel_event: threading.Event | None = None,
    ) -> None:
        if not roots:
            raise WritableResourceLimitError("writable watchdog requires host roots")
        self._raw_roots = roots
        self._max_growth_bytes = max_growth_bytes
        self._free_space_reserve_bytes = free_space_reserve_bytes
        self._max_entries = max_entries
        self._scan_timeout_seconds = scan_timeout_seconds
        self._scan_poll_seconds = scan_poll_seconds
        self._free_space_poll_seconds = free_space_poll_seconds
        self._caller_cancel_event = caller_cancel_event
        self._resource_event = threading.Event()
        self._stop_event = threading.Event()
        self._failure_lock = threading.Lock()
        self._failure: WritableResourceLimitError | None = None
        self._threads: list[threading.Thread] = []
        self._roots: tuple[Path, ...] = ()
        self._baseline_bytes = 0

    @property
    def cancellation(self) -> CompositeCancellation:
        return CompositeCancellation(self._caller_cancel_event, self._resource_event)

    def _record_failure(self, message: str, cause: BaseException | None = None) -> None:
        failure = WritableResourceLimitError(message)
        if cause is not None:
            failure.__cause__ = cause
        with self._failure_lock:
            if self._failure is None:
                self._failure = failure
        self._resource_event.set()
        self._stop_event.set()

    @staticmethod
    def _is_indirect(path: Path, info: os.stat_result) -> bool:
        is_junction = getattr(os.path, "isjunction", lambda value: False)
        return bool(
            path.is_symlink()
            or is_junction(path)
            or getattr(info, "st_reparse_tag", 0)
            or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        )

    @staticmethod
    def _canonical_root(path: Path) -> Path:
        try:
            absolute = path.absolute()
            info = path.lstat()
            canonical = path.resolve(strict=True)
        except OSError as exc:
            raise WritableResourceLimitError("writable host root is unavailable") from exc
        if (
            WritableMountWatchdog._is_indirect(path, info)
            or not stat.S_ISDIR(info.st_mode)
            or os.path.normcase(str(absolute)) != os.path.normcase(str(canonical))
        ):
            raise WritableResourceLimitError("writable host root uses filesystem indirection")
        return canonical

    def _prepare_roots(self) -> tuple[Path, ...]:
        candidates = sorted(
            {self._canonical_root(item) for item in self._raw_roots},
            key=lambda item: (len(item.parts), os.path.normcase(str(item))),
        )
        roots: list[Path] = []
        for candidate in candidates:
            if any(candidate == root or root in candidate.parents for root in roots):
                continue
            roots.append(candidate)
        return tuple(roots)

    def _scan(self) -> int:
        deadline = time.monotonic() + self._scan_timeout_seconds
        total = 0
        entries = 0
        for root in self._roots:
            stack = [root]
            while stack:
                if time.monotonic() > deadline:
                    raise WritableResourceLimitError("writable host scan timed out")
                directory = stack.pop()
                self._canonical_root(directory)
                try:
                    iterator = os.scandir(directory)
                except OSError as exc:
                    raise WritableResourceLimitError(
                        "writable host directory is unreadable"
                    ) from exc
                with iterator:
                    for entry in iterator:
                        entries += 1
                        if entries > self._max_entries:
                            raise WritableResourceLimitError(
                                "writable host entry inventory exceeds limit"
                            )
                        if time.monotonic() > deadline:
                            raise WritableResourceLimitError(
                                "writable host scan timed out"
                            )
                        candidate = Path(entry.path)
                        try:
                            # DirEntry stat can zero st_nlink/st_ino on
                            # Windows, defeating the writable-root hardlink
                            # boundary.  Use the path handle's lstat result.
                            info = candidate.lstat()
                            linked = entry.is_symlink()
                        except OSError as exc:
                            raise WritableResourceLimitError(
                                "writable host entry is unreadable"
                            ) from exc
                        if linked or self._is_indirect(Path(entry.path), info):
                            raise WritableResourceLimitError(
                                "writable host tree contains filesystem indirection"
                            )
                        if stat.S_ISDIR(info.st_mode):
                            stack.append(Path(entry.path))
                        elif stat.S_ISREG(info.st_mode):
                            if getattr(info, "st_nlink", 1) > 1:
                                raise WritableResourceLimitError(
                                    "writable host tree contains a hardlink"
                                )
                            total += int(info.st_size)
                        else:
                            raise WritableResourceLimitError(
                                "writable host tree contains a special file"
                            )
        return total

    def _bounded_scan(self) -> int:
        result: list[int] = []
        failure: list[BaseException] = []
        completed = threading.Event()

        def worker() -> None:
            try:
                result.append(self._scan())
            except BaseException as exc:  # fail closed across all filesystem errors
                failure.append(exc)
            finally:
                completed.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        if not completed.wait(self._scan_timeout_seconds + 0.1):
            raise WritableResourceLimitError("writable host scan did not terminate")
        if failure:
            error = failure[0]
            if isinstance(error, WritableResourceLimitError):
                raise error
            raise WritableResourceLimitError("writable host scan failed") from error
        if len(result) != 1:
            raise WritableResourceLimitError("writable host scan produced no result")
        return result[0]

    def _check_free_space(self) -> None:
        checked: set[tuple[int, int]] = set()
        for root in self._roots:
            try:
                info = root.stat()
                volume = (int(info.st_dev), int(info.st_ino if os.name == "nt" else 0))
                if volume in checked:
                    continue
                checked.add(volume)
                free = int(_disk_usage(root).free)
            except (OSError, ValueError) as exc:
                raise WritableResourceLimitError(
                    "host free-space observation failed"
                ) from exc
            if free < self._free_space_reserve_bytes:
                raise WritableResourceLimitError("host free-space reserve was crossed")

    def _size_loop(self) -> None:
        while not self._stop_event.wait(self._scan_poll_seconds):
            try:
                current = self._bounded_scan()
                if current - self._baseline_bytes > self._max_growth_bytes:
                    raise WritableResourceLimitError(
                        "aggregate writable host growth exceeded policy"
                    )
            except BaseException as exc:
                self._record_failure("writable host growth watchdog failed", exc)
                return

    def _free_space_loop(self) -> None:
        while not self._stop_event.wait(self._free_space_poll_seconds):
            try:
                self._check_free_space()
            except BaseException as exc:
                self._record_failure("host free-space watchdog failed", exc)
                return

    def __enter__(self) -> "WritableMountWatchdog":
        self._roots = self._prepare_roots()
        self._check_free_space()
        self._baseline_bytes = self._bounded_scan()
        self._threads = [
            threading.Thread(target=self._size_loop, daemon=True),
            threading.Thread(target=self._free_space_loop, daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=self._scan_timeout_seconds + 1.0)
            if thread.is_alive():
                self._record_failure("writable host watchdog did not stop")
        if self._failure is None:
            try:
                self._check_free_space()
                final = self._bounded_scan()
                if final - self._baseline_bytes > self._max_growth_bytes:
                    raise WritableResourceLimitError(
                        "aggregate writable host growth exceeded policy"
                    )
            except BaseException as failure:
                self._record_failure("final writable host resource check failed", failure)
        if self._failure is not None:
            raise self._failure
        return False


__all__ = [
    "CompositeCancellation",
    "WritableMountWatchdog",
    "WritableResourceLimitError",
]
