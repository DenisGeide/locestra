# ADR 0006: Verified process ownership and explicit resource boundaries

- Статус: Accepted.
- Дата: 2026-07-14.
- Владелец: владелец платформы.
- Изменение: новый lifecycle/lease mechanism оформляется следующим ADR.

## Контекст

Venv launcher PID отличался от фактического gateway/voice listener PID. Старый stop завершал PID из файла и любой процесс на известных портах без проверки identity, что создавало риск PID reuse/foreign listener kill. Locks существовали только как скрытые asyncio globals.

## Решение

Start регистрирует фактический listener/process после проверки command identity и пишет совместимый PID плюс versioned owner metadata. Stop повторно проверяет root/command/port, использует bounded wait и никогда не завершает unknown/reused PID или чужой listener. Strong Ollama `11434` остаётся host-owned. ComfyUI не принимает чужой `8388` и завершает только принятую identity.

Реальные in-process границы `gpu-heavy`, worktree, per-agent и fast/image документируются как current constraints. Outbound action lock и cross-process leases определяются target architecture, но не выдаются за реализованные.

## Последствия

Lifecycle становится fail-closed при конфликте порта. Любое изменение требует start/readiness/stop/restart evidence. Process-tree cancellation, Job Objects, rollback partial start и durable resource leases остаются техническим долгом.
