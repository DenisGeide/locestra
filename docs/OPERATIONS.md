# Operations и lifecycle

- Статус: single-workstation runbook updated through verified Stages 005–006.
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
| Coding tasks | Gateway/Coding Engine | exact task/worktree/container owner records | engine finally/recovery/doctor after identity checks | ignored coding DB/artifacts/worktrees |
| MCP servers | Managed MCP Hub, lazy on demand | exact server/session owner records and process identity | Hub finally/watchdog or `scripts/stop.ps1` | ignored health/circuit/audit/runtime views |

`run/*.owner.json` содержит `version`, `name`, фактический listener/process `pid`, `root`, `port`, `fragments`, root-identity policy, process start time и record timestamp. Совместимый `run/*.pid` содержит фактический PID. Наличие PID-файла само по себе не даёт права завершать процесс: перед adopt/stop повторно проверяются command line, root/port и live identity. Чужой listener вызывает безопасную ошибку.

## Start

Каноническая команда:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
```

Последовательность: runtime directories/config template → генерация/current-user-only DACL gateway credential → frozen Python environment + fail-fast config validation → current-user-only SQLite storage/migration → Docker engine → host strong Ollama availability → fast Ollama → gateway → voice → optional Telegram → Compose → readiness. Каждый wait имеет deadline. Повторный start принимает уже работающий component только после identity, auth boundary и health check.

Fast Ollama `11435` является platform-owned: matching listener без valid owner/legacy evidence отклоняется, а не принимается как host-owned. Это сохраняет симметрию start/stop; только strong Ollama `11434` имеет external ownership.

## Network exposure and authentication

Docker Desktop reaches host services through `host.docker.internal`. For that
reason gateway `8787` and voice `8788` bind `0.0.0.0`; they are not
loopback-only listeners. Every `/v1/*` request to either service requires the
same runtime-generated bearer stored in ignored `run/gateway-api-key.txt`.
`start.ps1` passes it to Open WebUI at runtime, gateway and Telegram forward it
to voice, and startup/doctor verify that unauthenticated calls receive HTTP
401. Plain health remains an unauthenticated lightweight probe and must not
return secrets; any request using the `load_model` health control requires the
same bearer.

Open WebUI `3737` and n8n `5678` are still published by Compose on
`127.0.0.1`. Keep Windows Firewall inbound rules and router port forwarding
closed for `8787/8788`; the scripts do not claim to configure or prove host
firewall policy. Do not run this single-user profile on an untrusted LAN or
expose it publicly.

Скрипт пока не выполняет транзакционный rollback при ошибке в середине. После failure запустите `stop.ps1`, изучите соответствующий log и повторите start. Не удаляйте volumes/data как способ диагностики.

## Health model

- `GET /health/live`: процесс gateway отвечает; внешние зависимости не определяют liveness.
- `GET /health/ready`: required capabilities — SQLite journal, fast Ollama/model и strong Ollama/model.
- `GET /health`: сохраняет legacy `status`, `fast_model_present`, `strong_model_present` для scripts/Open WebUI и добавляет versioned canonical report.
- Optional capability health может быть `ok`, `degraded`, `unavailable`, `disabled` или `on_demand`. Её отказ не делает core readiness false; install-only observations прямо помечаются в `detail`, а не вводят отдельный ложный status.
- Stopped ComfyUI в idle — `on_demand`, не failure.

HTTP 200 означает, что health endpoint ответил; readiness определяется полями report.

Codex is optional in the public baseline. With `ENABLE_CODEX_EXEC=false`, a
missing CLI or logged-out session is reported as an optional warning and does
not fail bootstrap/doctor. Set the flag to `true` only when cloud execution is
intentionally configured; that mode fails closed unless the existing user
Codex CLI is installed and authenticated. Lifecycle scripts inspect but never
create, replace, or modify Codex login/global configuration.

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
| coding worktree lease | exact canonical owned worktree across Coding Engine processes sharing the registry | one task owner/heartbeat per worktree | stale/foreign evidence fails closed; recovery does not guess ownership |
| MCP server/session lock | exact Hub process/session owner | configured server concurrency | lazy lifecycle, bounded call, watchdog/finally cleanup |
| outbound action | target only | будущие Codex/browser/Telegram/n8n actions | общего lock/policy сейчас нет |

Stage 005 provides a cross-process lease for each exact coding worktree, and
Stage 006 owns each managed MCP process/session. A system-wide Resource
Coordinator for `gpu-heavy`, agents, image generation, and outbound recipients
is still planned; gateway-local locks must not be described as global.

## Storage и retention

SQLite, `inbox/`, `outputs/`, logs, `%TEMP%` Codex files и Docker volumes переживают отдельные части lifecycle. Stop не удаляет пользовательские данные. Controlled Memory имеет scoped CLI для retention/export/delete/purge; общего API для task legacy rows, Open WebUI/n8n/inbox/temp всё ещё нет. `data/`, DB/WAL/SHM, backups и файловые memory exports создаются current-user-only; невозможность DACL hardening является ошибкой. Перед очисткой требуется точный scope/backup и отдельное разрешение.

## Обязательная проверка lifecycle change

После изменения scripts: `start → /health/ready → stop → отсутствие owned listeners/processes → start → readiness`, затем doctor, unit tests и простой Open WebUI/gateway stream. Проверка также подтверждает, что PID принадлежит реальному listener, нет дубликатов и strong Ollama/volumes не были удалены.

Stage 005 changes must additionally prove source repository preservation,
worktree/container cleanup, cancellation/timeout descendant cleanup, and
verification/review state. Stage 006 changes must prove registry validation,
generated-view consistency, live Context7/Playwright/local-diagnostics calls,
broken-server isolation, and zero owned MCP processes after stop.

Useful MCP checks:

```powershell
uv run python -m services.mcp_hub.cli validate
uv run python -m services.mcp_hub.cli status
uv run python -m services.mcp_hub.cli doctor --live
uv run python -m services.mcp_hub.cli stop
```
