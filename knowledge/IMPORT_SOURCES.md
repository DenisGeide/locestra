# Knowledge Import Sources

- Inventory snapshot: 2026-07-15, после approved scoped Stage 004 live index.
- Allowed statuses: `discovered`, `allowed`, `blocked`, `imported`, `unsupported`.
- Важно: `allowed` означает policy-compatible candidate, а не consent и не факт импорта.
- На snapshot ignored `data/knowledge.sqlite3` содержит воспроизводимый derived index только зарегистрированного Local Agent project; status ниже отличает импортированный repository source от не предоставленных архивов.

| Source | Format/adapter | Scope | Sensitivity | Status | Owner approval | Evidence/notes |
|---|---|---|---|---|---|---|
| Git-tracked files текущего Locestra repository | Markdown/TXT/config/source adapters | Только canonical checkout root, exact owner, tracked inventory | `internal` ceiling по default | imported | applied 2026-07-15 | Approved reference snapshot: 143 tracked paths, 128 indexed, 15 policy/privacy blocked; вместе с bounded Git history — 129 active sources и 1,384 active fragments. Source: `config/knowledge.json`, `services/knowledge/repository.py`. |
| Git history metadata текущего repository | commit hash + author timestamp + subject; без patch | Тот же registered Git project | internal | imported | applied 2026-07-15 | Импортировано вместе с approved repository index; bounded sanitized metadata прошло secret scan, patches не импортировались. Source: `services/knowledge/repository.py`. |
| `archives/.gitkeep` | placeholder, не archive | `archives/` текущего project | internal | discovered | not requested | Не импортировать: payload отсутствует; каталог Git-ignored кроме placeholder. Source: `.gitignore`, `archives/.gitkeep`. |
| Реальный Markdown/TXT archive в `archives/` | Markdown/TXT adapter | Только один explicit file внутри registered project | operator-declared, не ниже scanner result | discovered | not provided | Файл не предоставлен. Будущий import начинается с exact-file dry-run. Source: [Archive Import Plan](../docs/ARCHIVE_IMPORT_PLAN.md). |
| ChatGPT export | Conversation JSON/HTML adapter только при supported shape | Не зарегистрирован | sensitive | unsupported | not provided | Export/fixture отсутствует; история не фабриковалась. |
| Fantik export | Adapter не подтверждён | Не зарегистрирован | sensitive/unknown | unsupported | not provided | Export и format specification отсутствуют. |
| User notes вне repository | Нет разрешённого adapter scope | Произвольный личный каталог запрещён | sensitive/unknown | unsupported | not provided | Full-disk/user-directory discovery не выполнялся. |
| `inbox/` Codex bundles | Заблокированный runtime dataset | Project path, но отдельный lifecycle | sensitive | blocked | not requested | Directory denylist; может содержать prompt/context. Содержимое не читалось. Source: `config/knowledge.json`, [Privacy](../constitution/PRIVACY.md). |
| `logs/` и `outputs/` | Заблокированный runtime/artifact dataset | Project path, отдельный lifecycle | sensitive/unknown | blocked | not requested | Directory denylist; не источник Knowledge Engine. Содержимое не читалось. |
| `data/*.sqlite*`, backups | Database/backup | Project path, отдельные storage owners | sensitive | blocked | not requested | Directory/suffix denylist; databases не сканировались как archive. |
| `.env*`, credentials, key/cookie files | Secret/runtime channels | Любой path | secret | blocked | cannot approve for model context | Path/name/suffix denylist; содержимое намеренно не читалось. |
| `.git/` object/config storage | Git metadata boundary, не content source | Только validated internal Git metadata | internal/secret-capable | blocked | not applicable | Direct file import запрещён. Git вызывается bounded commands; patches/objects не импортируются напрямую. |
| Browser profiles/cookies, `.ssh`, `.aws`, `.azure`, `.kube` | Credential/private stores | Вне разрешённого source scope | secret | blocked | cannot approve for model context | Explicit constitutional deny boundary; не сканировались. |
| Open WebUI/n8n Docker volumes | External application histories | Вне registered project source catalog | sensitive/unknown | unsupported | not provided | Отдельные retention/export/delete owners; автоматического adapter/import нет. |
| Controlled Memory Engine records | Typed memory database, не archive source | Отдельный store/contract | internal/sensitive | blocked | not applicable | Knowledge Engine не индексирует Memory DB; связь только через explicit candidate proposal/invalidation/purge. |

## Runtime update rule

После verified `index/import --approved` оператор меняет status только конкретного source на `imported` и фиксирует generation/source ID, hash или другой non-secret evidence. Failed, blocked или dry-run result не является import. При purge status должен отражать отсутствие active derived source; external original file учитывается отдельно.

## Не выполненные действия

- Ни один произвольный каталог вне current project не перечислялся и не читался.
- `.env`, credentials, cookies, browser profiles, private keys, model blobs и databases не открывались.
- ChatGPT/Fantik/notes history не создавалась из предположений.
- Никакой source не был повышен в active Memory Engine.
