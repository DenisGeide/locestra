# Scoped Knowledge and Archive Engine

- Статус: Stage 004 complete; full regression/foundation/doctor/smoke и approved scoped live-index gate зафиксированы в [Current State](CURRENT_STATE.md).
- Владелец: `services/knowledge/`; persistent dataset отделён от Controlled Memory Engine.
- Contract schema: `1.0`; SQLite schema: `1`; policy: `2026-07-15.2`; parser: `1.0`.
- Storage: ignored `data/knowledge.sqlite3`; canonical local operator boundary: `python -m services.knowledge.cli`.
- Нормативные правила: [Privacy](../constitution/PRIVACY.md), [Security](../constitution/SECURITY.md), [Memory](../constitution/MEMORY.md), [Context Strategy](CONTEXT_STRATEGY.md) и [Archive Import Plan](ARCHIVE_IMPORT_PLAN.md).

## Назначение и строгие границы

Knowledge Engine индексирует явно зарегистрированные project sources, строит воспроизводимую repository map и возвращает bounded evidence с provenance. Он не является чатом, model router, Artifact Store, task journal или долговременной персональной памятью.

Ключевое разделение:

| Dataset | Содержимое | Может стать active memory автоматически |
|---|---|---|
| Knowledge source/index | Source versions, bounded fragments, FTS rows, explicit fact candidates, conflicts, repository maps | Нет |
| Controlled Memory Engine | Подтверждённые typed user/project/task records | Только через отдельные propose + confirm boundaries |
| Task journal | Lifecycle/route/attempt state | Нет |
| Original archive/repository | Source of truth вне derived index | Нет |

Knowledge database пересоздаваема из разрешённых sources. Она имеет отдельный SQLite application ID `LAIK`, checksummed schema ledger, foreign keys, WAL, FTS5 и `secure_delete`. Прямой доступ других modules к её tables не является контрактом.

Stage 004 delivered the typed local-only CLI/class Context Envelope contract.
Stage 005 adds its verified consumer inside the isolated local coding workflow.
Fast/strong chat, docs, and unrelated routes still do not open KnowledgeStore or
receive repository retrieval automatically.

## Scope и identity

Identity project состоит из локального owner namespace и canonical realpath. CLI использует namespace label `local-user` только в текущей single-user local operator boundary. Multi-user authorization отсутствует. Прежде чем публиковать Knowledge CRUD/retrieval по HTTP или Telegram, Entry должен получить authenticated actor/session и вывести owner scope server-side.

Source registration не расширяет filesystem permission. Manual import разрешает только allowlisted root files/directories/extensions внутри project. Repository indexing использует только `git ls-files` tracked inventory; first-party tracked code может находиться вне manual directory allowlist, но общий denylist имён/директорий/extensions, size limits и secret checks сохраняется.

Не поддерживаются source escape, UNC/device path, ADS, encoded/unicode aliases, reparse/symlink/junction и hardlink. Обычный repository с внутренним `.git` и стандартный linked worktree поддерживаются. Для linked worktree валидируется точная цепочка `.git` pointer → `.git/worktrees/<name>` → local common `.git` → backlink; config includes, alternates, device/UNC, reparse и hardlinked metadata fail-closed. Произвольная external Git metadata не разрешена.

## Source и generation model

Manual и repository observations одного URI имеют разные `source_origin`, поэтому не подменяют друг друга. Source version содержит content hash, size/mtime, parser/version, derivation/policy version и sensitivity. Generation строится отдельно, затем активируется одним compare-and-swap относительно предыдущей generation и `mutation_epoch`.

Пока generation имеет status `building`, retrieval её не видит. Успешная публикация переводит предыдущую generation в `superseded`, новую — в `active`. Ошибка очищает непубличные derived rows и фиксирует payload-free audit reason. Abandoned build старше одного часа восстанавливается как failed/removed при следующем store startup.

Purge увеличивает project mutation epoch и не позволяет одновременно строящейся generation воскресить удалённые данные. Active retrieval использует project generation view; stale history доступна только по explicit freshness contract и всё равно проходит live validation.

## Repository indexing

`index --approved` выполняет:

1. canonical project и Git metadata validation;
2. bounded tracked-file inventory без submodules/symlinks;
3. Git commit, index/head object IDs и dirty-path observation;
4. path/size/UTF-8/secret/privacy validation каждого candidate;
5. deterministic parse/chunk/fact extraction;
6. optional bounded Git history metadata без patches;
7. repository map и worktree revision;
8. atomic generation publish и Memory source invalidation.

Default policy ограничивает один файл 2 MiB, всю попытку 64 MiB, fragment 1 200 chars, tracked inventory 50 000 files, Git output 16 MiB и history 500 commits. Превышение общего byte budget не публикует частичный index.

Incremental path использует Git changed inventory, index/head object IDs, size/mtime и предыдущую map. Неизменившиеся non-manifest sources переиспользуют previous source version без повторного content read; changed sources перечитываются и сравниваются по hash. Manifests перечитываются, чтобы commands/map не устарели. Rename связывается через identical content hash; удалённый repository source исчезает из новой generation и invalidates связанную Memory candidate provenance.

### Repository map v1

Map привязана к owner, canonical path, credential-sanitized Git remote, commit SHA, worktree revision и policy version. Она содержит:

- language counts;
- manifests и entry points;
- top-level modules;
- tests и documentation;
- bounded extracted symbols;
- безопасно извлечённые commands;
- `AGENTS.md` hierarchy;
- allowed file observations и blocked source hashes/reason codes.

Map и каждый returned fragment маркируются `untrusted=true`, `local_only=true`. Перед выдачей map повторно проверяются tracked inventory, commit/remote, hashes/mtime allowed files и blocked-source transitions. Это обеспечивает freshness, но на очень большом repository полная map validation может быть заметным I/O bottleneck.

Git вызывается через найденный absolute executable вне project, с очищенными config/trace/askpass/SSH overrides, bounded output и timeout. Git history хранит только commit hash, author timestamp и bounded subject; patches не читаются. Remote credentials/query/fragment не сохраняются.

## Parsers, fragments и факты

Поддерживаются Markdown/TXT, allowlisted repository/config text, explicit conversation JSON/HTML adapters и bounded Git metadata. Input должен быть UTF-8 и не содержать NUL/binary content.

Chunking deterministic: Markdown учитывает headings, text режется по строкам, сверхдлинная строка делится bounded parts. Basic symbols извлекаются через Python AST и ограниченные language-specific patterns; это навигационные hints, а не полноценный compiler/LSP index.

Только explicit `Fact|Факт: key = value` и `Decision|Решение: key = value` создают fact rows. Они получают status `candidate`; разные values одного key в active generation получают `conflicted`. Извлечённая строка не считается истинной и не выбирает winner.

Prompt injection внутри source не исполняется: parser не вызывает модель или tools, а output остаётся untrusted evidence. Parser/adapter/extraction/derivation/policy versions входят в provenance и freshness checks.

## Retrieval contract

Input `RetrievalRequestV1`:

- exact owner/project scope;
- query до 2 048 chars;
- optional allowed source kinds;
- token budget 128–32 768;
- максимум 1–32 fragments;
- `active_only` или explicit `include_stale`.

Output `RetrievalResultV1`:

- ranked fragments;
- source kind/content/title;
- generation/source URI/hash/size/mtime;
- locator и optional line range;
- commit/worktree/policy/parser/extraction provenance;
- score/reason, stale/conflict flags;
- exact conservative estimated-token sum;
- `untrusted=true`, `local_only=true`, degraded/reason when freshness filtering occurred.

SQLite FTS5 `unicode61` выполняет bounded lexical candidate selection. Engine пагинирует candidates, затем live-проверяет policy/parser/chunk version, tracked membership, content hash/size/mtime и commit. Duplicate content не заполняет budget повторно. Невалидный или stale fragment по default исключается, а не выдаётся из cache.

Vector/embedding backend намеренно отсутствует: его можно добавить только за тем же retrieval contract после измеримого RU/EN code-search eval, privacy review и доказательства выигрыша latency/quality. Сейчас зрелые primitives — Git metadata, FTS5, structured map и bounded `rg` fallback.

`rg-search` работает только по file list из свежей privacy-approved repository map, fixed-string query, absolute trusted executable, disabled user config, bounded batches/output/matches и post-search revision check. Он не сканирует untracked или blocked files.

## Context Envelope v1

`context` сохраняет fixed task sections:

- goal и constraints;
- modified files;
- unresolved errors;
- verification plan;
- bounded fresh tool results;
- compact repository summary;
- retrieved evidence.

Query для evidence учитывает goal, active modified file names и errors. Свежие tool results остаются отдельной higher-priority section. Goal/constraints/files/errors/verification/tool results проходят secret scan и не режутся молча. Context builder сначала компактит repository summary, затем уменьшает retrieved fragments; если fixed sections не помещаются, возвращает ошибку.

Conservative token estimate считается по сериализованному envelope и не превышает заданный budget. Это не означает физическое окно 128K/256K и не заменяет tokenizer конкретной модели. Максимальный contract budget сейчас 32 768.

## Archive и Memory lifecycle

Archive import описан в [ARCHIVE_IMPORT_PLAN.md](ARCHIVE_IMPORT_PLAN.md). Conversation source получает sensitivity не ниже `sensitive`. Raw archive не становится active memory.

`candidates` показывает explicit extracted facts. `propose-memory FACT_ID --confirm PROPOSE-MEMORY` повторно проверяет active conflict state, policy/parser/chunk version, live hash/mtime и repository tracked membership, затем создаёт scoped `project_knowledge` record со status `candidate` и provenance URI/hash/locator. Отдельная команда Memory Engine `confirm RECORD_ID` является второй, независимой границей подтверждения.

При source change/reclassification/removal Knowledge Engine invalidates Memory records, связанные exact URI/owner/project. При source purge Memory hard purge выполняется первым; knowledge logical delete не начинается, пока Memory physical purge не подтверждён.

## Delete, rebuild и физические ограничения

`purge-source` по default — preview. Apply требует confirmation, в точности равного `source_id`. Purge scoped по owner/project/source, удаляет associations, fragments, FTS rows, facts/conflicts и применимые maps; repository-source purge invalidates map/revision. Source original остаётся read-only и не удаляется.

Knowledge DB использует `secure_delete`, checkpoint/truncate WAL и `VACUUM`. Открытый reader может вернуть `physical_purge_complete=false`; после освобождения reader используется `compact`. Успешный SQLite purge не гарантирует forensic erase из backups, VSS/OS snapshots, pagefile, SSD wear levelling или копий другого приложения.

Derived index можно полностью удалить и пересоздать из зарегистрированных sources. Встроенной wildcard/all-purge команды нет намеренно; операция требует exact source identity.

## CLI

```powershell
uv run python -m services.knowledge.cli status

# Import одного allowlisted файла: сначала preview, затем publish
uv run python -m services.knowledge.cli import --project C:\work\repo --source docs\note.md --approved --dry-run
uv run python -m services.knowledge.cli import --project C:\work\repo --source docs\note.md --approved

# Repository index: сначала bounded dry-run
uv run python -m services.knowledge.cli index --project C:\work\repo --approved --dry-run
uv run python -m services.knowledge.cli index --project C:\work\repo --approved

uv run python -m services.knowledge.cli map --project C:\work\repo
uv run python -m services.knowledge.cli retrieve --project C:\work\repo --query "startup command" --token-budget 2000
uv run python -m services.knowledge.cli rg-search --project C:\work\repo --query "KnowledgeEngine"
uv run python -m services.knowledge.cli context --project C:\work\repo --goal "исправить retrieval" --modified-file services/knowledge/engine.py --verification "uv run pytest"
uv run python -m services.knowledge.cli candidates --project C:\work\repo

# Preview, затем exact-ID purge
uv run python -m services.knowledge.cli purge-source --project C:\work\repo --source-id SOURCE_ID
uv run python -m services.knowledge.cli purge-source --project C:\work\repo --source-id SOURCE_ID --confirm SOURCE_ID
uv run python -m services.knowledge.cli compact
```

Custom `--database` автоматически использует sibling `memory.sqlite3`, если `--memory-database` не задан: test/fixture CLI не должен мутировать production Memory Engine. Management API не опубликован по HTTP.

## Security и privacy controls

- no full-disk discovery; только explicit project/source или Git-tracked inventory;
- fail-closed source handles, no-follow и before/after identity check;
- path/name/directory/extension/size/total-byte allowlist/denylist;
- canonicalized raw/encoded/HTML/unicode/structured secret scanning;
- secret-like paths и blocked findings redacted до hash where needed;
- no repository-provided Git/rg executable, config, hooks, pager, askpass или SSH command;
- bounded subprocess timeout/output и no shell interpolation;
- owner/project exact isolation;
- source content, map, rg matches и tool results всегда untrusted/local-only;
- payload-free operational audit;
- generation CAS/mutation epoch against import/purge races.

Secret detection остаётся defense-in-depth, не абсолютной DLP. Не следует намеренно помещать credentials в registered project даже ради проверки scanner.

## Известные ограничения

1. Stage 005 integrates Context Envelope into the isolated local coding
   workflow using the single-user owner boundary. General gateway routes still
   do not derive a knowledge owner from an authenticated actor or inject
   repository context universally.
2. Full LSP/tree-sitter semantic index и vector reranking отсутствуют.
3. Repository map freshness validation перечитывает approved files и может быть дороже incremental index на больших проектах.
4. Поддерживается только стандартный локальный layout linked Git worktree; bare/custom external metadata, alternates и config includes не поддерживаются.
5. Archive adapters поддерживают только documented minimal shapes; реальные ChatGPT/Fantik exports не проверены, потому что не предоставлены.
6. Original archives, external app histories, backups и OS copies имеют отдельный retention/delete lifecycle.
7. Conservative token estimate не равен tokenizer accounting конкретной модели.
8. Knowledge source management не имеет multi-user HTTP authorization, background scheduler или automatic TTL sweeper.

## Verification contract

Gate требует: Markdown/TXT exact provenance, idempotent duplicate import, change/delete/rename invalidation, explicit conflict, prompt-injection inertness, secret/private-path exclusions, scope escape rejection, budgeted retrieval с line/hash/commit, repository map fixture, cascade purge, Memory boundary, full regression, doctor и smoke. Фактические counts/results фиксируются в [Current State](CURRENT_STATE.md) и [System Manifest](../SYSTEM_MANIFEST.md), а не предполагаются из наличия кода.
