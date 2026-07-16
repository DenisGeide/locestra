# Правила контролируемого улучшения

- Статус: нормативный документ; исполнительный workflow запланирован на этап 011.
- Владелец: владелец платформы.
- Назначение: границы анализа ошибок и изменения системы.
- Применение: failure records, proposals, experiments, approvals и rollback.
- Изменение: только владельцем после independent review; этот механизм не может изменить собственные ограничения.

## Допустимый цикл

`наблюдение → структурированная ошибка → proposal → изолированный эксперимент → baseline/candidate comparison → независимый review → точное одобрение → применение → мониторинг → retain/rollback`

До явного одобрения система может собирать redacted evidence, предлагать изменение и экспериментировать только в disposable fixture/worktree без внешних side effects.

## Protected scope

Автономно запрещено изменять или применять:

- `constitution/`, permissions и security policy;
- production credentials, deployment и network exposure;
- модельные веса и trusted baselines;
- approval/rollback mechanisms;
- Git remotes и пользовательские ветки;
- внешние recipients и приватные datasets.

Изменение diff, условий или evaluation set после approval инвалидирует approval.

## Неудачи

Две подтверждённые неудачи одинаковой локальной стратегии прекращают повтор. Следующий шаг — новая стратегия, redacted Codex handoff или safe stop. Неудачи сохраняются с provenance, но без секретов.

## Утверждение об улучшении

Система не говорит «стало лучше» без воспроизводимого baseline, objective oracle, resource/safety metrics и regression gate. Holdout не используется для автоматической подгонки.

## Критерий изменения

Политика меняется только отдельным одобренным решением владельца и не через self-improvement pipeline.
