# ADR 0004: Versioned boundary contracts without premature service split

- Статус: Accepted.
- Дата: 2026-07-14.
- Владелец: владелец платформы.
- Изменение: новое несовместимое решение оформляется следующим ADR/schema version.

## Контекст

Gateway уже является работающим OpenAI-compatible control plane, но внутренние request/route/task/tool/artifact структуры не имели строгих границ. Немедленное разбиение монолита на пустые services увеличило бы сложность до появления реальных потребителей.

## Решение

Ввести Pydantic contracts `services.contracts.v1` с `schema_version=1.0` для NormalizedRequest, Plan, RouteDecision, ToolSpec, TaskState и ArtifactMetadata. Внешний `ChatRequest` остаётся permissive для совместимости OpenAI/Open WebUI; после ingress он преобразуется в строгий внутренний contract. Pydantic JSON Schema — единственный machine-readable schema source; ручные копии JSON Schema не коммитятся.

Текущий classifier/executors не переименовываются и не объявляются Planner/Router нового поколения. Новые runtime modules выделяются только при существующем consumer и contract test.

## Последствия

Route names и `/v1` сохраняются. Large binary/tool output запрещён внутри task/request JSON и заменяется artifact reference. Полный Planner/Router остаётся этапом 002; Memory/Knowledge/Tool Registry — последующими этапами.
