# 🤖 BrownDust2 Whitelist Bot

A Discord bot that manages player whitelist and ban status for a BrownDust private game server. It operates directly on the game's MySQL `account` table, stores admin permissions locally in SQLite, and integrates with GitHub Issues to process whitelist requests submitted via issue templates.

---

## ✨ Features

| Feature | Details |
|---|---|
| **Whitelist management** | Directly updates the game's `account.Block` field — no separate table needed |
| **Three-tier Block system** | `0` = Whitelisted · `1` = Not Whitelisted · `2` = Banned |
| **Dual identifier support** | Every account command accepts either a numeric `Uid` or a `UserName` string |
| **Tiered permissions** | Regular members can self-whitelist (Block 1→0 only); admins have full Block control |
| **GitHub Issues integration** | Reads whitelist request issues, whitelists the account, posts a reply, and closes the issue automatically |
| **Async-safe HTTP** | All GitHub API calls run in a thread pool — the Discord event loop is never blocked |
| **Local admin store** | Admin list lives in `admin.db` (SQLite), completely independent of MySQL |
| **Duplicate detection** | Every state-changing command checks the current Block value and returns a clear error if the operation is redundant |

---

## 📋 Requirements

- Python 3.11+
- A Discord bot application with **Message Content**, **Server Members**, and **Guild** intents enabled
- MySQL 8.x game database with the `account` table (see [Database Schema](#database-schema))
- A GitHub personal access token with `repo` scope (for Issues integration)

### Python dependencies

```
discord.py>=2.3.0
mysql-connector-python>=8.3.0
requests>=2.31.0
```

Install with:

```bash
pip install discord.py mysql-connector-python requests
```

> **Note:** `mysql-connector-python` is used for **read-only** lookups (uid resolution, current status, login date). All account state changes go through the game server REST API (`/Account/Ban`).

---

## ⚙️ Configuration

On first run the bot creates a `config.json` in the working directory:

```json
{
    "mysql": {
        "host": "localhost",
        "database": "brown_dust",
        "user": "root",
        "password": "",
        "port": 3306
    },
    "discord": {
        "token": "YOUR_DISCORD_BOT_TOKEN_HERE"
    },
    "github": {
        "token": "YOUR_GITHUB_TOKEN_HERE"
    },
    "admin": {
        "default_admin_id": 999999999999999999
    },
    "game_api": {
        "base_url": "http://localhost:5000",
        "adminkey": "YOUR_ADMIN_KEY_HERE"
    }
}
```

| Field | Description |
|---|---|
| `mysql.*` | Credentials for the game's MySQL database (read-only) |
| `discord.token` | Your Discord bot token |
| `github.token` | GitHub personal access token with `repo` scope |
| `admin.default_admin_id` | Discord user ID automatically granted admin on first boot |
| `game_api.base_url` | Base URL of the game server API (e.g. `http://192.168.1.10:5000`) |
| `game_api.adminkey` | Admin key accepted by `/Account/Ban` endpoint |

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

### Block field values

The `Block` column in MySQL mirrors the `isban` parameter of the game server API:

| Block / isban | Meaning | Discord display |
|---|---|---|
| `0` | Whitelisted (`isban=0`) | ✅ Whitelisted |
| `1` | Banned (`isban=1`) | 🚫 Banned |

> All writes to this field are made exclusively through `GET /Account/Ban?uid=...&isban=...&adminkey=...`.  
> The bot never issues `UPDATE` statements to MySQL directly.

Admin permissions are stored separately in `admin.db` (SQLite, created automatically at startup).

---

## 🚀 Running the Bot

```bash
# 1. Clone the repository
git clone https://github.com/360NENZ/BrownDust2-Whitelist-Bot.git
cd BrownDust2-Whitelist-Bot

# 2. Install dependencies
pip install discord.py mysql-connector-python requests

# 3. Start the bot (config.json is created automatically on first run)
python bot.py

# 4. Edit config.json with your real credentials, then restart
python bot.py
```

The bot performs a MySQL connectivity check before starting. If the connection fails, it exits with an error rather than starting in a broken state.

---

## 📖 Command Reference

### Architecture: API over direct DB

All account state changes (whitelist / ban) are sent to the game server as:

```
GET /Account/Ban?uid={uid}&isban={0|1}&adminkey={adminkey}
```

MySQL is used **read-only** — to resolve usernames to UIDs, read current `Block` status, and fetch login dates.  This means the game server remains the single source of truth for account state.

### Permission model

| Who | Can do |
|---|---|
| **Regular member** (has any non-`@everyone`, non-`null` role) | `/white` — self-whitelist via `isban=0` when currently `Block=1` |
| **Admin** (listed in `admin.db`) | All commands; can call `isban=0` or `isban=1` from any current state |

---

### `/white <uid\|username>`

Whitelist an account. Available to any member who holds at least one real server role.

**Restriction:** Regular users can only change `Block 1 → 0`. If the account is banned (`Block = 2`), the bot refuses and advises contacting an administrator.

```
✅ User username(uid) added to whitelist!
❌ Account [identifier] does not exist!
❌ User username (uid) is already whitelisted.
❌ Account username (uid) is banned. Only an administrator can remove this restriction.
```

---

### `/query <uid\|username>` *(Admin)*

Display full account information. Status is read from MySQL `Block` which mirrors `isban`.

```
🔍 Account Query
━━━━━━━━━━━━━━
👤 Account: username
🆔 UID: uid
🏳️ Status: ✅ Whitelisted
🕒 Last Login: 2026-03-01 02:43:11
```

---

### `/adduser <uid\|username>` *(Admin)*

Whitelist an account — calls `GET /Account/Ban?isban=0`. Unlike `/white`, this bypasses all restrictions; admins can whitelist from any current `Block` state.

---

### `/ban---

### `/ban <uid\|username> [reason]` *(Admin)*

Ban an account — calls `GET /Account/Ban?isban=1`. Returns an error if the account is already banned (`Block=1`).

```
🚫 Account banned
User: username
UID: uid
Original Status: ✅ Normal (Whitelisted)
Current Status: 🚫 Banned
```

---

### `/unban <uid\|username>` *(Admin)*

Unban an account — calls `GET /Account/Ban?isban=0`. Returns an error if the account is already whitelisted (`Block=0`).

```
✅ Account unbanned
User: username
UID: uid
Result: ✅ Success
```

---

### `/whitelisted` *(Admin)*

List the first 20 accounts with `Block = 0`, ordered by most recent login.

---

### `/banned` *(Admin)*

List the first 20 accounts with `Block = 1` (isban=1), ordered by most recent login.

---

### `/processissue <owner> <repo> <issue_number>` *(Admin)*

Fetch a single GitHub Issue, extract the game username from the whitelist request template, whitelist the account, post an auto-reply comment, and close the issue.

The bot uses a **deferred response** for this command — Discord will show "thinking…" while work is in progress, with up to 15 minutes to complete.

---

### `/batchprocess <owner> <repo>` *(Admin)*

Process **all open issues** in the given repository in one pass. For each issue:
1. Extract the username from the issue body.
2. Whitelist the account (skips accounts that are already whitelisted).
3. Post an auto-reply comment and close the issue.

Final response shows total issues processed, usernames found, whitelisted, and skipped counts.

---

### `/setadmin <user>` *(Admin)*

Grant admin privileges to a Discord user. The user is added to `admin.db`.

---

### `/removeadmin <user>` *(Admin)*

Revoke admin privileges from a Discord user. Admins cannot revoke their own privileges.

---

### `/help`

Show the full command reference (ephemeral — only visible to the user who ran it).

---

## 🐙 GitHub Issues Integration

The bot parses whitelist requests submitted via the included `whitelist_request.yml` issue template. The template produces a structured body that the bot reads with a targeted regex. It falls back to simpler patterns if the exact label format is absent.

After processing, the bot posts the following auto-reply and closes the issue:

```
🤖 [Auto-Reply]
✅ Success: Account `username` added to whitelist!
✅ 成功： 已自动过白，请重新登录游戏。
```

### Required template file

Place `whitelist_request.yml` in `.github/ISSUE_TEMPLATE/` in your game's GitHub repository.

---

## 🔒 Security Notes

- **Never commit `config.json`** — it contains your bot token, database password, and GitHub token. Add it to `.gitignore`.
- **`admin.db` is local only** — it is never read or written by the game server and does not travel through MySQL.
- The bot does **not** store passwords and never reads the `Password` column.
- All SQL values are passed as parameterised queries — there is no SQL injection surface.
- The `/white` command calls `isban=0` only when `Block == 1`. Accounts that are already whitelisted (`Block=0`) cannot be re-submitted, preventing race conditions. Any account state change is validated against current MySQL state before the API call is made.

---

## 📁 File Structure

```
BrownDust2-Whitelist-Bot/
├── bot.py                          # Main bot source
├── config.json                     # Runtime config (gitignored)
├── admin.db                        # SQLite admin store (auto-created)
├── account.sql                     # Database Schema
├── .github/
│   └── ISSUE_TEMPLATE/
│       └── whitelist_request.yml   # GitHub whitelist request template
├── .gitignore                      # Git Ignore
├── LICENSE                         # MIT License
└── README.md
```

---

## 🤝 Contributing

Pull requests are welcome. For significant changes please open a discussion issue first.

1. Fork the repository.
2. Create a feature branch: `git checkout -b feat/your-feature`.
3. Commit with a descriptive message following the convention above.
4. Open a pull request against `main`.

---

## 📄 License

MIT License. See `LICENSE` for details.
