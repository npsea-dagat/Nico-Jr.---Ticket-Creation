# Alfred — Discord → Jira Bot

Alfred watches a Discord channel and automatically creates Jira tickets from natural language messages. No slash commands needed — just write naturally and Alfred figures out the rest.

---

## How it works

1. A team member posts a message in the watched channel (e.g. *"The login button is broken on Safari, high priority, assign it to Ana"*).
2. Alfred sends the message to Claude AI to determine intent.
3. If it's a ticket request, Claude extracts the title, description, priority, assignee, and issue type.
4. Alfred creates the Jira ticket via the Jira REST API.
5. Alfred replies in Discord with the ticket number and a direct link.
6. If the message isn't a ticket request (casual chat, questions, etc.), Alfred stays silent.

---

## Prerequisites

- Python 3.10 or higher
- A Discord account and a server where you have admin permissions
- A Jira Cloud account with a project to create tickets in
- An Anthropic API key

---

## Setup

### 1. Clone or download this folder

Place the `alfred-bot/` folder anywhere on your machine.

### 2. Create a virtual environment (recommended)

```bash
cd alfred-bot
python3 -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Create your `.env` file

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

Then open `.env` in a text editor and fill in each value (see the section below for where to get each one).

### 5. Run Alfred

```bash
python main.py
```

You should see:
```
Alfred is online! Logged in as Alfred#1234
Watching channel ID: 123456789012345678
```

---

## Getting your credentials

### Discord bot token

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) and click **New Application**.
2. Name it **Alfred**, then go to the **Bot** tab.
3. Click **Reset Token** and copy the token → this is your `DISCORD_BOT_TOKEN`.
4. Under **Privileged Gateway Intents**, enable **Message Content Intent**.
5. Go to **OAuth2 → URL Generator**, select the `bot` scope, and under Bot Permissions select **Send Messages** and **Read Message History**.
6. Open the generated URL in your browser to invite Alfred to your server.

### Discord channel ID

1. In Discord, open **Settings → Advanced** and enable **Developer Mode**.
2. Right-click the channel you want Alfred to watch → **Copy Channel ID**.
3. Paste it as `DISCORD_CHANNEL_ID` in your `.env`.

### Anthropic API key

1. Go to [console.anthropic.com](https://console.anthropic.com).
2. Click **API Keys → Create Key**.
3. Copy it as `ANTHROPIC_API_KEY`.

### Jira credentials

| Variable | Where to find it |
|---|---|
| `JIRA_BASE_URL` | Your Jira URL, e.g. `https://yourcompany.atlassian.net` |
| `JIRA_EMAIL` | The email you log into Jira with |
| `JIRA_API_TOKEN` | [id.atlassian.com/manage-profile/security/api-tokens](https://id.atlassian.com/manage-profile/security/api-tokens) → Create API token |
| `JIRA_PROJECT_KEY` | The short prefix on your tickets, e.g. `ENG` for `ENG-42` |

---

## Example messages Alfred will act on

| Message | What Alfred creates |
|---|---|
| "The checkout page crashes when the cart is empty" | Bug, Medium priority |
| "We need a dark mode option, high priority" | Story, High priority |
| "Set up CI/CD pipeline for the mobile app, assign to James" | Task, assigned to James |
| "Big refactor of the auth system — this is an epic" | Epic, Medium priority |
| "hey what time is standup?" | *(ignored — not a ticket request)* |

---

## Project structure

```
alfred-bot/
├── main.py          # All bot logic (Claude, Jira, Discord)
├── .env             # Your secrets — never commit this
├── .env.example     # Template showing required variables
├── requirements.txt # Python dependencies
└── README.md        # This file
```

---

## Troubleshooting

**Alfred doesn't respond at all**
- Make sure the `DISCORD_CHANNEL_ID` in your `.env` matches the channel you're posting in.
- Check that the **Message Content Intent** is enabled on your bot's Discord developer page.

**"Jira API returned 400" error**
- The `JIRA_PROJECT_KEY` might be wrong — check it matches exactly (case-sensitive).
- The `issue_type` Claude chose might not exist in your project. Go to Jira → Project Settings → Issue Types to see what's available.

**Assignee not being set**
- Alfred searches Jira for the name mentioned in the message. Make sure the name matches a real user's display name or email in Jira.
- If Alfred can't find the user it will create the ticket unassigned and log a warning to the console.

**"I'm having trouble reaching my AI brain"**
- Your `ANTHROPIC_API_KEY` may be invalid or you may have hit your usage limit.
