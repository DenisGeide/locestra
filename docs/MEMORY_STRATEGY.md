# Стратегия памяти

- Статус: Stage 003 implemented; controlled Memory Engine отделён от task journal и archive.
- Владелец: `services/memory`; SQLite task journal остаётся собственностью control plane.
- Нормативные правила: [MEMORY](../constitution/MEMORY.md) и [PRIVACY](../constitution/PRIVACY.md).
- Изменение: со schema migration, backup/restore и privacy tests.

## Честный baseline

`data/memory.sqlite3` содержит task journal и additive schema v3 для controlled memory. Это один SQLite-файл, но разные contracts/tables/lifecycles: task state не становится long-term memory автоматически, а Memory Engine не является repository index или RAG.

190 существовавших на migration boundary task rows сохраняются без скрытого rewrite как `legacy_payload=1`; verified pre-migration snapshot содержит первые 184, а 6 совместимых old-writer rows появились до окончательного lifecycle stop. Они не участвуют в memory retrieval/export. Новые task writes получают bounded privacy filtering и `legacy_payload=0`. Codex bundles, временные Codex results и истории Open WebUI/n8n по-прежнему имеют отдельные retention gaps. Наличие этих данных не разрешает выдавать их как память в новой задаче.

## Разделение слоёв

| Слой | Назначение | Срок жизни | Источник истины | Допустимая выдача |
|---|---|---|---|---|
| Active context | Цель, plan, последние events и errors текущей задачи | до завершения/сжатия | TaskState + свежие tool results | только текущей задаче |
| Task journal | Lifecycle, route, executor, artifacts и evidence refs | управляемый retention | versioned task records | audit/recovery в том же scope |
| Long-term memory | подтверждённые предпочтения и факты пользователя/проекта | до исправления/удаления/TTL | typed memory records | только совпадающему owner/project scope |
| Archive | импортированные исходные материалы и завершённые task bundles | policy-defined | immutable source + metadata | через явный import/retrieval |
| Knowledge index | производные chunks/embeddings/symbol maps | пересоздаваемый | исходный content hash/revision | только с source references |
| Artifact Store | файлы, diff, logs, media и большие outputs | retention policy | content-addressed artifact metadata | по разрешённой ссылке |

Ни один слой не подменяет другой. Knowledge index можно удалить и пересоздать; исходный документ не становится memory fact автоматически.

## Целевая memory record

Минимальные поля этапа 003: `record_id`, `schema_version`, `owner`, `scope`, `kind`, typed `value`, `source`, `source_hash`, `created_at`, `updated_at`, `confidence/status`, `expires_at`, `supersedes`, `deleted_at`, `sensitivity`, `producer`.

Model-generated inference хранится как `candidate`, пока пользователь или hashed objective source её не подтверждает. Разные active values одного key образуют visible conflict без automatic winner. Исправление создаёт новую version/supersedes связь; soft delete исключает retrieval, explicit hard purge удаляет controlled content и provenance.

## Active memory

В active memory попадают только данные, необходимые текущей задаче: контракт, route/plan, ограниченный список фактов, artifacts и unresolved errors. Большие tool outputs остаются artifacts. После завершения active state сворачивается в task evidence; автоматического повышения в long-term memory нет.

## Archive и knowledge

Archive хранит источник с owner, import consent, hash, media type, sensitivity и retention. Stage 004 Knowledge Engine создаёт из разрешённого источника chunks/symbol maps и lexical FTS evidence с точной ссылкой на source revision; vector embeddings намеренно deferred до eval. При изменении/удалении источника все производные записи инвалидируются или удаляются.

## Изоляция и приватность

- Ключ scope: минимум owner + project/workspace; cross-project retrieval default-deny.
- Secrets, credentials, cookies и `.env` запрещены во всех слоях.
- Экспорт показывает content, provenance, retention и derived links.
- Удаление каскадно охватывает chunks, embeddings, caches и artifacts согласно policy.
- Cloud executor не получает memory автоматически; handoff перечисляет каждую разрешённую запись.

## Реализованная миграционная граница

Schema v3 использует checksummed migration ledger, SQLite online backup/full verification/explicit failure-safe restore, WAL/FK, owner-only ACL и отдельные `memory_records`, `memory_sources`, conflict и payload-free audit tables. Реализованы CRUD, confirm/reject/supersede, scoped search, export/delete/purge, retention/invalidation и read-only bounded Planner retrieval. Archive references принимают только positive-allowlisted typed metadata. Vector storage намеренно не добавлен без measured need. Подробный operational contract: [MEMORY_ENGINE.md](MEMORY_ENGINE.md). Отдельный Stage 004 repository/archive index реализован в [KNOWLEDGE_ENGINE.md](KNOWLEDGE_ENGINE.md); он не импортирует архивы автоматически и создаёт Memory candidate только через отдельное явное подтверждение.
