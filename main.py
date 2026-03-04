"""
Nico Jr. — Discord bot that creates and updates Jira tickets from natural language messages.

Flow:
  1. A message arrives in the watched channel.
  2. Nico Jr. collects any attachments (screenshots / recordings) from the message.
  3. Nico Jr. fetches the current list of Epics from Jira.
  4. Claude reads the message and decides: create, update, or ignore.
     - For create: Claude extracts all fields AND picks the best matching Epic.
     - For update: Claude extracts the ticket key and only the fields to change.
  5. Nico Jr. calls the Jira REST API and replies with a confirmation in Discord.
"""

import os
import re
import sys
import json
import base64
import signal
import atexit
import asyncio
import sqlite3
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import tasks
import anthropic
import requests
from dotenv import load_dotenv

# Load all secrets from the .env file
load_dotenv()

# ── PID file — ensures only one instance runs at a time ───────────────────────

PID_FILE = os.path.join(os.path.dirname(__file__), ".alfred.pid")

def _write_pid():
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

def _remove_pid():
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass

# Write PID on startup, remove it on clean exit
_write_pid()
atexit.register(_remove_pid)

# ── Configuration ──────────────────────────────────────────────────────────────

DISCORD_TOKEN     = os.getenv("DISCORD_BOT_TOKEN")
REMINDERS_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))  # channel where bug reminders are posted

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

JIRA_BASE_URL     = os.getenv("JIRA_BASE_URL")   # e.g. https://yourcompany.atlassian.net
JIRA_EMAIL        = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN    = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEY  = os.getenv("JIRA_PROJECT_KEY")

# How Nico Jr. links a ticket to an Epic depends on your Jira project type:
#   "customfield_10014" → classic / company-managed projects  (most common)
#   "parent"            → next-gen / team-managed projects
# Change this value in your .env if needed, or just edit the line below.
JIRA_EPIC_LINK_FIELD = os.getenv("JIRA_EPIC_LINK_FIELD", "customfield_10014")

# ── Bug reminder configuration ─────────────────────────────────────────────────

# How long after creation (and between subsequent reminders) before Nico Jr. pings
REMINDER_INTERVALS: dict[str, timedelta] = {
    "Highest": timedelta(hours=24),
    "High":    timedelta(hours=24),
    "Medium":  timedelta(hours=48),
    "Low":     timedelta(hours=72),
    "Lowest":  timedelta(hours=72),
}

# Statuses that count as "resolved" and suppress reminders, keyed by priority.
# Actual Jira statuses in this project (from /rest/api/3/search/jql):
#   Blocked | Dropped | For Prod Release | For Prod Testing |
#   In Prod and Working as Expected | In Prod and for Monitoring | QA Testing
#
# "In Prod and Working as Expected" = the combined terminal state for all priorities.
# "Dropped" = won't-fix — no point reminding.
# Low priority follows the same rule: only fully resolved (WAE) stops the reminder.
REMINDER_TERMINAL_STATUSES: dict[str, set] = {
    "Highest": {"In Prod and Working as Expected", "Dropped"},
    "High":    {"In Prod and Working as Expected", "Dropped"},
    "Medium":  {"In Prod and Working as Expected", "Dropped"},
    "Low":     {"In Prod and Working as Expected", "Dropped"},
    "Lowest":  {"In Prod and Working as Expected", "Dropped"},
}

# Display order and emoji for priority groups in reminder messages
PRIORITY_ORDER  = ["Highest", "High", "Medium", "Low", "Lowest"]
PRIORITY_EMOJI  = {"Highest": "🔴", "High": "🟠", "Medium": "🟡", "Low": "🔵", "Lowest": "⚪"}

# ── Ticket title prefix configuration ─────────────────────────────────────────

# Maps a substring of the Epic summary to its short label (case-insensitive).
# Add an entry for each epic. The first matching keyword wins.
# Example: "Mobile App Redesign" → if "Mobile" is a key, it uses that label.
EPIC_ABBREVIATIONS: dict[str, str] = {
    # "Mobile": "MA",
    # "Backend": "BE",
    # "Android": "ANDR",
}

# Maps Jira display names to their team tag, prepended to ticket titles.
ASSIGNEE_TAGS: dict[str, str] = {
    "Charl Lance Cua":        "[BE3]",
    "Benjamin Perez":         "[BEx]",
    "Michael Gian Tiqui":     "[FE2]",
    "Francis Mario Calvadores": "[BE2]",
    "Davidson Ramos":         "[BE1]",
    "Rey Robert Castro":      "[AD2]",
    "Reniel Don Galerio":     "[IO2]",
    "Rolando Maming":         "[AD1]",
    "Milky Joy Agora":        "[IO1]",
    "Jasper Caparas":         "[UI]",
    "John Allen De Chavez":   "[FE1]",
}


def build_title_prefix(epic_key: str | None, assignee_name: str | None, epics: list) -> str:
    """
    Return the prefix string to prepend to a ticket title, e.g. "[MA][BE3] ".
    Includes the epic abbreviation (if found) followed by the assignee tag (if found).
    Returns an empty string if neither is found.
    """
    parts = []

    # Epic abbreviation — look up the epic's summary and match against EPIC_ABBREVIATIONS
    if epic_key and EPIC_ABBREVIATIONS:
        for epic in epics:
            if epic["key"] == epic_key:
                summary_lower = epic["summary"].lower()
                for keyword, abbrev in EPIC_ABBREVIATIONS.items():
                    if keyword.lower() in summary_lower:
                        parts.append(f"[{abbrev}]")
                        break
                break

    # Assignee tag — case-insensitive substring match on display name
    if assignee_name:
        name_lower = assignee_name.lower()
        for full_name, tag in ASSIGNEE_TAGS.items():
            if full_name.lower() in name_lower or name_lower in full_name.lower():
                parts.append(tag)
                break

    return "".join(parts) + (" " if parts else "")

# Tracks when each ticket was last reminded — persisted so bot restarts don't
# cause duplicate pings.
_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
os.makedirs(_DATA_DIR, exist_ok=True)

REMINDER_STATE_FILE = os.path.join(_DATA_DIR, ".reminder_state.json")

# ── Persistent memory database ─────────────────────────────────────────────────

MEMORY_DB   = os.path.join(_DATA_DIR, "memory.db")
MAX_MESSAGES = 10_000   # prune oldest records beyond this cap


def init_memory_db() -> None:
    """Create the messages table if it doesn't already exist."""
    with sqlite3.connect(MEMORY_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id   INTEGER NOT NULL,
                channel_name TEXT,
                author_name  TEXT,
                content      TEXT,
                created_at   TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_created ON messages(created_at)")
        conn.commit()


def store_message_in_memory(
    channel_id: int,
    channel_name: str,
    author_name: str,
    content: str,
    created_at: str,
) -> None:
    """Persist one message and prune the oldest rows beyond MAX_MESSAGES."""
    with sqlite3.connect(MEMORY_DB) as conn:
        conn.execute(
            "INSERT INTO messages (channel_id, channel_name, author_name, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (channel_id, channel_name, author_name, content, created_at),
        )
        conn.execute(f"""
            DELETE FROM messages WHERE id NOT IN (
                SELECT id FROM messages ORDER BY id DESC LIMIT {MAX_MESSAGES}
            )
        """)
        conn.commit()


def get_cross_channel_history(limit: int = 40, exclude_channel_id: int | None = None) -> list:
    """
    Return the most recent messages from across the server, excluding the
    current channel (whose history is already fetched live from Discord).
    Each entry: {"channel": str, "author": str, "content": str}
    """
    with sqlite3.connect(MEMORY_DB) as conn:
        conn.row_factory = sqlite3.Row
        if exclude_channel_id is not None:
            rows = conn.execute(
                "SELECT channel_name, author_name, content FROM messages "
                "WHERE channel_id != ? ORDER BY id DESC LIMIT ?",
                (exclude_channel_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT channel_name, author_name, content FROM messages "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [
        {"channel": r["channel_name"], "author": r["author_name"], "content": r["content"]}
        for r in reversed(rows)
    ]

# ── Set up API clients ─────────────────────────────────────────────────────────

# Anthropic async client — must be async to avoid blocking the Discord event loop
claude_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


async def _call_claude(make_request, max_retries: int = 3):
    """
    Call the Claude API with exponential backoff on transient errors.

    Retries on: rate limits, network errors, timeouts, and 5xx server errors.
    Raises immediately on: auth errors, bad requests, and other non-retryable failures.
    """
    for attempt in range(max_retries):
        try:
            return await make_request()
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt  # 1s → 2s → 4s
            print(f"[Nico Jr.] Claude transient error ({type(e).__name__}), retrying in {wait}s… (attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code >= 500 and attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"[Nico Jr.] Claude server error {e.status_code}, retrying in {wait}s… (attempt {attempt + 1}/{max_retries})")
                await asyncio.sleep(wait)
            else:
                raise

# Jira uses HTTP Basic Auth: base64-encoded "email:api_token"
_jira_auth = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
JIRA_HEADERS = {
    "Authorization": f"Basic {_jira_auth}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# ── Jira response cache (5-minute TTL) ────────────────────────────────────────
# Avoids hammering Jira on every message; cache is refreshed automatically.
_jira_cache: dict[str, tuple[datetime, list]] = {}
JIRA_CACHE_TTL = timedelta(minutes=5)


def _cached_jira(key: str, fetcher) -> list:
    entry = _jira_cache.get(key)
    if entry:
        fetched_at, data = entry
        if datetime.now(timezone.utc) - fetched_at < JIRA_CACHE_TTL:
            return data
    data = fetcher()
    _jira_cache[key] = (datetime.now(timezone.utc), data)
    return data

# ── Jira: Fetch all Epics in the project ──────────────────────────────────────

def get_jira_epics() -> list:
    """
    Return all open Epics in the project as a list of dicts:
      [{
        "key":      "TA-5",
        "summary":  "Mobile App Redesign",
        "status":   "In Progress",
        "assignee": "Ana Reyes",
        "url":      "https://yourcompany.atlassian.net/browse/TA-5"
      }, ...]

    Used to give Claude context so it can pick the best matching Epic
    when creating a new ticket. Returns an empty list on failure so the
    rest of the flow can continue without epics.
    """
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    jql = f'project = "{JIRA_PROJECT_KEY}" AND issuetype = Epic AND statusCategory != Done ORDER BY created DESC'

    response = requests.post(
        url,
        headers=JIRA_HEADERS,
        json={"jql": jql, "fields": ["summary", "status", "assignee"], "maxResults": 50},
        timeout=10,
    )

    if response.status_code != 200:
        print(f"[Nico Jr.] Warning: could not fetch Epics ({response.status_code}): {response.text}")
        return []

    epics = []
    for issue in response.json().get("issues", []):
        fields   = issue["fields"]
        assignee = fields.get("assignee")
        epics.append({
            "key":      issue["key"],
            "summary":  fields["summary"],
            "status":   fields.get("status", {}).get("name", "Unknown"),
            "assignee": assignee["displayName"] if assignee else "Unassigned",
            "url":      f"{JIRA_BASE_URL}/browse/{issue['key']}",
        })
    return epics


# ── Jira: Fetch Parent tickets in the project ─────────────────────────────────

def get_jira_parents() -> list:
    """
    Return open Stories, Tasks, and Bugs that can act as parent issues
    (i.e. issues a sub-task can be linked under via the 'parent' field):
      [{
        "key":        "TA-12",
        "summary":    "User auth flow",
        "status":     "In Progress",
        "assignee":   "Ana Reyes",
        "issue_type": "Story",
        "url":        "https://yourcompany.atlassian.net/browse/TA-12"
      }, ...]

    Returns an empty list on failure so the rest of the flow continues.
    """
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    jql = (
        f'project = "{JIRA_PROJECT_KEY}" '
        f'AND issuetype in (Story, Task, Bug) '
        f'AND statusCategory != Done '
        f'ORDER BY updated DESC'
    )

    response = requests.post(
        url,
        headers=JIRA_HEADERS,
        json={"jql": jql, "fields": ["summary", "status", "assignee", "issuetype"], "maxResults": 50},
        timeout=10,
    )

    if response.status_code != 200:
        print(f"[Nico Jr.] Warning: could not fetch parent tickets ({response.status_code}): {response.text}")
        return []

    parents = []
    for issue in response.json().get("issues", []):
        fields   = issue["fields"]
        assignee = fields.get("assignee")
        parents.append({
            "key":        issue["key"],
            "summary":    fields["summary"],
            "status":     fields.get("status", {}).get("name", "Unknown"),
            "assignee":   assignee["displayName"] if assignee else "Unassigned",
            "issue_type": fields.get("issuetype", {}).get("name", ""),
            "url":        f"{JIRA_BASE_URL}/browse/{issue['key']}",
        })
    return parents


# ── Jira: Fetch assignable users for the project ──────────────────────────────

def get_jira_assignees() -> list:
    """
    Return all users who can be assigned to issues in the project:
      [{"name": "Ana Reyes", "email": "ana@company.com", "account_id": "..."}, ...]

    Used so Nico Jr. can answer "who can I assign this to?" and so Claude
    can validate assignee names when creating or updating tickets.
    Returns an empty list on failure so the rest of the flow continues.
    """
    url = f"{JIRA_BASE_URL}/rest/api/3/user/assignable/search"
    response = requests.get(
        url,
        headers=JIRA_HEADERS,
        params={"project": JIRA_PROJECT_KEY, "maxResults": 50},
        timeout=10,
    )

    if response.status_code != 200:
        print(f"[Nico Jr.] Warning: could not fetch assignees ({response.status_code}): {response.text}")
        return []

    return [
        {
            "name":       user.get("displayName", ""),
            "email":      user.get("emailAddress", ""),
            "account_id": user.get("accountId", ""),
        }
        for user in response.json()
        if user.get("active", True)  # only include active users
    ]


# ── Claude: Understand the message ────────────────────────────────────────────

async def analyze_message(message_text: str, epics: list, attachments: list, history: list, assignees: list, parents: list, server_history: list, quoted_text: str = "", drive_count: int = 0) -> dict:
    """
    Ask Claude what the user wants to do. Returns one of three shapes:

    Create:  {"action": "create", "title": ..., "description": ...,
               "priority": ..., "assignee": ..., "issue_type": ...,
               "epic_key": "PROJ-5" or null, "parent_key": "PROJ-12" or null}

    Update:  {"action": "update", "ticket_key": "PROJ-123",
               "fields": {<only the fields the user mentioned>}}

    Ignore:  {"action": "none"}

    epics          — list of {"key", "summary"} dicts fetched from Jira
    attachments    — list of Discord attachment URLs (screenshots, recordings, etc.)
    history        — list of {"author", "content"} dicts from recent channel messages
    assignees      — list of {"name", "email"} dicts of valid Jira assignees
    parents        — list of {"key", "summary", "issue_type"} dicts of parent-capable issues
    server_history — list of {"channel", "author", "content"} dicts from other channels
    """

    # List valid assignees so Claude uses real names when setting the assignee field
    if assignees:
        assignees_text = "\n".join(
            f'  - {a["name"]}' + (f' ({a["email"]})' if a["email"] else "")
            for a in assignees
        )
        assignees_section = f"\nAssignable team members in this Jira project:\n{assignees_text}\n"
    else:
        assignees_section = ""

    # Format epics for the prompt so Claude can choose the best match
    if epics:
        epics_text = "\n".join(
            f'  - {e["key"]}: {e["summary"]} [{e["status"]}] — assigned to {e["assignee"]}'
            for e in epics
        )
        epics_section = f"""
Available Epics in this Jira project (pick the most relevant one, or null if none fit):
{epics_text}
"""
    else:
        epics_section = "\nThere are no open Epics in this project.\n"

    # Format parent tickets so Claude can suggest linking sub-tasks to a parent
    if parents:
        parents_text = "\n".join(
            f'  - {p["key"]} [{p["issue_type"]}]: {p["summary"]} [{p["status"]}] — {p["assignee"]}'
            for p in parents
        )
        parents_section = f"""
Open parent tickets in this project (Stories, Tasks, Bugs). Use parent_key when the user is \
creating a sub-task or explicitly wants to nest a ticket under one of these:
{parents_text}
"""
    else:
        parents_section = "\nThere are no open parent tickets in this project.\n"

    # Format recent channel history so Claude has full context
    if history:
        history_lines = "\n".join(f'  {m["author"]}: {m["content"]}' for m in history)
        history_section = f"""
Recent conversation in this channel (for context — use this to fill in missing details):
{history_lines}
"""
    else:
        history_section = ""

    # Cross-channel server memory — conversations from other channels
    if server_history:
        server_lines = "\n".join(
            f'  #{m["channel"]} | {m["author"]}: {m["content"]}' for m in server_history
        )
        server_history_section = f"""
Recent conversations across other channels (server memory — use for broader context and continuity):
{server_lines}
"""
    else:
        server_history_section = ""

    # Tell Claude about attachments so it can reference them naturally,
    # but don't expose the URLs — the files will be uploaded directly to Jira.
    attachment_parts = []
    if attachments:
        attachment_parts.append(f"{len(attachments)} Discord file(s) (screenshot(s) / recording(s))")
    if drive_count:
        attachment_parts.append(f"{drive_count} Google Drive file(s)")
    if attachment_parts:
        attachments_section = (
            f"\nThe user provided the following attachments: {', '.join(attachment_parts)}. "
            f"These will be uploaded directly to the Jira ticket. "
            f"Reference them naturally in the description (e.g. 'See attached screenshot', 'See attached Drive file') "
            f"but do NOT include any URLs.\n"
        )
    else:
        attachments_section = ""

    # Pre-check: if the message contains a ticket key pattern (e.g. PROJ-123),
    # it is almost certainly an update — tell Claude this explicitly so it doesn't
    # get confused by create-heavy conversation history.
    ticket_key_match = re.search(r'\b([A-Z][A-Z0-9]+-\d+)\b', message_text)
    if ticket_key_match:
        intent_hint = (
            f"\nCRITICAL: The message contains an existing ticket key "
            f"({ticket_key_match.group(1)}). This is an UPDATE request. "
            f"Do NOT create a new ticket.\n"
        )
    else:
        intent_hint = ""

    if quoted_text:
        quoted_section = f"""
━━━ FORWARDED / QUOTED CONTENT ━━━
You have FULL ACCESS to this content — it was extracted directly from the forwarded or quoted message.
Do NOT say you cannot read it. Read it and act on it.
If the current message below is empty, the user forwarded this content with no extra instruction —
decide what action to take based on the forwarded content alone (e.g. if it describes a bug or task, you may offer to CREATE a ticket via CHAT, but never auto-create without an explicit instruction).

{quoted_text}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    else:
        quoted_section = ""

    prompt = f"""You are Nico Jr., a team assistant embedded in a Discord server. You manage Jira tickets and chat with the team. You can see all messages — not just ones that tag you.
{intent_hint}{quoted_section}
━━━ CURRENT MESSAGE (the ONLY message you are deciding on) ━━━
\"\"\"{message_text}\"\"\"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{history_section}{server_history_section}{assignees_section}{epics_section}{parents_section}{attachments_section}
Decide which of these five actions to take:

1. CREATE  — the CURRENT MESSAGE itself explicitly asks to file / log / create a new ticket.
2. UPDATE  — the CURRENT MESSAGE references an existing ticket key (e.g. PROJ-123) and asks to change something.
3. CHAT    — the CURRENT MESSAGE is directed at you, asks you a question, or you can add clear value.
4. TIMER   — the CURRENT MESSAGE asks to set a timer or be pinged after a duration (e.g. "set a timer for 5 minutes", "remind me in 30 seconds", "ping me in 2 hours").
5. IGNORE  — anything else.

━━━ STRICT RULES FOR UPDATE ━━━
- You may ONLY choose UPDATE if the CURRENT MESSAGE itself contains an explicit ticket key (e.g. TA-6793).
- NEVER infer or guess the ticket key from conversation history, server memory, or prior messages.
- If the user wants to change a ticket but has not written the key in the CURRENT MESSAGE, choose CHAT and ask them which ticket they mean.

━━━ STRICT RULES FOR CREATE ━━━
- You may ONLY choose CREATE if the CURRENT MESSAGE contains an explicit filing instruction.
  Accepted trigger phrases (in any language): "file", "log", "create", "open a ticket",
  "add a ticket", "pafile", "i-ticket", "gawa ng ticket", "ticket mo", "i-log", "ilagay sa jira",
  "report this", or a clear equivalent.
- Conversation history, server memory, or prior messages DO NOT trigger CREATE — ever.
  If a filing instruction appeared in a previous message, it was already handled. Ignore it.
- Reactions, short replies, or follow-up chatter after a ticket was created ("waw", "ok",
  "thanks", "attentive", "nice", single words/emoji) are NEVER create — choose IGNORE.
- When in doubt between CREATE and anything else, choose IGNORE.

━━━ STRICT RULES FOR TIMER ━━━
- Choose TIMER only when the CURRENT MESSAGE explicitly requests a countdown timer or a timed ping.
- Extract the exact duration in seconds (e.g. "5 minutes" → 300, "1 hour 30 minutes" → 5400, "90 seconds" → 90).
- Extract an optional short label describing what the timer is for (e.g. "lunch", "standup", "break").

━━━ STRICT RULES FOR CHAT ━━━
- Only respond if the CURRENT MESSAGE is clearly addressed to you or asks something you can answer.
- Default to IGNORE when unsure.

━━━ GENERAL RULES ━━━
- If a ticket key is in the CURRENT MESSAGE → always UPDATE, never CREATE.
- Default priority → "Medium". Default issue_type → "Task".
- Use "Bug" for broken things, "Story" for features, "Epic" for large initiatives, "Task" otherwise.
- Set parent_key only when explicitly requested.
- For updates, include only fields the current message mentions.

━━━ REACTION RULES ━━━
Every response may include an optional "reaction" field — a single Unicode emoji to react to the message with.
React naturally, like a coworker would. Examples:
  😂 or 🤣 — genuinely funny joke or meme
  😢 — something sad or unfortunate
  😠 — something annoying or offensive
  👍 — agreement, good idea, or approval
  ❤️ — something wholesome or appreciated
  🎉 — good news, celebration, achievement
  😮 — surprising or unexpected
  🤔 — interesting or thought-provoking
  👀 — something worth watching or suspicious
  ✅ — confirmed, done, understood
Only react when it genuinely fits — don't react to every message. Use null if nothing feels right.

--- If CREATE ---
Reply with ONLY:
{{
  "action": "create",
  "title": "Short, clear ticket title (max 100 characters)",
  "description": "Full description. Reference attachments naturally (e.g. 'See attached screenshot') — no URLs.",
  "priority": "Highest | High | Medium | Low | Lowest",
  "assignee": "name or email, or null",
  "issue_type": "Bug | Task | Story | Epic",
  "epic_key": "matching Epic key or null",
  "parent_key": "parent ticket key or null",
  "reaction": "single emoji or null"
}}

--- If UPDATE ---
Reply with ONLY:
{{
  "action": "update",
  "ticket_key": "PROJ-123",
  "fields": {{
    "title": "new title if mentioned",
    "description": "new description if mentioned — no URLs",
    "priority": "new priority if mentioned",
    "assignee": "new assignee if mentioned",
    "issue_type": "new type if mentioned",
    "epic_key": "new epic key if mentioned",
    "parent_key": "new parent key if mentioned"
  }},
  "reaction": "single emoji or null"
}}

--- If TIMER ---
Reply with ONLY:
{{
  "action": "timer",
  "duration_seconds": 300,
  "label": "short label or null",
  "reaction": "single emoji or null"
}}

--- If CHAT ---
Reply with ONLY:
{{
  "action": "chat",
  "reaction": "single emoji or null"
}}

--- If IGNORE ---
Reply with ONLY:
{{
  "action": "ignore",
  "reaction": "single emoji or null"
}}

Respond with JSON only — no explanation, no markdown fences."""

    response = await _call_claude(lambda: claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    ))

    # Strip any accidental markdown fences before parsing
    raw = response.content[0].text.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw).strip()
    return json.loads(raw)


# ── Claude: Conversational reply ──────────────────────────────────────────────

async def chat_with_nico_jr(message_text: str, author_name: str, history: list, epics: list, assignees: list, parents: list, server_history: list, quoted_text: str = "") -> str:
    """
    Generate a conversational reply from Nico Jr. when the user isn't asking
    to create or update a ticket. Nico Jr. responds like a helpful, friendly
    coworker who happens to also manage Jira tickets.
    """

    # Give Nico Jr. awareness of the team's active epics so he can reference them
    if epics:
        epics_text = "\n".join(
            f"  - {e['key']}: {e['summary']} [{e['status']}] — {e['assignee']} — {e['url']}"
            for e in epics
        )
        epics_context = f"\nActive Epics in the team's Jira project:\n{epics_text}\n"
    else:
        epics_context = ""

    # Give Nico Jr. awareness of open parent tickets (Stories, Tasks, Bugs)
    if parents:
        parents_text = "\n".join(
            f"  - {p['key']} [{p['issue_type']}]: {p['summary']} [{p['status']}] — {p['assignee']} — {p['url']}"
            for p in parents
        )
        parents_context = f"\nOpen parent tickets (Stories, Tasks, Bugs) in the team's Jira project:\n{parents_text}\n"
    else:
        parents_context = ""

    # Give Nico Jr. the full list of assignable team members
    if assignees:
        assignees_text = "\n".join(
            f'  - {a["name"]}' + (f' ({a["email"]})' if a["email"] else "")
            for a in assignees
        )
        assignees_context = f"\nAssignable team members in Jira:\n{assignees_text}\n"
    else:
        assignees_context = ""

    # Format the conversation history so Nico Jr. has full context
    if history:
        history_text = "\n".join(f"  {m['author']}: {m['content']}" for m in history)
        history_context = f"\nRecent conversation in this channel:\n{history_text}\n"
    else:
        history_context = ""

    # Cross-channel memory
    if server_history:
        server_lines = "\n".join(
            f"  #{m['channel']} | {m['author']}: {m['content']}" for m in server_history
        )
        server_history_context = f"\nRecent conversations across other channels (your server memory):\n{server_lines}\n"
    else:
        server_history_context = ""

    if quoted_text:
        quoted_block = f"""
━━━ FORWARDED / QUOTED CONTENT (you have FULL ACCESS — read it and respond based on it) ━━━
{quoted_text}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IMPORTANT: You CAN read the content above. Do NOT say you cannot read embedded or forwarded messages.
The content has been extracted and provided to you directly. Respond to it naturally.
"""
    else:
        quoted_block = ""

    prompt = f"""You are Nico Jr., a helpful and friendly coworker embedded in a team's Discord channel.
You help the team manage their Jira project, but you're also just a good coworker to chat with.

Your personality:
- Warm, direct, and professional — like a senior teammate, not a customer support bot
- You know the team's work and can reference their active epics, parent tickets, and team members naturally
- You keep replies concise — this is a chat, not an essay
- You can have opinions, crack the occasional light joke, and push back if something seems off
- You never say things like "As an AI..." or "I'm just a bot" — you're Nico Jr., a coworker
- If someone asks you to do something outside your abilities, be honest but keep it casual
- You can remind people you can create and update Jira tickets if it seems relevant
- You CAN read forwarded messages and embedded content — it is extracted and provided to you directly
{epics_context}{parents_context}{assignees_context}{history_context}{server_history_context}{quoted_block}
{author_name} just said: \"\"\"{message_text}\"\"\"

Reply naturally as Nico Jr.. Keep it short unless a detailed answer is genuinely needed."""

    response = await _call_claude(lambda: claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    ))

    return response.content[0].text.strip()


# ── Jira: Look up a user ───────────────────────────────────────────────────────

def find_jira_user(name_or_email: str) -> str | None:
    """
    Search Jira Cloud for a user by display name or email.
    Returns the accountId string if found, otherwise None.

    Jira requires an accountId (not a name) when setting the assignee field.
    """
    if not name_or_email:
        return None

    url = f"{JIRA_BASE_URL}/rest/api/3/user/search"
    response = requests.get(
        url,
        headers=JIRA_HEADERS,
        params={"query": name_or_email},
        timeout=10,
    )

    if response.status_code == 200:
        users = response.json()
        if users:
            # Use the first result — Jira returns the best match first
            return users[0]["accountId"]

    return None


# ── Discord embed extraction ───────────────────────────────────────────────────

def _embeds_to_text(embeds: list) -> str:
    """
    Convert a list of Discord Embed objects to a plain-text summary for Claude.
    Captures all readable attributes: provider, author, title, url, description, fields, images.
    """
    parts = []
    for embed in embeds:
        lines = []
        embed_type = getattr(embed, "type", "rich") or "rich"

        # Source website (e.g. "GitHub", "Twitter")
        provider = getattr(embed, "provider", None)
        if provider and getattr(provider, "name", None):
            lines.append(f"Source: {provider.name}")

        # Author line (common in bot embeds and rich link previews)
        author = getattr(embed, "author", None)
        if author and getattr(author, "name", None):
            lines.append(f"Author: {author.name}")

        if embed.title:
            lines.append(f"Title: {embed.title}")
        if embed.url:
            lines.append(f"URL: {embed.url}")
        if embed.description:
            lines.append(f"Description: {embed.description}")
        for field in embed.fields:
            lines.append(f"{field.name}: {field.value}")

        # For image/gif/video embeds that carry no text, at least log the URL
        if not lines:
            if embed_type in ("image", "gifv"):
                img = getattr(embed, "image", None) or getattr(embed, "thumbnail", None)
                url = (getattr(img, "url", None) if img else None) or embed.url
                if url:
                    lines.append(f"[Image: {url}]")
            elif embed_type == "video":
                if embed.url:
                    lines.append(f"[Video: {embed.url}]")

        if lines:
            parts.append("\n".join(lines))

    return "\n\n".join(parts)


# ── Google Drive: Detect and download publicly shared files ───────────────────

_DRIVE_URL_RE = re.compile(
    r'https?://(?:drive|docs)\.google\.com/[^\s<>"\']+',
    re.IGNORECASE,
)

def _extract_drive_urls(text: str) -> list:
    """
    Find all Google Drive / Docs / Sheets / Slides share URLs in text.
    Returns a list of dicts: {share_url, download_url, filename_hint}.
    """
    results = []
    seen = set()
    for share_url in _DRIVE_URL_RE.findall(text):
        m = re.search(
            r'/(?:file/d|document/d|spreadsheets/d|presentation/d)/([a-zA-Z0-9_-]+)',
            share_url,
        )
        if not m:
            m = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', share_url)
        if not m:
            continue
        file_id = m.group(1)
        if file_id in seen:
            continue
        seen.add(file_id)

        if 'docs.google.com/document' in share_url:
            download_url   = f"https://docs.google.com/document/d/{file_id}/export?format=pdf"
            filename_hint  = f"document_{file_id}.pdf"
        elif 'docs.google.com/spreadsheets' in share_url:
            download_url   = f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=xlsx"
            filename_hint  = f"spreadsheet_{file_id}.xlsx"
        elif 'docs.google.com/presentation' in share_url:
            download_url   = f"https://docs.google.com/presentation/d/{file_id}/export?format=pdf"
            filename_hint  = f"slides_{file_id}.pdf"
        else:
            download_url   = f"https://drive.google.com/uc?export=download&id={file_id}"
            filename_hint  = f"drive_file_{file_id}"

        results.append({"share_url": share_url, "download_url": download_url, "filename_hint": filename_hint})
    return results


def _download_drive_file(download_url: str, filename_hint: str) -> tuple:
    """
    Download a publicly shared Google Drive file.
    Handles the large-file virus-scan confirmation page Google sometimes shows.
    Returns (content_bytes, filename).
    """
    session = requests.Session()
    resp = session.get(download_url, timeout=60, allow_redirects=True)
    resp.raise_for_status()

    # Google shows an HTML confirmation page for large files
    if "text/html" in resp.headers.get("Content-Type", ""):
        confirm = re.search(r'confirm=([0-9A-Za-z_]+)', resp.text)
        if not confirm:
            raise ValueError(
                "Google Drive returned a confirmation page with no token — "
                "the file may be private or require sign-in."
            )
        sep = "&" if "?" in download_url else "?"
        resp = session.get(
            f"{download_url}{sep}confirm={confirm.group(1)}",
            timeout=60,
            allow_redirects=True,
        )
        resp.raise_for_status()

    # Prefer the real filename from Content-Disposition
    cd = resp.headers.get("Content-Disposition", "")
    fn = re.search(r"filename\*?=['\"]?(?:UTF-\d+'[^']*')?([^;\r\n\"']+)", cd)
    filename = fn.group(1).strip() if fn else filename_hint

    return resp.content, filename


def upload_drive_attachments(ticket_key: str, drive_files: list) -> list:
    """
    Download each Google Drive file and upload it to the Jira ticket.
    Returns a list of successfully uploaded filenames.
    """
    if not drive_files:
        return []

    upload_url = f"{JIRA_BASE_URL}/rest/api/3/issue/{ticket_key}/attachments"
    upload_headers = {
        "Authorization": JIRA_HEADERS["Authorization"],
        "X-Atlassian-Token": "no-check",
    }

    uploaded = []
    for info in drive_files:
        try:
            content, filename = _download_drive_file(info["download_url"], info["filename_hint"])
            content_type = "application/octet-stream"
            upload_resp = requests.post(
                upload_url,
                headers=upload_headers,
                files={"file": (filename, content, content_type)},
                timeout=60,
            )
            if upload_resp.status_code == 200:
                uploaded.append(filename)
                print(f"[Nico Jr.] Uploaded Drive file '{filename}' to {ticket_key}")
            else:
                print(f"[Nico Jr.] Warning: could not upload Drive file '{filename}' ({upload_resp.status_code})")
        except Exception as e:
            print(f"[Nico Jr.] Warning: failed to process Drive file {info['share_url']}: {e}")

    return uploaded


# ── Jira: Upload attachments to a ticket ──────────────────────────────────────

def upload_jira_attachments(ticket_key: str, attachment_urls: list) -> list:
    """
    Download each Discord attachment and upload it to the Jira ticket.

    Jira requires the special 'X-Atlassian-Token: no-check' header to bypass
    CSRF protection on the attachment endpoint.

    Returns a list of filenames that were successfully uploaded.
    """
    if not attachment_urls:
        return []

    upload_url = f"{JIRA_BASE_URL}/rest/api/3/issue/{ticket_key}/attachments"
    upload_headers = {
        "Authorization": JIRA_HEADERS["Authorization"],
        "X-Atlassian-Token": "no-check",
    }

    uploaded = []
    for url in attachment_urls:
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()

            # Strip query-string tokens Discord appends to CDN URLs
            filename = url.split("?")[0].split("/")[-1] or "attachment"
            content_type = resp.headers.get("Content-Type", "application/octet-stream")

            upload_resp = requests.post(
                upload_url,
                headers=upload_headers,
                files={"file": (filename, resp.content, content_type)},
                timeout=30,
            )

            if upload_resp.status_code == 200:
                uploaded.append(filename)
                print(f"[Nico Jr.] Uploaded '{filename}' to {ticket_key}")
            else:
                print(f"[Nico Jr.] Warning: could not upload '{filename}' ({upload_resp.status_code}): {upload_resp.text}")

        except Exception as e:
            print(f"[Nico Jr.] Warning: failed to process attachment {url}: {e}")

    return uploaded


# ── Jira: Embed images in the ticket description ──────────────────────────────

def embed_images_in_description(ticket_key: str, image_urls: list) -> None:
    """
    Fetch the current ADF description for a ticket, append a mediaSingle node
    for each image URL so the images render inline in the description body,
    then write it back.

    Uses ADF media type "external" which renders an image from a public URL
    directly in the Jira ticket view — no Atlassian Media API upload needed.
    """
    if not image_urls:
        return

    # Fetch current description (ADF JSON)
    resp = requests.get(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{ticket_key}",
        headers=JIRA_HEADERS,
        params={"fields": "description"},
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"[Nico Jr.] Could not fetch description for {ticket_key} ({resp.status_code})")
        return

    existing = resp.json().get("fields", {}).get("description")

    # Build one mediaSingle node per image
    image_nodes = [
        {
            "type": "mediaSingle",
            "attrs": {"layout": "center"},
            "content": [
                {
                    "type": "media",
                    "attrs": {"type": "external", "url": url},
                }
            ],
        }
        for url in image_urls
    ]

    if existing and existing.get("type") == "doc":
        existing["content"].extend(image_nodes)
        updated = existing
    else:
        updated = {"type": "doc", "version": 1, "content": image_nodes}

    patch = requests.put(
        f"{JIRA_BASE_URL}/rest/api/3/issue/{ticket_key}",
        headers=JIRA_HEADERS,
        json={"fields": {"description": updated}},
        timeout=10,
    )
    if patch.status_code == 204:
        print(f"[Nico Jr.] Embedded {len(image_urls)} image(s) in {ticket_key} description")
    else:
        print(f"[Nico Jr.] Could not embed images in description ({patch.status_code}): {patch.text}")


# ── Jira: Build an ADF description block ──────────────────────────────────────

def build_adf_description(text: str) -> dict:
    """
    Wrap a plain-text description in Atlassian Document Format (ADF),
    which is what Jira Cloud requires for the description field.
    """
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


# ── Jira: Set the Epic link on a fields dict ───────────────────────────────────

def apply_epic_to_fields(jira_fields: dict, epic_key: str) -> None:
    """
    Add the Epic link to a Jira fields dict in-place.

    classic projects  → customfield_10014 (Epic Link) expects the key as a string
    next-gen projects → parent expects {"key": epic_key}

    JIRA_EPIC_LINK_FIELD controls which strategy is used (default: customfield_10014).
    """
    if JIRA_EPIC_LINK_FIELD == "parent":
        jira_fields["parent"] = {"key": epic_key}
    else:
        # customfield_10014 is the Epic Link field on classic / company-managed projects
        jira_fields[JIRA_EPIC_LINK_FIELD] = epic_key


# ── Jira: Create a ticket ──────────────────────────────────────────────────────

def create_jira_ticket(
    title: str,
    description: str,
    priority: str,
    assignee_name: str | None,
    issue_type: str,
    epic_key: str | None,
    parent_key: str | None = None,
) -> dict:
    """
    Create a Jira issue via the REST API v3.

    Returns {"key": "PROJ-123", "url": "https://..."} on success.
    Raises an Exception with details on failure.
    """

    fields = {
        "project":     {"key": JIRA_PROJECT_KEY},
        "summary":     title,
        "description": build_adf_description(description),
        "issuetype":   {"name": issue_type},
        "priority":    {"name": priority},
    }

    # Only set assignee if we can resolve the user to a Jira accountId
    if assignee_name:
        account_id = find_jira_user(assignee_name)
        if account_id:
            fields["assignee"] = {"accountId": account_id}
        else:
            print(f"[Nico Jr.] Warning: could not find Jira user '{assignee_name}' — ticket will be unassigned.")

    # parent_key takes precedence over epic_key when both are provided —
    # they both use the 'parent' field in next-gen projects, and a ticket
    # can only have one parent.
    if parent_key:
        fields["parent"] = {"key": parent_key}
        print(f"[Nico Jr.] Linking ticket under parent {parent_key}")
    elif epic_key:
        apply_epic_to_fields(fields, epic_key)
        print(f"[Nico Jr.] Linking ticket to Epic {epic_key}")

    url = f"{JIRA_BASE_URL}/rest/api/3/issue"
    response = requests.post(url, headers=JIRA_HEADERS, json={"fields": fields}, timeout=10)

    if response.status_code == 201:
        key = response.json()["key"]
        return {
            "key": key,
            "url": f"{JIRA_BASE_URL}/browse/{key}",
        }

    # If Jira rejects the parent due to a hierarchy mismatch, retry without it.
    # This happens when Claude picks a parent ticket that is at the wrong level
    # for the issue type being created (e.g. a sub-task as parent of a Bug).
    if response.status_code == 400:
        body = response.json()
        if "parentId" in body.get("errors", {}):
            print(f"[Nico Jr.] Parent {parent_key} rejected (hierarchy mismatch) — retrying without parent.")
            fields.pop("parent", None)
            retry = requests.post(url, headers=JIRA_HEADERS, json={"fields": fields}, timeout=10)
            if retry.status_code == 201:
                key = retry.json()["key"]
                return {
                    "key": key,
                    "url": f"{JIRA_BASE_URL}/browse/{key}",
                    "parent_dropped": True,  # caller can surface this to the user
                }

    # Surface Jira's own error message to help with debugging
    raise Exception(f"Jira API returned {response.status_code}: {response.text}")


# ── Jira: Update an existing ticket ───────────────────────────────────────────

def update_jira_ticket(ticket_key: str, fields: dict) -> dict:
    """
    Update specific fields on an existing Jira issue.

    Only the fields present in the `fields` dict are changed — everything
    else on the ticket is left untouched.

    Returns {"key": "PROJ-123", "url": "https://..."} on success.
    Raises an Exception with details on failure.
    """

    jira_fields = {}

    if "title" in fields:
        jira_fields["summary"] = fields["title"]

    if "description" in fields:
        jira_fields["description"] = build_adf_description(fields["description"])

    if "priority" in fields:
        jira_fields["priority"] = {"name": fields["priority"]}

    if "issue_type" in fields:
        jira_fields["issuetype"] = {"name": fields["issue_type"]}

    if "assignee" in fields:
        account_id = find_jira_user(fields["assignee"])
        if account_id:
            jira_fields["assignee"] = {"accountId": account_id}
        else:
            print(f"[Nico Jr.] Warning: could not find Jira user '{fields['assignee']}' — assignee not updated.")

    if "parent_key" in fields and fields["parent_key"]:
        jira_fields["parent"] = {"key": fields["parent_key"]}
        print(f"[Nico Jr.] Updating parent to {fields['parent_key']}")
    elif "epic_key" in fields and fields["epic_key"]:
        apply_epic_to_fields(jira_fields, fields["epic_key"])
        print(f"[Nico Jr.] Updating Epic link to {fields['epic_key']}")

    if not jira_fields:
        raise Exception("No valid fields to update were found in the request.")

    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{ticket_key}"
    response = requests.put(url, headers=JIRA_HEADERS, json={"fields": jira_fields}, timeout=10)

    # Jira returns 204 No Content on a successful update
    if response.status_code == 204:
        return {
            "key": ticket_key,
            "url": f"{JIRA_BASE_URL}/browse/{ticket_key}",
        }

    raise Exception(f"Jira API returned {response.status_code}: {response.text}")


# ── Reminder state helpers ─────────────────────────────────────────────────────

def load_reminder_state() -> dict:
    """Load the persisted reminder state (ticket key → last_reminded ISO string)."""
    try:
        with open(REMINDER_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_reminder_state(state: dict) -> None:
    with open(REMINDER_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Jira: Fetch stale bug tickets ─────────────────────────────────────────────

def get_stale_bug_tickets() -> list:
    """
    Return all open Bug tickets that are not yet in a terminal status for
    their priority level. Each dict contains:
      key, summary, status, priority, assignee, created_at (datetime), url
    """
    # Exclude both terminal statuses up-front so we don't process resolved or
    # dropped tickets. Per-priority filtering then runs in Python below.
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    jql = (
        f'project = "{JIRA_PROJECT_KEY}" '
        f'AND issuetype = Bug '
        f'AND status NOT IN ("In Prod and Working as Expected", "Dropped") '
        f'ORDER BY created ASC'
    )
    resp = requests.post(
        url,
        headers=JIRA_HEADERS,
        json={
            "jql": jql,
            "fields": ["summary", "status", "assignee", "priority", "created"],
            "maxResults": 100,
        },
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"[Nico Jr.] Warning: could not fetch bug tickets ({resp.status_code}): {resp.text}")
        return []

    bugs = []
    for issue in resp.json().get("issues", []):
        fields   = issue["fields"]
        priority = fields.get("priority", {}).get("name", "Medium")
        status   = fields.get("status", {}).get("name", "Unknown")

        # Skip if this ticket's status is terminal for its own priority level
        terminal = REMINDER_TERMINAL_STATUSES.get(priority, {"Working as Expected"})
        if status in terminal:
            continue

        assignee    = fields.get("assignee")
        created_str = fields.get("created", "")
        try:
            # Jira timestamps: "2026-03-01T10:30:00.000+0000"
            created_at = datetime.fromisoformat(created_str.replace("+0000", "+00:00"))
        except Exception:
            continue

        bugs.append({
            "key":        issue["key"],
            "summary":    fields["summary"],
            "status":     status,
            "priority":   priority,
            "assignee":   assignee["displayName"] if assignee else "Unassigned",
            "created_at": created_at,
            "url":        f"{JIRA_BASE_URL}/browse/{issue['key']}",
        })
    return bugs


# ── Reminder formatting helpers ────────────────────────────────────────────────

def _format_age(delta: timedelta) -> str:
    """'1d 6h', '3d', '14h', etc."""
    total_hours = int(delta.total_seconds() / 3600)
    days, hours = divmod(total_hours, 24)
    if days and hours:
        return f"{days}d {hours}h"
    if days:
        return f"{days}d"
    return f"{hours}h"


def _build_single_reminder(bug: dict, now: datetime) -> str:
    """Build a single Discord reminder message for one bug ticket."""
    interval = REMINDER_INTERVALS.get(bug["priority"], timedelta(hours=48))
    hours    = int(interval.total_seconds() / 3600)
    emoji    = PRIORITY_EMOJI.get(bug["priority"], "⚪")
    age      = _format_age(now - bug["created_at"])
    return (
        f"🐛 **Bug Reminder** — **[{bug['key']}]({bug['url']})**\n"
        f"> **Summary:** {bug['summary']}\n"
        f"> **Priority:** {emoji} {bug['priority']}  ·  **Status:** {bug['status']}\n"
        f"> **Assignee:** {bug['assignee']}  ·  **Open for:** {age}\n"
        f"> *Reminders every {hours}h until resolved.*"
    )


# ── Timer helper ──────────────────────────────────────────────────────────────

async def _run_timer(channel, user, duration: float, label: str | None, confirm_msg=None) -> None:
    """Sleep for `duration` seconds, then ping the user and freeze the countdown display."""
    await asyncio.sleep(duration)
    label_text = f" — **{label}**" if label else ""
    # Edit the confirmation message to a static "0:00" so the timestamp stops counting up
    if confirm_msg:
        static_label = f" for **{label}**" if label else ""
        try:
            await confirm_msg.edit(content=f"⏱️ Timer set{static_label}! Fires **0:00**")
        except Exception:
            pass
    await channel.send(f"⏰ {user.mention} Time's up{label_text}!")


# ── Discord bot setup ──────────────────────────────────────────────────────────

# message_content intent is required to read what users actually wrote
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


@tasks.loop(hours=1)
async def check_bug_reminders():
    """
    Runs every hour. Sends one reminder message per stale Bug ticket that:
      - Was created AFTER this feature was first enabled (started_at), and
      - Has not yet reached a terminal status for its priority, and
      - Has been open longer than its priority's reminder interval.
    """
    channel = client.get_channel(REMINDERS_CHANNEL_ID)
    if channel is None:
        return

    now   = datetime.now(timezone.utc)
    state = load_reminder_state()

    # Record the first-run timestamp — only tickets created after this moment
    # will ever receive reminders. Pre-existing tickets are silently skipped.
    if "started_at" not in state:
        state["started_at"] = now.isoformat()
        save_reminder_state(state)
        print(f"[Nico Jr.] Reminder baseline set — only tickets created from now on will be reminded.")

    started_at = datetime.fromisoformat(state["started_at"])

    bugs = await asyncio.to_thread(get_stale_bug_tickets)

    # Only consider tickets created after the feature was enabled
    new_bugs = [b for b in bugs if b["created_at"] > started_at]

    # Clean up state entries for tickets that are no longer stale
    # (preserve the special "started_at" key)
    active_keys = {b["key"] for b in new_bugs} | {"started_at"}
    state = {k: v for k, v in state.items() if k in active_keys}

    # Decide which tickets are due for a reminder right now
    due: list[dict] = []
    for bug in new_bugs:
        interval = REMINDER_INTERVALS.get(bug["priority"], timedelta(hours=48))
        last_str = state.get(bug["key"], {}).get("last_reminded")
        if last_str:
            if now - datetime.fromisoformat(last_str) >= interval:
                due.append(bug)
        else:
            # Never reminded — fire once it's older than the interval
            if now - bug["created_at"] >= interval:
                due.append(bug)

    if not due:
        save_reminder_state(state)
        return

    # Send one message per ticket so developers aren't overwhelmed
    for bug in due:
        await channel.send(_build_single_reminder(bug, now))
        state[bug["key"]] = {"last_reminded": now.isoformat()}
        save_reminder_state(state)  # persist after each send

    print(f"[Nico Jr.] Sent {len(due)} bug reminder(s)")


@check_bug_reminders.before_loop
async def before_check_bug_reminders():
    """Wait until the bot is fully connected before starting the loop."""
    await client.wait_until_ready()


@client.event
async def on_ready():
    """Fired once when Nico Jr. successfully connects to Discord."""
    print(f"Nico Jr. is online! Logged in as {client.user}")
    print(f"Watching all channels · Bug reminders → channel {REMINDERS_CHANNEL_ID}")
    init_memory_db()
    print(f"[Nico Jr.] Memory DB ready ({MEMORY_DB})")
    check_bug_reminders.start()
    print(f"[Nico Jr.] Bug reminder loop started (checking every hour)")


async def process_message(message: discord.Message):
    """
    Core handler — runs whenever a message (new or edited) should be acted on.
    Guards (channel check, mention check, etc.) are applied by the callers.
    """

    # ── Step 1: Collect any attachments (screenshots, recordings, etc.) ────
    attachment_urls = [a.url for a in message.attachments]
    if attachment_urls:
        print(f"[Nico Jr.] {len(attachment_urls)} attachment(s) found: {attachment_urls}")

    # ── Step 1b: Fetch quoted/forwarded message if present ───────────────
    quoted_text = ""
    snapshots = getattr(message, "message_snapshots", None)
    print(f"[Nico Jr.] msg.type={message.type} | snapshots={bool(snapshots)} | reference={bool(message.reference)} | embeds={len(message.embeds)}")
    if snapshots:
        # Native Discord forward — content + embeds live in message_snapshots
        snapshot = snapshots[0]
        print(f"[Nico Jr.] Snapshot: content={repr(snapshot.content[:80] if snapshot.content else '')} | embeds={len(snapshot.embeds)} | attachments={len(snapshot.attachments)}")
        parts = []
        if snapshot.content:
            parts.append(snapshot.content)
        embed_text = _embeds_to_text(snapshot.embeds)
        print(f"[Nico Jr.] Snapshot embed_text: {repr(embed_text[:200])}")
        if embed_text:
            parts.append(f"[Embedded content]\n{embed_text}")
        if parts:
            quoted_text = "\n\n".join(parts)
        else:
            print(f"[Nico Jr.] WARNING: forwarded snapshot has no readable content")
        for att in snapshot.attachments:
            attachment_urls.append(att.url)
    elif message.reference and message.reference.message_id:
        # Discord reply — fetch the original message
        try:
            ref_channel = message.channel
            if message.reference.channel_id and message.reference.channel_id != message.channel.id:
                ref_channel = client.get_channel(message.reference.channel_id)
            if ref_channel:
                ref_msg = await ref_channel.fetch_message(message.reference.message_id)
                print(f"[Nico Jr.] Replied-to msg: content={repr(ref_msg.content[:80])} | embeds={len(ref_msg.embeds)}")
                parts = []
                if ref_msg.content:
                    parts.append(ref_msg.content)
                embed_text = _embeds_to_text(ref_msg.embeds)
                print(f"[Nico Jr.] Reply embed_text: {repr(embed_text[:200])}")
                if embed_text:
                    parts.append(f"[Embedded content]\n{embed_text}")
                if parts:
                    quoted_text = "\n\n".join(parts)
                if ref_msg.attachments:
                    attachment_urls.extend(a.url for a in ref_msg.attachments)
        except Exception as e:
            print(f"[Nico Jr.] Could not fetch referenced message: {e}")
    print(f"[Nico Jr.] Final quoted_text: {repr(quoted_text[:200])}")

    # ── Step 1c.5: Extract embeds from the message itself ────────────────
    message_embed_text = _embeds_to_text(message.embeds) if message.embeds else ""

    # ── Step 1c: Extract Google Drive links from message + quoted text ───
    drive_files = _extract_drive_urls(message.content or "")
    if quoted_text:
        drive_files.extend(_extract_drive_urls(quoted_text))
    if drive_files:
        print(f"[Nico Jr.] {len(drive_files)} Google Drive link(s) found")

    print(f"[Nico Jr.] Message from {message.author.name}: {message.content}")

    # ── Step 2: Fetch recent channel history for context ──────────────────
    # Grab the last 20 messages before this one (excluding Nico Jr.'s own replies)
    # so Claude can understand the full conversation, not just the tagged message.
    history = []
    async for past_msg in message.channel.history(limit=20, before=message):
        if past_msg.author == client.user:
            continue  # skip Nico Jr.'s own messages — they add noise
        entry = {"author": past_msg.author.display_name, "content": past_msg.content}
        # Note any attachments in the history too
        if past_msg.attachments:
            entry["content"] += " [+ {} attachment(s)]".format(len(past_msg.attachments))
        history.append(entry)
    history.reverse()  # put oldest messages first so the conversation reads naturally
    print(f"[Nico Jr.] Loaded {len(history)} past message(s) for context")

    # ── Step 3: Fetch Epics, parents, assignees in parallel (cached 5 min) ─
    epics, parents, assignees = await asyncio.gather(
        asyncio.to_thread(_cached_jira, "epics",     get_jira_epics),
        asyncio.to_thread(_cached_jira, "parents",   get_jira_parents),
        asyncio.to_thread(_cached_jira, "assignees", get_jira_assignees),
    )
    print(f"[Nico Jr.] Fetched {len(epics)} Epic(s), {len(parents)} parent ticket(s), and {len(assignees)} assignee(s) from Jira")

    # ── Step 3b: Load cross-channel server memory ──────────────────────────
    server_history = await asyncio.to_thread(
        get_cross_channel_history, 40, message.channel.id
    )

    # ── Step 4: Ask Claude what the user wants ─────────────────────────────
    try:
        full_content = message.content or ""
        if message_embed_text:
            full_content += f"\n\n[Embedded content in this message]\n{message_embed_text}"
        analysis = await analyze_message(full_content, epics, attachment_urls, history, assignees, parents, server_history, quoted_text, len(drive_files))
    except json.JSONDecodeError as e:
        print(f"[Nico Jr.] JSON parse error: {e}")
        await message.reply("Sorry, I had trouble understanding that. Could you rephrase your request?")
        return
    except anthropic.AuthenticationError:
        print("[Nico Jr.] Claude auth error — check ANTHROPIC_API_KEY")
        await message.reply("I can't reach my AI brain right now (auth issue). Tell the bot admin to check the API key.")
        return
    except anthropic.RateLimitError:
        print("[Nico Jr.] Claude rate limit hit after all retries")
        await message.reply("I'm getting a lot of requests right now — give me a moment and try again.")
        return
    except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
        print(f"[Nico Jr.] Claude connectivity error after all retries: {e}")
        await message.reply("I'm having trouble reaching Claude right now. Try again in a moment.")
        return
    except Exception as e:
        print(f"[Nico Jr.] Unexpected Claude error: {type(e).__name__}: {e}")
        await message.reply("Something went wrong on my end. Try again in a moment.")
        return

    action = analysis.get("action", "ignore")

    # ── Step 4b: React to the message if Claude suggested one ──────────────
    reaction = analysis.get("reaction")
    if reaction:
        try:
            await message.add_reaction(reaction)
        except Exception as e:
            print(f"[Nico Jr.] Could not add reaction '{reaction}': {e}")

    # ── Step 5: Silently ignore if not relevant ────────────────────────────
    if action == "ignore":
        return

    # ── Step 5b: Chat back for general conversation ────────────────────────
    if action in ("chat", "none"):
        print("[Nico Jr.] Responding conversationally.")
        try:
            reply = await chat_with_nico_jr(full_content, message.author.display_name, history, epics, assignees, parents, server_history, quoted_text)
            await message.reply(reply)
        except Exception as e:
            print(f"[Nico Jr.] Chat error: {e}")
            await message.reply("Sorry, my brain froze for a second. What were you saying?")
        return

    # ── Step 5c: Set a timer ───────────────────────────────────────────────
    if action == "timer":
        duration = analysis.get("duration_seconds", 0)
        label    = analysis.get("label") or None

        if not duration or duration <= 0:
            await message.reply(
                "I couldn't figure out how long to set the timer for. "
                "Try something like 'set a timer for 5 minutes'."
            )
            return

        if duration > 86400:
            await message.reply("I can only set timers up to 24 hours. Give me a shorter duration!")
            return

        # Discord live countdown timestamp — renders and ticks in every client
        fire_ts = int(datetime.now(timezone.utc).timestamp()) + int(duration)
        discord_ts = f"<t:{fire_ts}:R>"  # e.g. "in 5 minutes", counts down live

        label_text = f" for **{label}**" if label else ""
        confirm_msg = await message.reply(f"⏱️ Timer set{label_text}! Fires {discord_ts}")

        asyncio.create_task(_run_timer(message.channel, message.author, duration, label, confirm_msg))
        return

    # ── Step 6: Route to create or update ─────────────────────────────────
    if action == "create":
        status_msg = await message.reply("On it! Creating your Jira ticket...")

        try:
            prefix = build_title_prefix(analysis.get("epic_key"), analysis.get("assignee"), epics)
            final_title = prefix + analysis["title"]
            ticket = await asyncio.to_thread(
                create_jira_ticket,
                final_title,
                analysis["description"],
                analysis.get("priority", "Medium"),
                analysis.get("assignee"),
                analysis.get("issue_type", "Task"),
                analysis.get("epic_key"),
                analysis.get("parent_key"),
            )
        except Exception as e:
            print(f"[Nico Jr.] Jira error: {e}")
            await status_msg.edit(
                content=(
                    "I couldn't create the Jira ticket — there was a problem connecting to Jira. "
                    "Please check the bot logs or try again later."
                )
            )
            return

        # Build the confirmation message
        epic_lookup   = {e["key"]: e["summary"] for e in epics}
        parent_lookup = {p["key"]: p["summary"] for p in parents}

        epic_key   = analysis.get("epic_key")
        parent_key = analysis.get("parent_key")
        epic_label = f"{epic_key} — {epic_lookup[epic_key]}" if epic_key and epic_key in epic_lookup else (epic_key or "None")

        meta_lines = [
            f"> **Type:** {analysis.get('issue_type', 'Task')}  |  **Priority:** {analysis.get('priority', 'Medium')}",
            f"> **Epic:** {epic_label}",
        ]
        if parent_key:
            if ticket.get("parent_dropped"):
                meta_lines.append(f"> **Parent:** ~~{parent_key}~~ *(hierarchy mismatch — ticket created without parent)*")
            else:
                parent_label = f"{parent_key} — {parent_lookup[parent_key]}" if parent_key in parent_lookup else parent_key
                meta_lines.append(f"> **Parent:** {parent_label}")
        if analysis.get("assignee"):
            meta_lines.append(f"> **Assignee:** {analysis['assignee']}")
        if attachment_urls:
            meta_lines.append(f"> **Attachments:** {len(attachment_urls)} file(s) included in description")

        confirmation = (
            f"Ticket created!\n\n"
            f"> **[{ticket['key']}]({ticket['url']})** — {final_title}\n"
            + "\n".join(meta_lines)
            + f"\n> {ticket['url']}"
        )

        # Upload attachments to the ticket and embed images in the description
        uploaded = await asyncio.to_thread(upload_jira_attachments, ticket["key"], attachment_urls)
        if uploaded:
            await asyncio.to_thread(embed_images_in_description, ticket["key"], attachment_urls)
            meta_lines.append(f"> **Attachments:** {len(uploaded)} file(s) uploaded & embedded in description")
        drive_uploaded = await asyncio.to_thread(upload_drive_attachments, ticket["key"], drive_files)
        if drive_uploaded:
            meta_lines.append(f"> **Drive files:** {len(drive_uploaded)} file(s) uploaded from Google Drive")

        await status_msg.edit(content=confirmation)
        print(f"[Nico Jr.] Created ticket {ticket['key']}")

    elif action == "update":
        ticket_key = analysis.get("ticket_key", "").upper()
        fields     = analysis.get("fields", {})

        # Hard guard: only proceed if the ticket key is literally in the message.
        # This prevents Claude from guessing a key from conversation history.
        if not ticket_key or not re.search(r'\b' + re.escape(ticket_key) + r'\b', message.content, re.IGNORECASE):
            await message.reply(
                "Which ticket did you mean? Include the ticket key (e.g. `TA-6794`) and I'll update it."
            )
            return

        status_msg = await message.reply(f"On it! Updating **{ticket_key}**...")

        try:
            ticket = await asyncio.to_thread(update_jira_ticket, ticket_key, fields)
        except Exception as e:
            print(f"[Nico Jr.] Jira update error: {e}")
            await status_msg.edit(
                content=(
                    f"I couldn't update **{ticket_key}** — there was a problem connecting to Jira. "
                    "Please check the bot logs or try again later."
                )
            )
            return

        # Build a human-readable summary of what changed
        epic_lookup   = {e["key"]: e["summary"] for e in epics}
        parent_lookup = {p["key"]: p["summary"] for p in parents}

        field_labels = {
            "title":       "Title",
            "description": "Description",
            "priority":    "Priority",
            "assignee":    "Assignee",
            "issue_type":  "Type",
        }
        changed = [
            f"> **{label}** → {fields[key]}"
            for key, label in field_labels.items()
            if key in fields
        ]
        # Parent gets special formatting to include the summary
        if "parent_key" in fields and fields["parent_key"]:
            pk = fields["parent_key"]
            parent_label = f"{pk} — {parent_lookup[pk]}" if pk in parent_lookup else pk
            changed.append(f"> **Parent** → {parent_label}")
        # Epic gets special formatting to include the name
        elif "epic_key" in fields and fields["epic_key"]:
            ek = fields["epic_key"]
            epic_label = f"{ek} — {epic_lookup[ek]}" if ek in epic_lookup else ek
            changed.append(f"> **Epic** → {epic_label}")
        uploaded = await asyncio.to_thread(upload_jira_attachments, ticket_key, attachment_urls)
        if uploaded:
            await asyncio.to_thread(embed_images_in_description, ticket_key, attachment_urls)
            changed.append(f"> **Attachments** → {len(uploaded)} file(s) uploaded & embedded in description")
        drive_uploaded = await asyncio.to_thread(upload_drive_attachments, ticket_key, drive_files)
        if drive_uploaded:
            changed.append(f"> **Drive files** → {len(drive_uploaded)} file(s) uploaded from Google Drive")

        changes_text = "\n".join(changed) if changed else "> (no recognisable fields were changed)"

        confirmation = (
            f"Ticket updated!\n\n"
            f"> **[{ticket['key']}]({ticket['url']})**\n"
            f"{changes_text}\n"
            f"> {ticket['url']}"
        )

        await status_msg.edit(content=confirmation)
        print(f"[Nico Jr.] Updated ticket {ticket['key']}")


# ── Discord event handlers ─────────────────────────────────────────────────────

def _should_process(message: discord.Message) -> bool:
    """Return True if this message should be acted on by Nico Jr."""
    if message.author == client.user:
        return False
    if message.guild is None:  # ignore DMs
        return False
    has_content = bool(message.content.strip())
    has_attachments = bool(message.attachments)
    has_forward = bool(getattr(message, "message_snapshots", None))
    has_embeds = bool(message.embeds)
    if not has_content and not has_attachments and not has_forward and not has_embeds:
        return False
    if message.author.bot:  # ignore other bots
        return False
    return True


def _remember(message: discord.Message) -> None:
    """Store a human message in the persistent server memory DB."""
    if message.guild and not message.author.bot and message.content.strip():
        store_message_in_memory(
            channel_id=message.channel.id,
            channel_name=getattr(message.channel, "name", str(message.channel.id)),
            author_name=message.author.display_name,
            content=message.content,
            created_at=message.created_at.isoformat(),
        )


@client.event
async def on_message(message: discord.Message):
    """Fired every time a new message is sent."""
    _remember(message)

    if message.content.strip().lower() == "!restart" and not message.author.bot:
        if message.author.guild_permissions.administrator:
            await message.reply("Restarting... brb!")
            await client.close()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        else:
            await message.reply("Sorry, only server admins can restart me.")
        return

    if _should_process(message):
        await process_message(message)


@client.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    """Fired when a message is edited."""
    _remember(after)
    if not _should_process(after):
        return
    print(f"[Nico Jr.] Edited message from {after.author.name} — reprocessing.")
    await process_message(after)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
