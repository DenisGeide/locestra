# Repositories Knowledge

- Dataset status: repository registry; не заменяет live Git inventory или Repository Map v1.

| Repository | Status | Verified facts | Source |
|---|---|---|---|
| Locestra | verified registry entry | Canonical path is the current checkout root; public baseline branch `main`; source tree contains services/scripts/config/docs/tests. | [System Manifest](../SYSTEM_MANIFEST.md) |
| Locestra knowledge index | verified imported derived index | Explicit `index --approved` created a Repository Map for the canonical project: 143 tracked paths, 128 indexed and 15 policy/privacy blocked; 129 active sources/1,384 active fragments including bounded Git history. | [Import Sources](IMPORT_SOURCES.md), [Knowledge Engine](../docs/KNOWLEDGE_ENGINE.md#repository-indexing) |
| Other repositories | unverified/not provided | Не обнаруживались за пределами current project и не зарегистрированы. | [Security](../constitution/SECURITY.md), [Import Sources](IMPORT_SOURCES.md) |

Repository Map v1 после verified index является authoritative для languages/manifests/entry points/modules/symbols/tests/commands/docs/AGENTS hierarchy. Этот human-readable файл не должен вручную дублировать mutable map fields или выдавать предположение за live state.

Git remote, если он есть, хранится только после credential sanitization. Стандартный local linked worktree поддерживается через bounded `.git/worktrees` validation; custom/bare external metadata не разрешена, а один repository path не даёт permission на соседние worktrees.
