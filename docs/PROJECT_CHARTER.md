# Project Charter: Locestra

- Статус: accepted; gates этапов 000–001 подтверждены validator, tests, doctor/smoke, lifecycle evidence и independent review.
- Владелец: пользователь и владелец этого компьютера.
- Применяется к: репозиторию, локальным сервисам, model profiles, инструментам и интерфейсам платформы.
- Контроль: [Конституция](../constitution/CORE.md), [разрешения](PERMISSIONS.md), [manifest](../SYSTEM_MANIFEST.md), tests и review.
- Изменение: отдельный проверяемый diff с объяснением влияния на цели и non-goals.

## Миссия

Дать одному владельцу единый AI-интерфейс для ежедневной разработки: лёгкие задачи выполняются быстро и локально, обычное программирование — локальным coding-agent, а действительно сложное программирование и review при разрешённой передаче данных — Codex. Голос, browser, документация, изображения и automation подключаются как независимые capabilities.

## Пользователь и основные сценарии

Основной пользователь — владелец Windows workstation. Главный сценарий — работа с явно выбранным Git-репозиторием: исследовать, изменить файлы, запустить проверки, показать evidence и при разрешении создать локальный commit. Дополнительные сценарии: локальный chat, документация, браузерная проверка, транскрипция, генерация изображений и автоматизация.

## Цели

1. Один entry point и автоматический выбор маршрута без ручного выбора модели.
2. Высокое качество программирования с локальным Qwen Code и контролируемой эскалацией в Codex.
3. Local-first обработка, явная cloud boundary и отсутствие скрытых лимитов локального inference.
4. Модульная архитектура со стабильными контрактами, health и degraded state.
5. Проверяемые результаты: файл, diff, тест, API response или другой объективный артефакт.
6. Сохранение пользовательского worktree, данных и Git-истории.
7. Возможность обновлять модели и adapters без смены единого UI и `/v1` boundary.

## Scope этапов 000–012

В scope входят governance, архитектура, router, memory, knowledge, coding, MCP/tools, voice, vision/images, interfaces, controlled improvement и evaluation. Последовательность и gates опубликованы в [ROADMAP.md](ROADMAP.md).

## Non-goals

- Обещать равенство локальных моделей Codex/Claude без benchmark evidence.
- Обучать или автономно изменять веса моделей.
- Автономно переписывать Конституцию, permissions, production config или approval system.
- Сканировать весь компьютер или индексировать данные вне allowlisted workspace.
- Выполнять push, deploy, publish, payments, account actions или массовые сообщения без явного разрешения.
- Строить собственный аналог зрелого upstream-инструмента только ради владения кодом.
- Делать платформу multi-user/public service на текущем security baseline.

## Критерии успеха

- `start → readiness → stop → restart` воспроизводимы и диагностируются существующими scripts.
- Open WebUI видит только `local-agent-auto`, а gateway сохраняет OpenAI-compatible `/v1` contract.
- Routing и coding fixtures проверяют реальное действие, а не только HTTP 200.
- В critical safety cases: ноль утечек секретов, несанкционированных внешних действий и ложных `success`.
- Любой cloud handoff имеет scope, data classification, provenance и требуемое approval.
- Ошибка optional capability изолирована и честно показана как degraded/unavailable.
- Существенные изменения имеют regression evidence и rollback path.

## Ограничения

Reference implementation currently targets a single Windows 11 workstation with a high-memory NVIDIA GPU and Docker Desktop/WSL2. VRAM is shared between the strong model and ComfyUI. Codex requires Internet access and a separate external account. Current security gaps are listed in [CURRENT_STATE.md](CURRENT_STATE.md) and are not guaranteed capabilities.

## Критерий изменения Charter

Charter меняется, когда меняются пользователь, миссия, границы данных, основные сценарии или измеримые критерии успеха. Техническая замена модели или версии инструмента отражается в manifest и сама по себе не требует переписывать Charter.
