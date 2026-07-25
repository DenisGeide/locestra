from __future__ import annotations

import hashlib
import stat
import struct
from pathlib import Path

import pytest

from services.coding.public_preflight import (
    PublicDataPreflightError,
    build_public_data_snapshot,
)
from services.coding.git import CodingRepositoryError, validate_coding_git_config
from tests.coding_fixtures import coding_fixture


def _snapshot(repository: Path):
    return build_public_data_snapshot(repository, knowledge_blocked_files=0)


def _rewrite_sha1_index(path: Path, transform) -> None:
    payload = path.read_bytes()
    assert payload[:4] == b"DIRC"
    body = bytearray(payload[:-20])
    transform(body)
    path.write_bytes(bytes(body) + hashlib.sha1(body).digest())


def test_public_preflight_accepts_exact_https_fixture_and_binds_metadata():
    with coding_fixture(run_id="public-gate-positive") as fixture:
        fixture.git(["config", "branch.main.remote", "origin"])
        fixture.git(["config", "branch.main.merge", "refs/heads/main"])
        remote_url = fixture.git(["remote", "get-url", "origin"]).stdout.strip()
        assert remote_url == "https://example.invalid/local-agent-coding-fixture.git"

        first = _snapshot(fixture.repository)
        second = _snapshot(fixture.repository)

        assert first == second
        assert first.git_metadata_bytes > 0
        assert len(first.git_metadata_manifest_sha256) == 64
        fixture.assert_remote_unchanged()


def test_public_preflight_authenticates_files_omitted_from_knowledge_projection():
    with coding_fixture(run_id="public-gate-knowledge-omissions") as fixture:
        baseline = _snapshot(fixture.repository)
        projected = build_public_data_snapshot(
            fixture.repository,
            knowledge_blocked_files=4,
        )

        assert projected.knowledge_blocked_files == 4
        assert projected.tracked_manifest_sha256 == baseline.tracked_manifest_sha256
        assert (
            projected.git_object_manifest_sha256 == baseline.git_object_manifest_sha256
        )
        assert (
            projected.git_metadata_manifest_sha256
            == baseline.git_metadata_manifest_sha256
        )


def test_public_preflight_accepts_authenticated_normal_git_gc_metadata():
    with coding_fixture(run_id="public-gate-gc") as fixture:
        fixture.git(["gc", "--prune=now"])

        snapshot = _snapshot(fixture.repository)

        object_root = fixture.repository / ".git" / "objects"
        assert (object_root / "info" / "commit-graph").is_file()
        assert list((object_root / "pack").glob("*.pack"))
        assert snapshot.git_objects > 0


@pytest.mark.parametrize(
    "remote_url",
    [
        "C:/private/repository.git",
        "file:///C:/private/repository.git",
        "https://localhost/repository.git",
        "https://127.0.0.1/repository.git",
        "https://example.invalid/repository.git?token=public",
        "https://example.invalid/repository.git#fragment",
        "https://alice:password@example.invalid/repository.git",
        "ssh://git@example.invalid/repository.git",
        "ext::powershell -Command whoami",
    ],
)
def test_public_preflight_rejects_non_inert_remote_before_cloud_boundary(
    remote_url: str,
):
    with coding_fixture(run_id="public-gate-remote") as fixture:
        fixture.git(["remote", "set-url", "origin", remote_url])

        with pytest.raises(
            PublicDataPreflightError,
            match="Git config|remote|metadata scope",
        ):
            _snapshot(fixture.repository)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("alias.escape", "!powershell -Command whoami"),
        ("include.path", "C:/private/gitconfig"),
        ("core.fsmonitor", "true"),
        ("core.sshCommand", "powershell -Command whoami"),
        ("core.worktree", "C:/private"),
        ("core.excludesFile", "C:/private/ignore"),
        ("core.hooksPath", "C:/private/hooks"),
        ("diff.external", "powershell -Command whoami"),
        ("difftool.fixture.cmd", "powershell -Command whoami"),
        ("mergetool.fixture.cmd", "powershell -Command whoami"),
        ("filter.fixture.process", "powershell -Command whoami"),
        ("protocol.ext.allow", "always"),
        ("url.https://example.invalid/.insteadOf", "ext::"),
        ("credential.helper", "store"),
        ("http.sslCert", "C:/private/client.pem"),
        ("gpg.program", "powershell"),
        ("submodule.fixture.update", "!powershell -Command whoami"),
        ("trailer.issue.cmd", "powershell -Command whoami"),
        ("gc.recentObjectsHook", "powershell -Command whoami"),
        ("uploadpack.packObjectsHook", "powershell -Command whoami"),
        ("browser.fixture.cmd", "powershell -Command whoami"),
        ("man.fixture.cmd", "powershell -Command whoami"),
        ("sendemail.smtpPass", "not-a-public-setting"),
    ],
)
def test_public_preflight_fail_closed_config_allowlist(key: str, value: str):
    with coding_fixture(run_id="public-gate-config") as fixture:
        fixture.git(["config", "--local", key, value])

        with pytest.raises(
            PublicDataPreflightError,
            match="Git config|metadata scope",
        ):
            _snapshot(fixture.repository)


def test_public_preflight_nfkc_matches_tracked_private_path():
    with coding_fixture(run_id="public-gate-nfkc-tree") as fixture:
        disguised = fixture.repository / ".ｅｎｖ"
        disguised.write_text("ordinary-looking text\n", encoding="utf-8")
        fixture.git(["add", "--force", disguised.name])
        fixture.git(["commit", "-m", "add NFKC-equivalent private path"])

        with pytest.raises(PublicDataPreflightError, match="privacy-sensitive"):
            _snapshot(fixture.repository)


@pytest.mark.parametrize(
    "remote_url",
    [
        "git@github.com:openai/example.git",
        "ssh://git@github.com/openai/example.git",
        "C:/offline/example.git",
        "../offline/example.git",
    ],
)
def test_generic_git_gate_accepts_standard_offline_and_ssh_clone_config(
    remote_url: str,
):
    with coding_fixture(run_id="generic-git-config-positive") as fixture:
        fixture.git(["remote", "set-url", "origin", remote_url])
        fixture.git(["config", "branch.main.remote", "origin"])
        fixture.git(["config", "branch.main.merge", "refs/heads/main"])

        validate_coding_git_config(fixture.repository)


def test_generic_git_gate_reads_and_rejects_worktree_only_config():
    with coding_fixture(run_id="generic-git-worktree-config") as fixture:
        fixture.git(["config", "extensions.worktreeConfig", "true"])
        fixture.git(
            ["config", "--worktree", "alias.escape", "!powershell -Command whoami"]
        )

        with pytest.raises(CodingRepositoryError, match="not allowlisted"):
            validate_coding_git_config(fixture.repository)


@pytest.mark.parametrize("key", ["core.fsmonitor", "diff.external"])
def test_generic_git_gate_rejects_command_config_without_executing_marker(key: str):
    with coding_fixture(run_id="generic-git-config-marker") as fixture:
        marker = fixture.root / "config-command-ran.txt"
        command = fixture.root / "write-marker.cmd"
        command.write_text(
            f'@echo off\r\necho invoked>"{marker}"\r\n',
            encoding="utf-8",
        )
        fixture.git(["config", "--local", key, str(command)])

        with pytest.raises(CodingRepositoryError, match="not allowlisted"):
            validate_coding_git_config(fixture.repository)
        assert not marker.exists()


@pytest.mark.parametrize(
    "private_ref",
    [
        "refs/heads/ｃredentials",
        "refs/heads/public/．ssh",
    ],
)
def test_public_preflight_validates_nfkc_names_inside_packed_refs(private_ref: str):
    with coding_fixture(run_id="public-gate-packed-ref") as fixture:
        fixture.git(["pack-refs", "--all", "--prune"])
        packed_refs = fixture.repository / ".git" / "packed-refs"
        with packed_refs.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{fixture.baseline_sha} {private_ref}\n")

        with pytest.raises(PublicDataPreflightError, match="packed ref name"):
            _snapshot(fixture.repository)


def test_public_preflight_rejects_linked_admin_from_main_and_extra_linked_view():
    with coding_fixture(run_id="public-gate-linked-admin") as fixture:
        first = fixture.add_worktree("first")

        with pytest.raises(PublicDataPreflightError, match="additional linked"):
            _snapshot(fixture.repository)
        assert _snapshot(first.path).tracked_files > 0

        fixture.add_worktree("second")
        with pytest.raises(PublicDataPreflightError, match="additional linked"):
            _snapshot(first.path)


@pytest.mark.parametrize(
    "relative",
    [
        "info/opaque-control",
        "pack/opaque-control",
        "info/commit-graphs/opaque-control",
    ],
)
def test_public_preflight_rejects_unrecognized_object_control_file(relative: str):
    with coding_fixture(run_id="public-gate-object-control") as fixture:
        target = fixture.repository / ".git" / "objects" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"unaccounted object control payload\n")

        with pytest.raises(PublicDataPreflightError, match="object-storage control"):
            _snapshot(fixture.repository)


def test_public_preflight_rejects_authenticated_but_invalid_reverse_index_table():
    with coding_fixture(run_id="public-gate-reverse-index") as fixture:
        fixture.git(["gc", "--prune=now"])
        reverse_index = next(
            (fixture.repository / ".git" / "objects" / "pack").glob("*.rev")
        )
        payload = bytearray(reverse_index.read_bytes())
        assert payload[:4] == b"RIDX"
        payload[12:16] = payload[16:20]
        payload[-20:] = hashlib.sha1(payload[:-20]).digest()
        reverse_index.chmod(stat.S_IREAD | stat.S_IWRITE)
        reverse_index.write_bytes(payload)

        with pytest.raises(PublicDataPreflightError, match="reverse-index table"):
            _snapshot(fixture.repository)


def test_public_preflight_rejects_git_index_checksum_mismatch():
    with coding_fixture(run_id="public-gate-index-checksum") as fixture:
        index = fixture.repository / ".git" / "index"
        payload = bytearray(index.read_bytes())
        payload[-1] ^= 0x01
        index.write_bytes(payload)

        with pytest.raises(PublicDataPreflightError, match="index checksum"):
            _snapshot(fixture.repository)


def test_public_preflight_rejects_unknown_authenticated_git_index_extension():
    with coding_fixture(run_id="public-gate-index-extension") as fixture:
        index = fixture.repository / ".git" / "index"

        def append_unknown(body: bytearray) -> None:
            body.extend(b"ABCD" + struct.pack("!I", 4) + b"data")

        _rewrite_sha1_index(index, append_unknown)

        with pytest.raises(PublicDataPreflightError, match="index extension"):
            _snapshot(fixture.repository)


def test_public_preflight_rejects_unsupported_authenticated_git_index_version():
    with coding_fixture(run_id="public-gate-index-version") as fixture:
        index = fixture.repository / ".git" / "index"

        def change_version(body: bytearray) -> None:
            body[4:8] = struct.pack("!I", 4)

        _rewrite_sha1_index(index, change_version)

        with pytest.raises(PublicDataPreflightError, match="index v2"):
            _snapshot(fixture.repository)


def test_public_preflight_rejects_privacy_sensitive_git_index_entry():
    with coding_fixture(run_id="public-gate-index-path") as fixture:
        index = fixture.repository / ".git" / "index"

        def disguise_entry(body: bytearray) -> None:
            source = b"tests/test_calculator.py\0"
            target = b".kube/test_calculator.py\0"
            location = body.find(source)
            assert location >= 0 and len(source) == len(target)
            body[location : location + len(source)] = target

        _rewrite_sha1_index(index, disguise_entry)

        with pytest.raises(PublicDataPreflightError, match="Git index path"):
            _snapshot(fixture.repository)


def test_public_preflight_rejects_privacy_sensitive_cache_tree_entry():
    with coding_fixture(run_id="public-gate-cache-tree") as fixture:
        index = fixture.repository / ".git" / "index"

        def disguise_cache_tree(body: bytearray) -> None:
            tree = body.find(b"TREE")
            assert tree >= 0
            extension_size = struct.unpack("!I", body[tree + 4 : tree + 8])[0]
            start = tree + 8
            end = start + extension_size
            location = body.find(b"tests\0", start, end)
            assert location >= 0
            body[location : location + 6] = b".kube\0"

        _rewrite_sha1_index(index, disguise_cache_tree)

        with pytest.raises(PublicDataPreflightError, match="cache-tree"):
            _snapshot(fixture.repository)


def test_public_preflight_accepts_public_stage_zero_index_change():
    with coding_fixture(run_id="public-gate-index-binding") as fixture:
        before = _snapshot(fixture.repository)
        readme = fixture.repository / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8") + "\nPublic staged change.\n",
            encoding="utf-8",
        )
        fixture.git(["add", "README.md"])

        after = _snapshot(fixture.repository)

        assert after.changed_manifest_sha256 != before.changed_manifest_sha256
        assert after.git_metadata_manifest_sha256 != before.git_metadata_manifest_sha256
