from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from services.memory.privacy import (
    FORBIDDEN_REFERENCE,
    REDACTED,
    MemoryPrivacyError,
    PrivacyAction,
    Sensitivity,
    inspect_memory_payload,
    sanitize_export_value,
    sanitize_reference,
    sanitize_task_metadata,
    sanitize_task_text,
    validate_memory_payload,
)


def _synthetic_secrets() -> list[str]:
    return [
        "sk-" + "A7b_" * 8,
        "ghp_" + "Aa7" * 12,
        "AKIA" + "A7B9" * 4,
        "xoxb-" + "A7b9-" * 6,
        "AIza" + "A7b_" * 8,
        "123456789:" + "A7b_" * 10,
        "eyJ" + "A7b_" * 4 + "." + "B8c_" * 4 + "." + "C9d_" * 4,
        "Bearer " + "A7b_" * 8,
        "postgresql://user:" + "A7b_" * 6 + "@example.invalid/db",
        "-----BEGIN " + "PRIVATE KEY-----\n" + "A7b9" * 12 + "\n-----END PRIVATE KEY-----",
    ]


@pytest.mark.parametrize("secret_value", _synthetic_secrets())
def test_known_secret_patterns_are_rejected_without_payload_in_error(secret_value: str) -> None:
    with pytest.raises(MemoryPrivacyError) as captured:
        validate_memory_payload("record.value", {"note": f"prefix {secret_value} suffix"})

    error = captured.value
    assert error.reason_code.startswith(("secret.", "source."))
    assert secret_value not in str(error)
    assert secret_value not in repr(error)
    assert all(secret_value not in reason for reason in error.reason_codes)


def test_structured_secret_field_and_split_token_are_rejected() -> None:
    with pytest.raises(MemoryPrivacyError) as field_error:
        validate_memory_payload("record.value", {"nested": {"password": "harmless-fixture"}})
    assert field_error.value.reason_code == "secret.sensitive_field"

    split_value = {"first": "sk-", "second": "A7b_" * 8}
    decision = inspect_memory_payload(split_value)
    assert decision.action is PrivacyAction.REJECT
    assert decision.sensitivity is Sensitivity.SECRET


def test_unknown_high_entropy_token_is_rejected_but_typed_identifiers_are_allowed() -> None:
    entropy_fixture = "Q7vN2xL9pR4mT8kW3zY6cF1hJ5sD0aB"
    with pytest.raises(MemoryPrivacyError) as captured:
        validate_memory_payload("record.value", {"note": entropy_fixture})
    assert captured.value.reason_code == "secret.high_entropy"
    assert entropy_fixture not in str(captured.value)

    safe = {
        "commit_sha": "a" * 40,
        "record_id": "123e4567-e89b-42d3-a456-426614174000",
    }
    validate_memory_payload("record.value", safe)


@pytest.mark.parametrize(
    "source_uri",
    [
        r"C:\project\.env",
        r"C:\Users\fixture\.ssh\id_rsa",
        r"C:\Users\fixture\.npmrc",
        r"C:\Users\fixture\Chrome\User Data\Default\Cookies",
        r"\\server\share\notes.txt",
        "https://user:"
        + "fixture-"
        + "password"
        + "@example.invalid/source",
    ],
)
def test_source_path_denylist_rejects_without_reading_or_echoing_path(source_uri: str) -> None:
    with pytest.raises(MemoryPrivacyError) as captured:
        validate_memory_payload("record.value", "safe preference", source_uri=source_uri)
    assert captured.value.reason_code.startswith("source.")
    assert source_uri not in str(captured.value)
    assert sanitize_reference(source_uri) == FORBIDDEN_REFERENCE


@pytest.mark.parametrize(
    "source_uri",
    [
        "project://.env",
        "project://../README.md",
        "project://%2e%2e/README.md",
        "project://%252e%252e/README.md",
        "project://C%253A%252Ffixture%252FREADME.md",
        "https%253A%252F%252Fuser%253Afixture-password%2540example.invalid%252Fsource",
        "local-file://server/share/notes.txt",
        "local-file:////server/share/notes.txt",
        "local-file:%252F%252F%252F%252Fserver%252Fshare%252Fnotes.txt",
        "project://.ssh",
        "project://.aws",
        "local-file://C:/Users/fixture/.ssh",
        "project://.env%20",
        "project://credentials.json%20",
        "project://id_rsa%2e",
        "project://.env::$DATA",
        "project://credentials.json::$DATA",
        "project://id_rsa::$DATA",
        "local-file://C:/synthetic/.env::$DATA",
    ],
)
def test_encoded_source_paths_and_credentials_fail_closed(source_uri: str) -> None:
    with pytest.raises(MemoryPrivacyError) as captured:
        validate_memory_payload("record.value", "safe preference", source_uri=source_uri)

    assert captured.value.reason_code.startswith("source.")
    assert source_uri not in str(captured.value)
    assert sanitize_reference(source_uri) == FORBIDDEN_REFERENCE


def test_public_placeholder_source_and_env_example_are_not_overblocked() -> None:
    validate_memory_payload(
        "record.value",
        {"credential_policy": "Use placeholders only."},
        source_uri=r"C:\project\.env.example",
    )
    assert sanitize_reference("docs/security-policy.md") == "docs/security-policy.md"


def _secret_bearing_source_uris() -> list[tuple[str, str]]:
    synthetic = "sk-" + "A7b_" * 8
    encoded_authorization = quote("Authorization: Bearer " + synthetic, safe="")
    double_encoded_assignment = quote(
        quote("access_token=" + synthetic, safe=""),
        safe="",
    )
    return [
        (
            "https://example.invalid/docs?access_token=" + synthetic,
            "source.query_secret_forbidden",
        ),
        (
            "https://example.invalid/docs?note=" + encoded_authorization,
            "source.query_secret_forbidden",
        ),
        (
            "https://example.invalid/docs?payload=" + double_encoded_assignment,
            "source.query_secret_forbidden",
        ),
        (
            "https://example.invalid/docs#access_token=" + synthetic,
            "source.fragment_secret_forbidden",
        ),
        (
            "https://example.invalid/docs#" + quote("token=" + synthetic, safe=""),
            "source.fragment_secret_forbidden",
        ),
    ]


@pytest.mark.parametrize("source_uri,reason_code", _secret_bearing_source_uris())
def test_source_url_query_and_fragment_secrets_are_rejected_after_decoding(
    source_uri: str,
    reason_code: str,
) -> None:
    with pytest.raises(MemoryPrivacyError) as captured:
        validate_memory_payload("record.value", "safe preference", source_uri=source_uri)

    assert captured.value.reason_code == reason_code
    assert source_uri not in str(captured.value)
    assert sanitize_reference(source_uri) == FORBIDDEN_REFERENCE


@pytest.mark.parametrize(
    "source_uri",
    [
        "https://example.invalid/docs?page=2&lang=ru#install",
        (
            "https://example.invalid/commit?sha="
            + "a" * 40
            + "&request_id=123e4567-e89b-42d3-a456-426614174000#token-authentication"
        ),
        "https://example.invalid/docs?topic=token-authentication&access_token=redacted",
    ],
)
def test_normal_source_url_query_and_fragment_remain_allowed(source_uri: str) -> None:
    validate_memory_payload("record.value", "safe preference", source_uri=source_uri)
    assert sanitize_reference(source_uri) == source_uri


def test_nfkc_and_control_normalization_is_central_and_deterministic() -> None:
    value = {"language": "Ｒｕｓｓｉａｎ\u200b", "style": "brief\x00"}
    exported = sanitize_export_value(value)
    assert exported == {"language": "Russian", "style": "brief"}


def test_task_text_redacts_and_truncates_without_raising() -> None:
    secret_value = "sk-" + "A7b_" * 8
    text = "Authorization: Bearer " + secret_value + "\n" + "x" * 10_000
    sanitized = sanitize_task_text(text, "prompt")

    assert secret_value not in sanitized
    assert REDACTED in sanitized
    assert len(sanitized) <= 2_048
    assert sanitize_task_text(object(), "prompt") == "[OMITTED:privacy-limit]"  # type: ignore[arg-type]


def test_task_metadata_recursively_redacts_and_handles_cycles_nonblocking() -> None:
    secret_value = "ghp_" + "Aa7" * 12
    metadata: dict[str, object] = {
        "safe": "kept",
        "nested": {"api_key": secret_value, "output": f"token={secret_value}"},
    }
    metadata["cycle"] = metadata

    sanitized = sanitize_task_metadata(metadata)
    serialized = json.dumps(sanitized, ensure_ascii=False)
    assert sanitized["safe"] == "kept"
    assert secret_value not in serialized
    assert REDACTED in serialized
    assert "[OMITTED:cyclic]" in serialized


def test_export_defense_rejects_legacy_secret_and_returns_normalized_safe_value() -> None:
    secret_value = "xoxb-" + "A7b9-" * 6
    with pytest.raises(MemoryPrivacyError) as captured:
        sanitize_export_value({"legacy": secret_value})
    assert secret_value not in str(captured.value)

    assert sanitize_export_value({"preference": "  concise  "}) == {
        "preference": "  concise  "
    }


def test_bounded_structured_payloads_fail_with_payload_free_reason() -> None:
    oversized = "safe words " * 5_000
    with pytest.raises(MemoryPrivacyError) as captured:
        validate_memory_payload("record.value", {"note": oversized})
    assert captured.value.reason_code == "content.too_large"
    assert oversized[:100] not in str(captured.value)

    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(MemoryPrivacyError) as cyclic_error:
        validate_memory_payload("record.value", cyclic)
    assert cyclic_error.value.reason_code == "content.cyclic"


def test_invalid_subject_and_unsupported_value_never_echo_input() -> None:
    private_subject = "not allowed subject with spaces"
    with pytest.raises(MemoryPrivacyError) as subject_error:
        validate_memory_payload(private_subject, "safe")
    assert subject_error.value.reason_code == "privacy.subject_invalid"
    assert private_subject not in str(subject_error.value)

    with pytest.raises(MemoryPrivacyError) as binary_error:
        validate_memory_payload("record.value", b"binary-fixture")
    assert binary_error.value.reason_code == "content.binary_forbidden"
