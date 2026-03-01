import asyncio
import discord
from discord.ext import commands
from discord import app_commands
import mysql.connector
from mysql.connector import Error
import sqlite3
import requests
import json
import os
import re
import logging

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────
def load_config():
    config_file = 'config.json'
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"Error reading config file: {e}")

    config = {
        "mysql": {
            "host": "localhost",
            "database": "brown_dust",
            "user": "root",
            "password": "",
            "port": 3306
        },
        "discord": {"token": "YOUR_DISCORD_BOT_TOKEN_HERE"},
        "github":  {"token": "YOUR_GITHUB_TOKEN_HERE"},
        "admin":   {"default_admin_id": 999999999999999999},
        "game_api": {
            "base_url": "http://localhost:5000",
            "adminkey": "YOUR_ADMIN_KEY_HERE"
        }
    }
    try:
        with open(config_file, 'w') as f:
            json.dump(config, f, indent=4)
        print(f"Config file created at {config_file}. Please fill in your credentials.")
    except Exception as e:
        print(f"Failed to create config file: {e}")
    return config

try:
    CONFIG = load_config()
except Exception as e:
    print(f"Failed to load configuration: {e}")
    exit(1)

# ─────────────────────────────────────────────
# Bot setup
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix='', intents=intents, help_command=None)

DB_CONFIG  = CONFIG['mysql']
ADMIN_DB   = 'admin.db'
API_CONFIG = CONFIG['game_api']   # base_url + adminkey for /Account/Ban

# ─────────────────────────────────────────────
# Block / isban field semantics
#
# The game server controls account state through:
#   GET /Account/Ban?uid={uid}&isban={0|1}&adminkey={key}
#
# The resulting MySQL Block value mirrors isban:
#   isban=0  →  Block=0  →  ✅ Whitelisted
#   isban=1  →  Block=1  →  🚫 Banned
# ─────────────────────────────────────────────
BLOCK_LABELS = {
    0: "✅ Whitelisted",
    1: "🚫 Banned",
}

def fmt_block(block: int) -> str:
    return BLOCK_LABELS.get(block, f"❓ Unknown ({block})")

# ─────────────────────────────────────────────
# Permission helper for /white
# Available to any member who holds at least one
# role that is NOT @everyone and NOT named "null".
# ─────────────────────────────────────────────
def has_real_role(member: discord.Member) -> bool:
    return any(
        r != member.guild.default_role and r.name.lower() != 'null'
        for r in member.roles
    )

# ─────────────────────────────────────────────
# Identifier resolver
# ─────────────────────────────────────────────
def resolve_identifier(identifier: str):
    """
    '12345'     → ("Uid", 12345)
    'xialuoli'  → ("UserName", "xialuoli")
    """
    s = identifier.strip()
    if s.isdigit():
        return "Uid", int(s)
    return "UserName", s

# ─────────────────────────────────────────────
# SQLite admin.db  (independent of MySQL)
# ─────────────────────────────────────────────
def init_admin_db():
    try:
        con = sqlite3.connect(ADMIN_DB)
        cur = con.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER UNIQUE NOT NULL,
                added_by TEXT    NOT NULL,
                added_at TEXT    DEFAULT (datetime('now'))
            )
        ''')
        default_id = CONFIG['admin']['default_admin_id']
        cur.execute("SELECT 1 FROM admins WHERE user_id = ?", (default_id,))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO admins (user_id, added_by) VALUES (?, ?)",
                (default_id, 'SYSTEM')
            )
        con.commit()
        logger.info("admin.db initialised.")
    except Exception as e:
        logger.error(f"admin.db init error: {e}")
    finally:
        if 'con' in locals():
            con.close()

def is_admin(user_id: int) -> bool:
    try:
        con = sqlite3.connect(ADMIN_DB)
        cur = con.cursor()
        cur.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
        return cur.fetchone() is not None
    except Exception as e:
        logger.error(f"is_admin error: {e}")
        return False
    finally:
        if 'con' in locals():
            con.close()

def add_admin(user_id: int, added_by: str):
    try:
        con = sqlite3.connect(ADMIN_DB)
        cur = con.cursor()
        cur.execute(
            "INSERT INTO admins (user_id, added_by) VALUES (?, ?)",
            (user_id, added_by)
        )
        con.commit()
        return True, None
    except sqlite3.IntegrityError:
        return False, "User is already an admin."
    except Exception as e:
        return False, f"Unexpected error: {e}"
    finally:
        if 'con' in locals():
            con.close()

def remove_admin(user_id: int):
    try:
        con = sqlite3.connect(ADMIN_DB)
        cur = con.cursor()
        cur.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        con.commit()
        return (True, None) if cur.rowcount else (False, "User is not an admin.")
    except Exception as e:
        return False, f"Unexpected error: {e}"
    finally:
        if 'con' in locals():
            con.close()

# ─────────────────────────────────────────────
# MySQL connectivity check  (reads only — all
# writes go through the game API)
# ─────────────────────────────────────────────
def init_db():
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        if conn.is_connected():
            logger.info("MySQL connection verified (read-only).")
        conn.close()
    except Error as e:
        logger.error(f"MySQL error: {e}")
        return str(e)
    return None


# ─────────────────────────────────────────────
# MySQL read helpers  — account table (read-only)
#
# The game server owns all writes; we only read
# current state, uid lookups, and login dates.
# ─────────────────────────────────────────────
def _get_account(cursor, identifier: str):
    """
    Returns (Uid, UserName, Block, LoginDate) or None.
    Accepts numeric Uid or UserName string.
    Block mirrors isban: 0=Whitelisted  1=Banned.
    """
    col, val = resolve_identifier(identifier)
    cursor.execute(
        f"SELECT `Uid`, `UserName`, `Block`, `LoginDate` "
        f"FROM `account` WHERE `{col}` = %s LIMIT 1",
        (val,)
    )
    return cursor.fetchone()


def _read_account(identifier: str):
    """
    Open a short-lived MySQL connection, read one account row, close.
    Returns (uid, username, block, login_date, error_msg).
    """
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cur  = conn.cursor()
        row  = _get_account(cur, identifier)
        if not row:
            return None, None, None, None, f"Account [{identifier}] does not exist!"
        uid, username, block, login_date = row
        return uid, username, block, login_date, None
    except Error as e:
        return None, None, None, None, f"Database error: {e}"
    except Exception as e:
        return None, None, None, None, f"Unexpected error: {e}"
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


def query_account(identifier: str):
    """
    Returns (account_dict, error_msg).
    account_dict keys: uid, username, block, login_date
    """
    uid, username, block, login_date, err = _read_account(identifier)
    if err:
        return None, err
    return {"uid": uid, "username": username, "block": block, "login_date": login_date}, None


# ─────────────────────────────────────────────
# Game API layer  — GET /Account/Ban
#
# This is the ONLY write surface for account state.
# Direct MySQL writes are never performed.
#
#   GET /Account/Ban?uid={uid}&isban={0|1}&adminkey={key}
#
#   isban=0  →  unblock / whitelist
#   isban=1  →  block   / ban
# ─────────────────────────────────────────────
async def _api_set_ban(uid: int, isban: int) -> tuple[bool, str | None]:
    """
    Call the game server's /Account/Ban endpoint.
    Returns (success, error_msg).
    """
    if isban not in (0, 1):
        return False, f"Invalid isban value '{isban}'. Must be 0 or 1."
    try:
        base   = API_CONFIG['base_url'].rstrip('/')
        url    = f"{base}/Account/Ban"
        params = {
            'uid':      str(uid),
            'isban':    isban,
            'adminkey': API_CONFIG['adminkey'],
        }
        resp = await asyncio.to_thread(
            requests.get, url, params=params, timeout=10
        )
        if resp.status_code == 200:
            logger.info(f"API /Account/Ban uid={uid} isban={isban} → 200 OK")
            return True, None
        else:
            msg = f"API error {resp.status_code}: {resp.text[:200]}"
            logger.error(f"API /Account/Ban uid={uid} isban={isban} → {msg}")
            return False, msg
    except requests.exceptions.Timeout:
        return False, "Game API request timed out."
    except requests.exceptions.RequestException as e:
        return False, f"Game API network error: {e}"
    except Exception as e:
        return False, f"Unexpected API error: {e}"


# ─────────────────────────────────────────────
# High-level account operations
# All state changes go through _api_set_ban().
# MySQL is used only to read current state / uid.
# ─────────────────────────────────────────────

async def add_to_whitelist(identifier: str, added_by: str, admin: bool = False):
    """
    Whitelist an account → API isban=0.
    Returns (success, uid, username, error_msg).

    admin=False (/white — regular member):
      • Allowed only when Block == 1 (currently banned/inactive).
      • Block == 0 already → duplicate error.
    admin=True  (/adduser, GitHub pipelines):
      • Allowed from any Block state.
    """
    uid, username, block, _, err = _read_account(identifier)
    if err:
        return False, None, None, err
    if block == 0:
        return False, uid, username, f"User **{username}** ({uid}) is already whitelisted."
    if not admin and block != 1:
        return False, uid, username, (
            f"Account **{username}** ({uid}) cannot be self-whitelisted. "
            f"Please contact an administrator."
        )
    ok, api_err = await _api_set_ban(uid, isban=0)
    if not ok:
        return False, uid, username, api_err
    logger.info(f"Whitelisted '{username}' (Uid {uid}) by {added_by} [admin={admin}]")
    return True, uid, username, None


async def ban_user(identifier: str, banned_by: str, reason: str):
    """
    Ban an account → API isban=1.
    Returns (success, uid, username, old_block, error_msg).
    Duplicate detection: error when Block is already 1.
    """
    uid, username, block, _, err = _read_account(identifier)
    if err:
        return False, None, None, None, err
    if block == 1:
        return False, uid, username, block, f"User **{username}** ({uid}) is already banned."
    ok, api_err = await _api_set_ban(uid, isban=1)
    if not ok:
        return False, uid, username, block, api_err
    logger.info(f"Banned '{username}' (Uid {uid}) by {banned_by}. Reason: {reason}")
    return True, uid, username, block, None


async def unban_user(identifier: str):
    """
    Unban an account → API isban=0.
    Returns (success, uid, username, error_msg).
    Duplicate detection: error when Block is already 0.
    """
    uid, username, block, _, err = _read_account(identifier)
    if err:
        return False, None, None, err
    if block == 0:
        return False, uid, username, (
            f"User **{username}** ({uid}) is already whitelisted "
            f"(current status: {fmt_block(block)})."
        )
    ok, api_err = await _api_set_ban(uid, isban=0)
    if not ok:
        return False, uid, username, api_err
    logger.info(f"Unbanned '{username}' (Uid {uid}), was Block={block}")
    return True, uid, username, None


# ─────────────────────────────────────────────
# GitHub helpers
# ─────────────────────────────────────────────
def extract_username_from_issue(issue_body: str):
    if not issue_body:
        return None

    # Primary: template labelled field
    m = re.search(
        r'游戏账号\s*\([^)]*Game\s*Username[^\)]*\)\s*[\r\n]*\s*([^\r\n]+)',
        issue_body, re.IGNORECASE
    )
    if m:
        username = re.sub(r'^[:\-\s]+', '', m.group(1).strip())
        return username or None

    # Fallback: placeholder example line
    m = re.search(r'例如:\s*([a-zA-Z0-9_]+)', issue_body)
    if m:
        return m.group(1).strip()

    # Last resort: first plausible alphanumeric token
    skip = {'game', 'username', 'example', 'for', 'the', 'and', 'or', 'not', 'yes', 'no'}
    tokens = [t for t in re.findall(r'([a-zA-Z0-9_]{3,20})', issue_body)
              if t.lower() not in skip]
    return tokens[0] if tokens else None


# Async-safe HTTP wrappers
# (requests is blocking — offload to thread pool to keep the event loop free)
async def _http_get(url: str, headers: dict) -> requests.Response:
    return await asyncio.to_thread(requests.get, url, headers=headers)

async def _http_post(url: str, payload: dict, headers: dict) -> requests.Response:
    return await asyncio.to_thread(requests.post, url, json=payload, headers=headers)

async def _http_patch(url: str, payload: dict, headers: dict) -> requests.Response:
    return await asyncio.to_thread(requests.patch, url, json=payload, headers=headers)


async def get_usernames_from_issue(repo_owner, repo_name, issue_number, token):
    try:
        headers = {'Authorization': f'token {token}'}
        url = f'https://api.github.com/repos/{repo_owner}/{repo_name}/issues/{issue_number}'
        resp = await _http_get(url, headers)
        if resp.status_code != 200:
            return [], f"GitHub API error {resp.status_code}: {resp.text}"
        body = resp.json().get('body') or ""
        username = extract_username_from_issue(body)
        if not username:
            return [], "No valid username found in the issue body."
        return [username], None
    except requests.exceptions.RequestException as e:
        return [], f"Network error: {e}"
    except Exception as e:
        return [], f"Unexpected error: {e}"


async def close_github_issue_with_comment(repo_owner, repo_name, issue_number, token, username):
    try:
        headers = {'Authorization': f'token {token}'}
        comment_url = (
            f'https://api.github.com/repos/{repo_owner}/{repo_name}'
            f'/issues/{issue_number}/comments'
        )
        payload = {
            'body': (
                f'🤖 [Auto-Reply]\n'
                f'✅ Success: Account `{username}` added to whitelist!\n'
                f'✅ 成功： 已自动过白，请重新登录游戏。'
            )
        }
        cr = await _http_post(comment_url, payload, headers)
        if cr.status_code not in (200, 201):
            return False, f"Failed to post comment: {cr.status_code}"
        issue_url = (
            f'https://api.github.com/repos/{repo_owner}/{repo_name}/issues/{issue_number}'
        )
        pr = await _http_patch(issue_url, {'state': 'closed'}, headers)
        ok = pr.status_code == 200
        return ok, (None if ok else f"Failed to close issue: {pr.status_code}")
    except Exception as e:
        return False, f"Error: {e}"

# ─────────────────────────────────────────────
# Slash commands
# ─────────────────────────────────────────────

# ── /help ────────────────────────────────────
@app_commands.command(name="help", description="Show all bot commands")
async def help(interaction: discord.Interaction):
    text = """
**Discord Bot Commands**

**General (any member with a role):**
- `/white <uid|username>` — Whitelist your account (only when currently Banned; contact admin if needed)

**Admin only:**
- `/query <uid|username>` — Look up full account information
- `/adduser <uid|username>` — Whitelist an account (isban=0, bypasses all restrictions)
- `/ban <uid|username> [reason]` — Ban an account (isban=1)
- `/unban <uid|username>` — Unban an account (isban=0)
- `/whitelisted` — List all whitelisted accounts (Block=0)
- `/banned` — List all banned accounts (Block=1)
- `/processissue <owner> <repo> <number>` — Process one GitHub whitelist issue
- `/batchprocess <owner> <repo>` — Batch-process all open whitelist issues
- `/setadmin <user>` — Grant admin privileges to a Discord user
- `/removeadmin <user>` — Revoke admin privileges from a Discord user

**Account state** is controlled via the game server API (`/Account/Ban`):
`isban=0` = ✅ Whitelisted  ·  `isban=1` = 🚫 Banned
**Uid or UserName:** all account commands accept either format.
**Admin list** is stored in `admin.db` (local SQLite, independent of MySQL).
"""
    await interaction.response.send_message(text, ephemeral=True)


# ── /white ───────────────────────────────────
@app_commands.command(name="white", description="Add an account to the whitelist")
@app_commands.describe(identifier="Game Uid (numeric) or UserName")
async def white(interaction: discord.Interaction, identifier: str):
    # Available to any member who has at least one real role
    member = interaction.user
    if not isinstance(member, discord.Member) or not has_real_role(member):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    success, uid, username, error_msg = await add_to_whitelist(identifier, str(interaction.user), admin=False)
    if success:
        await interaction.response.send_message(
            f"✅ User **{username}**({uid}) added to whitelist!"
        )
        logger.info(f"/white: '{username}' (Uid {uid}) whitelisted by {interaction.user}")
    else:
        await interaction.response.send_message(f"❌ {error_msg}")


# ── /query ───────────────────────────────────
@app_commands.command(name="query", description="Query full account information (Admin only)")
@app_commands.describe(identifier="Game Uid (numeric) or UserName")
async def query(interaction: discord.Interaction, identifier: str):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    account, error_msg = query_account(identifier)
    if not account:
        await interaction.response.send_message(f"❌ Account [{identifier}] does not exist!")
        return

    block = account['block']
    # isban semantics: Block=0 → Whitelisted (isban=0), Block=1 → Banned (isban=1)
    status_st = fmt_block(block)

    msg = (
        f"🔍 **Account Query**\n"
        f"━━━━━━━━━━━━━━\n"
        f"👤 Account: `{account['username']}`\n"
        f"🆔 UID: `{account['uid']}`\n"
        f"🏳️ Status: {status_st}\n"
        f"🕒 Last Login: `{account['login_date']}`"
    )
    await interaction.response.send_message(msg)


# ── /adduser ─────────────────────────────────
@app_commands.command(name="adduser", description="Add an account to the whitelist (Admin only)")
@app_commands.describe(identifier="Game Uid (numeric) or UserName")
async def adduser(interaction: discord.Interaction, identifier: str):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    success, uid, username, error_msg = await add_to_whitelist(identifier, str(interaction.user), admin=True)
    if success:
        await interaction.response.send_message(
            f"✅ User **{username}**({uid}) added to whitelist!"
        )
        logger.info(f"/adduser: '{username}' (Uid {uid}) whitelisted by {interaction.user}")
    else:
        await interaction.response.send_message(f"❌ {error_msg}")


# ── /ban ─────────────────────────────────────
@app_commands.command(name="ban", description="Ban an account (Block → 2) by UserName or Uid")
@app_commands.describe(identifier="Game Uid (numeric) or UserName", reason="Reason for ban")
async def ban(
    interaction: discord.Interaction,
    identifier: str,
    reason: str = "No reason provided"
):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    success, uid, username, old_block, error_msg = await ban_user(
        identifier, str(interaction.user), reason
    )
    if success:
        msg = (
            f"🚫 **Account banned**\n"
            f"User: `{username}`\n"
            f"UID: `{uid}`\n"
            f"Original Status: {fmt_block(old_block)}\n"
            f"Current Status: {fmt_block(2)}"
        )
        await interaction.response.send_message(msg)
        logger.info(f"/ban: '{username}' (Uid {uid}) by {interaction.user}. Reason: {reason}")
    else:
        await interaction.response.send_message(f"❌ {error_msg}")


# ── /unban ───────────────────────────────────
@app_commands.command(name="unban", description="Unban an account (Block → 0) by UserName or Uid")
@app_commands.describe(identifier="Game Uid (numeric) or UserName")
async def unban(interaction: discord.Interaction, identifier: str):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    success, uid, username, error_msg = await unban_user(identifier)
    if success:
        msg = (
            f"✅ **Account unbanned**\n"
            f"User: `{username}`\n"
            f"UID: `{uid}`\n"
            f"Result: ✅ Success"
        )
        await interaction.response.send_message(msg)
        logger.info(f"/unban: '{username}' (Uid {uid}) by {interaction.user}")
    else:
        await interaction.response.send_message(f"❌ {error_msg}")


# ── /whitelisted ─────────────────────────────
@app_commands.command(name="whitelisted", description="List all whitelisted accounts (Block=0)")
async def whitelisted(interaction: discord.Interaction):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cur  = conn.cursor()
        cur.execute(
            "SELECT `Uid`, `UserName`, `LoginDate` FROM `account` "
            "WHERE `Block` = 0 ORDER BY `LoginDate` DESC"
        )
        rows = cur.fetchall()
        if not rows:
            await interaction.response.send_message("📋 No whitelisted accounts found.")
            return
        lines = [f"📋 Whitelisted Accounts ({len(rows)} total):"]
        for uid, uname, ldate in rows[:20]:
            lines.append(f"- `{uname}` (Uid: {uid}, Last Login: {ldate})")
        if len(rows) > 20:
            lines.append(f"... and {len(rows) - 20} more.")
        await interaction.response.send_message("\n".join(lines))
    except Error as e:
        await interaction.response.send_message(f"❌ Database error: {e}")
        logger.error(f"/whitelisted db error: {e}")
    except Exception as e:
        await interaction.response.send_message(f"❌ Unexpected error: {e}")
        logger.error(f"/whitelisted error: {e}")
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


# ── /banned ──────────────────────────────────
@app_commands.command(name="banned", description="List all banned accounts (Block=2)")
async def banned(interaction: discord.Interaction):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cur  = conn.cursor()
        cur.execute(
            "SELECT `Uid`, `UserName`, `LoginDate` FROM `account` "
            "WHERE `Block` = 1 ORDER BY `LoginDate` DESC"
        )
        rows = cur.fetchall()
        if not rows:
            await interaction.response.send_message("📋 No banned accounts found.")
            return
        lines = [f"📋 Banned Accounts — isban=1 ({len(rows)} total):"]
        for uid, uname, ldate in rows[:20]:
            lines.append(f"- `{uname}` (Uid: {uid}, Last Login: {ldate})")
        if len(rows) > 20:
            lines.append(f"... and {len(rows) - 20} more.")
        await interaction.response.send_message("\n".join(lines))
    except Error as e:
        await interaction.response.send_message(f"❌ Database error: {e}")
        logger.error(f"/banned db error: {e}")
    except Exception as e:
        await interaction.response.send_message(f"❌ Unexpected error: {e}")
        logger.error(f"/banned error: {e}")
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


# ── /processissue ────────────────────────────
@app_commands.command(name="processissue", description="Process a single GitHub whitelist issue")
@app_commands.describe(
    repo_owner="GitHub repository owner",
    repo_name="GitHub repository name",
    issue_number="Issue number to process"
)
async def processissue(
    interaction: discord.Interaction,
    repo_owner: str,
    repo_name: str,
    issue_number: int
):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    github_token = CONFIG['github']['token']
    if github_token == "YOUR_GITHUB_TOKEN_HERE":
        await interaction.response.send_message("❌ GitHub token not configured.", ephemeral=True)
        return

    # Defer before any I/O — extends window to 15 minutes
    await interaction.response.defer()

    try:
        usernames, error_msg = await get_usernames_from_issue(
            repo_owner, repo_name, issue_number, github_token
        )
        if error_msg:
            await interaction.followup.send(f"❌ {error_msg}")
            logger.error(f"/processissue {repo_owner}/{repo_name}#{issue_number}: {error_msg}")
            return
        if not usernames:
            await interaction.followup.send("❌ No valid username found in this issue.")
            return

        added, skipped, skip_reasons = 0, 0, []
        for uname in usernames:
            success, uid, username, err = await add_to_whitelist(uname, f"GitHub Issue #{issue_number}", admin=True)
            if success:
                added += 1
            else:
                skipped += 1
                skip_reasons.append(err)

        close_success, close_error = await close_github_issue_with_comment(
            repo_owner, repo_name, issue_number, github_token, usernames[0]
        )
        lines = [
            f"✅ Found **{len(usernames)}** username(s): "
            f"whitelisted **{added}**, skipped **{skipped}**."
        ]
        lines += [f"  • {r}" for r in skip_reasons]
        lines.append("Issue closed ✅" if close_success else f"Could not close issue: {close_error}")
        await interaction.followup.send("\n".join(lines))
        logger.info(
            f"/processissue {repo_owner}/{repo_name}#{issue_number}: "
            f"added {added}, skipped {skipped}"
        )

    except Exception as e:
        await interaction.followup.send(f"❌ Unexpected error: {e}")
        logger.error(f"/processissue unexpected error: {e}")


# ── /batchprocess ────────────────────────────
@app_commands.command(name="batchprocess", description="Batch-process all open whitelist issues")
@app_commands.describe(
    repo_owner="GitHub repository owner",
    repo_name="GitHub repository name"
)
async def batchprocess(interaction: discord.Interaction, repo_owner: str, repo_name: str):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    github_token = CONFIG['github']['token']
    if github_token == "YOUR_GITHUB_TOKEN_HERE":
        await interaction.response.send_message("❌ GitHub token not configured.", ephemeral=True)
        return

    # Defer immediately — many HTTP calls, guaranteed to exceed the 3s window
    await interaction.response.defer()

    try:
        headers = {'Authorization': f'token {github_token}'}
        url = f'https://api.github.com/repos/{repo_owner}/{repo_name}/issues?state=open'
        resp = await _http_get(url, headers)

        if resp.status_code != 200:
            await interaction.followup.send(
                f"❌ Could not fetch issues: {resp.status_code} — {resp.text}"
            )
            logger.error(f"/batchprocess fetch failed: {resp.status_code}")
            return

        issues = resp.json()
        all_usernames: list[str] = []

        for issue in issues:
            num = issue['number']
            usernames, err = await get_usernames_from_issue(
                repo_owner, repo_name, num, github_token
            )
            if err:
                logger.error(f"/batchprocess issue #{num}: {err}")
                continue
            all_usernames.extend(usernames)
            close_target = usernames[0] if usernames else "N/A"
            await close_github_issue_with_comment(
                repo_owner, repo_name, num, github_token, close_target
            )

        added, skipped = 0, 0
        for uname in set(all_usernames):
            success, *_ = await add_to_whitelist(uname, "Batch Process", admin=True)
            if success:
                added += 1
            else:
                skipped += 1

        await interaction.followup.send(
            f"✅ Processed **{len(issues)}** issue(s) — "
            f"found **{len(all_usernames)}** username(s), "
            f"whitelisted **{added}**, skipped **{skipped}** "
            f"(already whitelisted / account not found)."
        )
        logger.info(
            f"/batchprocess {repo_owner}/{repo_name}: "
            f"{len(issues)} issues, added {added}, skipped {skipped}"
        )

    except requests.exceptions.RequestException as e:
        await interaction.followup.send(f"❌ Network error: {e}")
        logger.error(f"/batchprocess network error: {e}")
    except Exception as e:
        await interaction.followup.send(f"❌ Unexpected error: {e}")
        logger.error(f"/batchprocess unexpected error: {e}")


# ── /setadmin ────────────────────────────────
@app_commands.command(name="setadmin", description="Grant admin privileges to a Discord user")
async def setadmin(interaction: discord.Interaction, user: discord.User):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    success, error_msg = add_admin(user.id, str(interaction.user))
    if success:
        await interaction.response.send_message(
            f"✅ **{user.display_name}** has been granted admin privileges "
            f"by {interaction.user.display_name}."
        )
        logger.info(f"Admin granted to {user} by {interaction.user}")
    else:
        await interaction.response.send_message(f"❌ {error_msg}")


# ── /removeadmin ─────────────────────────────
@app_commands.command(name="removeadmin", description="Revoke admin privileges from a Discord user")
async def removeadmin(interaction: discord.Interaction, user: discord.User):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.", ephemeral=True
        )
        return

    if user.id == interaction.user.id:
        await interaction.response.send_message(
            "❌ You cannot revoke your own admin privileges.", ephemeral=True
        )
        return

    success, error_msg = remove_admin(user.id)
    if success:
        await interaction.response.send_message(
            f"✅ **{user.display_name}**'s admin privileges have been revoked "
            f"by {interaction.user.display_name}."
        )
        logger.info(f"Admin revoked from {user} by {interaction.user}")
    else:
        await interaction.response.send_message(f"❌ {error_msg}")


# ─────────────────────────────────────────────
# Command registration & bot events
# ─────────────────────────────────────────────
async def register_commands():
    for cmd in (
        help, white, query, adduser, ban, unban,
        whitelisted, banned,
        processissue, batchprocess,
        setadmin, removeadmin,
    ):
        bot.tree.add_command(cmd)


@bot.event
async def on_ready():
    logger.info(f'{bot.user} connected to Discord.')
    init_admin_db()

    db_err = init_db()
    if db_err:
        logger.error(f"MySQL error on startup: {db_err}")
        await bot.change_presence(
            status=discord.Status.dnd,
            activity=discord.Game(name="Database Error")
        )
        return

    # Verify game API reachability (non-fatal — bot still starts)
    try:
        base = API_CONFIG['base_url'].rstrip('/')
        probe = await asyncio.to_thread(
            requests.get, f"{base}/Account/Ban",
            params={'uid': '0', 'isban': '0', 'adminkey': API_CONFIG['adminkey']},
            timeout=5
        )
        logger.info(f"Game API probe → HTTP {probe.status_code}")
    except Exception as e:
        logger.warning(f"Game API unreachable at startup: {e}")

    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game(name="Ready")
    )

    await register_commands()
    try:
        await bot.tree.sync()
        logger.info("Slash commands synced.")
    except Exception as e:
        logger.error(f"Command sync error: {e}")


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    TOKEN = CONFIG['discord']['token']
    if TOKEN == "YOUR_DISCORD_BOT_TOKEN_HERE":
        logger.error("Discord bot token not set. Please update config.json.")
        exit(1)

    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        if conn.is_connected():
            logger.info("MySQL pre-flight check passed.")
            conn.close()
    except Error as e:
        logger.error(f"Cannot connect to MySQL: {e}")
        exit(1)
    except Exception as e:
        logger.error(f"Unexpected MySQL error: {e}")
        exit(1)

    bot.run(TOKEN)
