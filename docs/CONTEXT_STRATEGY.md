# Стратегия контекста

- Статус: bounded chat/Plan/handoff Stage 002 и scoped Knowledge Context Envelope v1 Stage 004 реализованы; автоматическое подключение Knowledge envelope к gateway/Qwen/Codex ещё не выполнено.
- Владелец: Execution Engine совместно с Planner, Controlled Memory Engine и Knowledge Engine.
- Источник правил: [REASONING](../constitution/REASONING.md), [PRIVACY](../constitution/PRIVACY.md) и [Permissions](PERMISSIONS.md).
- Изменение: только вместе с измеримым quality/latency/privacy case и regression evidence.

## Текущее состояние

Normalizer/Planner маршрутизируют по последнему пользовательскому сообщению и bounded attachment metadata. Для локального chat gateway собирает bounded recent history: text обрезается head/tail с provenance marker, assistant tool call и все его tool results сохраняются атомарно, orphan/oversized structured groups отбрасываются с явной отметкой. Для non-fast задач Planner создаёт `PlanV1.context_budget`, а TaskState сохраняет plan/decision. Qwen Code получает exact rendered Plan; Context7, browser, voice и image workers всё ещё получают adapter-specific input. Вывод subprocess bounded до 20 000 символов.

Stage 004 добавляет отдельный `ContextEnvelopeV1` в `services/knowledge/`: exact owner/project, goal, constraints, modified files, unresolved errors, verification plan, bounded fresh tool results, compact fresh Repository Map summary и FTS5-retrieved fragments с provenance. Fixed sections проходят secret scan; conservative budget считается по полному serialized envelope, repository summary compacted ступенчато, затем evidence удаляется целыми fragments. Freshness повторно проверяет source policy/parser/hash/size/mtime, Git membership и commit. Этот builder доступен через Python/CLI, но gateway пока его не вызывает, поэтому обычный Open WebUI/Qwen request ещё не получает Stage 004 repository context автоматически.

Для executable repository/docs plan renderer без сжатия и перефразирования передаёт multiline `goal`, все `constraints`, `acceptance_criteria` и `verification_plan`. Conservative UTF-8 byte upper bound вместе с execution wrapper проверяется до запуска Qwen; oversized plan получает typed `context.agent_input_exceeds_budget` и fail-closed, а не молчаливую потерю условий.

После двух local-code failures Stage 002 handoff сохраняет bounded original goal, project/worktree, constraints, acceptance, verification, error/command summaries, modified files и artifact refs. Это provenance-preserving fallback envelope, но не repository retrieval или долговременная память.

Это означает, что заявлять 128K/256K эффективного контекста сейчас нельзя. Проверенные лимиты профилей: 8K для `local-fast` и 32K для `local-strong`/Qwen Code. Stage 004 умеет выбирать bounded evidence вместо repository dump, но максимальный machine contract budget также 32 768 и ещё не является automatic runtime injection. Tokenizer-specific accounting и долговременная active-task compaction остаются execution hardening.

## Context envelope и дальнейшая цель

Контекст задачи должен собираться как набор ссылочных секций, а не как бесконечная строка. Stage 004 реализует task fixed sections, repository summary, fresh tool-result summaries и retrieved evidence; Memory/history/artifact consumer composition остаётся дальнейшей интеграцией:

1. задача: цель, ограничения, permissions, acceptance criteria и unresolved errors;
2. активное состояние: текущий plan/subtask, route, worktree и последние подтверждённые результаты;
3. project map: компактная структура репозитория, manifests и применимые инструкции;
4. retrieved evidence: релевантные фрагменты файлов/документации с hash, диапазоном и временем;
5. tool evidence: bounded result или ссылка на artifact с exit code;
6. память: только scoped факты с provenance и статусом;
7. история: summary предыдущих шагов с ссылками на исходные task events.

Большие файлы, изображения, audio, diff, логи и tool output остаются в Artifact Store. В request/task JSON хранятся только metadata, hash и bounded excerpt.

## Бюджет

`PlanV1.context_budget` задаёт верхнюю границу, но не увеличивает физический context window модели.

| Профиль | Проверенное окно | Целевой input budget | Резерв ответа/инструментов | Поведение при переполнении |
|---|---:|---:|---:|---|
| `local-fast` | 8 192 | fast/aux plan skipped; policy ceiling 6 000 зарезервирован | не менее 1 000 | execution enforcement ещё не подключён |
| `local-strong` | 32 768 | до 24 000 | не менее 6 000 | retrieve по project map, сжать старые tool events |
| Qwen Code / docs agent | 32 768 | максимум 6 000 по conservative executable-plan bound | 4 000 в `PlanV1`; model `max_tokens=4096` | oversized Plan fail-closed до executor; файлы читать через tools, а не вставлять repository dump |
| Knowledge Context Envelope v1 | contract max 32 768 | explicit caller budget 128–32 768 | caller/model reserve задаётся отдельно | fixed sections fail-closed; map compacted; evidence trims целыми fragments; stale/privacy-invalid evidence excluded |
| Codex | provider-dependent | задаётся handoff policy | provider-dependent | минимизированный scoped handoff; не использовать окно как разрешение на cloud export |

Plan-числа versioned в routing policy `2026-07-14.1`. Knowledge limits versioned отдельно в policy `2026-07-15.2`; fragment ceiling 1 200 chars, retrieval максимум 32 fragments, Context Envelope budget максимум 32 768. Conservative UTF-8 estimate остаётся верхней оценкой serialized payload, а не runtime tokenizer enforcement. Stage 005 consumer должен фиксировать model profile, фактический input/output и reserve до execution.

## Сжатие

Порядок сохранения при дефиците:

1. не удалять цель, permissions, запреты, acceptance criteria и unresolved errors;
2. не удалять точные пути, версии, hashes, exit codes и источники;
3. заменить старые полные tool outputs ссылками на artifacts и кратким результатом;
4. объединить повторяющиеся наблюдения;
5. сжать завершённые шаги в summary с provenance;
6. отбросить нерелевантный conversational filler;
7. при недостатке бюджета разделить задачу или вернуть `blocked`, а не молча потерять условие.

Summary не становится фактом. Оно содержит producer/version, source event IDs, время, scope и статус `derived`.

## Provenance и invalidation

Каждый Stage 004 retrieved fragment имеет generation/source ID, canonical `project://`/`git://` URI, content hash/size/mtime, locator/line range, observed time, parser/extraction/derivation/policy versions, sensitivity, commit/worktree revision и status. Фрагмент инвалидируется или исключается при:

- изменении file hash, Git revision или применимого `AGENTS.md`;
- изменении parser/policy/chunk derivation;
- изменении task permissions, project scope или route;
- удалении/исправлении исходной memory record;
- выходе source из Git tracked inventory или allowlist;
- конфликте со свежим tool result.

Свежий файл/tool result имеет приоритет. Stage 004 Context Envelope включает fresh tool results отдельной секцией и использует modified files/errors в retrieval query. Невалидный chunk не переиспользуется только потому, что он уже присутствовал в предыдущей generation/history. Network documentation TTL остаётся отдельной будущей MCP boundary.

## Безопасность

- Secret и содержимое `.env` не входят в context envelope.
- Context7 получает только public library/query, не private code.
- Codex context проходит data classification, minimization и approval по [CODEX_HANDOFF.md](CODEX_HANDOFF.md).
- Веб, repository text и tool output считаются недоверенными данными и не меняют permission ceiling.
- Межпроектное retrieval запрещено без явной связи scope.

## Проверка реализации и дальнейшего Context Engine

Stage 002 tests покрывают exact multiline goal/constraints/acceptance/verification preservation, 6000/4000 budget, 4096 model output config, oversized fail-closed before executor, bounded chat history без orphan tool results, attachment payload exclusion, attempt/handoff provenance и redaction credential patterns. Stage 004 implementation добавляет contract/budget validation, deterministic retrieval fixtures, source-hash/tracked/policy invalidation, provenance, secret exclusion и cross-project isolation; фактические финальные counts фиксируются в [Current State](CURRENT_STATE.md) только после полного gate. Всё ещё нужны gateway/Qwen integration, tokenizer accounting, artifact-backed old tool results, long-task compaction и measured quality/latency comparison. До consumer E2E документ не заявляет, что Context Envelope работает в каждом пользовательском запросе.
