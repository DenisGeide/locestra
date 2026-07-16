# ADR 0002: Local-first routing and Codex cloud boundary

- Статус: Accepted as policy; enforcement incomplete.
- Дата: 2026-07-14.
- Владелец: владелец платформы.
- Изменение: не редактировать принятое решение задним числом; заменить новым ADR со ссылкой `Supersedes`.

## Контекст

Пользователю нужен автоматический выбор fast/strong/local coding/Codex, но Codex является облачным executor и может получать приватный код.

## Решение

Chat, обычный coding, voice и image остаются local-first. Codex используется для сложного coding/review только в разрешённом workspace и после data classification: public/non-sensitive scope может быть заранее разрешён policy, private/sensitive требует task-scoped approval, secrets запрещены. Недоступный или неразрешённый Codex получает только redacted local handoff bundle без отправки данных.

## Последствия

`ENABLE_CODEX_EXEC` означает capability, а не approval. Текущий runtime ещё не реализует classification/approval/provenance и зафиксирован как gap в [CURRENT_STATE.md](../CURRENT_STATE.md). Router stages должны сохранить автоматический UX, не скрывая cloud boundary.
