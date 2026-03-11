import asyncio
import datetime
import discord
from discord.ext import commands
from discord import app_commands
import mysql.connector
from mysql.connector import Error
import requests
import yaml
import json
import os
import re
import logging

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# Configuration  —  YAML preferred, JSON fallback
#
# Search order:
#   1. config.yml
#   2. config.yaml
#   3. config.json
#   4. Write config.yml template and exit
# ─────────────────────────────────────────────
_CONFIG_DEFAULT_TEMPLATE = """\
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

# Output language for Discord responses
#   en   — English only
#   zh   — Chinese only
#   both — Bilingual (English + Chinese, default)
language: both

# Roles permitted to use /white (self-whitelist)
# Each entry is a role name (string) OR a role ID (integer)
white_roles:
  - Verified

admin:
  # Path to the admin list file (YAML)
  file: admin.yml
  # Discord user ID of the bootstrap administrator
  default_admin_id: 999999999999999999
  # Discord username of the bootstrap administrator (optional — preferred for lookup)
  default_admin_username: ""
"""


def load_config() -> dict:
    for path in ('config.yml', 'config.yaml'):
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    cfg = yaml.safe_load(f)
                logger.info(f"Config loaded from {path}")
                return cfg
            except yaml.YAMLError as e:
                logger.error(f"YAML error in {path}: {e}")

    if os.path.exists('config.json'):
        try:
            with open('config.json', 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            logger.info("Config loaded from config.json")
            return cfg
        except json.JSONDecodeError as e:
            logger.error(f"JSON error in config.json: {e}")

    # No config found — write template
    try:
        with open('config.yml', 'w', encoding='utf-8') as f:
            f.write(_CONFIG_DEFAULT_TEMPLATE)
        print("config.yml created. Fill in your credentials and restart.")
    except Exception as e:
        print(f"Could not write config.yml: {e}")

    # Return a minimal in-memory default so the process can continue to the
    # token check and print a clear "token not set" error rather than crashing.
    import yaml as _y
    return _y.safe_load(_CONFIG_DEFAULT_TEMPLATE)


try:
    CONFIG = load_config()
except Exception as e:
    print(f"Fatal: could not load configuration: {e}")
    exit(1)

# ─────────────────────────────────────────────
# Constants derived from config
# ─────────────────────────────────────────────
DB_CONFIG  = CONFIG['mysql']
API_CONFIG = CONFIG['game_api']
ADMIN_CFG  = CONFIG.get('admin', {})
ADMIN_YML  = ADMIN_CFG.get('file', 'admin.yml')
LANG       = CONFIG.get('language', 'both')   # 'en' | 'zh' | 'both'

# white_roles: normalise to a set containing strings (names) and ints (IDs)
_raw_roles  = CONFIG.get('white_roles', [])
WHITE_ROLES: set = {int(r) if str(r).isdigit() else str(r) for r in _raw_roles}

# ─────────────────────────────────────────────
# Bot setup  —  slash commands only
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.guilds  = True
intents.members = True
# message_content intentionally omitted — bot never reads message bodies

bot = commands.Bot(command_prefix='', intents=intents, help_command=None)

# ─────────────────────────────────────────────
# Bilingual output
# ─────────────────────────────────────────────
def T(en: str, zh: str) -> str:
    """Return text in the language set by config.language."""
    if LANG == 'en':   return en
    if LANG == 'zh':   return zh
    return f"{en}\n{zh}"


# ─────────────────────────────────────────────
# Block / isban semantics
#
#   GET /Account/Ban?uid=...&isban=0|1&adminkey=...
#   isban=0 / Block=0  →  ✅ Whitelisted  /  ✅ 已过白
#   isban=1 / Block=1  →  🚫 Banned       /  🚫 已封禁
# ─────────────────────────────────────────────
def fmt_block(block: int) -> str:
    if block == 0: return T("✅ Whitelisted", "✅ 已过白")
    if block == 1: return T("🚫 Banned",      "🚫 已封禁")
    return T(f"❓ Unknown ({block})", f"❓ 未知状态（{block}）")


def fmt_method(method: str) -> str:
    """
    Translate the internal method token into a bilingual display string.
    method == "API"       →  "via API" / "通过API"
    method == "DB:<err>"  →  "via database (API failed: <err>)" / "通过数据库（API失败：<err>）"
    """
    if method == "API":
        return T("via API", "通过API")
    api_err = method[3:]   # strip leading "DB:"
    return T(
        f"via database (API failed: {api_err})",
        f"通过数据库（API失败：{api_err}）"
    )


# ─────────────────────────────────────────────
# Permission helper for /white
# Checks member roles against white_roles in config
# ─────────────────────────────────────────────
def has_permitted_role(member: discord.Member) -> bool:
    """Return True if the member holds at least one role in WHITE_ROLES."""
    if not WHITE_ROLES:
        return False
    for role in member.roles:
        if role == member.guild.default_role:
            continue
        if role.name in WHITE_ROLES or role.id in WHITE_ROLES:
            return True
    return False


# ─────────────────────────────────────────────
# Identifier resolver
# ─────────────────────────────────────────────
def resolve_identifier(identifier: str):
    """
    '12345'    → ("Uid",      12345)
    'xialuoli' → ("UserName", "xialuoli")
    """
    s = identifier.strip()
    if s.isdigit():
        return "Uid", int(s)
    return "UserName", s


# ─────────────────────────────────────────────
# Admin management  —  admin.yml
#
# Each entry schema:
#   id:       Discord user ID  (integer)
#   username: Discord username (string)   ← preferred for lookup
#   added_by: who granted admin
#   added_at: ISO-8601 timestamp
#   note:     free text
#
# Lookup: username match (case-insensitive) takes priority over id match.
# File is written manually (not via yaml.dump) to preserve comment metadata.
# ─────────────────────────────────────────────
def _load_admins() -> list:
    if not os.path.exists(ADMIN_YML):
        return []
    try:
        with open(ADMIN_YML, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}
        return data.get('admins', []) or []
    except Exception as e:
        logger.error(f"Could not read {ADMIN_YML}: {e}")
        return []


def _yml_str(val) -> str:
    """
    Serialize val as a YAML double-quoted scalar string.
    Handles newlines, tabs, backslashes, and double-quotes safely.
    Using double-quoted YAML scalars means all standard escape sequences
    are valid and no special YAML characters can leak out of the value.
    """
    s = str(val) if val is not None else ''
    s = (s
         .replace('\\', '\\\\')
         .replace('"',  '\\"')
         .replace('\n', '\\n')
         .replace('\r', '\\r')
         .replace('\t', '\\t'))
    return f'"{s}"'


def _oneline(val) -> str:
    """Collapse any newlines in val to ' / ' for use inside # comment lines."""
    return str(val).replace('\r\n', ' / ').replace('\n', ' / ').replace('\r', ' / ')


def _save_admins(entries: list):
    """Write admin list to ADMIN_YML with per-entry comment metadata."""
    lines = [
        f"# Admin list  —  {ADMIN_YML}\n",
        "# Managed by the bot; safe to edit manually.\n",
        "# Lookup priority: username (case-insensitive) > id\n",
        "# Fields: id, username, added_by, added_at, note\n",
        "#\n",
        "admins:\n",
    ]
    for e in entries:
        uid      = e.get('id', '')
        uname    = e.get('username', '')
        added_by = e.get('added_by', '')
        added_at = e.get('added_at', '')
        note     = e.get('note', '')
        # Build a single-line comment — _oneline() collapses any embedded
        # newlines (e.g. from bilingual T() strings) so the comment never
        # spills onto a second line, which would corrupt the YAML.
        comment  = f"  # added_at: {_oneline(added_at)}  |  added_by: {_oneline(added_by)}"
        if note:
            comment += f"  |  note: {_oneline(note)}"
        lines.append(comment + "\n")
        lines.append(f"  - id:       {uid}\n")
        lines.append(f"    username: {_yml_str(uname)}\n")
        lines.append(f"    added_by: {_yml_str(added_by)}\n")
        lines.append(f"    added_at: {_yml_str(added_at)}\n")
        if note:
            lines.append(f"    note:     {_yml_str(note)}\n")
        lines.append("\n")
    try:
        with open(ADMIN_YML, 'w', encoding='utf-8') as f:
            f.writelines(lines)
    except Exception as e:
        logger.error(f"Could not write {ADMIN_YML}: {e}")


def _init_admin_yml():
    """Ensure admin.yml exists and contains the bootstrap admin from config."""
    entries     = _load_admins()
    default_id  = int(ADMIN_CFG.get('default_admin_id', 0) or 0)
    default_name = str(ADMIN_CFG.get('default_admin_username', '') or '')
    already = any(
        (default_id  and e.get('id') == default_id) or
        (default_name and e.get('username', '').lower() == default_name.lower())
        for e in entries
    )
    if not already and default_id:
        entries.append({
            'id':       default_id,
            'username': default_name,
            'added_by': 'SYSTEM',
            'added_at': datetime.datetime.now().isoformat(timespec='seconds'),
            'note':     'Bootstrap admin from config',
        })
        _save_admins(entries)
        logger.info(f"admin.yml: bootstrap admin id={default_id} written.")
    else:
        logger.info(f"admin.yml: {len(entries)} admin(s) loaded.")


def _migrate_admin_db():
    """
    If admin.db exists, copy all records to admin.yml then rename
    admin.db → admin.db.migrated.  Warns in log.
    """
    db_path = 'admin.db'
    if not os.path.exists(db_path):
        return
    logger.warning(
        "admin.db detected — migrating records to admin.yml. "
        "Original file will be renamed to admin.db.migrated."
    )
    try:
        import sqlite3
        con = sqlite3.connect(db_path)
        rows = con.execute("SELECT user_id, added_by, added_at FROM admins").fetchall()
        con.close()
    except Exception as e:
        logger.error(f"admin.db migration read failed: {e}")
        return
    entries     = _load_admins()
    existing_ids = {e.get('id') for e in entries}
    added = 0
    for (user_id, added_by, added_at) in rows:
        if user_id not in existing_ids:
            entries.append({
                'id':       user_id,
                'username': '',
                'added_by': added_by or 'MIGRATED',
                'added_at': str(added_at or datetime.datetime.now().isoformat(timespec='seconds')),
                'note':     'Migrated from admin.db',
            })
            existing_ids.add(user_id)
            added += 1
    _save_admins(entries)
    os.rename(db_path, db_path + '.migrated')
    logger.info(f"admin.db migration complete: {added} record(s) added.")


def is_admin(user_id: int, username: str = '') -> bool:
    """Username match (case-insensitive) takes priority over id match."""
    for e in _load_admins():
        if username and e.get('username', '').lower() == username.lower():
            return True
        if e.get('id') == user_id:
            return True
    return False


def add_admin(user_id: int, username: str, added_by: str, note: str = '') -> tuple:
    entries = _load_admins()
    for e in entries:
        dup_id   = e.get('id') == user_id
        dup_name = username and e.get('username', '').lower() == username.lower()
        if dup_id or dup_name:
            return False, T("User is already an admin.", "该用户已是管理员。")
    entries.append({
        'id':       user_id,
        'username': username,
        'added_by': added_by,
        'added_at': datetime.datetime.now().isoformat(timespec='seconds'),
        'note':     note,
    })
    _save_admins(entries)
    return True, None


def remove_admin(user_id: int, username: str = '') -> tuple:
    entries  = _load_admins()
    filtered = [
        e for e in entries
        if not (
            e.get('id') == user_id or
            (username and e.get('username', '').lower() == username.lower())
        )
    ]
    if len(filtered) == len(entries):
        return False, T("User is not an admin.", "该用户不是管理员。")
    _save_admins(filtered)
    return True, None


# ─────────────────────────────────────────────
# MySQL helpers  — account table
#
# Reads  : uid lookup, current Block, LoginDate
# Writes : fallback only when the game API fails
# ─────────────────────────────────────────────
def init_db():
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        if conn.is_connected():
            logger.info("MySQL connection verified.")
        conn.close()
    except Error as e:
        logger.error(f"MySQL error: {e}")
        return str(e)
    return None


def _read_account(identifier: str):
    """
    Short-lived MySQL read.  Returns (uid, username, block, login_date, error_msg).
    """
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cur  = conn.cursor()
        col, val = resolve_identifier(identifier)
        cur.execute(
            f"SELECT `Uid`, `UserName`, `Block`, `LoginDate` "
            f"FROM `account` WHERE `{col}` = %s LIMIT 1",
            (val,)
        )
        row = cur.fetchone()
        if not row:
            return None, None, None, None, T(
                f"Account [{identifier}] does not exist!",
                f"账号 [{identifier}] 不存在！"
            )
        uid, username, block, login_date = row
        return uid, username, block, login_date, None
    except Error as e:
        return None, None, None, None, T(f"Database error: {e}", f"数据库错误：{e}")
    except Exception as e:
        return None, None, None, None, T(f"Unexpected error: {e}", f"意外错误：{e}")
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


def query_account(identifier: str):
    """Returns (account_dict, error_msg).  account_dict keys: uid, username, block, login_date."""
    uid, username, block, login_date, err = _read_account(identifier)
    if err:
        return None, err
    return {"uid": uid, "username": username, "block": block, "login_date": login_date}, None


def _db_set_ban(uid: int, isban: int):
    """
    Direct MySQL fallback write.  Returns (success, error_msg).
    Called only after _api_set_ban() has already failed.
    """
    if isban not in (0, 1):
        return False, f"Invalid isban value '{isban}'."
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cur  = conn.cursor()
        cur.execute("UPDATE `account` SET `Block` = %s WHERE `Uid` = %s", (isban, uid))
        conn.commit()
        if cur.rowcount == 0:
            return False, T(
                f"No rows updated for Uid {uid}.",
                f"Uid {uid} 无更新行，账号可能不存在。"
            )
        logger.warning(f"DB fallback: Block={isban} set for Uid={uid} (game API unavailable)")
        return True, None
    except Error as e:
        return False, T(f"Database fallback error: {e}", f"数据库回退错误：{e}")
    except Exception as e:
        return False, T(f"Unexpected DB error: {e}", f"意外数据库错误：{e}")
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


# ─────────────────────────────────────────────
# Game API layer  — GET /Account/Ban
#
# Response shapes the server may return:
#   App-level:   {"code": 200|4xx|500, "msg": "...", "uid": N, "userName": "...", "isBan": N}
#   ASP.NET val: {"status": 400, "errors": {...}, "traceId": "..."}
# ─────────────────────────────────────────────
def _parse_api_error(data: dict, http_status: int) -> str:
    if "traceId" in data and "errors" in data:
        missing = ", ".join(f"`{k}`" for k in data["errors"])
        return T(
            f"Game API validation error — missing field(s): {missing}. Check config.",
            f"游戏API参数验证失败，缺少字段：{missing}，请检查配置。"
        )
    code = data.get("code", http_status)
    msg  = data.get("msg", "")
    if code == 404:
        return T("Account not found on game server.", "游戏服务器上未找到该账号。")
    if code == 403:
        return T(
            "Admin key rejected. Check game_api.adminkey in config.",
            "管理员密钥无效，请检查配置中的 adminkey。"
        )
    if code == 500:
        return T(
            f"Server error: admin key not configured server-side. ({msg})",
            f"服务器错误：服务端未配置管理员密钥。（{msg}）"
        )
    if code == 400:
        return T(f"Bad request: {msg}", f"请求错误：{msg}")
    return T(f"Unexpected API response (code={code}): {msg}", f"意外API响应（code={code}）：{msg}")


async def _api_set_ban(uid: int, isban: int):
    """
    Call GET /Account/Ban.
    Returns (success, api_uid, api_username, api_isban, error_msg).
    """
    if isban not in (0, 1):
        return False, None, None, None, f"Invalid isban '{isban}'."
    try:
        base = API_CONFIG["base_url"].rstrip("/")
        resp = await asyncio.to_thread(
            requests.get,
            f"{base}/Account/Ban",
            params={"uid": str(uid), "isban": isban, "adminkey": API_CONFIG["adminkey"]},
            timeout=10
        )
        try:
            data = resp.json()
        except ValueError:
            return False, None, None, None, T(
                f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:120]}",
                f"非JSON响应（HTTP {resp.status_code}）：{resp.text[:120]}"
            )
        if data.get("code") == 200:
            logger.info(
                f"API /Account/Ban uid={uid} isban={isban} → 200 "
                f"userName={data.get('userName')!r} isBan={data.get('isBan')}"
            )
            return True, data.get("uid"), data.get("userName"), data.get("isBan"), None
        err = _parse_api_error(data, resp.status_code)
        logger.error(f"API /Account/Ban uid={uid} isban={isban} → HTTP {resp.status_code} body={data}")
        return False, None, None, None, err
    except requests.exceptions.Timeout:
        return False, None, None, None, T("Game API timed out (10 s).", "游戏API请求超时（10秒）。")
    except requests.exceptions.ConnectionError:
        return False, None, None, None, T(
            f"Cannot connect to game API at {API_CONFIG['base_url']}.",
            f"无法连接游戏API：{API_CONFIG['base_url']}。"
        )
    except Exception as e:
        return False, None, None, None, T(f"API error: {e}", f"API错误：{e}")


# ─────────────────────────────────────────────
# Shared write core  —  API first, DB fallback
#
# Returns (success, out_uid, out_username, method, error_msg)
#   method tokens:
#     "API"        — game API succeeded
#     "DB:<err>"   — API failed, DB fallback succeeded (err = API error detail)
#     None         — both failed; error_msg contains combined detail
# ─────────────────────────────────────────────
async def _do_set_ban(uid: int, username: str, isban: int):
    ok, api_uid, api_uname, _, api_err = await _api_set_ban(uid, isban)
    if ok:
        return (
            True,
            api_uid   if api_uid   is not None else uid,
            api_uname if api_uname is not None else username,
            "API",
            None
        )
    # API failed — attempt DB fallback
    logger.warning(f"API failed uid={uid} isban={isban}: {api_err}. Trying DB fallback.")
    db_ok, db_err = _db_set_ban(uid, isban)
    if db_ok:
        return True, uid, username, f"DB:{api_err}", None
    return False, uid, username, None, T(
        f"API: {api_err} | DB: {db_err}",
        f"API：{api_err} | 数据库：{db_err}"
    )


# ─────────────────────────────────────────────
# High-level account operations
# ─────────────────────────────────────────────
async def add_to_whitelist(identifier: str, added_by: str, admin: bool = False):
    """
    Returns (success, uid, username, method, error_msg).
    admin=False (/white): only allowed when Block == 1.
    admin=True  (/adduser, GitHub): allowed from any Block state.
    """
    uid, username, block, _, err = _read_account(identifier)
    if err:
        return False, None, None, None, err
    if block == 0:
        return False, uid, username, None, T(
            f"**{username}** ({uid}) is already whitelisted.",
            f"**{username}**（{uid}）已在白名单中。"
        )
    if not admin and block != 1:
        return False, uid, username, None, T(
            f"**{username}** ({uid}) cannot be self-whitelisted. Contact an admin.",
            f"**{username}**（{uid}）无法自助过白，请联系管理员。"
        )
    ok, out_uid, out_uname, method, err = await _do_set_ban(uid, username, isban=0)
    if ok:
        logger.info(f"Whitelisted '{out_uname}' (Uid {out_uid}) by {added_by} [{method}] admin={admin}")
    return ok, out_uid, out_uname, method, err


async def ban_user(identifier: str, banned_by: str, reason: str):
    """Returns (success, uid, username, old_block, method, error_msg)."""
    uid, username, block, _, err = _read_account(identifier)
    if err:
        return False, None, None, None, None, err
    if block == 1:
        return False, uid, username, block, None, T(
            f"**{username}** ({uid}) is already banned.",
            f"**{username}**（{uid}）已被封禁。"
        )
    ok, out_uid, out_uname, method, err = await _do_set_ban(uid, username, isban=1)
    if ok:
        logger.info(f"Banned '{out_uname}' (Uid {out_uid}) by {banned_by} [{method}]. Reason: {reason}")
    return ok, out_uid, out_uname, block, method, err


async def unban_user(identifier: str):
    """Returns (success, uid, username, method, error_msg)."""
    uid, username, block, _, err = _read_account(identifier)
    if err:
        return False, None, None, None, err
    if block == 0:
        return False, uid, username, None, T(
            f"**{username}** ({uid}) is already whitelisted.",
            f"**{username}**（{uid}）已在白名单中。"
        )
    ok, out_uid, out_uname, method, err = await _do_set_ban(uid, username, isban=0)
    if ok:
        logger.info(f"Unbanned '{out_uname}' (Uid {out_uid}) [{method}], was Block={block}")
    return ok, out_uid, out_uname, method, err


# ─────────────────────────────────────────────
# GitHub helpers
# Fine-grained PATs require Bearer scheme; classic PATs accept both.
# ─────────────────────────────────────────────
def _github_headers(token: str) -> dict:
    return {
        'Authorization':        f'Bearer {token}',
        'Accept':               'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    }

async def _http_get(url, headers):
    return await asyncio.to_thread(requests.get, url, headers=headers)

async def _http_post(url, payload, headers):
    return await asyncio.to_thread(requests.post, url, json=payload, headers=headers)

async def _http_patch(url, payload, headers):
    return await asyncio.to_thread(requests.patch, url, json=payload, headers=headers)


def extract_username_from_issue(issue_body: str):
    if not issue_body:
        return None
    m = re.search(
        r'游戏账号\s*\([^)]*Game\s*Username[^\)]*\)\s*[\r\n]*\s*([^\r\n]+)',
        issue_body, re.IGNORECASE
    )
    if m:
        username = re.sub(r'^[:\-\s]+', '', m.group(1).strip())
        return username or None
    m = re.search(r'例如:\s*([a-zA-Z0-9_]+)', issue_body)
    if m:
        return m.group(1).strip()
    skip = {'game', 'username', 'example', 'for', 'the', 'and', 'or', 'not', 'yes', 'no'}
    tokens = [t for t in re.findall(r'([a-zA-Z0-9_]{3,20})', issue_body) if t.lower() not in skip]
    return tokens[0] if tokens else None


async def get_usernames_from_issue(repo_owner, repo_name, issue_number, token):
    try:
        resp = await _http_get(
            f'https://api.github.com/repos/{repo_owner}/{repo_name}/issues/{issue_number}',
            _github_headers(token)
        )
        if resp.status_code != 200:
            return [], T(
                f"GitHub API error {resp.status_code}: {resp.text}",
                f"GitHub API错误 {resp.status_code}：{resp.text}"
            )
        username = extract_username_from_issue(resp.json().get('body') or "")
        if not username:
            return [], T(
                "No valid username found in issue body.",
                "Issue内容中未找到有效游戏账号。"
            )
        return [username], None
    except Exception as e:
        return [], T(f"Network error: {e}", f"网络错误：{e}")


async def close_github_issue_with_comment(repo_owner, repo_name, issue_number, token, username):
    try:
        headers  = _github_headers(token)
        base_url = f'https://api.github.com/repos/{repo_owner}/{repo_name}'
        comment_body = T(
            f'🤖 [Auto-Reply]\n'
            f'✅ Account `{username}` has been added to the whitelist!\n'
            f'Please re-login to the game.',
            f'🤖 [自动回复]\n'
            f'✅ 账号 `{username}` 已成功过白！\n'
            f'✅ 请重新登录游戏。'
        )
        cr = await _http_post(
            f'{base_url}/issues/{issue_number}/comments',
            {'body': comment_body},
            headers
        )
        if cr.status_code not in (200, 201):
            logger.error(
                f"GitHub comment failed HTTP {cr.status_code} "
                f"{repo_owner}/{repo_name}#{issue_number}: {cr.text[:300]}"
            )
            return False, T(
                f"Failed to post comment (HTTP {cr.status_code}). "
                f"Check token Issues: Read & Write permission.",
                f"评论发送失败（HTTP {cr.status_code}），请检查令牌Issues读写权限。"
            )
        pr = await _http_patch(
            f'{base_url}/issues/{issue_number}',
            {'state': 'closed', 'state_reason': 'completed'},
            headers
        )
        if pr.status_code == 200:
            return True, None
        logger.error(
            f"GitHub close failed HTTP {pr.status_code} "
            f"{repo_owner}/{repo_name}#{issue_number}: {pr.text[:300]}"
        )
        return False, T(
            f"Failed to close issue (HTTP {pr.status_code}). "
            f"Check token Issues: Read & Write permission.",
            f"关闭Issue失败（HTTP {pr.status_code}），请检查令牌Issues读写权限。"
        )
    except Exception as e:
        return False, T(f"GitHub API error: {e}", f"GitHub API错误：{e}")


# ─────────────────────────────────────────────
# Slash commands
# ─────────────────────────────────────────────

# ── /help ────────────────────────────────────
@app_commands.command(name="help", description="Show all bot commands / 显示所有指令")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(T(
        """**Discord Whitelist Bot — Commands**

**General** (roles listed in `white_roles` config):
`/white <uid|username>` — Self-whitelist your game account

**Admin only:**
`/query <uid|username>` — Look up full account info
`/adduser <uid|username>` — Force-whitelist an account (bypasses all restrictions)
`/ban <uid|username> [reason]` — Ban an account
`/unban <uid|username>` — Unban an account
`/whitelisted` — List all whitelisted accounts (Block=0)
`/banned` — List all banned accounts (Block=1)
`/processissue <owner> <repo> <number>` — Process one GitHub whitelist issue
`/batchprocess <owner> <repo>` — Batch-process all open whitelist issues
`/setadmin <user>` — Grant admin privileges
`/removeadmin <user>` — Revoke admin privileges

**Execution method** is shown in every response:
`via API` — game server API succeeded
`via database (API failed: ...)` — API failed, direct DB write used as fallback

**Admin list** is stored in `admin.yml`. Username match takes priority over ID.""",

        """**Discord 白名单机器人 — 指令列表**

**普通成员**（需拥有 `white_roles` 配置中的身份组）：
`/white <uid|用户名>` — 自助申请白名单

**仅管理员：**
`/query <uid|用户名>` — 查询账号详细信息
`/adduser <uid|用户名>` — 强制加入白名单（跳过所有限制）
`/ban <uid|用户名> [原因]` — 封禁账号
`/unban <uid|用户名>` — 解封账号
`/whitelisted` — 列出所有白名单账号（Block=0）
`/banned` — 列出所有封禁账号（Block=1）
`/processissue <owner> <repo> <编号>` — 处理单个GitHub过白Issue
`/batchprocess <owner> <repo>` — 批量处理所有开放Issue
`/setadmin <用户>` — 授予管理员权限
`/removeadmin <用户>` — 撤销管理员权限

**执行方式**会显示在所有操作响应中：
`通过API` — 游戏服务器API执行成功
`通过数据库（API失败：...）` — API失败，已通过直接写库作为回退

**管理员列表**保存于 `admin.yml`，优先按用户名匹配，其次按ID匹配。"""
    ), ephemeral=True)


# ── /white ───────────────────────────────────
@app_commands.command(name="white", description="Self-whitelist your game account / 申请白名单")
@app_commands.describe(identifier="Game Uid or UserName / 游戏UID或用户名")
async def white(interaction: discord.Interaction, identifier: str):
    member = interaction.user
    if not isinstance(member, discord.Member) or not has_permitted_role(member):
        await interaction.response.send_message(
            T("❌ You don't have the required role to use this command.",
              "❌ 您没有使用此指令所需的身份组。"),
            ephemeral=True
        )
        return
    await interaction.response.defer()
    try:
        ok, uid, username, method, err = await add_to_whitelist(
            identifier, str(interaction.user), admin=False
        )
        if ok:
            await interaction.followup.send(T(
                f"✅ **{username}** ({uid}) added to whitelist! [{fmt_method(method)}]",
                f"✅ **{username}**（{uid}）已成功过白！【{fmt_method(method)}】"
            ))
        else:
            await interaction.followup.send(f"❌ {err}")
    except Exception as e:
        await interaction.followup.send(f"❌ {T(f'Unexpected error: {e}', f'意外错误：{e}')}")
        logger.error(f"/white error: {e}")


# ── /query ───────────────────────────────────
@app_commands.command(name="query", description="Query account info (Admin) / 查询账号信息（管理员）")
@app_commands.describe(identifier="Game Uid or UserName / 游戏UID或用户名")
async def query(interaction: discord.Interaction, identifier: str):
    if not is_admin(interaction.user.id, interaction.user.name):
        await interaction.response.send_message(
            T("❌ Admin only.", "❌ 仅管理员可用。"), ephemeral=True
        )
        return
    account, err = query_account(identifier)
    if not account:
        await interaction.response.send_message(f"❌ {err}")
        return
    await interaction.response.send_message(T(
        f"🔍 **Account Query**\n━━━━━━━━━━━━━━\n"
        f"👤 Username: `{account['username']}`\n"
        f"🆔 UID: `{account['uid']}`\n"
        f"🏳️ Status: {fmt_block(account['block'])}\n"
        f"🕒 Last Login: `{account['login_date']}`",

        f"🔍 **账号查询**\n━━━━━━━━━━━━━━\n"
        f"👤 用户名：`{account['username']}`\n"
        f"🆔 UID：`{account['uid']}`\n"
        f"🏳️ 状态：{fmt_block(account['block'])}\n"
        f"🕒 最后登录：`{account['login_date']}`"
    ))


# ── /adduser ─────────────────────────────────
@app_commands.command(name="adduser", description="Force-whitelist an account (Admin) / 强制过白（管理员）")
@app_commands.describe(identifier="Game Uid or UserName / 游戏UID或用户名")
async def adduser(interaction: discord.Interaction, identifier: str):
    if not is_admin(interaction.user.id, interaction.user.name):
        await interaction.response.send_message(
            T("❌ Admin only.", "❌ 仅管理员可用。"), ephemeral=True
        )
        return
    await interaction.response.defer()
    try:
        ok, uid, username, method, err = await add_to_whitelist(
            identifier, str(interaction.user), admin=True
        )
        if ok:
            await interaction.followup.send(T(
                f"✅ **{username}** ({uid}) added to whitelist! [{fmt_method(method)}]",
                f"✅ **{username}**（{uid}）已成功过白！【{fmt_method(method)}】"
            ))
        else:
            await interaction.followup.send(f"❌ {err}")
    except Exception as e:
        await interaction.followup.send(f"❌ {T(f'Unexpected error: {e}', f'意外错误：{e}')}")
        logger.error(f"/adduser error: {e}")


# ── /ban ─────────────────────────────────────
@app_commands.command(name="ban", description="Ban an account (Admin) / 封禁账号（管理员）")
@app_commands.describe(
    identifier="Game Uid or UserName / 游戏UID或用户名",
    reason="Reason / 原因"
)
async def ban(
    interaction: discord.Interaction,
    identifier: str,
    reason: str = "No reason provided / 未提供原因"
):
    if not is_admin(interaction.user.id, interaction.user.name):
        await interaction.response.send_message(
            T("❌ Admin only.", "❌ 仅管理员可用。"), ephemeral=True
        )
        return
    await interaction.response.defer()
    try:
        ok, uid, username, old_block, method, err = await ban_user(
            identifier, str(interaction.user), reason
        )
        if ok:
            await interaction.followup.send(T(
                f"🚫 **Account Banned** [{fmt_method(method)}]\n"
                f"User: `{username}`  UID: `{uid}`\n"
                f"Before: {fmt_block(old_block)} → After: {fmt_block(1)}",

                f"🚫 **账号已封禁**【{fmt_method(method)}】\n"
                f"用户：`{username}`  UID：`{uid}`\n"
                f"变更：{fmt_block(old_block)} → {fmt_block(1)}"
            ))
        else:
            await interaction.followup.send(f"❌ {err}")
    except Exception as e:
        await interaction.followup.send(f"❌ {T(f'Unexpected error: {e}', f'意外错误：{e}')}")
        logger.error(f"/ban error: {e}")


# ── /unban ───────────────────────────────────
@app_commands.command(name="unban", description="Unban an account (Admin) / 解封账号（管理员）")
@app_commands.describe(identifier="Game Uid or UserName / 游戏UID或用户名")
async def unban(interaction: discord.Interaction, identifier: str):
    if not is_admin(interaction.user.id, interaction.user.name):
        await interaction.response.send_message(
            T("❌ Admin only.", "❌ 仅管理员可用。"), ephemeral=True
        )
        return
    await interaction.response.defer()
    try:
        ok, uid, username, method, err = await unban_user(identifier)
        if ok:
            await interaction.followup.send(T(
                f"✅ **Account Unbanned** [{fmt_method(method)}]\n"
                f"User: `{username}`  UID: `{uid}`\n"
                f"Status: {fmt_block(0)}",

                f"✅ **账号已解封**【{fmt_method(method)}】\n"
                f"用户：`{username}`  UID：`{uid}`\n"
                f"状态：{fmt_block(0)}"
            ))
        else:
            await interaction.followup.send(f"❌ {err}")
    except Exception as e:
        await interaction.followup.send(f"❌ {T(f'Unexpected error: {e}', f'意外错误：{e}')}")
        logger.error(f"/unban error: {e}")


# ── /whitelisted ─────────────────────────────
@app_commands.command(name="whitelisted", description="List whitelisted accounts (Admin) / 白名单列表（管理员）")
async def whitelisted(interaction: discord.Interaction):
    if not is_admin(interaction.user.id, interaction.user.name):
        await interaction.response.send_message(
            T("❌ Admin only.", "❌ 仅管理员可用。"), ephemeral=True
        )
        return
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cur  = conn.cursor()
        cur.execute(
            "SELECT `Uid`, `UserName`, `LoginDate` FROM `account` "
            "WHERE `Block`=0 ORDER BY `LoginDate` DESC"
        )
        rows = cur.fetchall()
        if not rows:
            await interaction.response.send_message(
                T("📋 No whitelisted accounts.", "📋 暂无白名单账号。")
            )
            return
        lines = [T(f"📋 Whitelisted Accounts ({len(rows)} total):", f"📋 白名单账号（共 {len(rows)} 个）：")]
        for uid, uname, ldate in rows[:20]:
            lines.append(f"- `{uname}` (UID: {uid}, {T('Last Login', '最后登录')}: {ldate})")
        if len(rows) > 20:
            lines.append(T(f"… and {len(rows)-20} more.", f"……以及另外 {len(rows)-20} 个。"))
        await interaction.response.send_message("\n".join(lines))
    except Error as e:
        await interaction.response.send_message(
            f"❌ {T(f'Database error: {e}', f'数据库错误：{e}')}"
        )
        logger.error(f"/whitelisted db error: {e}")
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


# ── /banned ──────────────────────────────────
@app_commands.command(name="banned", description="List banned accounts (Admin) / 封禁列表（管理员）")
async def banned(interaction: discord.Interaction):
    if not is_admin(interaction.user.id, interaction.user.name):
        await interaction.response.send_message(
            T("❌ Admin only.", "❌ 仅管理员可用。"), ephemeral=True
        )
        return
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cur  = conn.cursor()
        cur.execute(
            "SELECT `Uid`, `UserName`, `LoginDate` FROM `account` "
            "WHERE `Block`=1 ORDER BY `LoginDate` DESC"
        )
        rows = cur.fetchall()
        if not rows:
            await interaction.response.send_message(
                T("📋 No banned accounts.", "📋 暂无封禁账号。")
            )
            return
        lines = [T(f"📋 Banned Accounts ({len(rows)} total):", f"📋 封禁账号（共 {len(rows)} 个）：")]
        for uid, uname, ldate in rows[:20]:
            lines.append(f"- `{uname}` (UID: {uid}, {T('Last Login', '最后登录')}: {ldate})")
        if len(rows) > 20:
            lines.append(T(f"… and {len(rows)-20} more.", f"……以及另外 {len(rows)-20} 个。"))
        await interaction.response.send_message("\n".join(lines))
    except Error as e:
        await interaction.response.send_message(
            f"❌ {T(f'Database error: {e}', f'数据库错误：{e}')}"
        )
        logger.error(f"/banned db error: {e}")
    finally:
        if 'conn' in locals() and conn.is_connected():
            cur.close(); conn.close()


# ── /processissue ────────────────────────────
@app_commands.command(
    name="processissue",
    description="Process a GitHub whitelist issue (Admin) / 处理单个Issue（管理员）"
)
@app_commands.describe(
    repo_owner="Repository owner",
    repo_name="Repository name",
    issue_number="Issue number"
)
async def processissue(
    interaction: discord.Interaction,
    repo_owner: str,
    repo_name: str,
    issue_number: int
):
    if not is_admin(interaction.user.id, interaction.user.name):
        await interaction.response.send_message(
            T("❌ Admin only.", "❌ 仅管理员可用。"), ephemeral=True
        )
        return
    github_token = CONFIG['github']['token']
    if github_token == "YOUR_GITHUB_TOKEN_HERE":
        await interaction.response.send_message(
            T("❌ GitHub token not configured.", "❌ GitHub令牌未配置。"), ephemeral=True
        )
        return
    await interaction.response.defer()
    try:
        usernames, err = await get_usernames_from_issue(
            repo_owner, repo_name, issue_number, github_token
        )
        if err:
            await interaction.followup.send(f"❌ {err}")
            return
        if not usernames:
            await interaction.followup.send(
                T("❌ No valid username found in this issue.", "❌ Issue中未找到有效游戏账号。")
            )
            return
        added, skipped, notes = 0, 0, []
        for uname in usernames:
            ok, uid, username, method, op_err = await add_to_whitelist(
                uname, f"GitHub #{issue_number}", admin=True
            )
            if ok:
                added += 1
                notes.append(T(
                    f"  ✅ {username} ({uid}) [{fmt_method(method)}]",
                    f"  ✅ {username}（{uid}）【{fmt_method(method)}】"
                ))
            else:
                skipped += 1
                notes.append(f"  ⚠️ {uname}: {op_err}")
        close_ok, close_err = await close_github_issue_with_comment(
            repo_owner, repo_name, issue_number, github_token, usernames[0]
        )
        lines = [T(
            f"✅ Processed: whitelisted **{added}**, skipped **{skipped}**.",
            f"✅ 处理完成：过白 **{added}** 个，跳过 **{skipped}** 个。"
        )]
        lines += notes
        lines.append(
            T("✅ Issue closed.", "✅ Issue已关闭。") if close_ok
            else f"⚠️ {close_err}"
        )
        await interaction.followup.send("\n".join(lines))
        logger.info(
            f"/processissue {repo_owner}/{repo_name}#{issue_number}: "
            f"added={added} skipped={skipped}"
        )
    except Exception as e:
        await interaction.followup.send(f"❌ {T(f'Unexpected error: {e}', f'意外错误：{e}')}")
        logger.error(f"/processissue error: {e}")


# ── /batchprocess ────────────────────────────
@app_commands.command(
    name="batchprocess",
    description="Batch-process all open whitelist issues (Admin) / 批量处理Issue（管理员）"
)
@app_commands.describe(repo_owner="Repository owner", repo_name="Repository name")
async def batchprocess(interaction: discord.Interaction, repo_owner: str, repo_name: str):
    if not is_admin(interaction.user.id, interaction.user.name):
        await interaction.response.send_message(
            T("❌ Admin only.", "❌ 仅管理员可用。"), ephemeral=True
        )
        return
    github_token = CONFIG['github']['token']
    if github_token == "YOUR_GITHUB_TOKEN_HERE":
        await interaction.response.send_message(
            T("❌ GitHub token not configured.", "❌ GitHub令牌未配置。"), ephemeral=True
        )
        return
    await interaction.response.defer()
    try:
        resp = await _http_get(
            f'https://api.github.com/repos/{repo_owner}/{repo_name}/issues?state=open',
            _github_headers(github_token)
        )
        if resp.status_code != 200:
            await interaction.followup.send(T(
                f"❌ Could not fetch issues: HTTP {resp.status_code}",
                f"❌ 无法获取Issue列表：HTTP {resp.status_code}"
            ))
            return
        issues        = resp.json()
        all_usernames = []
        for issue in issues:
            num = issue['number']
            usernames, err = await get_usernames_from_issue(
                repo_owner, repo_name, num, github_token
            )
            if err:
                logger.error(f"/batchprocess issue #{num}: {err}")
                continue
            all_usernames.extend(usernames)
            if usernames:
                await close_github_issue_with_comment(
                    repo_owner, repo_name, num, github_token, usernames[0]
                )
        added, skipped = 0, 0
        for uname in set(all_usernames):
            ok, *_ = await add_to_whitelist(uname, "Batch Process", admin=True)
            if ok: added += 1
            else:  skipped += 1
        await interaction.followup.send(T(
            f"✅ Processed **{len(issues)}** issue(s) — "
            f"found **{len(all_usernames)}** username(s), "
            f"whitelisted **{added}**, skipped **{skipped}**.",
            f"✅ 共处理 **{len(issues)}** 个Issue，"
            f"找到 **{len(all_usernames)}** 个账号，"
            f"过白 **{added}** 个，跳过 **{skipped}** 个。"
        ))
        logger.info(
            f"/batchprocess {repo_owner}/{repo_name}: "
            f"issues={len(issues)} added={added} skipped={skipped}"
        )
    except Exception as e:
        await interaction.followup.send(f"❌ {T(f'Unexpected error: {e}', f'意外错误：{e}')}")
        logger.error(f"/batchprocess error: {e}")


# ── /setadmin ────────────────────────────────
@app_commands.command(
    name="setadmin",
    description="Grant admin privileges (Admin) / 授予管理员权限（管理员）"
)
async def setadmin(interaction: discord.Interaction, user: discord.User):
    if not is_admin(interaction.user.id, interaction.user.name):
        await interaction.response.send_message(
            T("❌ Admin only.", "❌ 仅管理员可用。"), ephemeral=True
        )
        return
    note = T(
        f"Granted via /setadmin by {interaction.user.name}",
        f"由 {interaction.user.name} 通过 /setadmin 授予"
    )
    ok, err = add_admin(user.id, str(user.name), str(interaction.user.name), note=note)
    if ok:
        await interaction.response.send_message(T(
            f"✅ **{user.display_name}** granted admin privileges "
            f"by {interaction.user.display_name}.",
            f"✅ **{user.display_name}** 已被 {interaction.user.display_name} 授予管理员权限。"
        ))
        logger.info(f"Admin granted: {user.name} (id={user.id}) by {interaction.user.name}")
    else:
        await interaction.response.send_message(f"❌ {err}")


# ── /removeadmin ─────────────────────────────
@app_commands.command(
    name="removeadmin",
    description="Revoke admin privileges (Admin) / 撤销管理员权限（管理员）"
)
async def removeadmin(interaction: discord.Interaction, user: discord.User):
    if not is_admin(interaction.user.id, interaction.user.name):
        await interaction.response.send_message(
            T("❌ Admin only.", "❌ 仅管理员可用。"), ephemeral=True
        )
        return
    if user.id == interaction.user.id:
        await interaction.response.send_message(
            T("❌ You cannot revoke your own admin privileges.",
              "❌ 不能撤销自己的管理员权限。"),
            ephemeral=True
        )
        return
    ok, err = remove_admin(user.id, str(user.name))
    if ok:
        await interaction.response.send_message(T(
            f"✅ **{user.display_name}**'s admin privileges revoked "
            f"by {interaction.user.display_name}.",
            f"✅ **{user.display_name}** 的管理员权限已被 {interaction.user.display_name} 撤销。"
        ))
        logger.info(f"Admin revoked: {user.name} (id={user.id}) by {interaction.user.name}")
    else:
        await interaction.response.send_message(f"❌ {err}")


# ─────────────────────────────────────────────
# Command registration & bot events
# ─────────────────────────────────────────────
async def register_commands():
    for cmd in (
        help_cmd, white, query, adduser, ban, unban,
        whitelisted, banned,
        processissue, batchprocess,
        setadmin, removeadmin,
    ):
        bot.tree.add_command(cmd)


@bot.event
async def on_message(message):
    # Slash commands only — never process text messages
    pass


@bot.event
async def on_ready():
    logger.info(f'{bot.user} connected to Discord.')

    # Admin store setup (migration then init)
    _migrate_admin_db()
    _init_admin_yml()

    # MySQL check
    db_err = init_db()
    if db_err:
        logger.error(f"MySQL error on startup: {db_err}")
        await bot.change_presence(
            status=discord.Status.dnd,
            activity=discord.Game(name="Database Error")
        )
        return

    # Game API probe (non-fatal — bot continues regardless)
    try:
        base  = API_CONFIG['base_url'].rstrip('/')
        probe = await asyncio.to_thread(
            requests.get,
            f"{base}/Account/Ban",
            params={'uid': '0', 'isban': '0', 'adminkey': API_CONFIG['adminkey']},
            timeout=5
        )
        try:
            pdata = probe.json()
            pcode = pdata.get("code", probe.status_code)
        except ValueError:
            pcode = probe.status_code
        if pcode in (200, 404):
            logger.info(f"Game API reachable (probe code={pcode})")
        elif pcode == 403:
            logger.warning("Game API: adminkey is INVALID — API writes will fail; DB fallback active.")
        elif pcode == 500:
            logger.warning("Game API: admin key not configured server-side — DB fallback active.")
        else:
            logger.warning(f"Game API probe returned unexpected code={pcode}")
    except requests.exceptions.ConnectionError:
        logger.warning(
            f"Game API unreachable at {API_CONFIG['base_url']}. "
            "All writes will use DB fallback until API becomes available."
        )
    except Exception as e:
        logger.warning(f"Game API probe error: {e}")

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
        logger.error("Discord bot token not set. Please fill in config.yml and restart.")
        exit(1)
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        if conn.is_connected():
            logger.info("MySQL pre-flight check passed.")
            conn.close()
    except Error as e:
        logger.error(f"Cannot connect to MySQL: {e}")
        exit(1)
    bot.run(TOKEN)
