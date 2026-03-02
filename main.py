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
import json
import base64
import signal
import atexit
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

# Tracks when each ticket was last reminded — persisted so bot restarts don't
# cause duplicate pings.
REMINDER_STATE_FILE = os.path.join(os.path.dirname(__file__), ".reminder_state.json")

# ── Persistent memory database ─────────────────────────────────────────────────

MEMORY_DB   = os.path.join(os.path.dirname(__file__), "memory.db")
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

# Anthropic client — used to call Claude
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Jira uses HTTP Basic Auth: base64-encoded "email:api_token"
_jira_auth = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
JIRA_HEADERS = {
    "Authorization": f"Basic {_jira_auth}",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

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

def analyze_message(message_text: str, epics: list, attachments: list, history: list, assignees: list, parents: list, server_history: list) -> dict:
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
    if attachments:
        attachments_section = (
            f"\nThe user attached {len(attachments)} file(s) (screenshot(s) / recording(s)). "
            f"These will be uploaded directly to the Jira ticket as attachments. "
            f"Reference them naturally in the description (e.g. 'See attached screenshot') "
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

    prompt = f"""You are Nico Jr., a team assistant embedded in a Discord server. You manage Jira tickets and chat with the team. You can see all messages — not just ones that tag you.
{intent_hint}
Message from {message_text.split(':')[0] if ':' in message_text else 'someone'}:
\"\"\"{message_text}\"\"\"
{history_section}{server_history_section}{assignees_section}{epics_section}{parents_section}{attachments_section}
Decide which of these four actions to take:

1. CREATE  — someone is explicitly asking to file / log / create a ticket, or attaches a screenshot of a bug and clearly wants it tracked.
2. UPDATE  — the message references an existing ticket key (e.g. PROJ-123) and asks to change something.
3. CHAT    — the message is directed at you, asks you a question, or you can add clear value (e.g. someone asks about team members, epics, or needs help you can provide).
4. IGNORE  — the conversation is between other people and doesn't involve or need you; chiming in would be intrusive or unhelpful.

Rules:
- NEVER create a ticket unless someone explicitly asks (words like "file", "log", "create", "open", "add a ticket", "pafile", "i-ticket", etc.).
- If a ticket key is present in the message it is ALWAYS an update, never a create.
- Default to IGNORE when in doubt — it is better to stay quiet than to interrupt.
- Only CHAT when the message is clearly addressed to you or when you have something genuinely useful to add.
- Default priority → "Medium". Default issue_type → "Task".
- Use "Bug" for broken things, "Story" for new features, "Epic" for large bodies of work, "Task" for everything else.
- Set parent_key only when explicitly requested or obviously a sub-task.
- For updates, only include fields the user actually mentions.

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
  "parent_key": "parent ticket key or null"
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
  }}
}}

--- If CHAT ---
Reply with ONLY:
{{
  "action": "chat"
}}

--- If IGNORE ---
Reply with ONLY:
{{
  "action": "ignore"
}}

Respond with JSON only — no explanation, no markdown fences."""

    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )

    # Claude returns a text block — parse it as JSON
    raw = response.content[0].text.strip()
    return json.loads(raw)


# ── Claude: Conversational reply ──────────────────────────────────────────────

def chat_with_nico_jr(message_text: str, author_name: str, history: list, epics: list, assignees: list, parents: list, server_history: list) -> str:
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
{epics_context}{parents_context}{assignees_context}{history_context}{server_history_context}
{author_name} just said: \"\"\"{message_text}\"\"\"

Reply naturally as Nico Jr.. Keep it short unless a detailed answer is genuinely needed."""

    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )

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

    bugs = get_stale_bug_tickets()

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

    # ── Step 3: Fetch Epics, parent tickets, and assignees from Jira ──────
    epics     = get_jira_epics()
    parents   = get_jira_parents()
    assignees = get_jira_assignees()
    print(f"[Nico Jr.] Fetched {len(epics)} Epic(s), {len(parents)} parent ticket(s), and {len(assignees)} assignee(s) from Jira")

    # ── Step 3b: Load cross-channel server memory ──────────────────────────
    server_history = get_cross_channel_history(limit=40, exclude_channel_id=message.channel.id)

    # ── Step 4: Ask Claude what the user wants ─────────────────────────────
    try:
        analysis = analyze_message(message.content, epics, attachment_urls, history, assignees, parents, server_history)
    except json.JSONDecodeError:
        await message.reply(
            "Sorry, I had trouble understanding that. Could you rephrase your request?"
        )
        return
    except Exception as e:
        print(f"[Nico Jr.] Claude API error: {e}")
        await message.reply(
            "I'm having trouble reaching my AI brain right now. Please try again in a moment."
        )
        return

    action = analysis.get("action", "ignore")

    # ── Step 5: Silently ignore if not relevant ────────────────────────────
    if action == "ignore":
        return

    # ── Step 5b: Chat back for general conversation ────────────────────────
    if action in ("chat", "none"):
        print("[Nico Jr.] Responding conversationally.")
        try:
            reply = chat_with_nico_jr(message.content, message.author.display_name, history, epics, assignees, parents, server_history)
            await message.reply(reply)
        except Exception as e:
            print(f"[Nico Jr.] Chat error: {e}")
            await message.reply("Sorry, my brain froze for a second. What were you saying?")
        return

    # ── Step 6: Route to create or update ─────────────────────────────────
    if action == "create":
        status_msg = await message.reply("On it! Creating your Jira ticket...")

        try:
            ticket = create_jira_ticket(
                title=analysis["title"],
                description=analysis["description"],
                priority=analysis.get("priority", "Medium"),
                assignee_name=analysis.get("assignee"),
                issue_type=analysis.get("issue_type", "Task"),
                epic_key=analysis.get("epic_key"),
                parent_key=analysis.get("parent_key"),
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
            f"> **[{ticket['key']}]({ticket['url']})** — {analysis['title']}\n"
            + "\n".join(meta_lines)
            + f"\n> {ticket['url']}"
        )

        # Upload attachments to the ticket and embed images in the description
        uploaded = upload_jira_attachments(ticket["key"], attachment_urls)
        if uploaded:
            embed_images_in_description(ticket["key"], attachment_urls)
            meta_lines.append(f"> **Attachments:** {len(uploaded)} file(s) uploaded & embedded in description")

        await status_msg.edit(content=confirmation)
        print(f"[Nico Jr.] Created ticket {ticket['key']}")

    elif action == "update":
        ticket_key = analysis.get("ticket_key", "").upper()
        fields     = analysis.get("fields", {})

        if not ticket_key:
            await message.reply(
                "I couldn't find a ticket number in your message. "
                "Please include the ticket key, e.g. `PROJ-123`."
            )
            return

        status_msg = await message.reply(f"On it! Updating **{ticket_key}**...")

        try:
            ticket = update_jira_ticket(ticket_key, fields)
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
        uploaded = upload_jira_attachments(ticket_key, attachment_urls)
        if uploaded:
            embed_images_in_description(ticket_key, attachment_urls)
            changed.append(f"> **Attachments** → {len(uploaded)} file(s) uploaded & embedded in description")

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
    if not message.content.strip() and not message.attachments:
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
