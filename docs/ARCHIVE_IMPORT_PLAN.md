# План безопасного импорта архивов

- Статус: Stage 004 implementation; политика импорта реализована, реальные пользовательские архивы не предоставлены и не импортированы.
- Владелец: локальный оператор платформы.
- Исполнитель: `services/knowledge/`; canonical boundary — `python -m services.knowledge.cli`.
- Политика: `config/knowledge.json`, schema `1.0`, policy `2026-07-15.2`.
- Нормативные правила: [Privacy](../constitution/PRIVACY.md), [Security](../constitution/SECURITY.md), [Memory](../constitution/MEMORY.md) и [Knowledge Engine](KNOWLEDGE_ENGINE.md).

## Честный исходный статус

На момент создания этого документа ChatGPT export, Fantik export, пользовательские notes и другие внешние архивы не были предоставлены. Каталог `archives/` — только явная локальная drop zone; его содержимое игнорируется Git, не обнаруживается автоматически и не импортируется без регистрации конкретного файла. `.gitkeep` не является архивом.

Платформа не сканирует Desktop, Documents, Telegram data, browser profiles, Open WebUI/n8n volumes или весь компьютер в поисках материалов. Наличие пути в `allowed_directories` задаёт допустимую границу внутри уже зарегистрированного проекта, но не даёт согласие на чтение.

Актуальный source inventory находится в [IMPORT_SOURCES.md](../knowledge/IMPORT_SOURCES.md). Runtime-состояние конкретного импорта является источником истины и проверяется через `status`, `map` и `retrieve`, а не выводится из наличия файла.

## Граница согласия и scope

Импорт возможен только если одновременно выполнены условия:

1. оператор передал absolute canonical project path;
2. source находится внутри этого проекта;
3. source соответствует allowlist и не попадает под denylist;
4. указан `--approved`, то есть дано явное согласие на конкретную операцию;
5. файл прошёл размер, тип, UTF-8, path/reparse/hardlink и secret checks;
6. parser распознал формат без malformed или active-content ambiguity.

`--dry-run` выполняет bounded read, privacy scan и parse, но не создаёт source, generation, fragments, facts или memory record. Dry-run нужен до первого импорта каждого нового export.

Пример для файла, уже помещённого оператором в `archives/` текущего проекта:

```powershell
uv run python -m services.knowledge.cli import `
  --project C:\path\to\locestra `
  --source archives\example.md `
  --approved --dry-run
```

Тот же вызов без `--dry-run` публикует новую atomic generation. Импортер читает source только для получения знания и не изменяет, не перемещает и не удаляет исходный файл.

## Поддерживаемые adapters

| Adapter | Допустимый input | Поведение |
|---|---|---|
| Markdown | UTF-8 `.md` | Deterministic bounded chunks по строкам и headings. |
| Text | UTF-8 `.txt` | Deterministic bounded chunks по строкам. |
| Conversation JSON | UTF-8 `.json`: list или объект `conversations`, далее list объектов с `messages` | Извлекаются только поддерживаемые text/content leaves; malformed или неизвестная top-level shape отклоняется. |
| Conversation HTML | UTF-8 `.html` с message containers, имеющими `data-role` | Active tags (`script`, `iframe`, `object`, `embed`, `base`), malformed nesting или неизвестная структура отклоняются. |
| Project docs/config | Только project allowlist и поддерживаемые текстовые extensions | Используется тот же privacy/parser pipeline; generic malformed JSON отклоняется. |

Binary media, SQLite, logs, model files, generated/vendor trees и неизвестные export formats не импортируются этим adapter. Новый формат требует отдельного bounded parser, fixture, negative privacy tests и обновления source inventory; переименование произвольного файла в `.json` или `.html` не считается поддержкой формата.

## Pipeline

```text
explicit file registration + consent
  -> canonical scope/path validation
  -> bounded no-follow read
  -> secret and structured-content scan
  -> format-specific parse
  -> normalized bounded fragments
  -> content/source hashes and provenance
  -> generation staging
  -> deduplication/conflict detection
  -> atomic generation activation
  -> optional memory candidate proposal
  -> separate Memory Engine confirmation
```

Archive text всегда маркируется `untrusted` и `local_only`; инструкции внутри сообщения или документа являются данными и не расширяют permissions. Conversation adapters принудительно повышают effective sensitivity до `sensitive`.

Каждый fragment имеет `source_id`, `source_uri`, source hash/size/mtime, generation, locator/line range, parser/version, extraction method, derivation/policy version, observed time, sensitivity и freshness status. Полный raw export не превращается в Memory Engine record.

## Facts, decisions и Memory boundary

Только явные строки формы `Fact: key = value`, `Decision: key = value` и их русские эквиваленты создают knowledge candidates. Это deterministic extraction, а не подтверждение истинности.

Если разные active sources дают разные values одному key, создаётся видимый conflict; winner автоматически не выбирается. Чтобы перенести не конфликтующий active candidate в Controlled Memory Engine, оператор должен:

1. выбрать точный `fact_id`;
2. вызвать `propose-memory` с literal confirmation `PROPOSE-MEMORY`;
3. проверить созданный Memory Engine record;
4. отдельно подтвердить его через `services.memory.cli confirm`.

До шага 4 запись остаётся `candidate`. Archive, repository text и model summary никогда не становятся active long-term memory автоматически.

## Запрещённые источники

Fail-closed исключаются, в частности:

- `.env*`, credentials, keys, cookies, browser profiles и credential directories;
- `.git`, `.ssh`, `.aws`, `.azure`, `.kube`, runtime/data/log/inbox/output/model/cache directories;
- databases, logs, private-key/model/checkpoint extensions и binary/NUL input;
- UNC/device paths, NTFS alternate data streams, encoded/unicode aliases, symlink/junction/reparse и hardlink sources;
- path escape за пределы canonical project;
- source или metadata с распознанным token/password/key/cookie/secret material;
- произвольный личный каталог, даже если оператор считает его «рядом» с проектом.

Детектор patterns/structure/entropy не является полной DLP-гарантией. Поэтому allowlist, минимизация, explicit source selection и запрет намеренно импортировать secrets остаются обязательными.

## Incremental re-import и invalidation

Повторный import того же source hash/size/mtime, parser/derivation/policy и не более слабой sensitivity idempotent. Изменение content, mtime, parser, policy или sensitivity создаёт новую source version и generation. Retrieval перед выдачей повторно проверяет live source; удалённый, изменённый, вышедший из allowlist или переставший быть Git-tracked source не выдаётся как fresh.

Source deletion из Knowledge Engine не удаляет исходный архивный файл: это отдельное пользовательское действие за пределами importer. Preview-first purge выполняется по exact `source_id`; применение требует literal confirmation, равного этому ID. Связанные knowledge fragments/facts/index и promoted memory provenance удаляются/инвалидируются в согласованном scope.

После logical delete движок включает SQLite secure-delete, WAL checkpoint/truncate и `VACUUM`; открытый reader может отложить physical compaction. Команда `compact` повторяет compaction. Даже успешный physical result не гарантирует forensic erase из OS snapshots, backups, pagefile, внешних копий или wear-levelled SSD.

## Operational checklist для нового архива

1. Получить архив от владельца и поместить только нужный файл в ignored `archives/` зарегистрированного проекта.
2. Добавить source в [IMPORT_SOURCES.md](../knowledge/IMPORT_SOURCES.md) со status `discovered`, форматом, scope, sensitivity и approval state; не копировать secret values.
3. Запустить explicit `import --approved --dry-run`.
4. Проверить status/reason, source kind, fragment count, sensitivity и отсутствие unexpected data.
5. Запустить тот же import без `--dry-run`.
6. Проверить `retrieve` с узким query/budget и provenance каждой выдачи.
7. Просмотреть conflicts/candidates; не подтверждать memory автоматически.
8. Обновить inventory status на `imported` только после проверенного runtime result.
9. При удалении сначала выполнить purge preview, затем exact-ID apply и проверить `complete`; source file и внешние copies обработать отдельно.

## Что намеренно не реализовано

- discovery или batch import произвольных пользовательских каталогов;
- автоматический import Open WebUI, n8n, Telegram, Codex inbox или browser histories;
- универсальный ChatGPT/Fantik adapter без предоставленного формата и fixture;
- OCR/audio/image parsing;
- vector embeddings и semantic reranker без измеримого eval;
- cloud sync/archive upload;
- обещание forensic secure erase.
