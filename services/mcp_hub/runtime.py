from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import psutil

from services.common import ROOT, RUN_DIR
from services.mcp_hub.config import (
    McpRegistry,
    ServerSpec,
    validate_installed_source,
    write_json_atomic,
)


MCP_RUN_DIR = RUN_DIR / "mcp"
STATUS_DIR = MCP_RUN_DIR / "status"
OWNER_DIR = MCP_RUN_DIR / "owners"
LOCK_DIR = MCP_RUN_DIR / "locks"


_OPERATION_LOCKS_MUTEX = threading.Lock()
_OPERATION_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_STATUS_LOCKS_MUTEX = threading.Lock()
_STATUS_PROCESS_LOCKS: dict[str, threading.Lock] = {}


def root_identity() -> str:
    return hashlib.sha256(str(ROOT.resolve()).casefold().encode("utf-8")).hexdigest()


def status_path(server_id: str) -> Path:
    return STATUS_DIR / f"{server_id}.json"


def owner_path(server_id: str) -> Path:
    return OWNER_DIR / f"{server_id}.json"


def lock_path(server_id: str) -> Path:
    return LOCK_DIR / f"{server_id}.lock"


def _guard_path(server_id: str) -> Path:
    return LOCK_DIR / f".{server_id}.generation.guard"


def operation_guard_path(server_id: str) -> Path:
    return LOCK_DIR / f".{server_id}.operation.guard"


def status_guard_path(server_id: str) -> Path:
    return LOCK_DIR / f".{server_id}.status.guard"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_status(server: ServerSpec) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "server_id": server.id,
        "state": "disabled" if not server.enabled else "on_demand",
        "consecutive_failures": 0,
        "circuit_open_until_epoch": 0.0,
        "last_reason_code": "not_checked",
        "checked_at": None,
    }


def _read_status_unlocked(
    server: ServerSpec, *, persist_cooldown: bool
) -> dict[str, Any]:
    path = status_path(server.id)
    if not path.is_file():
        return default_status(server)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = default_status(server)
        payload.update(state="degraded", last_reason_code="invalid_runtime_state")
        return payload
    expected = {
        "schema_version",
        "server_id",
        "state",
        "consecutive_failures",
        "circuit_open_until_epoch",
        "last_reason_code",
        "checked_at",
    }
    if (
        set(payload) != expected
        or payload.get("schema_version") != "1.0"
        or payload.get("server_id") != server.id
        or payload.get("state") not in {"disabled", "on_demand", "ready", "degraded", "circuit_open"}
        or not isinstance(payload.get("consecutive_failures"), int)
        or not isinstance(payload.get("circuit_open_until_epoch"), (int, float))
    ):
        invalid = default_status(server)
        invalid.update(state="degraded", last_reason_code="invalid_runtime_state")
        return invalid
    if payload["state"] == "circuit_open" and time.time() >= payload["circuit_open_until_epoch"]:
        payload.update(
            state="on_demand",
            consecutive_failures=0,
            circuit_open_until_epoch=0.0,
            last_reason_code="circuit_cooldown_elapsed",
            checked_at=_utc_now(),
        )
        if persist_cooldown:
            write_json_atomic(status_path(server.id), payload)
    return payload


def read_status(server: ServerSpec) -> dict[str, Any]:
    with _status_guard(server.id):
        return _read_status_unlocked(server, persist_cooldown=True)


def peek_status(server: ServerSpec) -> dict[str, Any]:
    """Read one atomically replaced status snapshot without waiting on writers."""

    return _read_status_unlocked(server, persist_cooldown=False)


def write_status(server: ServerSpec, payload: dict[str, Any]) -> None:
    with _status_guard(server.id):
        write_json_atomic(status_path(server.id), payload)


def record_success(server: ServerSpec) -> None:
    payload = default_status(server)
    payload.update(state="ready", last_reason_code="ok", checked_at=_utc_now())
    with _status_guard(server.id):
        write_json_atomic(status_path(server.id), payload)


def record_failure(server: ServerSpec, reason_code: str) -> None:
    with _status_guard(server.id):
        payload = _read_status_unlocked(server, persist_cooldown=False)
        failures = min(int(payload.get("consecutive_failures", 0)) + 1, 1_000)
        state = "degraded"
        open_until = 0.0
        if failures >= server.lifecycle.circuit_failure_threshold:
            state = "circuit_open"
            open_until = time.time() + server.lifecycle.circuit_cooldown_seconds
        payload.update(
            state=state,
            consecutive_failures=failures,
            circuit_open_until_epoch=open_until,
            last_reason_code=reason_code,
            checked_at=_utc_now(),
        )
        write_json_atomic(status_path(server.id), payload)


def circuit_open(server: ServerSpec) -> bool:
    return read_status(server)["state"] == "circuit_open"


def registry_snapshot(registry: McpRegistry) -> dict[str, Any]:
    servers = []
    for server in registry.servers:
        runtime = read_status(server)
        try:
            validate_installed_source(server)
            source_state = "ready"
        except (OSError, ValueError, json.JSONDecodeError):
            source_state = "unavailable"
        servers.append(
            {
                "id": server.id,
                "display_name": server.display_name,
                "version": server.version,
                "enabled": server.enabled,
                "configured_state": server.configured_state,
                "source_state": source_state,
                "runtime_state": runtime["state"],
                "last_reason_code": runtime["last_reason_code"],
                "checked_at": runtime["checked_at"],
                "consumers": list(server.consumers),
                "capabilities": list(server.capabilities),
                "locality": server.boundary.locality,
                "data_egress": server.boundary.data_egress,
                "permissions": list(server.boundary.permissions),
                "risk": server.boundary.risk,
            }
        )
    return {
        "schema_version": registry.schema_version,
        "policy_version": registry.policy_version,
        "servers": servers,
    }


def process_matches(pid: int, create_time: float, fragments: list[str]) -> bool:
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - create_time) > 0.02:
            return False
        command = "\x00".join(process.cmdline()).casefold()
    except (psutil.Error, OSError):
        return False
    return all(fragment.casefold() in command for fragment in fragments)


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


@contextmanager
def _generation_guard(server_id: str, *, timeout_seconds: float = 2.0) -> Iterator[None]:
    """Serialize ownership-generation publication and compare-and-delete.

    The stable guard is deliberately not an ownership record.  It prevents a
    validated old lock from being replaced by a new generation between the
    comparison and unlink steps.
    """

    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = _guard_path(server_id)
    handle = path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("MCP ownership generation guard is busy")
                    time.sleep(0.01)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("MCP ownership generation guard is busy")
                    time.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _operation_process_lock(server_id: str) -> threading.Lock:
    with _OPERATION_LOCKS_MUTEX:
        return _OPERATION_PROCESS_LOCKS.setdefault(server_id, threading.Lock())


def _status_process_lock(server_id: str) -> threading.Lock:
    with _STATUS_LOCKS_MUTEX:
        return _STATUS_PROCESS_LOCKS.setdefault(server_id, threading.Lock())


@contextmanager
def _status_guard(server_id: str, *, timeout_seconds: float = 5.0) -> Iterator[None]:
    process_lock = _status_process_lock(server_id)
    if not process_lock.acquire(timeout=timeout_seconds):
        raise TimeoutError("MCP status guard is busy")
    handle = None
    try:
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        handle = status_guard_path(server_id).open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + timeout_seconds
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("MCP status guard is busy")
                    time.sleep(0.01)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("MCP status guard is busy")
                    time.sleep(0.01)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        try:
            if handle is not None:
                handle.close()
        finally:
            process_lock.release()


class OperationGuardLease:
    """One exact advisory operation lease; release is idempotent."""

    def __init__(self, handle, process_lock: threading.Lock) -> None:
        self._handle = handle
        self._process_lock = process_lock
        self._release_lock = threading.Lock()

    def release(self) -> None:
        with self._release_lock:
            handle = self._handle
            if handle is None:
                return
            self._handle = None
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                try:
                    handle.close()
                finally:
                    self._process_lock.release()


def try_acquire_operation_guard(server_id: str) -> OperationGuardLease | None:
    """Try once without blocking; callers own bounded wait/cancellation policy."""

    process_lock = _operation_process_lock(server_id)
    if not process_lock.acquire(blocking=False):
        return None
    handle = None
    try:
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        handle = operation_guard_path(server_id).open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return OperationGuardLease(handle, process_lock)
    except (OSError, BlockingIOError):
        try:
            if handle is not None:
                handle.close()
        finally:
            process_lock.release()
        return None


def acquire_operation_guard(
    server_id: str, *, timeout_seconds: float
) -> OperationGuardLease:
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        lease = try_acquire_operation_guard(server_id)
        if lease is not None:
            return lease
        if time.monotonic() >= deadline:
            raise TimeoutError("MCP operation gate reached its bounded wait")
        time.sleep(0.05)


def _valid_lock(payload: dict[str, Any] | None, server: ServerSpec) -> bool:
    return bool(
        payload
        and payload.get("schema_version") == "1.0"
        and payload.get("server_id") == server.id
        and payload.get("root_identity") == root_identity()
        and isinstance(payload.get("nonce_sha256"), str)
        and len(payload["nonce_sha256"]) == 64
        and isinstance(payload.get("owner_pid"), int)
        and isinstance(payload.get("owner_create_time"), (int, float))
    )


def _valid_owner(payload: dict[str, Any] | None, server: ServerSpec) -> bool:
    return bool(
        _valid_lock(payload, server)
        and isinstance(payload.get("child_pid"), int)
        and isinstance(payload.get("child_create_time"), (int, float))
        and isinstance(payload.get("child_command_sha256"), str)
        and len(payload["child_command_sha256"]) == 64
    )


def _launcher_fragments(server: ServerSpec) -> list[str]:
    return [
        str((ROOT / "services" / "mcp_hub" / "launcher.py").resolve()),
        "--server-id",
        server.id,
    ]


def owner_inventory(registry: McpRegistry) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for server in registry.servers:
        path = owner_path(server.id)
        lock = lock_path(server.id)
        lock_payload = _read_record(lock) if lock.is_file() else None
        if not path.is_file():
            if not lock.exists():
                continue
            if not _valid_lock(lock_payload, server):
                results.append({"server_id": server.id, "state": "invalid_lock"})
                continue
            owner_live = process_matches(
                lock_payload["owner_pid"],
                float(lock_payload["owner_create_time"]),
                _launcher_fragments(server),
            )
            results.append(
                {
                    "server_id": server.id,
                    "state": "owned_acquiring" if owner_live else "stale_lock",
                }
            )
            continue
        payload = _read_record(path)
        if (
            not _valid_owner(payload, server)
            or not _valid_lock(lock_payload, server)
            or payload["nonce_sha256"] != lock_payload["nonce_sha256"]
        ):
            results.append({"server_id": server.id, "state": "invalid_owner_record"})
            continue
        owner_live = process_matches(
            payload["owner_pid"],
            float(payload["owner_create_time"]),
            _launcher_fragments(server),
        )
        results.append(
            {
                "server_id": server.id,
                "state": "owned_running" if owner_live else "stale_owner_record",
            }
        )
    return results


def create_runtime_lock(server: ServerSpec, payload: dict[str, Any]) -> bool:
    """Publish one complete lock generation without ever exposing partial JSON."""

    if not _valid_lock(payload, server):
        raise ValueError("invalid MCP lock generation")
    target = lock_path(server.id)
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    pending = LOCK_DIR / (
        f".{server.id}.{os.getpid()}.{payload['nonce_sha256']}.pending"
    )
    encoded = (
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    descriptor: int | None = None
    published = False
    try:
        descriptor = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("failed to persist MCP ownership lock")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            with _generation_guard(server.id):
                try:
                    os.link(pending, target)
                    published = True
                except FileExistsError:
                    return False
        except TimeoutError:
            return False
        return True
    except BaseException:
        if published:
            remove_runtime_record(server.id, payload["nonce_sha256"])
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            if published:
                remove_runtime_record(server.id, payload["nonce_sha256"])
            raise


def promote_runtime_owner(server: ServerSpec, payload: dict[str, Any]) -> bool:
    """CAS-promote the acquired generation to a running owner record."""

    if not _valid_owner(payload, server):
        raise ValueError("invalid MCP owner generation")
    try:
        with _generation_guard(server.id):
            lock_payload = _read_record(lock_path(server.id))
            if (
                not _valid_lock(lock_payload, server)
                or lock_payload["nonce_sha256"] != payload["nonce_sha256"]
            ):
                return False
            write_json_atomic(owner_path(server.id), payload)
            return True
    except TimeoutError:
        return False


def remove_runtime_record(server_id: str, nonce_sha256: str) -> bool:
    """Compare-and-delete one ownership generation; never unlink a newer lock."""

    if not isinstance(nonce_sha256, str) or len(nonce_sha256) != 64:
        return False
    owner = owner_path(server_id)
    lock = lock_path(server_id)
    try:
        with _generation_guard(server_id):
            for path in (owner, lock):
                if not path.exists():
                    continue
                payload = _read_record(path)
                if payload is None or payload.get("nonce_sha256") != nonce_sha256:
                    return False
            # The generation guard prevents a replacement lock from appearing
            # between this comparison and the lock unlink.
            owner.unlink(missing_ok=True)
            lock.unlink(missing_ok=True)
            return True
    except TimeoutError:
        return False


def _command_hash(process: psutil.Process) -> str:
    return hashlib.sha256(
        "\x00".join(process.cmdline()).casefold().encode("utf-8")
    ).hexdigest()


def _exact_process(
    pid: int,
    create_time: float,
    *,
    fragments: list[str] | None = None,
    command_sha256: str | None = None,
) -> psutil.Process | None:
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - create_time) > 0.02:
            return None
        if fragments is not None:
            command = "\x00".join(process.cmdline()).casefold()
            if not all(fragment.casefold() in command for fragment in fragments):
                return None
        if command_sha256 is not None and _command_hash(process) != command_sha256:
            return None
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return process
    except (psutil.Error, OSError, ValueError, TypeError):
        return None


def _identity(process: psutil.Process) -> tuple[int, float] | None:
    try:
        return process.pid, process.create_time()
    except psutil.Error:
        return None


def _identity_alive(identity: tuple[int, float]) -> psutil.Process | None:
    return _exact_process(identity[0], identity[1])


def _capture_tree(root: psutil.Process) -> list[psutil.Process]:
    try:
        candidates = [*root.children(recursive=True), root]
    except psutil.Error:
        candidates = [root]
    unique: dict[tuple[int, float], psutil.Process] = {}
    for process in candidates:
        identity = _identity(process)
        if identity is not None:
            unique[identity] = process
    return list(unique.values())


def _owned_targets(server: ServerSpec, payload: dict[str, Any]) -> list[psutil.Process]:
    owner = _exact_process(
        int(payload["owner_pid"]),
        float(payload["owner_create_time"]),
        fragments=_launcher_fragments(server),
    )
    child = None
    if _valid_owner(payload, server):
        child = _exact_process(
            int(payload["child_pid"]),
            float(payload["child_create_time"]),
            command_sha256=str(payload["child_command_sha256"]),
        )
    roots = [process for process in (owner, child) if process is not None]
    targets: dict[tuple[int, float], psutil.Process] = {}
    for root in roots:
        for process in _capture_tree(root):
            identity = _identity(process)
            if identity is not None:
                targets[identity] = process
    return list(targets.values())


def owned_process_identities(registry: McpRegistry) -> set[tuple[int, float]]:
    """Return live process identities backed by the exact current owner generation."""

    identities: set[tuple[int, float]] = set()
    for server in registry.servers:
        lock_payload = _read_record(lock_path(server.id))
        if not _valid_lock(lock_payload, server):
            continue
        owner_payload = _read_record(owner_path(server.id))
        payload = lock_payload
        if owner_payload is not None:
            if (
                not _valid_owner(owner_payload, server)
                or owner_payload["nonce_sha256"] != lock_payload["nonce_sha256"]
            ):
                continue
            payload = owner_payload
        owner = _exact_process(
            int(payload["owner_pid"]),
            float(payload["owner_create_time"]),
            fragments=_launcher_fragments(server),
        )
        if owner is not None:
            identity = _identity(owner)
            if identity is not None:
                identities.add(identity)
        if _valid_owner(payload, server):
            child = _exact_process(
                int(payload["child_pid"]),
                float(payload["child_create_time"]),
                command_sha256=str(payload["child_command_sha256"]),
            )
            if child is not None:
                identity = _identity(child)
                if identity is not None:
                    identities.add(identity)
    return identities


def _terminate_owned_processes(
    targets: list[psutil.Process], *, shutdown_timeout_seconds: float
) -> bool:
    """Terminate, wait, force-kill and wait again; report exact survivors."""

    identities = [identity for process in targets if (identity := _identity(process))]
    if not identities:
        return True
    live = [process for identity in identities if (process := _identity_alive(identity))]
    for process in reversed(live):
        try:
            process.terminate()
        except psutil.Error:
            pass
    grace_timeout = max(0.1, float(shutdown_timeout_seconds) * 0.6)
    force_timeout = max(0.1, float(shutdown_timeout_seconds) - grace_timeout)
    psutil.wait_procs(live, timeout=grace_timeout)
    alive_after_grace = [
        process for identity in identities if (process := _identity_alive(identity))
    ]
    for process in alive_after_grace:
        try:
            process.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive_after_grace, timeout=force_timeout)
    return not any(_identity_alive(identity) for identity in identities)


def stop_owned_servers(registry: McpRegistry) -> dict[str, list[str]]:
    """Stop only processes proven by PID, creation time, root identity and command identity."""

    stopped: list[str] = []
    stale: list[str] = []
    refused: list[str] = []
    for server in registry.servers:
        path = owner_path(server.id)
        lock = lock_path(server.id)
        lock_payload = _read_record(lock) if lock.is_file() else None
        if not path.is_file():
            if not lock.exists():
                continue
            if not _valid_lock(lock_payload, server):
                refused.append(server.id)
                continue
            targets = _owned_targets(server, lock_payload)
            was_live = bool(targets)
            if was_live and not _terminate_owned_processes(
                targets,
                shutdown_timeout_seconds=server.lifecycle.shutdown_timeout_seconds,
            ):
                refused.append(server.id)
                continue
            if not remove_runtime_record(server.id, lock_payload["nonce_sha256"]):
                refused.append(server.id)
                continue
            (stopped if was_live else stale).append(server.id)
            continue
        payload = _read_record(path)
        if (
            not _valid_owner(payload, server)
            or not _valid_lock(lock_payload, server)
            or payload["nonce_sha256"] != lock_payload["nonce_sha256"]
        ):
            refused.append(server.id)
            continue
        targets = _owned_targets(server, payload)
        was_live = bool(targets)
        if was_live and not _terminate_owned_processes(
            targets,
            shutdown_timeout_seconds=server.lifecycle.shutdown_timeout_seconds,
        ):
            refused.append(server.id)
            continue
        if not remove_runtime_record(server.id, payload["nonce_sha256"]):
            refused.append(server.id)
            continue
        (stopped if was_live else stale).append(server.id)
    return {"stopped": stopped, "stale_cleaned": stale, "refused": refused}


def reap_stale_server(registry: McpRegistry, server: ServerSpec) -> dict[str, list[str]]:
    """Remove only a dead exact owner record; never interrupt a live consumer."""

    observed = next(
        (item for item in owner_inventory(registry) if item["server_id"] == server.id),
        None,
    )
    if observed is None or observed["state"] in {"owned_running", "owned_acquiring"}:
        return {"stopped": [], "stale_cleaned": [], "refused": []}
    if observed["state"] not in {"stale_owner_record", "stale_lock"}:
        return {"stopped": [], "stale_cleaned": [], "refused": [server.id]}
    narrowed = registry.model_copy(update={"servers": [server]})
    return stop_owned_servers(narrowed)
