# Controlled Memory Engine

- Статус: Stage 003 implemented; live migration выполнена после verified online backup.
- Владелец: локальный control plane.
- Schema: SQLite application schema `3`, memory record schema `1.0`.
- Код: `services/memory/`; canonical management boundary: `python -m services.memory.cli`.
- Нормативные правила: [MEMORY](../constitution/MEMORY.md), [PRIVACY](../constitution/PRIVACY.md), [Permissions](PERMISSIONS.md).

## Назначение и границы

Memory Engine хранит небольшое число явных, типизированных и проверяемых фактов. Это не архив чатов, не копия репозитория, не vector database и не замена свежим файлам, Git или результатам инструментов.

Поддерживаемые типы:

- `user_profile` — подтверждённые предпочтения пользователя;
- `project_knowledge` — архитектурные решения, команды и устойчивые факты проекта;
- `task_history` — typed bounded evidence: summary, executor/route, attempts, files, tests, artifacts, commit и failures, но не полный transcript;
- `operational_state` — typed active goal/stage/errors/next action с timezone-aware heartbeat и согласованной lease pair;
- `archive_reference` — strict reference contract с positive allowlist типизированных timestamps/counts/identifiers; произвольный metadata text, nested content/messages/tool output запрещены, содержимое архива не импортируется автоматически.

Task journal `tasks` остаётся отдельным source of truth для lifecycle. Реальные Open WebUI/n8n histories, `inbox/`, старые task payloads и любые пользовательские архивы не импортировались в Memory Engine.

## Схема и миграции

`services/memory/migrations.py` применяет три additive migrations:

1. `legacy_tasks` — создаёт или валидирует исходную task schema;
2. `task_state_v1` — добавляет versioned task-state columns без фабрикации state;
3. `controlled_memory_v1` — добавляет memory records, provenance, conflicts, audit, privacy markers и индексы.

Версия фиксируется одновременно в `PRAGMA user_version=3` и checksummed `schema_migrations`. Неизвестная версия, разрыв ledger или изменённый checksum останавливают migration. Обычные reads migration не запускают.

Перед изменением существующей базы migration удерживает одну writer reservation от начала online backup до commit DDL. Поэтому committed WAL включён в snapshot, а другой writer не может вклиниться между backup и schema change. Backup сначала пишется как owner-only `.partial`, проходит integrity/FK/full-schema verification и только затем atomically переименовывается. Restore требует явного подтверждения, сохраняет verified либо raw safety copy повреждённого target и не удаляет target WAL до успешной замены. База, backups и файловые exports получают защищённый owner-only ACL; при невозможности hardening операция fail-closed. Файлы находятся в ignored `data/backups/` и считаются sensitive, потому что pre-Stage-003 backup содержит legacy prompts/results.

Restore является отдельной explicit операцией с `--confirm RESTORE`, safety backup текущей базы, проверкой и atomic replacement. Gateway перед restore должен быть остановлен.

## Модель записи

Одна logical record содержит:

- stable `record_id`, `record_schema_version`, `record_type`;
- `owner_id`, `scope_type`, `scope_key`, canonical project realpath и/или task ID;
- `memory_key`, canonical JSON value и service-generated hashes;
- timestamps created/observed/updated, confidence и status;
- validity interval, optional project commit SHA;
- producer/author, `supersedes_record_id`;
- sensitivity, retention, expiry, deletion marker и optimistic revision.

Provenance хранится отдельно в `memory_sources`: type, bounded URI, fragment, source hash, commit, mtime, observed time, producer и author. Один и тот же факт из разных источников остаётся одной record с несколькими sources.

Audit хранит только event metadata: opaque scope hash, IDs, action, status transition, reason code, actor category, count и policy version. Content, query, source URI, exception text, prompt и result туда не пишутся.

## Lifecycle и конфликты

Новая пользовательская или model-derived запись по умолчанию имеет status `candidate`. Initial `confirmed` разрешён только для hashed objective evidence (`file`, `git`, `manifest`, `task_state`, `tool_result`, `test_result`) и не для model producer. Пользователь подтверждает candidate отдельной операцией.

Statuses:

- `candidate` — существует, но не участвует в normal retrieval;
- `confirmed` — может участвовать в retrieval;
- `conflicted` — найден другой active value того же key; ни один winner не выбирается автоматически;
- `stale` — истёк TTL либо изменились commit/source hash/mtime;
- `rejected` — явно отклонено;
- `superseded` — заменено новой versioned record;
- `deleted` — soft delete, content исключён из normal list/retrieval.

Confirm conflicted record — явное разрешение конфликта: выбранная запись становится confirmed, остальные члены группы rejected. Edit/supersede создаёт новую запись и сохраняет связь с предыдущей. Upsert никогда не воскрешает deleted/rejected record неявно.

## Privacy boundary

`services/memory/privacy.py` применяет один bounded policy layer:

- NFKC/control normalization;
- ограничения размера, глубины и количества structured leaves;
- denylist для `.env`, credential/key files, browser profiles, secret directories, URL credentials, UNC sources, trailing dot/space aliases и NTFS alternate data streams;
- raw/percent-encoded/double-encoded secret checks для URL query/fragment и normalized source references;
- patterns для private keys, auth/cookies, common provider tokens, JWT и generic secret assignments;
- conservative entropy detector;
- scanning отдельных leaves и склеенного bounded view;
- strict reject для durable memory и повторную проверку перед export;
- non-throwing redaction для task prompt/result/metadata/state, чтобы privacy/storage projection не останавливала основное исполнение.

Детектор не является математической гарантией DLP: неизвестный секрет, похожий на обычный текст, может не распознаться. Секреты всё равно запрещено намеренно помещать в prompt, memory, logs, artifacts или Git.

На migration boundary обнаружено 190 существовавших task rows: verified snapshot зафиксировал первые 184, ещё 6 совместимых old-writer rows появились после snapshot до окончательного lifecycle stop. Все 190 сохранены без скрытого переписывания payload и помечены `legacy_payload=1`. Новые/обновлённые task rows проходят persistence filter, получают `privacy_version=stage003-v1` и `legacy_payload=0`. Legacy rows не участвуют в memory retrieval/export. Для них есть отдельный preview-first scoped purge.

## Retrieval и Planner

Retrieval выполняется после deterministic routing и не может изменить action, risk, permission или route. Жёсткие правила:

- fast/auxiliary path: без construction MemoryStore и без SQLite I/O;
- только `confirmed`, не expired и valid records;
- owner exact; user scope плюс exact project/task scope текущего запроса;
- project realpath canonicalized; commit-bound records с несовпадающей revision исключаются read-only фильтром, а explicit invalidator отдельно переводит их в `stale`;
- default types: user profile, project knowledge и bounded task history;
- lexical SQL prefilter до recency cap (до 512 candidates), Unicode casefold, generic scope stopwords, deterministic score и stable bounded selection;
- максимум 6 records и `min(1500 chars, 20% executable Plan input budget)`;
- полный rendered record с ID/type/why/sources/value учитывается в char budget и не режется посередине JSON; ниже threshold не добавляется;
- свежие files/Git/tool results имеют безусловный приоритет.

Memory content получает только local coding route: Qwen Code видит его как явно помеченный `UNTRUSTED RETRIEVED EVIDENCE` с record ID, reason и sources. Docs/Context7 не выполняет memory retrieval, чтобы stored content не попал в external MCP boundary. Codex/Codex bundle сохраняют только opaque record IDs; memory content и raw executor error после memory-assisted attempt автоматически в cloud не передаются. Additive `local_agent_memory` в success и error responses показывает IDs, score, why, source refs и disclosure mode без повторной выдачи content; references-only Codex metadata остаётся локальной и в handoff не попадает.

Любая migration/storage/retrieval/validation ошибка возвращает empty degraded result. Она не изменяет Router failure history и не должна блокировать chat/coding.

## CLI

Примеры ниже используют `uv run`; эквивалентно можно вызвать project Python напрямую.

```powershell
uv run python -m services.memory.cli status

'"uv run pytest"' | uv run python -m services.memory.cli add `
  --scope project --project C:\work\repo `
  --type project_knowledge `
  --subject project.test_command `
  --value-stdin

uv run python -m services.memory.cli list --scope project --project C:\work\repo
uv run python -m services.memory.cli search pytest --scope project --project C:\work\repo
uv run python -m services.memory.cli show RECORD_ID
uv run python -m services.memory.cli confirm RECORD_ID
uv run python -m services.memory.cli reject RECORD_ID
'"uv run pytest -q"' | uv run python -m services.memory.cli edit RECORD_ID --value-stdin
uv run python -m services.memory.cli delete RECORD_ID
uv run python -m services.memory.cli purge RECORD_ID --confirm-record-id RECORD_ID

uv run python -m services.memory.cli retrieve "run project tests" --project C:\work\repo
uv run python -m services.memory.cli retention
uv run python -m services.memory.cli retention --apply
uv run python -m services.memory.cli export --scope project --project C:\work\repo --format json

uv run python -m services.memory.cli backup
uv run python -m services.memory.cli verify-backup PATH_TO_BACKUP
uv run python -m services.memory.cli restore PATH_TO_BACKUP --confirm RESTORE
```

Legacy purge сначала только показывает count:

```powershell
uv run python -m services.memory.cli legacy-purge --task-id TASK_ID
uv run python -m services.memory.cli legacy-purge --task-id TASK_ID --apply --confirm PURGE-LEGACY-TASKS
```

Wildcard/all hard purge отсутствует намеренно. Management остаётся CLI-only local operator boundary: memory mutation endpoints не публикуются для Open WebUI/n8n. Gateway проверяет runtime-generated bearer credential для всех `/v1/*`, но это inference/execution boundary, а не замена отдельной scoped authorization model для CRUD/export/purge.

`--value-json` поддерживается для несекретных коротких значений, но может оставить payload в shell history/process list. Для обычной работы предпочтительны `--value-stdin` или `--value-file`. Автоматический sweeper переводит просроченный `ttl` в `stale`; `session`/`task` retention требуют явного lifecycle-вызова владельца соответствующей сессии/задачи и не выдаются как обещание фонового удаления.

## Удаление и физические ограничения

Soft delete логически исключает запись. Hard purge требует exact record ID, удаляет record/provenance/conflict links, сохраняет payload-free audit, включает `secure_delete`, checkpoint/truncate WAL и выполняет `VACUUM`.

Это не forensic secure erase: данные могут оставаться в migration backup, OS snapshot, внешней копии, SSD wear-levelled NAND или истории другого приложения. Controlled backups и exports нужно удалить отдельно; физическое уничтожение требует политики за пределами SQLite.

## Verification

Stage 003 tests покрывают empty/current/repeat migration, checksum/full-schema/FK failure, online backup/restore/corrupt-target/failed-replace safety, CRUD/revision/supersede, dedup/provenance, conflict resolution, scope isolation, encoded/aliased source rejection, NFKC revalidation, strict archive metadata, task redaction, TTL/commit/hash/mtime invalidation, read-only bounded retrieval under writer lock, concurrency, protected export, soft/hard delete, CLI, Codex disclosure boundary и degraded integration. Полный regression suite и live gates фиксируются в [Current State](CURRENT_STATE.md) и [System Manifest](../SYSTEM_MANIFEST.md).

Stage 004 реализовал отдельный [Knowledge Engine](KNOWLEDGE_ENGINE.md): scoped repository/archive indexing, symbol/chunk map и measured lexical retrieval. Archive и source code по-прежнему не превращаются в long-term memory автоматически: Knowledge fact сначала остаётся candidate и требует отдельного подтверждения в Memory Engine.
