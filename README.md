# 🤖 BrownDust2 Whitelist Bot

A Discord bot for managing player whitelist and ban status on a BrownDust private game server. Account state is controlled through the game server's REST API with a direct MySQL write as an automatic fallback. All user-facing messages are available in English, Chinese, or bilingual output. Admin permissions are stored in `admin.yml`.

---

## ✨ Features

| Feature | Details |
|---|---|
| **API-first writes** | All state changes go through `GET /Account/Ban`; direct MySQL write is used automatically if the API fails |
| **Execution method in responses** | Every success message shows how the operation was executed: `via API` or `via database (API failed: …)` |
| **Bilingual output** | All Discord responses support English, Chinese, or bilingual mode — set by `language` in config |
| **YAML config** | `config.yml` is the preferred format; `config.json` is also accepted as a fallback |
| **Role-based `/white`** | Permitted roles are listed in `white_roles` config (role names or IDs); no hardcoded role logic |
| **Admin YAML store** | Admin list lives in `admin.yml` with per-entry metadata comments; username lookup takes priority over ID |
| **admin.db migration** | If `admin.db` exists at startup, records are migrated to `admin.yml` automatically |
| **GitHub Issues integration** | Reads whitelist requests, whitelists the account, posts a bilingual reply, closes the issue |
| **Slash commands only** | `message_content` intent is not requested; text messages are never processed |
| **Duplicate detection** | All mutating commands check current state and return a clear error if the operation is a no-op |

---

## 📋 Requirements

- Python 3.11+
- Discord bot application with **Server Members** and **Guild** intents enabled
  - `message_content` intent is **not** required
- MySQL 8.x game database with the `account` table
- A GitHub fine-grained personal access token with **Issues: Read & Write** permission (for GitHub integration)

### Python dependencies

```
discord.py>=2.3.0
mysql-connector-python>=8.3.0
requests>=2.31.0
PyYAML>=6.0
```

Install with:

```bash
pip install discord.py mysql-connector-python requests PyYAML
```

---

## ⚙️ Configuration

The bot looks for configuration files in this order:

1. `config.yml`
2. `config.yaml`
3. `config.json`

If none are found, a `config.yml` template is written automatically. Fill in your credentials and restart.

### Full `config.yml` reference

```yaml
# ─────────────────────────────────────────────────────────────
# BrownDust Whitelist Bot  —  config.yml
# ─────────────────────────────────────────────────────────────

mysql:
  host:     localhost
  database: brown_dust
  user:     root
  password: ""
  port:     3306

discord:
  token: YOUR_DISCORD_BOT_TOKEN_HERE

github:
  token: YOUR_GITHUB_TOKEN_HERE

game_api:
  base_url: http://localhost:5000
  adminkey:  YOUR_ADMIN_KEY_HERE

# Output language for all Discord responses
#   en   — English only
#   zh   — Chinese only
#   both — Bilingual (English + Chinese, default)
language: both

# Roles permitted to use /white (self-whitelist)
# Each entry is a role name (string) OR a role ID (integer)
white_roles:
  - Verified
  # - 123456789012345678   # role ID example

admin:
  file: admin.yml                    # path to admin list file
  default_admin_id: 999999999999999999
  default_admin_username: ""         # optional, preferred for lookup
```

| Key | Description |
|---|---|
| `mysql.*` | Game database credentials (reads + DB fallback writes) |
| `discord.token` | Discord bot token |
| `github.token` | Fine-grained PAT with Issues: Read & Write |
| `game_api.base_url` | Base URL of the game server |
| `game_api.adminkey` | Admin key for `/Account/Ban` endpoint |
| `language` | Response language: `en`, `zh`, or `both` (default: `both`) |
| `white_roles` | List of role names or IDs that may use `/white` |
| `admin.file` | Path to the admin list YAML file (default: `admin.yml`) |
| `admin.default_admin_id` | Discord user ID granted admin on first boot |
| `admin.default_admin_username` | Discord username of bootstrap admin (preferred for lookup) |

---

## 👑 Admin Management — `admin.yml`

Admin permissions are stored in a human-readable YAML file. Each entry includes rich metadata written as inline comments, making the file self-documenting.

### Example `admin.yml`

```yaml
# Admin list  —  admin.yml
# Managed by the bot; safe to edit manually.
# Lookup priority: username (case-insensitive) > id
# Fields: id, username, added_by, added_at, note
#
admins:
  # added_at: 2025-01-01T12:00:00  |  added_by: SYSTEM  |  note: Bootstrap admin from config
  - id:       123456789012345678
    username: "AdminUser"
    added_by: "SYSTEM"
    added_at: "2025-01-01T12:00:00"
    note:     "Bootstrap admin from config"

  # added_at: 2025-06-15T09:30:00  |  added_by: AdminUser  |  note: Granted via /setadmin by AdminUser
  - id:       987654321098765432
    username: "Moderator1"
    added_by: "AdminUser"
    added_at: "2025-06-15T09:30:00"
    note:     "Granted via /setadmin by AdminUser"
```

### Lookup priority

When checking admin status, **username match (case-insensitive) takes priority over ID match**. This means if a user's Discord username matches an entry, they are recognized as an admin regardless of whether their ID also matches.

### Migrating from `admin.db`

If `admin.db` (SQLite) exists in the bot's working directory at startup, the bot will:

1. Read all records from `admin.db`
2. Merge them into `admin.yml` (skipping duplicates)
3. Rename `admin.db` → `admin.db.migrated`
4. Log a warning to the console

No manual migration step is needed.

---

## 🗄️ Database Schema

The bot operates on the existing game `account` table — it creates no tables of its own in MySQL.

```sql
CREATE TABLE `account` (
  `Uid`       INT(11)      NOT NULL AUTO_INCREMENT,
  `UserName`  VARCHAR(255) NULL DEFAULT NULL,
  `Password`  VARCHAR(255) NULL DEFAULT NULL,
  `Block`     INT(11)      NOT NULL,
  `IP`        VARCHAR(255) NULL DEFAULT NULL,
  `LoginDate` DATETIME     NULL DEFAULT NULL,
  PRIMARY KEY (`Uid`)
);
```

### Block / isban values

| `Block` / `isban` | Meaning | Display (en) | Display (zh) |
|---|---|---|---|
| `0` | Whitelisted | ✅ Whitelisted | ✅ 已过白 |
| `1` | Banned | 🚫 Banned | 🚫 已封禁 |

---

## ✍️ Write Strategy — API-first with DB fallback

Every account state change follows this path:

```
1. Read current state from MySQL (_read_account)
2. Duplicate check — bail early if already in target state
3. Call game API:  GET /Account/Ban?uid=...&isban=0|1&adminkey=...
   ├─ Success  → respond with "via API"
   └─ Failure  → call direct MySQL UPDATE (fallback)
                 ├─ Success  → respond with "via database (API failed: <reason>)"
                 └─ Failure  → return combined error message
```

The execution method appears in **every** success response, for example:

```
✅ PlayerOne (10042) added to whitelist! [via API]
via API / 通过API

✅ PlayerTwo (10043) added to whitelist! [via database (API failed: Game API timed out)]
通过数据库（API失败：游戏API请求超时）
```

---

## 🚀 Running the Bot

```bash
# 1. Clone
git clone https://github.com/360NENZ/BrownDust2-Whitelist-Bot.git
cd BrownDust2-Whitelist-Bot

# 2. Install dependencies
pip install discord.py mysql-connector-python requests PyYAML

# 3. Start once to generate config template
python bot.py

# 4. Edit config.yml with your credentials, then restart
python bot.py
```

The bot performs a MySQL connectivity check at startup and exits with an error if it cannot connect. The game API is probed non-fatally — if unreachable, the bot starts normally and uses DB fallback for all writes.

---

## 📖 Command Reference

### Permission model

| Who | Requirement | Can use |
|---|---|---|
| **Regular member** | Holds at least one role listed in `white_roles` | `/white` |
| **Admin** | Listed in `admin.yml` (by username or ID) | All commands |

---

### `/white <uid\|username>`

Self-whitelist a game account.

- **Allowed** when `Block = 1` (Banned → Whitelisted)
- **Blocked** when `Block = 0` (already whitelisted)
- **Blocked** when account is in any other state — advises contacting an admin

Responds with the execution method:
```
✅ PlayerOne (10042) added to whitelist! [via API]
✅ PlayerOne（10042）已成功过白！【通过API】
```

---

### `/query <uid\|username>` *(Admin)*

Display full account information read from MySQL.

```
🔍 Account Query / 账号查询
━━━━━━━━━━━━━━
👤 Username: PlayerOne
🆔 UID: 10042
🏳️ Status: ✅ Whitelisted / ✅ 已过白
🕒 Last Login: 2026-03-01 02:43:11
```

---

### `/adduser <uid\|username>` *(Admin)*

Force-whitelist an account. Bypasses all state restrictions; can whitelist from any `Block` value.

---

### `/ban <uid\|username> [reason]` *(Admin)*

Ban an account (`isban=1`). Returns an error if already banned.

```
🚫 Account Banned [via API] / 账号已封禁【通过API】
User: PlayerOne  UID: 10042
Before: ✅ Whitelisted → After: 🚫 Banned
```

---

### `/unban <uid\|username>` *(Admin)*

Unban an account (`isban=0`). Returns an error if already whitelisted.

---

### `/whitelisted` *(Admin)*

List all accounts with `Block=0`, sorted by last login. Shows up to 20, with a count if more exist.

---

### `/banned` *(Admin)*

List all accounts with `Block=1`, sorted by last login. Shows up to 20.

---

### `/processissue <owner> <repo> <number>` *(Admin)*

Process a single GitHub whitelist issue:
1. Fetch the issue body and extract the game username
2. Whitelist the account
3. Post a bilingual auto-reply comment
4. Close the issue with `state_reason: completed`

---

### `/batchprocess <owner> <repo>` *(Admin)*

Process all open issues in the repository in one pass. Each issue is commented on and closed after whitelisting.

---

### `/setadmin <user>` *(Admin)*

Grant admin privileges to a Discord user. The entry is written to `admin.yml` with timestamp, granting operator, and method note.

---

### `/removeadmin <user>` *(Admin)*

Revoke admin privileges. Cannot revoke your own privileges.

---

### `/help`

Show the full command reference in the configured language. Ephemeral (visible only to you).

---

## 📁 File Structure

```
BrownDust2-Whitelist-Bot/
├── bot.py                          # Main bot source
├── config.yml                      # Runtime config (gitignore this)
├── admin.yml                       # Admin list with metadata (gitignore this)
├── admin.db.migrated               # Renamed legacy file (if migration ran)
├── .github/
│   └── ISSUE_TEMPLATE/
│       └── whitelist_request.yml   # GitHub whitelist request template
├── .gitignore                      # Git Ignore
├── LICENSE                         # MIT License
└── README.md
```

> Add `config.yml`, `admin.yml`, and `admin.db*` to `.gitignore`.

---

## 📝 Commit History

```
feat: initial bot scaffold with MySQL whitelist/ban/admin tables
```

```
refactor: migrate to game account table; move admins to local SQLite
```

```
fix: resolve "Unknown interaction" 10062 on /batchprocess and /processissue
```

```
feat: redefine Block semantics; add /white and /query commands
```

```
feat: enforce tiered Block permissions; add /setblock for admins
```

```
refactor: replace direct MySQL writes with game server REST API
```

```
fix: defer all API-calling commands to prevent interaction timeout (10062)
```

```
feat: API-first writes with MySQL fallback; slash-commands only
```

```
fix: use Bearer auth scheme for GitHub API; fix collaborator issue access
```

```
feat: YAML config, bilingual output, role config, admin.yml, DB method labels
```

---

## 🤝 Contributing

Pull requests are welcome. For significant changes please open a discussion issue first.

1. Fork the repository
2. Create a feature branch: `git checkout -b feat/your-feature`
3. Commit with a descriptive message following the convention above
4. Open a pull request against `main`

---

## 📄 License

MIT License. See `LICENSE` for details.
