# Конфигурация

- Статус: platform contract этапа 001 и immutable routing policy этапа 002.
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

Gateway, voice и Telegram используют один `PlatformSettings`. Secret `TELEGRAM_BOT_TOKEN` разрешён только в env layer и представлен `SecretStr`; committed loader отклоняет его и неизвестные ключи. Config loader не печатает resolved values.

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
| Endpoints | `OLLAMA_BASE_URL`, `FAST_OLLAMA_BASE_URL`, Telegram gateway/voice URL, `COMFYUI_URL` | adapters |
| Protected ports | `GATEWAY_PORT`, `VOICE_PORT`, `OPEN_WEBUI_PORT`, `N8N_PORT` | baseline/scripts/Compose |
| Execution | `DEFAULT_PROJECT`, enable flags, `CODEX_SANDBOX`, `MAX_AUTOMATIC_CHAT_TOOLS` | gateway |
| Voice | Whisper model/device/compute | voice service |

Routing rules, thresholds, planner-route allowlist и LLM-signal policy не являются env keys и принадлежат только versioned `config/routing.json`.

Models/endpoints в [SYSTEM_MANIFEST.md](../SYSTEM_MANIFEST.md) должны соответствовать committed baseline и наблюдаемому runtime. Foundation validator проверяет manifest против публичного template; contract tests проверяют precedence, строгий committed envelope, запрет committed secret и fail-fast protected lifecycle override.

## Порты и миграция

Порты `3737/8787/8788/5678/11434/11435` и текущий ComfyUI address являются protected compatibility boundary. Lifecycle/doctor/smoke, image scripts, Python и Docker должны изменяться одним migration diff. Простая правка `.env`/committed config отклоняется resolver, пока такой migration не обновит compatibility baseline и все consumers.

До отдельной port migration используйте committed значения. Start не должен молча принять чужой listener на protected port: process ownership проверяется по identity.

## Qwen configuration

Gateway использует два committed immutable profiles:

- `config/qwen-code/settings.json` — local coding model, output ceiling 4096, без MCP servers;
- `config/qwen-docs/settings.json` — та же model policy и только `context7` MCP.

Qwen может записывать в `QWEN_HOME`, поэтому adapter не направляет CLI в committed directory. Перед запуском он байт-в-байт копирует выбранный profile в ignored writable `run/qwen-homes/qwen-code` или `run/qwen-homes/qwen-docs`, затем передаёт `QWEN_HOME` только Qwen child process. Codex/Node/PowerShell не наследуют этот override.

Code invocation дополнительно использует `--bare`, explicit OpenAI-compatible local endpoint/model и не загружает repository-provided Qwen hooks/extensions/MCP. Docs invocation запускается read-only, с `--allowed-mcp-server-names context7` и всегда в neutral `run/docs-workspace`, не в user project. Runtime flags (`--model`, approval `plan`/`yolo`) имеют приоритет над `settings.json`. `@upstash/context7-mcp@latest` остаётся честно зафиксированным supply-chain drift.

## Docker и host environment

Compose применяет стандартный порядок Docker interpolation: shell environment → `.env` → `${default}`. Его resolved config должен совпадать с protected baseline. Ollama scheduler variables устанавливаются host/start script; doctor сейчас проверяет user-level values, но не доказывает environment уже работающего host Ollama процесса.

## Изменение конфигурации

1. Изменить committed baseline/schema без secret; для routing semantics отдельно повысить `policy_version`.
2. Обновить `.env.example`, Manifest и affected scripts/adapters.
3. Добавить precedence/validation test, а для routing — fixed-corpus regression cases.
4. Для endpoint/model/port выполнить doctor и relevant E2E.
5. Не копировать resolved `.env` в report, logs или Git.

Новый ключ принимается только при наличии реального consumer, default, type/range validation, owner и failure behavior.
