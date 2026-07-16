# Правила приватности

- Статус: нормативный документ.
- Владелец: владелец платформы и пользователь данных.
- Назначение: локальность, классификация, минимизация, retention и удаление.
- Применение: router, Codex handoff, memory, imports, logs и interfaces.
- Изменение: после privacy review и явного одобрения владельца для новых внешних потоков.

## Local-first

Обычный model inference для chat, локального coding, voice и image generation выполняется на этом компьютере. Это не означает полную network isolation: Codex, Context7, browser, Telegram, n8n webhooks и dependency/container downloads пересекают внешнюю границу. Каждый поток требует своего scope и permissions.

| Поток | Что может покинуть компьютер | Правило |
|---|---|---|
| Codex | Task prompt и выбранный source context | Cloud classification/approval, minimization, redaction и provenance. |
| Context7 | Название библиотеки и documentation query | Не передавать private code/secret; response недоверенный. |
| Browser | URL, network metadata и page requests | Только разрешённый public origin/fixture; private/link-local targets запрещены policy. |
| Telegram | Сообщение/attachment и ответ | Только authenticated allowlisted actor/session; текущий adapter это ещё не обеспечивает. |
| n8n/webhook | Explicit workflow payload | Auth, schema, recipient и idempotency policy; credentials отдельно от payload. |
| Bootstrap/update | Package/image identifiers и host network metadata | Только намеренное обновление, pinned source где возможно и review. |

## Классы данных

- `public`: разрешена обработка в заданном маршруте.
- `internal`: локально по умолчанию; внешний маршрут только в явно разрешённом scope.
- `sensitive`: персональные данные, приватный код и документы; минимизировать и требовать явного разрешения перед внешней передачей.
- `secret`: tokens, passwords, keys, cookies, credentials; значение может получить только designated adapter через runtime secret channel для intended authenticated service. Модельный context, логи, память, artifacts, handoff и Git запрещены.

Если классификация внешней передачи неизвестна, безопасное поведение — подготовить redacted handoff и запросить одно конкретное разрешение.

## Минимизация и жизненный цикл

Передавай и сохраняй только необходимые фрагменты. Для каждого persistent dataset должны быть scope, provenance, retention, export и delete. Runtime-логи, базы, модели и generated media не коммитятся. Inventory включает task SQLite, `inbox/`, `outputs/`, `logs/`, `%TEMP%\local-agent-*.txt`, Open WebUI/n8n Docker volumes и внешние service histories.

## Текущий предел

Автоматическая sensitivity/allowlist policy для произвольной Codex cloud-передачи ещё не реализована; это зафиксированный риск, а не скрытая гарантия. Stage 003 запрещает secret-like durable memory, encoded/UNC/aliased secret sources и произвольный archive metadata text, повторно проверяет export и фильтрует новые task prompt/result/metadata/state перед persistence. Локальный memory content и raw error memory-assisted Qwen-попытки не входят в Codex handoff. Существовавшие task rows не переписаны скрыто: они явно помечены `legacy_payload=1`, исключены из memory retrieval/export и имеют отдельный scoped purge. Детектор patterns/entropy не является гарантией DLP и может пропустить неизвестный секрет, похожий на обычный текст.

Codex bundle `inbox/`, временный last-message файл, Open WebUI/n8n histories, controlled migration backups и внешние/OS copies имеют отдельные lifecycle boundaries. Hard purge SQLite не гарантирует forensic erase из snapshots и wear-levelled SSD. До реализации общего inventory/TTL владелец должен считать cloud coding потенциальной передачей и дополнительным локальным хранением содержимого выбранного workspace. Полный Stage 003 contract: [MEMORY_ENGINE.md](../docs/MEMORY_ENGINE.md).

## Критерий изменения

Новый источник или получатель данных требует data-flow update в [SECURITY_MODEL.md](../docs/SECURITY_MODEL.md), permission rule, redaction test и обновление manifest.
