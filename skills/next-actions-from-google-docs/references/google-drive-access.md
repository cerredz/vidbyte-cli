# Give the script read access to Google Drive

Read this file only when `python3 "<skill>/scripts/google_docs.py" check` fails. `<skill>` is the
folder that holds this skill's `SKILL.md`.

The script reads Google Docs through the Google Drive API. Every call needs an OAuth access token
that allows Drive access. There are three ways to provide one. Use the first that works for the
user, in this order:

| Way | Best for | Setup |
|---|---|---|
| A. A short-lived token from the OAuth 2.0 Playground | A first run, or an occasional run | About two minutes in the browser. No install and no Google Cloud project. Read-only. The token lasts about one hour. |
| B. The Google Cloud CLI (`gcloud`) | Weekly use | One-time setup: install gcloud, log in, and enable the Drive API in a Google Cloud project you own. After that, nothing to copy. |
| C. Your coding agent's own Google Drive connector | A host that already has one, or when A and B are blocked | Nothing new for Google. Your coding agent reads the documents and writes `items.json`. |

Rules for every way:

- The user does every Google sign-in in their own browser or terminal. Never ask the user to paste
  a token into the chat, never print one, and never put one on a command line.
- The script reads the token from the `GOOGLE_OAUTH_ACCESS_TOKEN` environment variable, from a
  file passed with `--token-file`, or from `gcloud auth print-access-token`. It never prints,
  logs, or saves the token.
- The script only reads. It never edits, moves, shares, or comments on a document.

## A. A short-lived token from the OAuth 2.0 Playground

The [OAuth 2.0 Playground](https://developers.google.com/oauthplayground/) is a Google page that
issues a token for the scopes you choose. It needs no Google Cloud project.

Ask the user to do these steps themselves:

1. Open <https://developers.google.com/oauthplayground/> in the browser.
2. Under **Step 1**, type this scope into the box for your own scopes, then click
   **Authorize APIs**:

   ```text
   https://www.googleapis.com/auth/drive.readonly
   ```

3. Sign in with the Google account that owns the documents, and allow the access. `drive.readonly`
   lets the token read files and nothing else.
4. Under **Step 2**, click **Exchange authorization code for tokens**.
5. Copy the **Access token** value. It starts with `ya29.`. Do not paste it into the chat.
6. Save it to a file in your own terminal. The file must hold only the token.

   macOS:

   ```bash
   pbpaste > ~/.google-docs-token && chmod 600 ~/.google-docs-token
   ```

   Linux (use `wl-paste` on Wayland, `xclip` on X11):

   ```bash
   wl-paste > ~/.google-docs-token && chmod 600 ~/.google-docs-token
   xclip -selection clipboard -o > ~/.google-docs-token && chmod 600 ~/.google-docs-token
   ```

   Windows PowerShell:

   ```powershell
   Get-Clipboard | Set-Content -NoNewline -Encoding ascii "$HOME\.google-docs-token"
   ```

   Without a clipboard tool, paste the token into a text editor and save it as `.google-docs-token`
   in the home folder.

7. Tell the coding agent "the token is saved". The agent then runs:

   ```bash
   python3 "<skill>/scripts/google_docs.py" check --token-file ~/.google-docs-token
   ```

   and passes the same `--token-file ~/.google-docs-token` to `gather`. The script expands `~`
   itself, so the same command works in bash, zsh, PowerShell, and cmd.

A user who starts the coding agent from a terminal can set an environment variable before
starting it, instead of a file:

```bash
export GOOGLE_OAUTH_ACCESS_TOKEN="$(pbpaste)"        # macOS; then start the coding agent here
```

```powershell
$env:GOOGLE_OAUTH_ACCESS_TOKEN = Get-Clipboard       # PowerShell; then start the coding agent here
```

The token stops working after about one hour. After the run, delete the file
(`rm ~/.google-docs-token`, or `Remove-Item "$HOME\.google-docs-token"` in PowerShell). To
withdraw the access at once, remove "OAuth 2.0 Playground" at
<https://myaccount.google.com/permissions>.

If the sign-in page says the access is blocked, the user's Google Workspace administrator does not
allow this app. Use way C, or ask the administrator.

## B. The Google Cloud CLI

`gcloud auth login --enable-gdrive-access` adds Google Drive access to the gcloud login
([reference](https://docs.cloud.google.com/sdk/gcloud/reference/auth/login)). The script then asks
gcloud for a fresh token on every run with `gcloud auth print-access-token`, so nothing is
copied by hand.

Two things to tell the user before they choose this way:

- gcloud asks for full Google Drive access (the `https://www.googleapis.com/auth/drive` scope),
  which allows reading and changing files. The script only reads, but the gcloud login can do more.
- Tokens from gcloud belong to gcloud's own project. Google refuses Drive calls billed to that
  project with "Google Drive API has not been used in project 32555940559 before or it is
  disabled". The fix is a Google Cloud project of your own, named in the `X-Goog-User-Project`
  header ([system parameters](https://docs.cloud.google.com/apis/docs/system-parameters)). The
  script sends that header from `--quota-project`. The Drive API has no charge.

Ask the user to run these in their own terminal:

1. Install the Google Cloud CLI from <https://docs.cloud.google.com/sdk/docs/install>.
2. Log in with Drive access. A browser window opens:

   ```bash
   gcloud auth login --enable-gdrive-access
   ```

3. Pick a Google Cloud project you own, or create one. Project ids are 6 to 30 lowercase letters,
   digits, or hyphens, and must be unique across Google Cloud:

   ```bash
   gcloud projects list
   gcloud projects create my-notes-project-2026
   ```

4. Enable the Drive API in that project
   ([reference](https://docs.cloud.google.com/sdk/gcloud/reference/services/enable)):

   ```bash
   gcloud services enable drive.googleapis.com --project my-notes-project-2026
   ```

Then the agent runs:

```bash
python3 "<skill>/scripts/google_docs.py" check --auth gcloud --quota-project my-notes-project-2026
```

and passes the same `--auth gcloud --quota-project <id>` to `gather`. Setting the environment
variable `GOOGLE_CLOUD_QUOTA_PROJECT` to the project id works in place of `--quota-project`.

To withdraw the access later: `gcloud auth revoke`.

## C. Your coding agent's own Google Drive connector

Use this way when the coding agent already has a Google Drive or Google Docs connector (an MCP
server or a built-in integration) that the user authorized, and ways A and B are not possible.
Tell the user one thing first: with this way, the document text passes through the coding agent's
own model before the script redacts it.

1. With the connector, find the Google Docs the user created or edited in the window, newest first,
   at most 30. Skip any document whose title or folder the user asked to exclude. Read each one's
   text. Only read: never edit, move, share, or comment on a document through the connector.
2. Write `items.json` into a new, empty folder under the system temp folder, never inside a
   repository. It must have exactly these fields, and no others:

   ```json
   {
     "schema": "notes-digest/1",
     "provider": "google-docs",
     "generated_at": "2026-10-11T18:00:00Z",
     "since": "2026-10-04T18:00:00Z",
     "items": [
       {
         "provider": "google-docs",
         "id": "<the Drive file id>",
         "title": "Launch plan",
         "link": "https://docs.google.com/document/d/<the Drive file id>/edit",
         "kind": "doc",
         "container": "My Drive/Projects",
         "created": "2026-09-30T09:12:00Z",
         "last_edited": "2026-10-10T16:40:00Z",
         "author": "me",
         "open_tasks": ["Send the pricing draft to the team"],
         "excerpt": "<the document text, as read>"
       }
     ]
   }
   ```

   - `since` is the window start, and `generated_at` is now, both in UTC.
   - `kind` is `doc` for Docs, Sheets, and Slides, and `file` for a Markdown or text file.
   - `link` must start with `https://`.
   - `container` is the folder path, or `Google Drive` when the connector does not say.
   - `created` may be `null`. `last_edited` is the last edit by anyone.
   - `author` is `me` when the user made the last edit, `other` when someone else did, and `null`
     when the connector does not say.
   - `open_tasks` lists unchecked task lines you saw, or is `[]`. The script also finds task lines
     in `excerpt`.
   - `excerpt` is the plain document text. The script redacts it and cuts it to size.

3. Build the digest from that file. No Google call is made:

   ```bash
   python3 "<skill>/scripts/google_docs.py" gather --from-items "<folder>/items.json"
   ```

   The script checks the format strictly. Exit 2 lists every problem; fix the file and run the
   command again. It then keeps only documents edited since `since` (or the window from `--days`
   or `--since`, when given), applies `--exclude`, redacts, and writes `items.json`, `digest.md`,
   and `inputs.json` into a new `out_dir`, as a normal `gather` does.
4. Delete the folder you wrote in step 2 after the run. It holds unredacted text.

## Error kinds from `check` and `gather`

Both commands print JSON. On exit 1, `error.kind` says what failed and `error.hint` says what to
do.

| `error.kind` | Meaning | Do this |
|---|---|---|
| `no_token` | No token was found, or it does not look like a Google access token. | Way A, B, or C above. |
| `auth` | Google rejected the token (HTTP 401), usually because it is older than one hour, or gcloud could not print a token. | Get a new token (way A, steps 1 to 6), or ask the user to run `gcloud auth login --enable-gdrive-access` again. |
| `scope` | The token has no Drive scope (HTTP 403). | Way A with the `drive.readonly` scope, or `gcloud auth login --enable-gdrive-access`. |
| `api_disabled` | The Drive API is off in the project the call is billed to (HTTP 403). With gcloud, this means `--quota-project` is missing. | Way B, steps 3 and 4, then pass `--quota-project`. |
| `quota_project_denied` | The account may not use the `--quota-project` project. | Use a project the user owns. |
| `domain_policy` | The Google Workspace administrator blocks this access. | Way C, or ask the administrator. |
| `rate_limit` | Google kept answering "rate limit exceeded" after five retries with backoff. | Wait a few minutes, then run `gather` again, or pass a smaller `--max-items`. |
| `network`, `server` | Google could not be reached, or kept failing. | Check the connection and try again in a few minutes. |
