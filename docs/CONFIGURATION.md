# Конфигурация

- Статус: platform/routing contract plus immutable Stage 005 coding policy and
  canonical Stage 006 MCP registry.
- Владелец: `services.config`; Manifest описывает наблюдаемые факты, но не является runtime parser.
- Коммитить разрешено: public defaults, schema и локальный committed baseline без credentials.
- Запрещено: `.env`, tokens, cookies, keys и secret values.

## Precedence

Python services разрешают конфигурацию один раз при старте процесса:

```text
code-safe defaults
  < config/platform.json
  < .env
  < inherited process environment
```

`config/platform.json` — versioned committed baseline этой workstation. `.env.example` — шаблон имён, не runtime source. `.env` — ignored runtime override. Process environment имеет наивысший приоритет, что соответствует прежнему `load_dotenv(override=False)` поведению.

Gateway, voice и Telegram используют один `PlatformSettings`. Secret
`TELEGRAM_BOT_TOKEN` разрешён только в env layer и представлен `SecretStr`;
committed loader отклоняет его и неизвестные ключи. `GATEWAY_API_KEY` также
env-only: `start.ps1` генерирует его в ignored owner-only runtime file, а
gateway, voice, Open WebUI и Telegram используют одно значение для
OpenAI-compatible `/v1/*` boundary. Config loader не печатает resolved values.

Routing semantics намеренно вынесена из этого precedence в отдельный [config/routing.json](../config/routing.json). Это committed, non-secret policy artifact, а не mutable workstation setting: он не читает `.env` и не допускает process-environment overrides. Такой разрыв сделан специально, чтобы одинаковый Git revision и одинаковые input facts давали одинаковый route.

## Routing policy

`services.orchestration.config` загружает `config/routing.json` в strict frozen Pydantic models и кеширует immutable snapshot. Текущие identifiers:

- `schema_version=1.0` — форма документа;
- `policy_version=2026-07-14.1` — observable routing semantics;
- thresholds — strong-chat length, context/tool limits, executable agent input ceiling 6000 и максимум двух local-code attempts;
- versioned RU/EN marker groups и `planner_routes` allowlist, определяющий routes с bounded `PlanV1`;
- `llm_signal.enabled=false`.

Loader отклоняет неизвестные поля, unsupported schema, duplicate/unbounded marker lists и значения вне bounded ranges. Secret-like дополнительное поле также отклоняется как unknown; routing file не является credential channel. Изменение rules/thresholds требует новой policy version, regression corpus и обновления [Routing](ROUTING.md). Runtime reload не реализован: новый process получает новый cached snapshot.

`llm_signal` сейчас только зарезервированный policy envelope. Planner/Router не вызывают LLM и не имеют hidden fallback к model classification. Включать flag без отдельной bounded implementation, timeout/failure semantics и evaluation gate нельзя.

Stage 001 fail-fast исключение: lifecycle-owned ports и Ollama/ComfyUI endpoint addresses пока продублированы в PowerShell/Compose. Resolver отклоняет их effective override, отличный от compatibility baseline, с требованием coordinated migration. Остальные public settings сохраняют обычный precedence. Это предотвращает split-brain, пока scripts не станут consumer одного runtime snapshot.

## Public keys

| Группа | Ключи | Потребитель |
|---|---|---|
| Models | `LOCAL_FAST_MODEL`, `LOCAL_STRONG_MODEL`, `LOCAL_AGENT_MODEL`, `CODEX_MODEL`, `CODEX_REASONING_EFFORT` | gateway/executors |
| Semantic reviewer identity | `LOCESTRA_OLLAMA_EXECUTABLE`, `LOCESTRA_OLLAMA_EXECUTABLE_SHA256` | coding reviewer/doctor |
| Endpoints | `OLLAMA_BASE_URL`, `FAST_OLLAMA_BASE_URL`, Telegram gateway/voice URL, `COMFYUI_URL` | adapters |
| Protected ports | `GATEWAY_PORT`, `VOICE_PORT`, `OPEN_WEBUI_PORT`, `N8N_PORT` | baseline/scripts/Compose |
| Execution | `DEFAULT_PROJECT`, enable flags, `CODEX_SANDBOX`, `MAX_AUTOMATIC_CHAT_TOOLS` | gateway |
| Voice | Whisper model/device/compute | voice service |

Routing rules, thresholds, planner-route allowlist и LLM-signal policy не являются env keys и принадлежат только versioned `config/routing.json`.

`ENABLE_CODEX_EXEC=false` is the portable public default. In this mode Codex
CLI installation and login are optional: `bootstrap.ps1` continues without
them, and `doctor.ps1` reports a warning/degraded optional capability rather
than failing local readiness. Setting `ENABLE_CODEX_EXEC=true` in ignored
`.env` or the process environment is an explicit operator choice; bootstrap and
doctor then require both a working Codex CLI and an authenticated session.
Neither script edits the user's Codex login or global configuration.

Models/endpoints в [SYSTEM_MANIFEST.md](../SYSTEM_MANIFEST.md) должны соответствовать committed baseline и наблюдаемому runtime. Foundation validator проверяет manifest против публичного template; contract tests проверяют precedence, строгий committed envelope, запрет committed secret и fail-fast protected lifecycle override.

The two semantic-review identity variables are optional and must remain empty
in tracked files. With both unset, the public portable mode discovers Ollama
and derives SHA-256 from one stable regular local executable. For a
pre-established production trust anchor, set an absolute executable path and
its expected SHA-256 in `.env` or the process environment; reviewer and doctor
then reject any mismatch.

Bootstrap also fails closed on strong-model identity: after pulling
`qwen3.6:35b` it verifies the pinned base manifest digest before creating
`local-strong`, then verifies the created alias against
`config/coding.json`. A changed upstream tag or locally drifted alias is an
explicit update/evaluation event, not something bootstrap silently accepts.

## Порты и миграция

Порты `3737/8787/8788/5678/11434/11435` и текущий ComfyUI address являются protected compatibility boundary. Lifecycle/doctor/smoke, image scripts, Python и Docker должны изменяться одним migration diff. Простая правка `.env`/committed config отклоняется resolver, пока такой migration не обновит compatibility baseline и все consumers.

До отдельной port migration используйте committed значения. Start не должен молча принять чужой listener на protected port: process ownership проверяется по identity.

## Qwen configuration

Gateway использует два committed immutable profiles:

- `config/qwen-code/settings.json` — local coding model, output ceiling 4096, без MCP servers;
- `config/qwen-docs/settings.json` — та же model policy и только `context7` MCP.

Qwen может записывать в `QWEN_HOME`, поэтому adapter не направляет CLI в committed directory. Перед запуском он байт-в-байт копирует выбранный profile в ignored writable `run/qwen-homes/qwen-code` или `run/qwen-homes/qwen-docs`, затем передаёт `QWEN_HOME` только Qwen child process. Codex/Node/PowerShell не наследуют этот override.

Stage 005 coding policy lives in `config/coding.json` and has no environment
override. Coding Qwen receives a generated task-scoped profile with no MCP,
hooks, extensions, or repository-provided settings.

Stage 006 MCP configuration has one canonical source:
`config/mcp-registry.json`. Tracked Qwen base profiles contain no MCP
definitions. Startup or `services.mcp_hub.cli generate` creates ignored runtime
views: platform Qwen receives local diagnostics, documentation Qwen receives
the exact Context7 allowlist, and coding Qwen receives none. Context7 and
Playwright dependencies are exact-version locked; mutable `@latest` definitions
are not the public source of truth.

## Docker и host environment

Compose применяет стандартный порядок Docker interpolation: shell environment → `.env` → `${default}`. Его resolved config должен совпадать с protected baseline. Ollama scheduler variables устанавливаются host/start script; doctor сейчас проверяет user-level values, но не доказывает environment уже работающего host Ollama процесса.

## Изменение конфигурации

1. Изменить committed baseline/schema без secret; для routing semantics отдельно повысить `policy_version`.
2. Обновить `.env.example`, Manifest и affected scripts/adapters.
3. Добавить precedence/validation test, а для routing — fixed-corpus regression cases.
4. Для endpoint/model/port выполнить doctor и relevant E2E.
5. Не копировать resolved `.env` в report, logs или Git.

For an MCP change, update the canonical registry and dependency lock together,
then run schema/duplicate validation, generated-view consistency, permission and
egress review, live discovery/call, failure isolation, and secret/audit checks.
Do not edit a generated view or global Qwen/Codex profile.

Новый ключ принимается только при наличии реального consumer, default, type/range validation, owner и failure behavior.
