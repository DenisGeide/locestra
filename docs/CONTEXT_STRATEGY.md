# Стратегия контекста

- Статус: bounded chat/Plan/handoff Stage 002 and scoped Knowledge Context
  Envelope v1 Stage 004 are implemented; Stage 005 adds the verified Coding
  Engine consumer. Context injection is still not universal across every route.
- Владелец: Execution Engine совместно с Planner, Controlled Memory Engine и Knowledge Engine.
- Источник правил: [REASONING](../constitution/REASONING.md), [PRIVACY](../constitution/PRIVACY.md) и [Permissions](PERMISSIONS.md).
- Изменение: только вместе с измеримым quality/latency/privacy case и regression evidence.

## Текущее состояние

Normalizer/Planner маршрутизируют по последнему пользовательскому сообщению и bounded attachment metadata. Для локального chat gateway собирает bounded recent history: text обрезается head/tail с provenance marker, assistant tool call и все его tool results сохраняются атомарно, orphan/oversized structured groups отбрасываются с явной отметкой. Для non-fast задач Planner создаёт `PlanV1.context_budget`, а TaskState сохраняет plan/decision. Qwen Code получает exact rendered Plan; Context7, browser, voice и image workers всё ещё получают adapter-specific input. Вывод subprocess bounded до 20 000 символов.

Stage 004 adds `ContextEnvelopeV1`: exact owner/project, goal, constraints,
modified files, unresolved errors, verification plan, bounded fresh tool
results, compact Repository Map summary, and FTS5-retrieved fragments with
provenance. Stage 005 `CodingContextBuilder` consumes that envelope for the
isolated coding worktree. Ordinary fast/strong chat and documentation routes do
not automatically receive repository retrieval.

Для executable repository/docs plan renderer без сжатия и перефразирования передаёт multiline `goal`, все `constraints`, `acceptance_criteria` и `verification_plan`. Conservative UTF-8 byte upper bound вместе с execution wrapper проверяется до запуска Qwen; oversized plan получает typed `context.agent_input_exceeds_budget` и fail-closed, а не молчаливую потерю условий.

После двух local-code failures Stage 002 handoff сохраняет bounded original goal, project/worktree, constraints, acceptance, verification, error/command summaries, modified files и artifact refs. Это provenance-preserving fallback envelope, но не repository retrieval или долговременная память.

This means the project does not claim universal 128K/256K effective context.
Bounded retrieval and task artifacts replace repository dumps. Exact model
limits are profile configuration, while tokenizer-specific accounting and
long-task active compaction remain future work.

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

Plan limits are versioned in routing policy `2026-07-14.1`; Knowledge limits
are versioned separately in policy `2026-07-15.2`. Conservative UTF-8 estimates
remain serialized-payload bounds rather than tokenizer enforcement. The Stage
005 coding consumer records its profile, bounded context artifact, attempts,
and output evidence.

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

Fresh files/tool results take priority. Stage 004 Context Envelope keeps fresh
tool results separate and uses modified files/errors in retrieval. Invalid
chunks are not reused from history. Stage 006 establishes the external
Context7 boundary for current documentation, but does not merge network docs
into repository Knowledge automatically.

## Безопасность

- Secret и содержимое `.env` не входят в context envelope.
- Context7 получает только public library/query, не private code.
- Codex context проходит data classification, minimization и approval по [CODEX_HANDOFF.md](CODEX_HANDOFF.md).
- Веб, repository text и tool output считаются недоверенными данными и не меняют permission ceiling.
- Межпроектное retrieval запрещено без явной связи scope.

## Проверка реализации и дальнейшего Context Engine

Stage 002 tests cover exact multiline goal/constraints/acceptance/verification,
budgets, bounded history, handoff provenance, and redaction. Stage 004 adds
contract/budget validation, deterministic retrieval, invalidation, provenance,
secret exclusion, and cross-project isolation. Stage 005 verifies the coding
consumer end-to-end. Tokenizer accounting, artifact-backed old tool results,
long-task compaction, and measured quality/latency comparison remain open; the
document does not claim Context Envelope use for every request.
