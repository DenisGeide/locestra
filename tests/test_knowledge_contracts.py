from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from services.knowledge.contracts import (
    ImportRequestV1,
    ProvenanceV1,
    RetrievalRequestV1,
    SourceKind,
    SourceRegistrationV1,
)


def test_source_registration_requires_explicit_consent() -> None:
    with pytest.raises(ValidationError, match="explicit consent"):
        ImportRequestV1(
            registration=SourceRegistrationV1(
                project_path=r"C:\fixture",
                consent=False,
            ),
            source_path="README.md",
        )


def test_provenance_roundtrip_is_strict_and_aware() -> None:
    provenance = ProvenanceV1(
        generation_id="gen_abc",
        source_id="source_abc",
        source_uri="project://README.md",
        source_origin="repository",
        source_hash="a" * 64,
        source_size_bytes=123,
        source_mtime_ns=456,
        fragment_locator="lines:1-4",
        start_line=1,
        end_line=4,
        parser="markdown-parser",
        parser_version="1.0",
        derivation_version="1.0|fixture|chunk:1200",
        policy_version="fixture-policy",
        extraction_method="deterministic-chunk",
        observed_at=datetime.now(timezone.utc),
        project_commit_sha="b" * 40,
        sensitivity="internal",
        status="active",
    )
    assert ProvenanceV1.model_validate_json(provenance.model_dump_json()) == provenance
    with pytest.raises(ValidationError):
        ProvenanceV1.model_validate({**provenance.model_dump(), "unexpected": True})


def test_retrieval_contract_enforces_budget_and_source_types() -> None:
    request = RetrievalRequestV1(
        project_path=r"C:\fixture",
        query="database migration",
        allowed_source_types=[SourceKind.REPOSITORY_FILE, SourceKind.MARKDOWN],
        token_budget=512,
        max_fragments=4,
    )
    restored = RetrievalRequestV1.model_validate_json(request.model_dump_json())
    assert restored.token_budget == 512
    with pytest.raises(ValidationError):
        RetrievalRequestV1(
            project_path=r"C:\fixture",
            query="database migration",
            token_budget=1,
        )
