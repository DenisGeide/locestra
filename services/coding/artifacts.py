from __future__ import annotations

import json
import os
import re
import secrets
import stat
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from services.common import RUN_DIR
from services.coding.config import CodingPolicy, get_coding_policy
from services.coding.contracts import ArtifactKind, ArtifactReferenceV1
from services.knowledge.privacy import detect_secret
from services.memory.privacy import sanitize_task_text


class ArtifactPolicyError(RuntimeError):
    pass


_SAFE_PRODUCER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_OCCURRENCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ARTIFACT_ID_DOMAIN = b"local-agent-artifact-id-v2"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class ArtifactStore:
    def __init__(
        self,
        task_id: str,
        *,
        root: Path | None = None,
        policy: CodingPolicy | None = None,
    ) -> None:
        if not task_id or len(task_id) > 64 or not all(char.isalnum() or char in "._-" for char in task_id):
            raise ArtifactPolicyError("invalid task id")
        self.task_id = task_id
        self.policy = policy or get_coding_policy()
        self.task_root = (root or RUN_DIR / "coding" / "tasks") / task_id
        self.artifact_root = self.task_root / "artifacts"
        self._references: dict[str, ArtifactReferenceV1] = {}
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        marker = self.task_root / "owner.json"
        expected = {
            "schema_version": "1.0",
            "task_id": task_id,
            "canonical_root": str(self.task_root.resolve(strict=True)),
        }
        if marker.exists():
            try:
                existing = json.loads(marker.read_text(encoding="utf-8", errors="strict"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ArtifactPolicyError("task artifact marker is unreadable") from exc
            if existing != expected:
                raise ArtifactPolicyError("task artifact marker ownership mismatch")
        else:
            _atomic_write(marker, json.dumps(expected, sort_keys=True).encode("utf-8"))

    @staticmethod
    def _artifact_id(
        *,
        kind: ArtifactKind,
        payload_sha256: bytes,
        suffix: str,
        media_type: str,
        producer: str,
        occurrence_id: str | None,
    ) -> str:
        """Return a provenance-scoped, content-addressed artifact identity.

        Payload-only IDs allow two independent evidence occurrences with equal
        bytes (for example, two silent verification commands) to alias.  They
        also allow equal bytes published with different producer/media metadata
        to overwrite the in-memory reference for the first role.  Length-framed
        metadata keeps the identity unambiguous while ``ArtifactReferenceV1``
        continues to authenticate the exact payload with its full SHA-256.
        """

        digest = sha256()
        digest.update(_ARTIFACT_ID_DOMAIN)
        occurrence = (
            b"\x00"
            if occurrence_id is None
            else b"\x01" + occurrence_id.encode("ascii")
        )
        components = (
            kind.value.encode("ascii"),
            producer.encode("ascii"),
            occurrence,
            suffix.casefold().encode("ascii"),
            media_type.encode("ascii"),
            payload_sha256,
        )
        for component in components:
            digest.update(len(component).to_bytes(8, byteorder="big"))
            digest.update(component)
        # 128 identity bits fit the v1 opaque-ID contract and avoid the 64-bit
        # collision surface of the previous truncated payload-only digest.
        digest_text = digest.hexdigest()[:32]
        return f"{kind.value}-{digest_text}"

    def write_bytes(
        self,
        *,
        kind: ArtifactKind,
        payload: bytes,
        suffix: str,
        media_type: str,
        producer: str,
        occurrence_id: str | None = None,
        secret_scan: bool = True,
        maximum: int | None = None,
    ) -> ArtifactReferenceV1:
        limit = maximum or self.policy.max_artifact_bytes
        if len(payload) > limit:
            raise ArtifactPolicyError("artifact exceeds the configured size limit")
        if secret_scan:
            finding = detect_secret(payload)
            if finding:
                raise ArtifactPolicyError(f"artifact blocked by privacy policy: {finding}")
        if not suffix.startswith(".") or not suffix[1:].isalnum():
            raise ArtifactPolicyError("artifact suffix is invalid")
        normalized_suffix = suffix.casefold()
        if not normalized_suffix.isascii():
            raise ArtifactPolicyError("artifact suffix must be ASCII")
        if not isinstance(media_type, str) or not 1 <= len(media_type) <= 128:
            raise ArtifactPolicyError("artifact media type is invalid")
        if not media_type.isascii() or any(char in media_type for char in "\r\n\x00"):
            raise ArtifactPolicyError("artifact media type must be bounded ASCII")
        if not isinstance(producer, str) or not _SAFE_PRODUCER.fullmatch(producer):
            raise ArtifactPolicyError("artifact producer is invalid")
        if occurrence_id is not None and (
            not isinstance(occurrence_id, str)
            or not _SAFE_OCCURRENCE_ID.fullmatch(occurrence_id)
        ):
            raise ArtifactPolicyError("artifact occurrence id is invalid")
        payload_digest = sha256(payload)
        artifact_id = self._artifact_id(
            kind=kind,
            payload_sha256=payload_digest.digest(),
            suffix=normalized_suffix,
            media_type=media_type,
            producer=producer,
            occurrence_id=occurrence_id,
        )
        path = self.artifact_root / f"{artifact_id}{normalized_suffix}"
        if path.exists():
            if path.read_bytes() != payload:
                raise ArtifactPolicyError("artifact identity collision")
        else:
            _atomic_write(path, payload)
        reference = ArtifactReferenceV1(
            artifact_id=artifact_id,
            kind=kind,
            path=str(path.resolve(strict=True)),
            sha256=payload_digest.hexdigest(),
            size_bytes=len(payload),
            media_type=media_type,
            producer=producer,
            created_at=datetime.now(timezone.utc),
        )
        self._references[reference.artifact_id] = reference
        return reference

    def reference(self, artifact_id: str) -> ArtifactReferenceV1:
        try:
            return self._references[artifact_id]
        except KeyError as exc:
            raise ArtifactPolicyError("artifact reference is not available in this task process") from exc

    def write_text(
        self,
        *,
        kind: ArtifactKind,
        text: str,
        producer: str,
        occurrence_id: str | None = None,
        suffix: str = ".txt",
        redact: bool = False,
        maximum: int | None = None,
    ) -> ArtifactReferenceV1:
        raw = text.encode("utf-8")
        content = (
            sanitize_task_text(text, kind.value)
            if redact and detect_secret(raw)
            else text
        )
        return self.write_bytes(
            kind=kind,
            payload=content.encode("utf-8"),
            suffix=suffix,
            media_type="text/plain; charset=utf-8",
            producer=producer,
            occurrence_id=occurrence_id,
            secret_scan=True,
            maximum=maximum,
        )

    def write_json(
        self,
        *,
        kind: ArtifactKind,
        value: object,
        producer: str,
        occurrence_id: str | None = None,
        maximum: int | None = None,
    ) -> ArtifactReferenceV1:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        return self.write_bytes(
            kind=kind,
            payload=payload,
            suffix=".json",
            media_type="application/json",
            producer=producer,
            occurrence_id=occurrence_id,
            secret_scan=True,
            maximum=maximum,
        )

    def read_verified(self, artifact: ArtifactReferenceV1) -> bytes:
        """Read and authenticate an owned artifact from one open file handle.

        Hashing and parsing separate reads leaves a substitution window.  This
        method opens the canonical owned path once, bounds the read by the
        signed size, and verifies the exact bytes returned to the caller.
        """

        path = Path(artifact.path)
        try:
            root = self.artifact_root.resolve(strict=True)
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
            if path.is_symlink():
                raise ArtifactPolicyError("artifact path must not be a symbolic link")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(resolved, flags)
            with os.fdopen(descriptor, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ArtifactPolicyError("artifact is not a regular file")
                payload = stream.read(artifact.size_bytes + 1)
                after = os.fstat(stream.fileno())
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ArtifactPolicyError("artifact changed while it was being read")
        except ArtifactPolicyError:
            raise
        except (OSError, ValueError) as exc:
            raise ArtifactPolicyError("artifact ownership validation failed") from exc
        if len(payload) != artifact.size_bytes:
            raise ArtifactPolicyError("artifact size validation failed")
        if sha256(payload).hexdigest() != artifact.sha256:
            raise ArtifactPolicyError("artifact hash validation failed")
        return payload

    def verify(self, artifact: ArtifactReferenceV1) -> bool:
        try:
            self.read_verified(artifact)
        except ArtifactPolicyError:
            return False
        return True
