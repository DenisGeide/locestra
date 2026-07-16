# Глоссарий

- Статус: канонические термины платформы.
- Владелец: владелец платформы.
- Применение: документация, schemas, prompts и отчёты.
- Изменение: добавлять термин только при реальном неоднозначном употреблении.

| Термин | Значение |
|---|---|
| Capability | Проверяемая возможность с владельцем, контрактом, permissions, health и failure behavior. |
| Component | Процесс, сервис или модуль, реализующий одну или несколько capabilities. |
| Model | Конкретные веса/идентификатор модели, например `qwen3.6:35b`. |
| Model profile | Стабильное имя и runtime-параметры модели, например `local-strong`; профиль можно обновить без смены потребителей. |
| Route | Воспроизводимое решение, каким executor/capability выполнить запрос. |
| NormalizedRequest | Строгий versioned internal request после ingress normalization; не содержит inline binary или credential values. |
| RouteDecision | Versioned результат выбора logical route/executor/profile/fallback/risk/locks; сам по себе не является approval. |
| Router | Policy-компонент, создающий route decision; не является моделью или исполнителем. |
| Planner | Компонент, нормализующий цель, ограничения, риск, критерии и шаги задачи. Полный planner запланирован на этап 002. |
| Executor | Модель/агент/инструмент, фактически выполняющий действие. |
| Reviewer | Логически независимая проверка цели, diff, evidence, безопасности и permissions. |
| Workspace | Явно выбранный и разрешённый корень файловой работы; не весь компьютер. |
| Worktree | Git working tree; отдельный task worktree является будущей изоляцией изменяющих coding-задач. |
| Local | Обработка на этом компьютере без передачи task content внешнему model provider. |
| Cloud | Внешняя обработка через сеть; Codex относится к cloud даже при запуске локальным CLI. |
| Approval | Явное, scoped и ограниченное по времени разрешение на конкретное действие/данные/получателя. |
| Capability flag | Техническая доступность функции. `ENABLE_CODEX_EXEC=true` не доказывает approval на приватные данные. |
| Permission ceiling | Максимум действий, разрешённых policy; задача может его сузить, но не расширить. |
| Provenance | Источник, время, producer/version, scope и transformations факта или артефакта. |
| Evidence | Проверяемый результат: файл, hash, exit code, test report, API response или tool event. |
| Artifact | Сохранённый результат задачи с owner/task id, provenance, retention и sensitivity. |
| TaskState | Versioned bounded snapshot выполнения с status, attempts, executor, project/worktree, artifact refs, modified files, unresolved errors и next action. |
| Task journal | SQLite legacy history плюс versioned `TaskState` snapshots; это не append-only event log и не полноценная долгосрочная память. |
| Liveness | Доказательство, что process/event loop отвечает; не обещает готовность dependencies. |
| Readiness | Доказательство, что required dependencies готовы принимать запросы; optional capability может быть degraded независимо. |
| Memory | Управляемые долгоживущие данные с provenance, просмотром, исправлением и удалением; этап 003. |
| Knowledge index | Производный поисковый индекс источников/кода с invalidation и source references; этап 004. |
| Handoff | Структурированная передача цели, context, errors и verification другому executor. |
| Degraded | Основной сервис жив, но одна или несколько optional capabilities недоступны. |
| Verified | Факт подтверждён актуальным объективным evidence. |
| Discovered | Факт найден в коде/config, но не проверен end-to-end. |
| Unavailable | Проверенная capability сейчас недоступна или завершилась ошибкой. |
| Planned | Целевое состояние, которое ещё не реализовано. |
| Protected file | Policy/config, которую self-improvement не может автономно изменить или применить. |

Нормативные правила находятся в [Конституции](../constitution/CORE.md); изменяемые технические факты — в [SYSTEM_MANIFEST.md](../SYSTEM_MANIFEST.md).
