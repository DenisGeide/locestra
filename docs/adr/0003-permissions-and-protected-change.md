# ADR 0003: Permission ceiling and protected change

- Статус: Accepted as policy; enforcement incremental.
- Дата: 2026-07-14.
- Владелец: владелец платформы.
- Изменение: не редактировать принятое решение задним числом; заменить новым ADR со ссылкой `Supersedes`.

## Контекст

Автономный агент должен выполнять локальные задачи без постоянных вопросов, но не получать неограниченные внешние и необратимые права. Будущий self-improvement создаёт дополнительный риск self-approval.

## Решение

[Permissions](../PERMISSIONS.md) задают ceiling. Задача может его сузить, но модель, prompt или tool output не расширяет. Локальные обратимые действия в scoped workspace разрешены; external write, private cloud export, destructive и production actions требуют точного approval.

Конституция, permissions, security policy, approval/rollback mechanisms, production credentials/config и trusted baselines являются protected scope. Self-improvement может создать proposal/experiment, но не одобрить или применить изменение.

## Последствия

Текущие Qwen yolo, Codex auto-exec, Telegram/browser и network gaps должны быть закрыты adapters/policy stages. До этого документы не заявляют полный enforcement.
