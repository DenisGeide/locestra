"""Controlled, local-first memory engine.

The task journal remains the source of truth for active execution.  This
package stores only explicit, scoped and reviewable long-lived records.
"""

from services.memory.contracts import (
    ArchiveReferenceMetadataV1,
    ArchiveReferenceValueV1,
    MEMORY_RECORD_SCHEMA_VERSION,
    MemoryRecordType,
    MemoryRecordV1,
    MemoryRetention,
    MemoryScope,
    MemorySensitivity,
    MemorySourceV1,
    MemoryStatus,
    MemoryUpsertV1,
    OperationalStateValueV1,
    RetrievalItemV1,
    RetrievalResultV1,
    TaskHistoryValueV1,
)
from services.memory.store import MemoryStore

__all__ = [
    "MEMORY_RECORD_SCHEMA_VERSION",
    "ArchiveReferenceMetadataV1",
    "ArchiveReferenceValueV1",
    "MemoryRecordType",
    "MemoryRecordV1",
    "MemoryRetention",
    "MemoryScope",
    "MemorySensitivity",
    "MemorySourceV1",
    "MemoryStatus",
    "MemoryStore",
    "MemoryUpsertV1",
    "OperationalStateValueV1",
    "RetrievalItemV1",
    "RetrievalResultV1",
    "TaskHistoryValueV1",
]
