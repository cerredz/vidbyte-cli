# Slack app and user token

Copied from the next-actions-from-slack skill; only the commands differ. Read this file only when
`all_notes.py providers` lists Slack as not available, or `sources.json` shows Slack `failed`:
`SLACK_USER_TOKEN` is not set, Slack refused it, or it is the wrong kind of token. It walks the user
through creating their own Slack app, which takes about five minutes, and setting the token without
ever pasting it into the chat.

Never ask the user to paste the token into the chat, never print it, and never put it on a command
line. A token typed into a conversation is stored in the transcript, and this token can read every
channel the user can read.

## Why the user creates their own app

The skill reads Slack with a **user token** (it starts with `xoxp-`) from an app the user creates in
their own workspace. Three reasons:

- `search.messages`, which finds the user's own messages in older threads, works only with a user
  token. A bot token (`xoxb-`) cannot call it.
- A user token sees exactly what the user sees, and nothing more.
- Since May 29, 2025, Slack limits `conversations.history` and `conversations.replies` to
  1 request per minute, 15 messages per call, for new installations of apps that are distributed
  outside the Slack Marketplace. "Internal customer-built applications are not impacted": an app
  the user creates for their own workspace keeps the normal limit (50+ requests per minute).
  Source: <https://docs.slack.dev/changelog/2025/05/29/rate-limit-changes-for-non-marketplace-apps>

Browser session tokens (`xoxc-`, `xoxd-`) copied from the Slack web app are refused by the script.
Do not suggest them.

## 1. Create the app from a manifest

The user does this in their browser:

1. Open <https://api.slack.com/apps> and sign in to the workspace.
2. Click **Create New App**, then **From a manifest**, and pick the workspace.
3. Choose the **JSON** tab, replace its content with the manifest below, and click **Next**, then
   **Create**.

```json
{
  "_metadata": { "major_version": 2, "minor_version": 1 },
  "display_information": { "name": "Next actions reader" },
  "oauth_config": {
    "scopes": {
      "user": ["search:read", "channels:read", "groups:read", "channels:history", "groups:history", "files:read"]
    }
  },
  "settings": { "org_deploy_enabled": false, "socket_mode_enabled": false, "token_rotation_enabled": false }
}
```

Only if the user wants DMs read (the skill's `--slack-include-dms` option), add these four scopes to the
`user` list: `"im:read", "im:history", "mpim:read", "mpim:history"`.

What each scope is for. All are read-only; the app cannot post, edit, or delete anything.

| Scope | Slack method | Why |
|---|---|---|
| `search:read` | `search.messages` | Find the user's own messages in the window, including replies in threads older than the window. |
| `channels:read`, `groups:read` | `users.conversations` | List the public and private channels the user belongs to. |
| `channels:history`, `groups:history` | `conversations.history`, `conversations.replies` | Read the window's messages and thread replies in those channels. |
| `files:read` | `files.list`, file downloads | List files created in the window, and read small text files. |
| `im:*`, `mpim:*` (optional) | the same methods, for DMs | Only with `--slack-include-dms`. |

`auth.test` and `chat.getPermalink` need no scope.

## 2. Install it and copy the user token

1. In the app's settings, open **OAuth & Permissions** and click **Install to Workspace**, then
   **Allow**.
2. Copy the **User OAuth Token**. It starts with `xoxp-`. Do not copy the Bot User OAuth Token
   (`xoxb-`); this app has none.

If the button says **Request to Install**, the workspace needs an admin's approval for new apps.
The user can send the request and wait. Meanwhile, if the coding agent has its own Slack tool (an
MCP server or connector), the skill can use it instead: see "Use the coding agent's own connector"
(Gather inputs, step 2) in `SKILL.md`, which passes `--from-items slack=<path>`.

## 3. Set SLACK_USER_TOKEN in the user's own terminal

The user runs one of these in the terminal they start their coding agent from. Each one reads the
token without showing it and without saving it in the shell history. Then they start (or restart)
the coding agent from that same terminal, so it inherits the variable.

macOS or Linux, bash or zsh:

```bash
read -rs SLACK_USER_TOKEN && export SLACK_USER_TOKEN
```

Windows PowerShell:

```powershell
$secure = Read-Host -AsSecureString "Slack user token"; $env:SLACK_USER_TOKEN = [System.Net.NetworkCredential]::new("", $secure).Password
```

The variable lasts until that terminal closes. To keep it longer, the user can store it in their
password manager or OS keychain and load it the same way; they should never put it in a file inside
a repository.

## 4. Check it

Run the free check, which calls only `auth.test` for Slack:

```bash
python3 "<skill>/scripts/all_notes.py" providers --check
```

The Slack row's `detail`, or Slack's `error` in `sources.json` after a `gather`, says what is wrong:

| The skill says | What to do |
|---|---|
| `SLACK_USER_TOKEN is not set` | The coding agent was started from a terminal without the variable. Repeat step 3, then restart the agent from that terminal. |
| `invalid_auth`, `token_revoked`, `token_expired` | The token is wrong or was revoked. Reinstall the app (step 2) and set the new token. |
| `bot_token` | The Bot User OAuth Token was copied. Copy the User OAuth Token (`xoxp-`). |
| `missing_scope (the token needs the user scope ...)`, in `warnings` | That part of Slack was skipped. Add the named scope to the manifest (**App Manifest** in the app settings), save, and reinstall the app. Slack gives the token new scopes only on reinstall. |
| `enterprise_is_restricted` from `search.messages` | The organization blocks search for apps. The skill continues without the search step, so replies in older threads may be missing. |

## 5. Remove access later

To stop the app reading Slack, open the app at <https://api.slack.com/apps>, go to
**OAuth & Permissions**, and click **Revoke All OAuth Tokens**, or delete the app under
**Basic Information**.
