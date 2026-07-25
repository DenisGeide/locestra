# Модель безопасности

## Stage 005 Coding Engine boundary

- Coding tasks resolve an exact canonical repository and run in a separately
  owned linked worktree, not directly in the source checkout.
- Qwen runs as an unprivileged user in a digest-pinned container with a
  read-only root, dropped capabilities, bounded processes, task-only mounts,
  and a narrow local-model proxy. Coding MCP is empty.
- Repository rules, worktree identity, source/Git metadata, mutation scopes,
  ignored files, verification evidence, and independent review are revalidated
  before completion or local commit.
- Codex execution requires both explicit scoped cloud permission and public
  classification. Private/sensitive work is not sent through the Stage 005
  contract; a local resumable handoff is produced instead.
- Timeout/cancellation terminates owned descendants; cleanup deletes only an
  exactly owned, clean completed worktree.
- The portable semantic-review default is local trust-on-first-use: it derives
  identity from one stable regular Ollama executable and rechecks the exact
  loopback listener. A stronger pre-established trust anchor is available
  through runtime-only `LOCESTRA_OLLAMA_EXECUTABLE` and
  `LOCESTRA_OLLAMA_EXECUTABLE_SHA256` pins; a mismatch fails closed.

## Stage 006 Managed MCP boundary

- One canonical registry exact-pins evaluated Context7, Playwright MCP, and
  local-diagnostics sources and minimal tool schemas.
- Generated Qwen views are project-scoped; global Qwen/Codex profiles are not
  changed. Coding remains MCP-free.
- Context7 is public-documentation egress only; Playwright MCP is restricted to
  a Hub-owned loopback fixture; local diagnostics has no network/path/command
  authority.
- Calls are bounded, schema-checked, secret-scanned, timeout/cancellation-safe,
  and audited without arguments, results, or content payloads.
- Optional MCP failure is isolated from core chat/coding readiness. Exact owner
  identity is required for stop/cleanup.

## Stage 004 Knowledge/Archive boundary

- Knowledge data хранится в отдельной owner-only SQLite DB и полностью пересоздаётся из явно разрешённых sources; это не active Memory и оно не отправляется cloud executor автоматически.
- Manual import требует exact project/source registration и consent. Repository index читает только Git-tracked paths внутри registered canonical worktree. `.env`, credentials, cookies, keys, databases, model blobs, generated/vendor/runtime directories, binary/non-UTF-8 и secret-bearing payload отклоняются до publication.
- Standard local linked worktree разрешён только при точной bounded цепочке `.git` pointer → `.git/worktrees/<name>` → local common `.git` → backlink. UNC/device metadata, arbitrary external layout, reparse/hardlink metadata, config includes и object alternates fail closed.
- Archive/repository text всегда `untrusted` и `local_only`; prompt внутри source не выдаёт permission. Retrieval exact owner/project scoped, budgeted и повторно проверяет current policy/parser/source/tracked state.
- Source deletion preview-first и требует exact confirmation. Coordinated Memory purge выполняется до Knowledge deletion с SQLite secure-delete/checkpoint/VACUUM evidence. Это не является обещанием forensic erase из SSD wear leveling, filesystem snapshots, backups, pagefile, external applications или original source.
- Текущий CLI использует single-user owner namespace. Knowledge CRUD/retrieval HTTP endpoint отсутствует; будущий gateway/Telegram boundary обязан выводить owner из authenticated session, а не принимать caller-supplied label.

- Статус: baseline/control-plane model updated through verified Stage 005
  coding and Stage 006 managed MCP boundaries; remaining risks are listed
  explicitly below.
- Владелец: владелец платформы.
- Применяется к: локальным процессам, Docker, Ollama, Codex, MCP/tools, данным и интерфейсам.
- Контроль: [SECURITY.md](../constitution/SECURITY.md), [PERMISSIONS.md](PERMISSIONS.md), validator, tests и review.
- Изменение: при новом data flow, endpoint, credential, external recipient или изменении trust boundary.

## Активы

- пользовательские репозитории и незакоммиченные изменения;
- prompts, документы, attachments и task history;
- credentials, tokens, cookies и account sessions;
- Git history/remotes;
- модели, system prompts, конфигурация и approval policy;
- CPU/RAM/GPU/VRAM, локальные процессы и availability;
- generated artifacts, logs, inbox и automation data.

## Границы доверия

| Граница | Доверие и риск |
|---|---|
| Пользователь → gateway/interfaces | Ввод может содержать ошибочные пути, команды и секреты; требуется normalization/scope. |
| Репозиторий/attachment → agent | Содержимое недоверенное и может включать prompt injection или вредоносный build. |
| Agent → terminal/Git/filesystem | Высокий риск мутаций; нужен workspace scope, args, timeout и diff review. |
| Gateway → локальные Ollama/Qwen/Whisper | Локальный data flow, но model output не является trusted evidence. |
| Platform → Codex | Cloud boundary; возможна передача private code, требуется classification/approval и redaction. |
| Platform → browser/Context7/MCP | External/network input; SSRF, prompt injection и dependency drift. |
| Host → Docker containers | Отдельные процессы/volumes; опубликованные порты и auth требуют контроля. |
| Telegram/n8n → gateway | Внешний actor/webhook; нужны authentication, allowlist, idempotency и size limits. |
| Self-improvement → policy/config | Protected boundary; proposal не может сам себя одобрить или применить. |

## Основные угрозы и целевые контрмеры

| Угроза | Контрмера |
|---|---|
| Path traversal/работа не с тем проектом | Canonical explicit workspace, allowlist, symlink/junction check; invalid explicit path не получает fallback. |
| Потеря пользовательского diff | Запрет reset/stash/checkout чужих изменений, task worktree и ownership lock. |
| Command injection/вредоносный build | Argument arrays, bounded commands, sandbox, read-only semantics, timeout и approval. |
| Prompt injection из кода/веба | Tool output как данные, policy precedence, permission ceiling и independent review. |
| Secret leakage | Не читать secret stores для общей диагностики, redaction, forbidden paths, staged/unstaged diff и только explicit owned foundation candidates. |
| Несанкционированная cloud передача | Data classification, scoped approval, redacted handoff и provenance. |
| Несанкционированный внешний action | Actor allowlist, recipient approval, outbox/idempotency и audit. |
| Ложное завершение | Objective artifact/test/diff и reviewer; `skipped`/HTTP 200 не равны success. |
| Resource denial/VRAM conflict | GPU/worktree locks, queue, timeout и degraded capability. |
| Supply-chain drift | Version/source recording, pinned dependencies where practical и staged upgrades with regression. |

## Подтверждённые текущие controls

- `.env`, runtime data/logs/models и generated outputs игнорируются Git.
- Gateway coordinates coding requests while the Stage 005 Coding Engine adds an
  exact cross-process worktree lease, owned container/process cleanup, and
  durable task state. This is not yet a system-wide resource coordinator.
- Codex receives strict task-derived read-only/workspace-write profiles only
  for explicitly approved public data; ordinary ingress remains local.
- Invalid explicit project path не заменяется default path.
- Leading overrides `/local`, `/codex`, `/voice`, `/vision`, `/image`, `/browser` stripped и не обходят project/critical/network/capability/cloud gates; conflicts блокируются.
- Обычный ingress не выдаёт scoped Codex approval: high-risk/Codex route создаёт local bounded/redacted handoff и typed non-success response. Enable flag больше не трактуется как permission.
- После двух Qwen failures создаётся один create-exclusive handoff с bounded goal/constraints/acceptance/evidence; recursion/третья automatic attempt запрещены.
- Qwen coding execution runs inside the Stage 005 task container with
  task-derived read-only/write mounts, a narrow model proxy, no coding MCP, and
  prompt input outside the process command line.
- Documentation execution uses a fresh neutral per-request workspace and the
  Stage 006 generated Context7-only view; runtime MCP state does not touch a
  user repository.
- Browser Router блокирует literal local/private targets; adapter проверяет DNS resolutions, redirects и HTTP(S) subrequests и запрещает credentials in URL.
- Inline chat audio декодируется только из valid base64, ограничивается routing attachment budget и передаётся локальному Whisper endpoint; inline payload не попадает в `NormalizedRequestV1`.
- Committed configuration отделена от env-only credential; единый typed resolver применяет precedence defaults → `config/platform.json` → `.env` → process environment, отклоняет secret/unknown committed keys и fail-fast блокирует несогласованный override lifecycle-owned addresses.
- Gateway создаёт строгие `NormalizedRequestV1`, `PlanV1`, `RouteDecisionV1`, `ExecutionAttemptV1` и `TaskStateV1`; inline binary не копируется в internal request/task JSON.
- SQLite migration additive: legacy rows сохранены, `created_at` не сбрасывается при update, а новые state snapshots versioned и не дублируют raw prompt/result.
- `/health/live` не зависит от внешних probes; `/health/ready` отдельно проверяет required dependencies; optional/on-demand/disabled capabilities не маскируются под ready.
- Host lifecycle хранит versioned ownership records, сверяет identity/root token/path boundary/start time/port и не останавливает чужой listener; sibling-prefix collision покрыт regression test, unowned fast listener отклоняется, externally managed strong Ollama на `11434` не присваивается платформой.
- Doctor/smoke/unit tests существуют; foundation validator включён в doctor и проверяет Stage 001 documents/diff candidates без чтения secret stores.

## Известные остаточные риски

These remain after Stages 005–006 and must not be described as solved:

1. The platform is still single-user. Open WebUI/n8n publish only on loopback,
   but gateway/voice bind host IPv4 interfaces because Docker Desktop reaches
   them through `host.docker.internal`. Their `/v1/*` routes require the same
   runtime-generated bearer credential, as does the voice model-load health
   action; plain health remains lightweight. This is not host-only binding,
   does not provide hostile multi-user authorization, and must be combined
   with closed inbound firewall/port-forwarding rules for ports `8787/8788`.
2. Stage 005 constrains each coding task, but a general actor-to-workspace
   allowlist and central policy ledger do not yet cover the whole platform.
3. The Codex contract is intentionally public-data-only. A broader
   classification/approval/transfer-provenance system for private code does not
   exist.
4. Telegram actor authorization and a unified outbound recipient/action policy
   are incomplete.
5. The direct browser adapter covers public initial URLs, redirects, and
   subrequests, but no component claims complete protection against every
   browser/network rebinding, service-worker, or protocol attack.
6. Historical task rows and external UI/automation volumes can have retention
   lifecycles outside the new Memory/Coding/MCP stores. Pattern redaction is not
   full DLP or forensic erase.
7. Exact pinning reduces Stage 005/006 supply-chain drift, but other upstream
   images/tools may still use mutable tags and every local dependency remains a
   trust decision.
8. The MCP Hub owns and cleans its exact process trees, but it is not an OS
   sandbox; evaluated Node/Python servers run with the local user's declared
   network/filesystem boundary.
9. ComfyUI can contend with the strong model for GPU capacity. System-wide
   resource scheduling remains later work.
10. The voice compatibility endpoint does not yet provide the durable bounded
    long-audio jobs, retention, and cancellation contract planned for Stage 008.
11. Whole-platform start/stop is not a distributed transaction. A hard host or
    daemon crash can leave stale ownership evidence requiring doctor/recovery.

До устранения этих рисков платформа предназначена только для одного доверенного пользователя с ingress, технически ограниченным этим host. «Доверенная LAN» недостаточна; публичный и multi-user доступ запрещены.
