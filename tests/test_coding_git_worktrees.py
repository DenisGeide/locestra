from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from services.coding import git as coding_git
from services.coding import worktrees as worktree_module
from services.coding.git import (
    CodingRepositoryError,
    applicable_agent_rules,
    git_environment,
    git_status_paths,
    resolve_repository,
    run_git,
)
from services.coding.worktrees import WorktreeError, WorktreeManager
from tests.coding_fixtures import coding_fixture, file_snapshot


def _manager(fixture) -> WorktreeManager:
    return WorktreeManager(
        registry_root=fixture.root / "manager-registry",
        owned_worktree_root=fixture.root / "manager-worktrees",
    )


def _expected_owned_path(
    manager: WorktreeManager,
    repository: Path,
    task_id: str,
) -> Path:
    source_hash = hashlib.sha256(
        str(repository).casefold().encode("utf-8")
    ).hexdigest()[:16]
    task_hash = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:12]
    return (manager.owned_worktree_root / source_hash / f"task-{task_hash}").resolve(
        strict=False
    )


def _branch_target(repository: Path, branch: str) -> str | None:
    result = run_git(
        repository,
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False,
    )
    if result.returncode == 1:
        return None
    assert result.returncode == 0
    return (
        run_git(repository, ["rev-parse", "--verify", f"refs/heads/{branch}"])
        .stdout.decode("ascii")
        .strip()
    )


@pytest.mark.parametrize("status_code", [b"R ", b"C "])
def test_porcelain_z_rename_and_copy_preserve_both_destination_and_source(
    monkeypatch: pytest.MonkeyPatch,
    status_code: bytes,
):
    def fake_run_git(repository, arguments, **kwargs):
        stdout = (
            b""
            if arguments[0] == "config"
            else status_code + b" forbidden/destination.py\0src/allowed.py\0"
        )
        return subprocess.CompletedProcess(arguments, 0, stdout, b"")

    monkeypatch.setattr(coding_git, "run_git", fake_run_git)
    monkeypatch.setattr(coding_git, "validate_coding_git_config", lambda _repository: None)

    assert coding_git.git_status_paths(Path("C:/synthetic")) == [
        "forbidden/destination.py",
        "src/allowed.py",
    ]


def test_zero_terminated_paths_preserve_security_relevant_whitespace():
    with coding_fixture(run_id="git-leading-whitespace-path") as fixture:
        target = fixture.repository / " src" / "evil.py"
        target.parent.mkdir()
        target.write_text("print('outside declared src scope')\n", encoding="utf-8")

        assert git_status_paths(fixture.repository) == [" src/evil.py"]


def test_external_git_filter_is_rejected_before_resolve_or_worktree_checkout():
    with coding_fixture(run_id="git-host-filter-rce") as fixture:
        marker = fixture.root / "FILTER_EXECUTED.txt"
        script = fixture.repository / "filter_driver.py"
        script.write_text(
            "import pathlib, sys\n"
            "pathlib.Path(sys.argv[1]).write_text('EXECUTED', encoding='utf-8')\n"
            "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
            encoding="utf-8",
        )
        (fixture.repository / ".gitattributes").write_text(
            "*.md filter=hostpwn\n",
            encoding="utf-8",
        )
        fixture.git(["add", ".gitattributes", "filter_driver.py"])
        fixture.git(["commit", "-m", "add inert filter fixture"])
        identity = resolve_repository(str(fixture.repository))
        command = f'"{sys.executable}" "{script}" "{marker}"'
        fixture.git(["config", "filter.hostpwn.smudge", command])

        with pytest.raises(CodingRepositoryError, match="command/filter drivers"):
            resolve_repository(str(fixture.repository))
        with pytest.raises(WorktreeError, match="failed to create"):
            _manager(fixture).create(task_id="filter-rce-task", repository=identity)

        assert not marker.exists()


def test_replace_ref_cannot_change_coding_head_and_is_rejected_on_mutation():
    with coding_fixture(run_id="git-replace-ref") as fixture:
        baseline_tree = fixture.git(["rev-parse", "HEAD^{tree}"]).stdout.strip()
        before = resolve_repository(str(fixture.repository))
        target = fixture.repository / "README.md"
        target.write_text("malicious replacement tree\n", encoding="utf-8")
        fixture.git(["add", "README.md"])
        fixture.git(["commit", "-m", "replacement payload"])
        replacement = fixture.git(["rev-parse", "HEAD"]).stdout.strip()
        fixture.git(["reset", "--hard", fixture.baseline_sha])
        fixture.git(["replace", fixture.baseline_sha, replacement])

        assert fixture.git(["rev-parse", "HEAD^{tree}"]).stdout.strip() != baseline_tree
        assert (
            run_git(fixture.repository, ["rev-parse", "HEAD^{tree}"])
            .stdout.decode("ascii")
            .strip()
            == baseline_tree
        )
        assert git_environment()["GIT_NO_REPLACE_OBJECTS"] == "1"
        with pytest.raises(
            CodingRepositoryError,
            match="repository scope failed canonical Git validation|replacement refs",
        ):
            resolve_repository(str(fixture.repository))
        assert before.base_commit == fixture.baseline_sha


def test_linked_worktree_rejects_legacy_graft_in_common_git_directory():
    with coding_fixture(run_id="git-linked-common-graft") as fixture:
        identity = resolve_repository(str(fixture.repository))
        manager = _manager(fixture)
        record = manager.create(task_id="common-graft-task", repository=identity)
        grafts = fixture.repository / ".git" / "info" / "grafts"
        grafts.parent.mkdir(exist_ok=True)
        grafts.write_text(f"{fixture.baseline_sha}\n", encoding="ascii")

        with pytest.raises(CodingRepositoryError, match="grafts"):
            git_status_paths(Path(record.worktree_path))


def test_nested_loose_object_hardlink_is_rejected_before_repository_use():
    with coding_fixture(run_id="git-loose-object-hardlink") as fixture:
        external = fixture.root / "external-object-payload"
        external.write_bytes(b"not a Git object")
        fanout = fixture.repository / ".git" / "objects" / "aa"
        fanout.mkdir(exist_ok=True)
        nested = fanout / ("b" * 38)
        os.link(external, nested)

        with pytest.raises(
            CodingRepositoryError,
            match="repository scope failed canonical Git validation",
        ):
            resolve_repository(str(fixture.repository))

        assert external.read_bytes() == b"not a Git object"


@pytest.mark.parametrize("kind", ["missing", "file", "nongit"])
def test_explicit_invalid_repository_never_falls_back(tmp_path: Path, kind: str):
    candidate = tmp_path / kind
    if kind == "file":
        candidate.write_text("not a repository", encoding="utf-8")
    elif kind == "nongit":
        candidate.mkdir()

    with pytest.raises(CodingRepositoryError, match="explicit repository|Git operation"):
        resolve_repository(str(candidate.resolve()))


def test_nested_path_resolves_canonical_root_and_reads_applicable_agents_hierarchy():
    with coding_fixture(run_id="nested-rules") as fixture:
        identity = resolve_repository(str(fixture.repository / "src"))

        assert identity.canonical_root == fixture.repository
        assert identity.base_commit == fixture.baseline_sha
        rules = applicable_agent_rules(
            identity.canonical_root,
            ["src/calculator.py"],
        )
        assert [path.relative_to(identity.canonical_root).as_posix() for path in rules] == [
            "AGENTS.md",
            "src/AGENTS.md",
        ]


@pytest.mark.parametrize(
    "mutation",
    [
        "remote-config",
        "tag",
        "remote-tracking-ref",
        "unrelated-local-branch",
        "owned-prefix-user-branch",
    ],
)
def test_repository_identity_binds_protected_shared_git_metadata(mutation: str):
    with coding_fixture(run_id=f"git-metadata-{mutation}") as fixture:
        user_branch = (
            "local-agent/task-user-owned"
            if mutation == "owned-prefix-user-branch"
            else "user-feature"
        )
        if mutation in {"unrelated-local-branch", "owned-prefix-user-branch"}:
            fixture.git(["branch", user_branch, fixture.baseline_sha])
        before = resolve_repository(str(fixture.repository))

        if mutation == "remote-config":
            fixture.git(
                [
                    "remote",
                    "set-url",
                    "origin",
                    str(fixture.root / "unexpected-remote.git"),
                ]
            )
        elif mutation == "tag":
            fixture.git(["tag", "unexpected-tag", fixture.baseline_sha])
        elif mutation == "remote-tracking-ref":
            fixture.git(
                [
                    "update-ref",
                    "refs/remotes/origin/unexpected",
                    fixture.baseline_sha,
                ]
            )
        else:
            fixture.git(["branch", "-D", user_branch])

        after = resolve_repository(str(fixture.repository))

        assert after.base_commit == before.base_commit
        assert after.dirty_paths == before.dirty_paths == ()
        assert after.git_metadata_fingerprint != before.git_metadata_fingerprint
        assert after.dirty_fingerprint != before.dirty_fingerprint


def test_dirty_source_is_preserved_while_task_worktree_starts_from_clean_baseline():
    with coding_fixture(run_id="dirty-source") as fixture:
        readme = fixture.repository / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nUSER_DIRTY_SENTINEL\n", encoding="utf-8")
        (fixture.repository / "user-untracked.txt").write_text(
            "USER_UNTRACKED_SENTINEL", encoding="utf-8"
        )
        before = file_snapshot(fixture.repository)
        identity = resolve_repository(str(fixture.repository))
        assert set(identity.dirty_paths) == {"README.md", "user-untracked.txt"}

        manager = _manager(fixture)
        record = manager.create(task_id="dirty-source-task", repository=identity)
        task_path = Path(record.worktree_path)

        assert git_status_paths(task_path) == []
        assert "USER_DIRTY_SENTINEL" not in (task_path / "README.md").read_text(encoding="utf-8")
        assert not (task_path / "user-untracked.txt").exists()
        assert file_snapshot(fixture.repository) == before
        assert resolve_repository(str(fixture.repository)).base_commit == fixture.baseline_sha

        manager.complete(record.task_id)
        removed = manager.cleanup(record.task_id)
        assert removed.status == "removed"
        assert not task_path.exists()
        fixture.assert_remote_unchanged()


def test_branch_collision_is_suffixed_and_active_or_dirty_cleanup_fails_closed():
    with coding_fixture(run_id="collision-cleanup") as fixture:
        identity = resolve_repository(str(fixture.repository))
        manager = _manager(fixture)
        task_id = "collision-task"
        first_candidate = manager._safe_branch(task_id, identity)
        fixture.git(["branch", first_candidate, identity.base_commit])

        record = manager.create(task_id=task_id, repository=identity)
        task_path = Path(record.worktree_path)
        assert record.branch == f"{first_candidate}-1"

        with pytest.raises(WorktreeError, match="completed"):
            manager.cleanup(task_id)
        assert task_path.exists()

        (task_path / "dirty.txt").write_text("owned dirty fixture", encoding="utf-8")
        manager.complete(task_id)
        blocked = manager.cleanup(task_id)
        assert blocked.status == "cleanup_blocked"
        assert task_path.exists()

        (task_path / "dirty.txt").unlink()
        manager.complete(task_id)
        removed = manager.cleanup(task_id)
        assert removed.status == "removed"
        assert not task_path.exists()


def test_owned_root_marker_and_unregistered_path_collision_are_fail_closed():
    with coding_fixture(run_id="ownership-marker") as fixture:
        identity = resolve_repository(str(fixture.repository))
        manager = _manager(fixture)

        source_hash = hashlib.sha256(
            str(identity.canonical_root).casefold().encode("utf-8")
        ).hexdigest()[:16]
        task_hash = hashlib.sha256(b"occupied-task").hexdigest()[:12]
        occupied = manager.owned_worktree_root / source_hash / f"task-{task_hash}"
        occupied.mkdir(parents=True)
        sentinel = occupied / "keep.txt"
        sentinel.write_text("do not delete", encoding="utf-8")

        with pytest.raises(WorktreeError, match="already exists"):
            manager.create(task_id="occupied-task", repository=identity)
        assert sentinel.read_text(encoding="utf-8") == "do not delete"

        marker = manager.owned_worktree_root / ".local-agent-owned.json"
        marker.write_text(json.dumps({"tampered": True}), encoding="utf-8")
        with pytest.raises(WorktreeError, match="marker"):
            WorktreeManager(
                registry_root=manager.registry_root,
                owned_worktree_root=manager.owned_worktree_root,
            )
        assert sentinel.exists()


def test_create_record_write_failure_compensates_exact_worktree_and_branch(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="worktree-record-write-failure") as fixture:
        identity = resolve_repository(str(fixture.repository))
        manager = _manager(fixture)
        task_id = "record-write-failure"
        branch = manager._safe_branch(task_id, identity)
        worktree = _expected_owned_path(
            manager,
            identity.canonical_root,
            task_id,
        )
        source_before = file_snapshot(fixture.repository)

        def fail_record_write(_record, **_kwargs) -> None:
            raise OSError("injected registry write failure")

        monkeypatch.setattr(manager, "_write_record", fail_record_write)

        with pytest.raises(WorktreeError, match="failed to create"):
            manager.create(task_id=task_id, repository=identity)

        assert manager.load(task_id) is None
        assert not worktree.exists()
        assert _branch_target(fixture.repository, branch) is None
        assert list(manager.create_intents_dir.iterdir()) == []
        assert file_snapshot(fixture.repository) == source_before
        assert fixture.git(["rev-parse", "HEAD"]).stdout.strip() == fixture.baseline_sha
        fixture.assert_remote_unchanged()


def test_post_add_validation_failure_uses_intent_bound_compensation(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="worktree-post-add-validation") as fixture:
        identity = resolve_repository(str(fixture.repository))
        manager = _manager(fixture)
        task_id = "post-add-validation-failure"
        branch = manager._safe_branch(task_id, identity)
        worktree = _expected_owned_path(
            manager,
            identity.canonical_root,
            task_id,
        )
        source_before = file_snapshot(fixture.repository)

        def fail_validation(intent):
            raise WorktreeError("injected post-add validation failure")

        monkeypatch.setattr(manager, "_validate_created_worktree", fail_validation)

        with pytest.raises(WorktreeError, match="failed to create"):
            manager.create(task_id=task_id, repository=identity)

        assert manager.load(task_id) is None
        assert not worktree.exists()
        assert _branch_target(fixture.repository, branch) is None
        assert list(manager.create_intents_dir.iterdir()) == []
        assert file_snapshot(fixture.repository) == source_before
        fixture.assert_remote_unchanged()


def test_stale_clean_crash_window_intent_is_safely_compensated(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="worktree-clean-crash-recovery") as fixture:
        identity = resolve_repository(str(fixture.repository))
        manager = _manager(fixture)
        task_id = "clean-crash-window"
        branch = manager._safe_branch(task_id, identity)
        worktree = _expected_owned_path(
            manager,
            identity.canonical_root,
            task_id,
        )
        source_before = file_snapshot(fixture.repository)

        def crash_before_record(_record, **_kwargs) -> None:
            raise SystemExit("simulated process crash")

        monkeypatch.setattr(manager, "_write_record", crash_before_record)
        with pytest.raises(SystemExit, match="simulated process crash"):
            manager.create(task_id=task_id, repository=identity)

        assert manager.load(task_id) is None
        assert worktree.is_dir()
        assert _branch_target(fixture.repository, branch) == fixture.baseline_sha
        assert manager.creation_intent_status()["added"] == 1

        recovery = WorktreeManager(
            registry_root=manager.registry_root,
            owned_worktree_root=manager.owned_worktree_root,
        )
        monkeypatch.setattr(worktree_module.psutil, "pid_exists", lambda _pid: False)
        report = recovery.recover_creation_intents()

        assert report.compensated == 1
        assert report.finalized == report.unresolved == report.invalid == 0
        assert report.orphaned_records == ()
        assert recovery.creation_intent_status()["pending"] == 0
        assert recovery.load(task_id) is None
        assert not worktree.exists()
        assert _branch_target(fixture.repository, branch) is None
        assert file_snapshot(fixture.repository) == source_before
        fixture.assert_remote_unchanged()


def test_live_exact_owner_identity_prevents_recovery_during_delayed_worktree_add(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="worktree-live-create-owner") as fixture:
        identity = resolve_repository(str(fixture.repository))
        policy = worktree_module.get_coding_policy().model_copy(
            update={"lease_stale_seconds": 10}
        )
        manager = WorktreeManager(
            registry_root=fixture.root / "manager-registry",
            owned_worktree_root=fixture.root / "manager-worktrees",
            policy=policy,
        )
        peer = WorktreeManager(
            registry_root=manager.registry_root,
            owned_worktree_root=manager.owned_worktree_root,
            policy=policy,
        )
        task_id = "live-delayed-create"
        expected_path = _expected_owned_path(
            manager,
            identity.canonical_root,
            task_id,
        )
        add_completed = threading.Event()
        release_owner = threading.Event()
        original_run_git = worktree_module.run_git
        real_utc_now = worktree_module._utc_now

        def delayed_run_git(repository, arguments, **kwargs):
            result = original_run_git(repository, arguments, **kwargs)
            if arguments[:2] == ["worktree", "add"]:
                add_completed.set()
                if not release_owner.wait(10):
                    raise AssertionError("test did not release delayed worktree owner")
            return result

        def shifted_recovery_clock():
            now = real_utc_now()
            if threading.current_thread().name == "intent-recovery":
                return now + timedelta(seconds=policy.lease_stale_seconds + 5)
            return now

        monkeypatch.setattr(worktree_module, "run_git", delayed_run_git)
        monkeypatch.setattr(worktree_module, "_utc_now", shifted_recovery_clock)
        created = []
        creation_errors: list[BaseException] = []

        def create_worktree() -> None:
            try:
                created.append(manager.create(task_id=task_id, repository=identity))
            except BaseException as exc:
                creation_errors.append(exc)

        creator = threading.Thread(target=create_worktree, name="intent-owner")
        creator.start()
        try:
            assert add_completed.wait(10)
            intent = manager._load_create_intent(task_id)
            assert intent is not None
            assert intent.owner_pid == os.getpid()
            assert intent.owner_create_time_ns == worktree_module._process_create_time_ns(
                os.getpid()
            )
            reports = []
            recovery_errors: list[BaseException] = []

            def recover() -> None:
                try:
                    reports.append(peer.recover_creation_intents())
                except BaseException as exc:
                    recovery_errors.append(exc)

            recovery = threading.Thread(target=recover, name="intent-recovery")
            recovery.start()
            recovery.join(timeout=10)

            assert recovery.is_alive() is False
            assert recovery_errors == []
            assert len(reports) == 1 and reports[0].live == 1
            assert reports[0].compensated == 0
            assert reports[0].orphaned_records == ()
            assert expected_path.is_dir()
            assert manager._load_create_intent(task_id) == intent
        finally:
            release_owner.set()
            creator.join(timeout=10)

        assert creator.is_alive() is False
        assert creation_errors == []
        assert len(created) == 1
        assert created[0].status == "active"
        assert Path(created[0].worktree_path) == expected_path
        assert manager._load_create_intent(task_id) is None


def test_legacy_create_intent_with_occupied_pid_is_preserved_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="worktree-legacy-owner-identity") as fixture:
        identity = resolve_repository(str(fixture.repository))
        manager = _manager(fixture)
        task_id = "legacy-create-owner"

        def crash_before_record(_record, **_kwargs) -> None:
            raise SystemExit("simulated legacy create crash")

        monkeypatch.setattr(manager, "_write_record", crash_before_record)
        with pytest.raises(SystemExit, match="simulated legacy create crash"):
            manager.create(task_id=task_id, repository=identity)

        intent_path = manager._intent_path(task_id)
        payload = json.loads(intent_path.read_text(encoding="utf-8"))
        assert payload.pop("owner_create_time_ns") > 0
        intent_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        legacy = manager._load_create_intent(task_id)
        assert legacy is not None and legacy.owner_create_time_ns is None
        worktree = Path(legacy.worktree_path)
        alive = True
        monkeypatch.setattr(
            worktree_module.psutil,
            "pid_exists",
            lambda _pid: alive,
        )

        live_report = manager.recover_creation_intents()

        assert live_report.live == 1
        assert live_report.compensated == 0
        assert worktree.is_dir()
        assert manager._load_create_intent(task_id) == legacy

        alive = False
        recovered = manager.recover_creation_intents()
        assert recovered.compensated == 1
        assert not worktree.exists()
        assert manager._load_create_intent(task_id) is None


def test_stale_dirty_crash_window_is_registered_as_safe_orphan(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="worktree-dirty-crash-recovery") as fixture:
        identity = resolve_repository(str(fixture.repository))
        manager = _manager(fixture)
        task_id = "dirty-crash-window"
        branch = manager._safe_branch(task_id, identity)
        worktree = _expected_owned_path(
            manager,
            identity.canonical_root,
            task_id,
        )
        source_before = file_snapshot(fixture.repository)

        def crash_before_record(_record, **_kwargs) -> None:
            raise SystemExit("simulated process crash")

        monkeypatch.setattr(manager, "_write_record", crash_before_record)
        with pytest.raises(SystemExit, match="simulated process crash"):
            manager.create(task_id=task_id, repository=identity)
        (worktree / "crash-evidence.txt").write_text(
            "preserve this unreviewed evidence\n",
            encoding="utf-8",
        )

        recovery = WorktreeManager(
            registry_root=manager.registry_root,
            owned_worktree_root=manager.owned_worktree_root,
        )
        monkeypatch.setattr(worktree_module.psutil, "pid_exists", lambda _pid: False)
        report = recovery.recover_creation_intents()
        orphaned = recovery.load(task_id)

        assert report.compensated == report.finalized == report.unresolved == 0
        assert len(report.orphaned_records) == 1
        assert orphaned is not None and orphaned.status == "orphaned"
        assert orphaned.worktree_path == str(worktree)
        assert recovery.creation_intent_status()["pending"] == 0
        assert worktree.is_dir()
        assert (worktree / "crash-evidence.txt").is_file()
        assert _branch_target(fixture.repository, branch) == fixture.baseline_sha
        assert file_snapshot(fixture.repository) == source_before
        fixture.assert_remote_unchanged()


def test_crash_after_active_record_write_finalizes_intent_without_deletion(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="worktree-finalize-crash-recovery") as fixture:
        identity = resolve_repository(str(fixture.repository))
        manager = _manager(fixture)
        task_id = "finalize-crash-window"
        branch = manager._safe_branch(task_id, identity)
        worktree = _expected_owned_path(
            manager,
            identity.canonical_root,
            task_id,
        )

        def crash_before_intent_removal(intent) -> None:
            raise SystemExit("simulated finalization crash")

        monkeypatch.setattr(
            manager,
            "_remove_create_intent",
            crash_before_intent_removal,
        )
        with pytest.raises(SystemExit, match="simulated finalization crash"):
            manager.create(task_id=task_id, repository=identity)

        active = manager.load(task_id)
        assert active is not None and active.status == "active"
        assert manager.creation_intent_status()["added"] == 1

        recovery = WorktreeManager(
            registry_root=manager.registry_root,
            owned_worktree_root=manager.owned_worktree_root,
        )
        monkeypatch.setattr(worktree_module.psutil, "pid_exists", lambda _pid: False)
        report = recovery.recover_creation_intents()

        assert report.finalized == 1
        assert report.compensated == report.unresolved == report.invalid == 0
        assert recovery.load(task_id) == active
        assert recovery.creation_intent_status()["pending"] == 0
        assert worktree.is_dir()
        assert _branch_target(fixture.repository, branch) == fixture.baseline_sha
        fixture.assert_remote_unchanged()


def test_branch_reservation_crash_is_proven_and_cas_compensated_from_prepared_intent(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="worktree-branch-only-crash") as fixture:
        identity = resolve_repository(str(fixture.repository))
        manager = _manager(fixture)
        task_id = "branch-only-crash"
        branch = manager._safe_branch(task_id, identity)
        worktree = _expected_owned_path(
            manager,
            identity.canonical_root,
            task_id,
        )
        original_write_intent = manager._write_create_intent

        def crash_before_reserved_phase(intent, *, previous):
            if intent.phase == "branch_reserved":
                raise SystemExit("simulated reservation journal crash")
            return original_write_intent(intent, previous=previous)

        monkeypatch.setattr(
            manager,
            "_write_create_intent",
            crash_before_reserved_phase,
        )
        with pytest.raises(SystemExit, match="reservation journal crash"):
            manager.create(task_id=task_id, repository=identity)

        assert not worktree.exists()
        assert _branch_target(fixture.repository, branch) == fixture.baseline_sha
        intent = manager._load_create_intent(task_id)
        assert intent is not None and intent.phase == "prepared"
        assert manager.active_owned_branch_refs() == (f"refs/heads/{branch}",)

        monkeypatch.setattr(manager, "_write_create_intent", original_write_intent)
        monkeypatch.setattr(worktree_module.psutil, "pid_exists", lambda _pid: False)
        report = manager.recover_creation_intents()

        assert report.compensated == 1
        assert report.paths_deleted == 0
        assert report.unresolved == report.invalid == 0
        assert _branch_target(fixture.repository, branch) is None
        assert manager.creation_intent_status()["pending"] == 0
        fixture.assert_remote_unchanged()


def test_peer_branch_winning_after_absent_probe_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="worktree-peer-branch-race") as fixture:
        identity = resolve_repository(str(fixture.repository))
        manager = _manager(fixture)
        task_id = "peer-branch-race"
        branch = manager._safe_branch(task_id, identity)
        worktree = _expected_owned_path(
            manager,
            identity.canonical_root,
            task_id,
        )
        source_before = file_snapshot(fixture.repository)
        original_reserve = manager._reserve_intent_branch
        captured_intents = []

        def let_peer_win(source, intent):
            captured_intents.append(intent)
            fixture.git(["branch", intent.branch, intent.base_commit])
            return original_reserve(source, intent)

        monkeypatch.setattr(manager, "_reserve_intent_branch", let_peer_win)

        with pytest.raises(WorktreeError, match="failed to create"):
            manager.create(task_id=task_id, repository=identity)

        assert len(captured_intents) == 1
        assert not manager._branch_reservation_owned(
            identity.canonical_root,
            captured_intents[0],
        )
        assert _branch_target(fixture.repository, branch) == fixture.baseline_sha
        assert not worktree.exists()
        assert manager.load(task_id) is None
        assert list(manager.create_intents_dir.iterdir()) == []
        assert file_snapshot(fixture.repository) == source_before
        fixture.assert_remote_unchanged()


@pytest.mark.parametrize(
    ("transition", "terminal_status"),
    [("complete", "complete"), ("mark_orphaned", "orphaned")],
)
def test_terminal_record_transition_cannot_be_resurrected_by_loaded_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
    terminal_status: str,
):
    with coding_fixture(run_id=f"worktree-heartbeat-race-{terminal_status}") as fixture:
        identity = resolve_repository(str(fixture.repository))
        manager = _manager(fixture)
        record = manager.create(task_id=f"heartbeat-{terminal_status}", repository=identity)
        peer = WorktreeManager(
            registry_root=manager.registry_root,
            owned_worktree_root=manager.owned_worktree_root,
        )
        lease = manager.lease(record)
        assert lease.on_heartbeat is not None

        heartbeat_loaded = threading.Event()
        allow_heartbeat_write = threading.Event()
        terminal_started = threading.Event()
        terminal_done = threading.Event()
        errors: list[BaseException] = []
        original_write = manager._write_record_unlocked

        def block_loaded_heartbeat(candidate, *, expected_absent=False):
            if threading.current_thread().name == "loaded-heartbeat":
                heartbeat_loaded.set()
                if not allow_heartbeat_write.wait(5):
                    raise AssertionError("test did not release the loaded heartbeat")
            return original_write(candidate, expected_absent=expected_absent)

        monkeypatch.setattr(manager, "_write_record_unlocked", block_loaded_heartbeat)

        def write_heartbeat() -> None:
            try:
                assert lease.on_heartbeat is not None
                lease.on_heartbeat(worktree_module._utc_now())
            except BaseException as exc:
                errors.append(exc)

        def write_terminal() -> None:
            try:
                terminal_started.set()
                getattr(peer, transition)(record.task_id)
            except BaseException as exc:
                errors.append(exc)
            finally:
                terminal_done.set()

        heartbeat_thread = threading.Thread(
            target=write_heartbeat,
            name="loaded-heartbeat",
        )
        terminal_thread = threading.Thread(target=write_terminal)
        heartbeat_thread.start()
        assert heartbeat_loaded.wait(5)
        terminal_thread.start()
        assert terminal_started.wait(5)
        assert terminal_done.wait(0.15) is False

        allow_heartbeat_write.set()
        heartbeat_thread.join(timeout=5)
        terminal_thread.join(timeout=5)

        assert errors == []
        assert heartbeat_thread.is_alive() is False
        assert terminal_thread.is_alive() is False
        terminal = manager.load(record.task_id)
        assert terminal is not None and terminal.status == terminal_status

        # A heartbeat arriving after the terminal write is an explicit no-op.
        lease.on_heartbeat(worktree_module._utc_now())
        assert manager.load(record.task_id) == terminal


def test_recovery_respects_live_filesystem_lease_even_when_registry_is_stale():
    with coding_fixture(run_id="worktree-live-lease-recovery") as fixture:
        policy = worktree_module.get_coding_policy().model_copy(
            update={"lease_heartbeat_seconds": 60}
        )
        manager = WorktreeManager(
            registry_root=fixture.root / "manager-registry",
            owned_worktree_root=fixture.root / "manager-worktrees",
            policy=policy,
        )
        record = manager.create(
            task_id="live-lease-recovery",
            repository=resolve_repository(str(fixture.repository)),
        )
        peer = WorktreeManager(
            registry_root=manager.registry_root,
            owned_worktree_root=manager.owned_worktree_root,
            policy=policy,
        )

        with manager.lease(record):
            stale = record.model_copy(
                update={
                    "owner_pid": 2_147_483_647,
                    "heartbeat_at": record.heartbeat_at
                    - timedelta(seconds=policy.lease_stale_seconds + 5),
                }
            )
            manager._write_record(stale)

            assert peer._has_live_lease(stale) is True
            assert peer.recover_orphans() == []
            persisted = peer.load(record.task_id)
            assert persisted is not None and persisted.status == "active"

        recovered = peer.recover_orphans()
        assert len(recovered) == 1
        assert recovered[0].task_id == record.task_id
        assert recovered[0].status == "orphaned"


def test_create_finalization_never_overwrites_racing_registry_record(
    monkeypatch: pytest.MonkeyPatch,
):
    with coding_fixture(run_id="worktree-record-finalize-race") as fixture:
        identity = resolve_repository(str(fixture.repository))
        manager = _manager(fixture)
        task_id = "record-finalize-race"
        branch = manager._safe_branch(task_id, identity)
        worktree = _expected_owned_path(
            manager,
            identity.canonical_root,
            task_id,
        )
        original_write = manager._write_record
        competing_hash = "f" * 64

        def inject_competing_record(record, *, expected_absent=False) -> None:
            competing = record.model_copy(
                update={"owner_token_hash": competing_hash}
            )
            original_write(competing, expected_absent=True)
            original_write(record, expected_absent=expected_absent)

        monkeypatch.setattr(manager, "_write_record", inject_competing_record)
        with pytest.raises(WorktreeError, match="failed to create"):
            manager.create(task_id=task_id, repository=identity)

        persisted = manager.load(task_id)
        assert persisted is not None
        assert persisted.owner_token_hash == competing_hash
        assert worktree.is_dir()
        assert _branch_target(fixture.repository, branch) == fixture.baseline_sha
        assert manager.creation_intent_status()["added"] == 1
        fixture.assert_remote_unchanged()


def test_branch_prefix_metadata_indirection_cannot_escape_common_git_directory():
    with coding_fixture(run_id="git-branch-prefix-indirection") as fixture:
        identity = resolve_repository(str(fixture.repository))
        external = fixture.root / "external-ref-target"
        external.mkdir()
        marker = external / "marker.txt"
        marker.write_text("must remain untouched\n", encoding="utf-8")
        indirection = fixture.repository / ".git" / "refs" / "heads" / "local-agent"
        try:
            os.symlink(external, indirection, target_is_directory=True)
        except OSError as exc:
            if os.name != "nt":
                pytest.skip(f"directory reparse fixture unavailable: {exc}")
            created = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(indirection),
                    str(external),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if created.returncode != 0:
                pytest.skip("directory junction fixture unavailable")

        with pytest.raises(WorktreeError, match="failed to create|metadata"):
            _manager(fixture).create(
                task_id="branch-prefix-indirection-task",
                repository=identity,
            )

        assert marker.read_text(encoding="utf-8") == "must remain untouched\n"
        assert not any(external.glob("task-*"))
