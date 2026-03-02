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

import discord
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
CHANNEL_ID        = int(os.getenv("DISCORD_CHANNEL_ID"))

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
    url = f"{JIRA_BASE_URL}/rest/api/3/search"
    jql = f'project = "{JIRA_PROJECT_KEY}" AND issuetype = Epic AND statusCategory != Done ORDER BY created DESC'

    response = requests.get(
        url,
        headers=JIRA_HEADERS,
        params={"jql": jql, "fields": "summary,status,assignee", "maxResults": 50},
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

def analyze_message(message_text: str, epics: list, attachments: list, history: list, assignees: list) -> dict:
    """
    Ask Claude what the user wants to do. Returns one of three shapes:

    Create:  {"action": "create", "title": ..., "description": ...,
               "priority": ..., "assignee": ..., "issue_type": ...,
               "epic_key": "PROJ-5" or null}

    Update:  {"action": "update", "ticket_key": "PROJ-123",
               "fields": {<only the fields the user mentioned>}}

    Ignore:  {"action": "none"}

    epics       — list of {"key", "summary"} dicts fetched from Jira
    attachments — list of Discord attachment URLs (screenshots, recordings, etc.)
    history     — list of {"author", "content"} dicts from recent channel messages
    assignees   — list of {"name", "email"} dicts of valid Jira assignees
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

    # Format recent channel history so Claude has full context
    if history:
        history_lines = "\n".join(f'  {m["author"]}: {m["content"]}' for m in history)
        history_section = f"""
Recent conversation in this channel (for context — use this to fill in missing details):
{history_lines}
"""
    else:
        history_section = ""

    # List any attachments so Claude can reference them in the description
    if attachments:
        attachments_text = "\n".join(f"  - {url}" for url in attachments)
        attachments_section = f"""
The user also attached the following files (screenshots / recordings). Include them
as a clearly labelled "Attachments" section at the end of the description:
{attachments_text}
"""
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

    prompt = f"""You are Nico Jr., a helpful assistant that manages Jira tickets from natural language.

Analyze the message below and decide what the user wants:
1. CREATE a new Jira ticket
2. UPDATE an existing Jira ticket (they mention a ticket key like PROJ-123)
3. Neither — ignore it
{intent_hint}
Message:
\"\"\"{message_text}\"\"\"
{history_section}{assignees_section}{epics_section}{attachments_section}
Rules for deciding intent:
- If the message contains a ticket key (e.g. PROJ-123), it is ALWAYS an update — never a create.
- Only choose "create" when no existing ticket key is present and the user clearly wants a new ticket.
- Use "none" for everything else (chat, questions, etc.)

--- If CREATE ---
Reply with ONLY this JSON:
{{
  "action": "create",
  "title": "Short, clear ticket title (max 100 characters)",
  "description": "Full description of the issue or task. If attachments were provided, add them at the end under a heading called Attachments, one per line as: Label: URL",
  "priority": "one of: Highest, High, Medium, Low, Lowest",
  "assignee": "name or email of the person to assign, or null if not mentioned",
  "issue_type": "one of: Bug, Task, Story, Epic",
  "epic_key": "the key of the most relevant Epic (e.g. PROJ-5), or null if none fit"
}}

--- If UPDATE ---
Reply with ONLY this JSON (include only the fields the user explicitly wants to change):
{{
  "action": "update",
  "ticket_key": "PROJ-123",
  "fields": {{
    "title": "new title if mentioned",
    "description": "new description if mentioned. Append attachments at the end if any were provided.",
    "priority": "new priority if mentioned",
    "assignee": "new assignee if mentioned",
    "issue_type": "new issue type if mentioned",
    "epic_key": "epic key if the user wants to change the epic"
  }}
}}

--- If NEITHER ---
Reply with ONLY:
{{
  "action": "none"
}}

Rules:
- Default priority → "Medium" when not specified.
- Default issue_type → "Task" when not specified.
- Use "Bug" for something broken, "Story" for new features, "Epic" for large bodies of work, "Task" for everything else.
- For updates, only include fields the user actually mentions — leave out the rest.
- Respond with JSON only — no explanation, no markdown fences."""

    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )

    # Claude returns a text block — parse it as JSON
    raw = response.content[0].text.strip()
    return json.loads(raw)


# ── Claude: Conversational reply ──────────────────────────────────────────────

def chat_with_nico_jr(message_text: str, author_name: str, history: list, epics: list, assignees: list) -> str:
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
        history_context = f"\nRecent conversation:\n{history_text}\n"
    else:
        history_context = ""

    prompt = f"""You are Nico Jr., a helpful and friendly coworker embedded in a team's Discord channel.
You help the team manage their Jira project, but you're also just a good coworker to chat with.

Your personality:
- Warm, direct, and professional — like a senior teammate, not a customer support bot
- You know the team's work and can reference their active epics and team members naturally
- You keep replies concise — this is a chat, not an essay
- You can have opinions, crack the occasional light joke, and push back if something seems off
- You never say things like "As an AI..." or "I'm just a bot" — you're Nico Jr., a coworker
- If someone asks you to do something outside your abilities, be honest but keep it casual
- You can remind people you can create and update Jira tickets if it seems relevant
{epics_context}{assignees_context}{history_context}
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

    # Link to the chosen Epic if one was provided
    if epic_key:
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

    if "epic_key" in fields and fields["epic_key"]:
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


# ── Discord bot setup ──────────────────────────────────────────────────────────

# message_content intent is required to read what users actually wrote
intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


@client.event
async def on_ready():
    """Fired once when Nico Jr. successfully connects to Discord."""
    print(f"Nico Jr. is online! Logged in as {client.user}")
    print(f"Watching channel ID: {CHANNEL_ID}")


@client.event
async def on_message(message: discord.Message):
    """Fired every time a message is sent in a channel Nico Jr. can see."""

    # Never respond to our own messages — this would cause an infinite loop
    if message.author == client.user:
        return

    # Only watch the one channel we care about
    if message.channel.id != CHANNEL_ID:
        return

    # Skip blank messages that contain only attachments and no text
    # (we still process them if there are attachments)
    has_text        = bool(message.content.strip())
    has_attachments = bool(message.attachments)

    if not has_text and not has_attachments:
        return

    # Only act when Nico Jr. is explicitly mentioned (@Nico Jr.)
    if client.user not in message.mentions:
        return

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

    # ── Step 3: Fetch Epics and assignees from Jira ────────────────────────
    epics     = get_jira_epics()
    assignees = get_jira_assignees()
    print(f"[Nico Jr.] Fetched {len(epics)} Epic(s) and {len(assignees)} assignee(s) from Jira")

    # ── Step 4: Ask Claude what the user wants ─────────────────────────────
    try:
        analysis = analyze_message(message.content, epics, attachment_urls, history, assignees)
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

    action = analysis.get("action", "none")

    # ── Step 5: Chat back if it's not a ticket request ────────────────────
    if action == "none":
        print("[Nico Jr.] Not a ticket request — responding as a coworker.")
        try:
            reply = chat_with_nico_jr(message.content, message.author.display_name, history, epics, assignees)
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
        # Look up the epic name from the list we already fetched
        epic_lookup = {e["key"]: e["summary"] for e in epics}
        epic_key    = analysis.get("epic_key")
        epic_label  = f"{epic_key} — {epic_lookup[epic_key]}" if epic_key and epic_key in epic_lookup else (epic_key or "None")

        meta_lines = [
            f"> **Type:** {analysis.get('issue_type', 'Task')}  |  **Priority:** {analysis.get('priority', 'Medium')}",
            f"> **Epic:** {epic_label}",
        ]
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

        await status_msg.edit(content=confirmation)
        print(f"[Nico Jr.] Created ticket {ticket['key']}")

    elif action == "update":
        ticket_key = analysis.get("ticket_key", "").upper()
        fields     = analysis.get("fields", {})

        # If there are attachments, append their URLs to the description
        if attachment_urls and "description" not in fields:
            attachments_text = "\n".join(f"- {url}" for url in attachment_urls)
            fields["description"] = f"Attachments:\n{attachments_text}"
        elif attachment_urls and "description" in fields:
            attachments_text = "\n".join(f"- {url}" for url in attachment_urls)
            fields["description"] += f"\n\nAttachments:\n{attachments_text}"

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
        # Show epic name alongside key when available
        epic_lookup = {e["key"]: e["summary"] for e in epics}

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
        # Epic gets special formatting to include the name
        if "epic_key" in fields and fields["epic_key"]:
            ek = fields["epic_key"]
            epic_label = f"{ek} — {epic_lookup[ek]}" if ek in epic_lookup else ek
            changed.append(f"> **Epic** → {epic_label}")
        if attachment_urls:
            changed.append(f"> **Attachments** → {len(attachment_urls)} file(s) added to description")

        changes_text = "\n".join(changed) if changed else "> (no recognisable fields were changed)"

        confirmation = (
            f"Ticket updated!\n\n"
            f"> **[{ticket['key']}]({ticket['url']})**\n"
            f"{changes_text}\n"
            f"> {ticket['url']}"
        )

        await status_msg.edit(content=confirmation)
        print(f"[Nico Jr.] Updated ticket {ticket['key']}")


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
