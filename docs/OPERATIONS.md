# Operations и lifecycle

- Статус: runbook этапа 001 для одного Windows workstation.
- Владелец: scripts control plane; strong Ollama/Desktop Docker имеют внешнее host ownership.
- Канонические endpoints/models: [SYSTEM_MANIFEST](../SYSTEM_MANIFEST.md); конфигурация: [CONFIGURATION](CONFIGURATION.md).
- Изменение: вместе с lifecycle test и rollback/recovery update.

## Process ownership

| Component | Start owner | Runtime identity | Stop owner | Persistent state |
|---|---|---|---|---|
| Gateway `8787` | `scripts/start.ps1` | project Python + `services.gateway.app:app` + port | `scripts/stop.ps1` после identity check | SQLite/logs |
| Voice `8788` | `scripts/start.ps1` | project Python + `services.voice.app:app` + port | `scripts/stop.ps1` после identity check | model cache/logs |
| Fast Ollama `11435` | `scripts/start.ps1` | Ollama `serve` owning protected port | `scripts/stop.ps1` после identity check | Ollama model store/logs |
| Telegram | `scripts/start.ps1`, только при runtime token | project Python + `services.telegram.bot` | `scripts/stop.ps1` после identity check | Telegram external session/logs |
| ComfyUI `8388` | `scripts/start-images.ps1` on demand | portable Python + exact `main.py`/port | `scripts/stop-images.ps1` после identity check | model/output directories |
| Strong Ollama `11434` | Ollama host/Desktop or start fallback | host Ollama | не принадлежит platform stop | Ollama model store |
| Open WebUI/n8n | Docker Compose project `local_agent` | named containers/project labels | `scripts/stop.ps1` (loads ignored gateway credential for Compose parsing) | named volumes сохраняются |

`run/*.owner.json` содержит `version`, `name`, фактический listener/process `pid`, `root`, `port`, `fragments`, root-identity policy, process start time и record timestamp. Совместимый `run/*.pid` содержит фактический PID. Наличие PID-файла само по себе не даёт права завершать процесс: перед adopt/stop повторно проверяются command line, root/port и live identity. Чужой listener вызывает безопасную ошибку.

## Start

Каноническая команда:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
```

Последовательность: runtime directories/config template → генерация/owner-only DACL gateway credential → frozen Python environment + fail-fast config validation → owner-only SQLite storage/migration → Docker engine → host strong Ollama availability → fast Ollama → gateway → voice → optional Telegram → Compose → readiness. Каждый wait имеет deadline. Повторный start принимает уже работающий component только после identity, auth boundary и health check.

Fast Ollama `11435` является platform-owned: matching listener без valid owner/legacy evidence отклоняется, а не принимается как host-owned. Это сохраняет симметрию start/stop; только strong Ollama `11434` имеет external ownership.

Скрипт пока не выполняет транзакционный rollback при ошибке в середине. После failure запустите `stop.ps1`, изучите соответствующий log и повторите start. Не удаляйте volumes/data как способ диагностики.

## Health model

- `GET /health/live`: процесс gateway отвечает; внешние зависимости не определяют liveness.
- `GET /health/ready`: required capabilities — SQLite journal, fast Ollama/model и strong Ollama/model.
- `GET /health`: сохраняет legacy `status`, `fast_model_present`, `strong_model_present` для scripts/Open WebUI и добавляет versioned canonical report.
- Optional capability health может быть `ok`, `degraded`, `unavailable`, `disabled` или `on_demand`. Её отказ не делает core readiness false; install-only observations прямо помечаются в `detail`, а не вводят отдельный ложный status.
- Stopped ComfyUI в idle — `on_demand`, не failure.

HTTP 200 означает, что health endpoint ответил; readiness определяется полями report.

## Stop

```powershell
powershell -ExecutionPolicy Bypass -File scripts/stop.ps1
```

Stop завершает только component с совпавшей ownership identity, ждёт bounded timeout и проверяет исчезновение процесса/listener. Strong Ollama `11434` не останавливается. Compose down удаляет containers/network, но сохраняет named volumes. Unknown/reused PID или чужой port не завершается автоматически и требует ручного исследования владельца.

## Recovery

1. Запустить `scripts/doctor.ps1` и проверить exact failed capability.
2. Сопоставить listener PID, owner record и command line; не убивать процесс только по номеру порта.
3. Для owned worker выполнить stop, убедиться, что protected listener исчез, затем start.
4. Проверить `/health/ready`, Open WebUI simple streaming и relevant module test.
5. При SQLite failure сохранить копию базы до migration/repair; journal нельзя молча пересоздавать.
6. При orphan CLI/Chromium исследовать process tree. Текущий `subprocess.run` не имеет Windows Job Object/cancellation tree и это остаётся долгом.

## Resource boundaries

| Lock | Текущий scope | Что реально сериализует | Ограничение |
|---|---|---|---|
| `FAST_MODEL_LOCK` | один gateway process | обычный fast chat | prompt translation/normalization его обходят |
| `GPU_LOCK` | один gateway process | strong chat, Qwen execution, image lifecycle entry | direct processes/другой gateway не видят lock |
| `AGENT_LOCK` | один gateway process | все Qwen jobs | разные worktree также сериализуются |
| `CODEX_LOCK` | один gateway process | все Codex jobs | не является approval/outbound policy |
| `IMAGE_LOCK` | один gateway process | image requests | Comfy script можно вызвать напрямую |
| worktree lock | canonical `realpath/normcase` в одном process | Qwen/Codex для одного path | нет cross-process lease/expiry |
| outbound action | target only | будущие Codex/browser/Telegram/n8n actions | общего lock/policy сейчас нет |

Целевой Resource Coordinator выдаёт owner/lease/timeout для `gpu-heavy`, `worktree`, `agent` и `outbound:<recipient>`. До его реализации in-process locks нельзя называть system-wide.

## Storage и retention

SQLite, `inbox/`, `outputs/`, logs, `%TEMP%` Codex files и Docker volumes переживают отдельные части lifecycle. Stop не удаляет пользовательские данные. Controlled Memory имеет scoped CLI для retention/export/delete/purge; общего API для task legacy rows, Open WebUI/n8n/inbox/temp всё ещё нет. `data/`, DB/WAL/SHM, backups и файловые memory exports создаются owner-only; невозможность DACL hardening является ошибкой. Перед очисткой требуется точный scope/backup и отдельное разрешение.

## Обязательная проверка lifecycle change

После изменения scripts: `start → /health/ready → stop → отсутствие owned listeners/processes → start → readiness`, затем doctor, unit tests и простой Open WebUI/gateway stream. Проверка также подтверждает, что PID принадлежит реальному listener, нет дубликатов и strong Ollama/volumes не были удалены.
