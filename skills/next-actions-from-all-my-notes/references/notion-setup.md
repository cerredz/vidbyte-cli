# Notion integration and token

Read this file only when `python3 "<skill>/scripts/all_notes.py" providers` lists Notion as not
available, or `providers --check` or `sources.json` shows Notion `failed`. `<skill>` is the folder
that holds this skill's `SKILL.md`. Setup takes about five minutes, and the user does all of it in
their own browser and terminal.

Never ask the user to paste the token into the chat, never print it, and never put it on a command
line. A token typed into a conversation is stored in the transcript.

## 1. Create an internal integration with read access only

The user does this in their browser:

1. Open <https://www.notion.so/profile/integrations> and click **New integration**.
2. Name it, for example "Next actions reader", pick the workspace, keep the type **Internal**, and
   save.
3. Open the integration's **Capabilities** (or **Configuration**) and set:
   - Content capabilities: **Read content** only. Clear **Update content** and **Insert content**.
   - Comment capabilities: none.
   - User capabilities: **Read user information without email addresses**.
4. Copy the **Internal Integration Secret**. It starts with `ntn_` (integrations made before
   September 2024 have secrets that start with `secret_`). Do not paste it into the chat.

The script only reads: it calls `GET` endpoints and `POST /v1/search`, which is a read. With only
**Read content**, the token cannot change anything even if asked to.

## 2. Share the pages with the integration

An internal integration sees only the pages shared with it. In Notion, open a top-level page or a
teamspace page the user writes in, open the **...** menu, choose **Connections**, and pick the
integration. Every page under it is shared too. Repeat for each top-level page. To leave a page
out, do not share it, or pass `--notion-exclude "<page title>"` to `gather`.

## 3. Set NOTION_TOKEN in the user's own terminal

The user runs one of these in the terminal they start their coding agent from. Each one reads the
token without showing it and without saving it in the shell history. Then they start (or restart)
the coding agent from that same terminal, so it inherits the variable.

macOS or Linux, bash or zsh:

```bash
read -rs NOTION_TOKEN && export NOTION_TOKEN
```

Windows PowerShell 7:

```powershell
$env:NOTION_TOKEN = Read-Host -MaskInput "Notion token"
```

Windows PowerShell 5.1, which has no `-MaskInput`:

```powershell
$secure = Read-Host -AsSecureString "Notion token"; $env:NOTION_TOKEN = [System.Net.NetworkCredential]::new("", $secure).Password
```

The variable lasts until that terminal closes. The user should never put the token in a file
inside a repository.

## 4. Check it

```bash
python3 "<skill>/scripts/all_notes.py" providers --check
```

For Notion this calls `GET /v1/users/me` and one `POST /v1/search` with a page size of 1. The
Notion row's `check`, or Notion's `error` in `sources.json` after a `gather`, says what is wrong:

| `kind` | What to do |
|---|---|
| `no_token` | The coding agent was started from a terminal without the variable. Repeat step 3, then restart the agent from that terminal. |
| `auth` | Notion rejected the token (401). Copy the secret again (step 1), or create a new one, and repeat step 3. |
| `scope` | Notion refused the request (403). Give the integration **Read content** (step 1). |
| `not_found` | The token works, but no page is shared with the integration. Do step 2. |
| `rate_limit` | Notion kept answering 429. Wait a few minutes and gather again, or lower `--notion-max-requests`. |
| `network`, `server` | Notion could not be reached, or kept failing. Try again in a few minutes. |

Optional: pass `--notion-my-user-id <id>` to `gather` so each page says whether the user or
someone else made the last edit. Without it, that field is empty.

## 5. Use the coding agent's own Notion connector instead

If the coding agent already has a Notion tool (such as the Notion MCP server) that the user
authorized, and the user prefers it or cannot create an integration, the skill can use it instead
of `NOTION_TOKEN`. Tell the user one thing first: with this way, the page text passes through the
coding agent's own model before the script redacts it.

1. With the connector, find the pages and database rows the user created or edited in the window,
   newest first, at most 40, and read each one. Only read: never create, edit, move, or comment on
   a page through the connector.
2. Write them to `items.json` in a new, empty folder under the system temp folder, in the
   `notes-digest/1` format shown in `SKILL.md` (Gather inputs, step 2), with `"provider": "notion"`,
   `kind` set to `page` or `database-row`, and `link` set to the page's `https://www.notion.so/...`
   URL.
3. Pass `--from-items notion="<folder>/items.json"` to `providers` and `gather`, with the options
   for the other providers as usual. No Notion API call is made.
4. Delete the folder you wrote in step 2 after the run. It holds unredacted text.

## 6. Remove access later

Open the integration at <https://www.notion.so/profile/integrations> and delete it, or remove it
from a page with **...** > **Connections**.
