# ADR 0001: Governance hierarchy and evidence model

- Статус: Accepted.
- Дата: 2026-07-14.
- Владелец: владелец платформы.
- Изменение: не редактировать принятое решение задним числом; заменить новым ADR со ссылкой `Supersedes`.

## Контекст

Платформа уже имела README, prompts и runtime config, но не имела единой иерархии правил или способа отличать наблюдаемый факт от обещания.

## Решение

Нормативный порядок: актуальная задача пользователя → [CORE](../../constitution/CORE.md) и специализированная Конституция → [Permissions](../PERMISSIONS.md)/Security → применимый task/`AGENTS.md` contract → свежие files/tool results → [Manifest](../../SYSTEM_MANIFEST.md)/Current State → memory/inference.

Используются статусы `verified`, `discovered`, `unavailable`, `planned`; completion требует objective evidence. `SYSTEM_MANIFEST.md` — единственный канонический список изменяемых технических фактов, а `CURRENT_STATE.md` — human-readable snapshot.

## Последствия

README больше не является источником истины о working capabilities. Документы обязаны ссылаться на runtime enforcement или честно отмечать gap. Изменения governance требуют review/approval.
