# Модель безопасности

## Stage 004 Knowledge/Archive boundary

- Knowledge data хранится в отдельной owner-only SQLite DB и полностью пересоздаётся из явно разрешённых sources; это не active Memory и оно не отправляется cloud executor автоматически.
- Manual import требует exact project/source registration и consent. Repository index читает только Git-tracked paths внутри registered canonical worktree. `.env`, credentials, cookies, keys, databases, model blobs, generated/vendor/runtime directories, binary/non-UTF-8 и secret-bearing payload отклоняются до publication.
- Standard local linked worktree разрешён только при точной bounded цепочке `.git` pointer → `.git/worktrees/<name>` → local common `.git` → backlink. UNC/device metadata, arbitrary external layout, reparse/hardlink metadata, config includes и object alternates fail closed.
- Archive/repository text всегда `untrusted` и `local_only`; prompt внутри source не выдаёт permission. Retrieval exact owner/project scoped, budgeted и повторно проверяет current policy/parser/source/tracked state.
- Source deletion preview-first и требует exact confirmation. Coordinated Memory purge выполняется до Knowledge deletion с SQLite secure-delete/checkpoint/VACUUM evidence. Это не является обещанием forensic erase из SSD wear leveling, filesystem snapshots, backups, pagefile, external applications или original source.
- Текущий CLI использует single-user owner namespace. Knowledge CRUD/retrieval HTTP endpoint отсутствует; будущий gateway/Telegram boundary обязан выводить owner из authenticated session, а не принимать caller-supplied label.

- Статус: baseline этапов 000–001 и завершённый control-plane hardening этапа 002, обновлено 2026-07-14.
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
- Gateway сериализует Qwen/Codex per worktree и тяжёлые GPU flows in-process.
- Codex получает configurable sandbox; чистый review запускается read-only.
- Invalid explicit project path не заменяется default path.
- Leading overrides `/local`, `/codex`, `/voice`, `/vision`, `/image`, `/browser` stripped и не обходят project/critical/network/capability/cloud gates; conflicts блокируются.
- Обычный ingress не выдаёт scoped Codex approval: high-risk/Codex route создаёт local bounded/redacted handoff и typed non-success response. Enable flag больше не трактуется как permission.
- После двух Qwen failures создаётся один create-exclusive handoff с bounded goal/constraints/acceptance/evidence; recursion/третья automatic attempt запрещены.
- Qwen read-only execution использует approval mode `plan`, write execution — `yolo`; Qwen/Codex prompt передаётся через stdin, а не остаётся в process command line.
- Documentation route всегда запускает Qwen/Context7 в neutral `run/docs-workspace`, даже при explicit project; committed profile копируется в ignored writable `run/qwen-homes/qwen-docs`, поэтому MCP runtime state не загрязняет worktree.
- Browser Router блокирует literal local/private targets; adapter проверяет DNS resolutions, redirects и HTTP(S) subrequests и запрещает credentials in URL.
- Inline chat audio декодируется только из valid base64, ограничивается routing attachment budget и передаётся локальному Whisper endpoint; inline payload не попадает в `NormalizedRequestV1`.
- Committed configuration отделена от env-only credential; единый typed resolver применяет precedence defaults → `config/platform.json` → `.env` → process environment, отклоняет secret/unknown committed keys и fail-fast блокирует несогласованный override lifecycle-owned addresses.
- Gateway создаёт строгие `NormalizedRequestV1`, `PlanV1`, `RouteDecisionV1`, `ExecutionAttemptV1` и `TaskStateV1`; inline binary не копируется в internal request/task JSON.
- SQLite migration additive: legacy rows сохранены, `created_at` не сбрасывается при update, а новые state snapshots versioned и не дублируют raw prompt/result.
- `/health/live` не зависит от внешних probes; `/health/ready` отдельно проверяет required dependencies; optional/on-demand/disabled capabilities не маскируются под ready.
- Host lifecycle хранит versioned ownership records, сверяет identity/root token/path boundary/start time/port и не останавливает чужой listener; sibling-prefix collision покрыт regression test, unowned fast listener отклоняется, externally managed strong Ollama на `11434` не присваивается платформой.
- Doctor/smoke/unit tests существуют; foundation validator включён в doctor и проверяет Stage 001 documents/diff candidates без чтения secret stores.

## Известные остаточные риски

Эти пункты обнаружены в коде/config 2026-07-14 и не должны называться устранёнными:

1. **Critical attack chain:** Open WebUI без auth, gateway/voice `0.0.0.0` и unrestricted Docker port publishing позволяют сетевому клиенту достичь gateway; gateway auth/workspace allowlist отсутствуют, а Qwen работает `yolo` с правами Windows-пользователя. Это может дать чтение/изменение любого существующего пути и доступ к UI history/served outputs. До исправления ingress должен быть ограничен localhost/host firewall и только доверенным пользователем.
2. Qwen Code write tasks используют `--approval-mode yolo`; read-only tasks используют `plan`, но OS-level read-only sandbox и independent enforcement всё ещё отсутствуют.
3. Любой существующий путь может стать workspace; общего allowlist и junction/symlink policy нет.
4. Codex cloud execution через current ingress fail-closed, но sensitivity classification, scoped approval ledger и transfer provenance отсутствуют. Pattern redaction handoff не является полным DLP.
5. Telegram actor allowlist отсутствует; любой actor с доступом к bot может вызвать общий gateway и получить автоматический ответ.
6. Browser проверяет public initial URL, redirects и HTTP(S) subrequests, но WebSocket/service-worker paths, общий outbound proxy/audit, DNS pinning на socket layer и доказанная защита от всех rebinding/TOCTOU вариантов отсутствуют. External navigation текущего revision ещё не прошла final E2E gate.
7. Legacy columns task journal и `%TEMP%\local-agent-*.txt` могут хранить полный prompt/result; `inbox/*-codex.md` применяет bounded pattern redaction, но не полный DLP. TTL/delete API отсутствуют; Open WebUI/n8n volumes имеют отдельный persistence lifecycle.
8. `main`, `latest` и `npx -y ...@latest` не только дрейфуют, но и исполняют mutable third-party code с правами пользователя.
9. Locks находятся в памяти одного gateway process и не являются межпроцессными leases.
10. ComfyUI запускается on-demand и временно конкурирует со strong model за GPU; его lock является in-process и требует межпроцессного усиления на следующих этапах.
11. Gateway/voice не имеют общего auth/rate limits; chat audio имеет bounded limit, но standalone voice upload всё ещё читается целиком и требует собственного request-size/job policy.
12. Process ownership снижает риск остановки чужого listener, но start/stop ещё не имеют межпроцессного mutex, полный start не транзакционен, а legacy PID migration не имеет nonce.

До устранения этих рисков платформа предназначена только для одного доверенного пользователя с ingress, технически ограниченным этим host. «Доверенная LAN» недостаточна; публичный и multi-user доступ запрещены.
