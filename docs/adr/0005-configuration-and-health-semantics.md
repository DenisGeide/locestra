# ADR 0005: Layered configuration and required/optional health semantics

- Статус: Accepted.
- Дата: 2026-07-14.
- Владелец: владелец платформы.
- Изменение: несовместимая семантика требует нового ADR и migration.

## Контекст

Модели/endpoints дублировались в Python modules, `.env.example`, Compose и scripts. Gateway health объединял обе Ollama probes в один failure и не отличал liveness, core readiness и optional capability degradation. Legacy scripts зависят от полей `/health.status` и model-present.

## Решение

Python services используют один typed resolver: safe defaults → `config/platform.json` → `.env` → process environment. Secrets являются env-only. Protected ports меняются только целостной migration.

Health v1 отдельно публикует liveness, readiness и capability states. SQLite, fast model и strong model являются required для gateway readiness; optional failure даёт canonical `degraded`, но не блокирует core startup. ComfyUI idle имеет `on_demand`. Legacy `/health` поля сохраняются; добавляются `/health/live` и `/health/ready`.

## Последствия

Один optional module больше не маскирует core readiness, а один Ollama failure не должен стирать успешный status другого. Config/health contracts покрываются unit tests. Полная унификация PowerShell/Compose и port migration остаётся отдельным изменением.
