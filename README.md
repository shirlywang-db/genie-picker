# Genie Picker

A Databricks App that gives business users one page to reach every Genie agent they're
allowed to use, across several workspaces. Users pick an agent from a dropdown, and its
**native Genie UI** loads on the same page in an iframe (`/embed/genie/rooms/<id>`).

- The dropdown only shows agents the signed-in user has access to, grouped by workspace.
- All questions and answers run in the embedded Genie UI under the user's own Databricks
  sign-in and data permissions.
- Each agent has a stable deep link (`?space=<id>`), and the last-used agent is remembered.

---

## Install

### Prerequisites

- **Databricks CLI** v0.229 or later, signed in to the workspace that will host the app:
  `databricks auth login --host <workspace URL> --profile <profile>`
- Permission to create Databricks Apps in that workspace.
- **Workspace admin** in every workspace whose agents should appear, for the one-time setup in step 4.

### 1. Get the code

```bash
git clone https://github.com/shirlywang-db/genie-picker.git
cd genie-picker
```

### 2. Choose the workspaces

Edit `GENIE_WORKSPACES` in `app.yaml`:

```yaml
  - name: GENIE_WORKSPACES
    value: "Sales=https://adb-1111111111111111.11.azuredatabricks.net, Finance=https://adb-2222222222222222.2.azuredatabricks.net"
```

- Comma separated, each entry optionally `Label=<workspace URL>`. The label is the dropdown group name;
  without one, the first part of the hostname is used.
- `?o=…` and paths are ignored, so you can paste a workspace URL straight from the browser.
- Only these workspaces are searched. **Include the hosting workspace** if its agents should appear too.
- Leave it empty to search only the hosting workspace.

### 3. Create and deploy the app

```bash
PROFILE=<profile>                      # CLI profile for the hosting workspace
APP_DIR=/Workspace/Users/<your user name>/genie-picker

databricks apps create genie-picker -p $PROFILE
databricks sync . $APP_DIR -p $PROFILE
databricks apps deploy genie-picker --source-code-path $APP_DIR -p $PROFILE
```

The app doesn't need user authorization (on-behalf-of-user) scopes; leave them unset.

To change the workspace list later, edit `app.yaml` and re-run the `sync` and `deploy` commands.

### 4. Set up the app's service principal in each listed workspace

Databricks creates a service principal for the app. Find its application ID:

```bash
databricks apps get genie-picker -p $PROFILE -o json | jq -r .service_principal_client_id
```

In **every workspace listed in `GENIE_WORKSPACES`** (including the hosting workspace, if listed),
a workspace admin:

1. **Adds the service principal to the workspace** (not needed in the hosting workspace):
   *Settings → Identity and access → Service principals → Add service principal*, then search for
   the app's service principal (named after the app).
2. **Lets it read Genie agent permissions**, using one of the options in
   [Service principal permissions](#service-principal-permissions). The simplest is to add it to the
   workspace's `admins` group.
3. **Allows embedding from the app's domain:** *Settings → Security → External access → Embed
   dashboards → Allow approved domains*, then add the app's hostname from its URL, or a wildcard
   such as `*.azure.databricksapps.com` (Azure) or `*.aws.databricksapps.com` (AWS). This setting
   covers Genie agents too.

The service principal's calls go from the app to each listed workspace. If your workspaces
restrict outbound network access from Databricks Apps (serverless egress policies), allow the
listed workspaces as destinations.

### 5. Grant users access

Users need, in each workspace: workspace access, permission on the Genie agent, and access to the
agent's underlying data. These are the same permissions they need to use Genie directly.

### 6. Open the app

Open the app URL (`databricks apps get genie-picker -p $PROFILE`) and share it with your users
(*Compute → Apps → genie-picker → Permissions*, **Can use**).

---

## How it works

```
Browser ──► genie-picker app (FastAPI)            builds the dropdown
   │            └─ every listed workspace: app SERVICE PRINCIPAL checks which agents the user can access
   │
   └──► iframe ──► https://<agent workspace>/embed/genie/rooms/<id>
                    the chat itself: runs in the browser AS THE USER, never through the app
```

| Step | Identity used | Notes |
|---|---|---|
| Build the dropdown (every workspace in `GENIE_WORKSPACES`) | The **app's service principal**, plus the user's **email** | The service principal checks each agent's permissions against the user. The app holds no user token |
| Chat with an agent | The **user**, signed in to the agent's workspace | Loaded directly by the browser; queries run with the user's own data permissions |

The app does **not** use on-behalf-of-user (OBO) authorization.

### Where the user's identity and permissions are used

**The user's email (app server).** Databricks Apps signs the user in before any request reaches
the app, and passes their email in the `x-forwarded-email` header. Databricks sets this header;
the browser can't supply it. The app uses it **only to look the user up** in each workspace. It's an
identity, not a credential, and it can't call any API.

**The user's own Databricks session (browser).** The iframe loads the agent directly from its
workspace, so Genie uses the user's normal sign-in for **that** workspace. If they aren't signed in
there, Genie asks them to sign in (SSO). The app never sees or handles this session. This is where
all questions and answers happen, entirely under the user's identity: the SQL Genie runs, the rows
returned, and conversation history.

| What the user experiences | Decided by | Enforced by |
|---|---|---|
| Which agents appear in the dropdown | The user's (or their direct groups') permission on each Genie agent | The app, via the service principal's check |
| Whether they can open and use an agent | The same Genie agent permission, plus access to that workspace | **Databricks**, in the iframe, regardless of what the dropdown showed |
| Which data and answers they get | The user's Unity Catalog permissions on the agent's tables | **Databricks**, in the iframe. Two users asking the same agent can see different results |

The dropdown is a convenience filter. It controls **what users can find, not what they can
access**. Even if the app listed an agent by mistake, Databricks would still block a user
without permission.

---

## The app's service principal

### Why a service principal, not the user's token

A user token issued to a Databricks App **only works in the app's own workspace**; other
workspaces reject it. Listing a user's agents across workspaces needs an identity that works in
all of them. The service principal is used the same way in **every** workspace, so there's one
consistent filtering method and the app never holds a user token.

### What it does (read-only)

For each workspace listed in `GENIE_WORKSPACES`:

1. Signs in to that workspace (`POST /oidc/v1/token`, client credentials).
2. Looks up the user and their groups (`GET /api/2.0/preview/scim/v2/Users?filter=userName eq …`).
   If the user isn't in that workspace, it shows nothing from it.
3. Lists Genie spaces (`GET /api/2.0/genie/spaces`).
4. Reads each space's permissions (`GET /api/2.0/permissions/genie/<id>`) and keeps the spaces
   that name the user, or any of their groups, at any permission level.

### What it never does

- It never runs queries, reads tables or uses a SQL warehouse.
- It never takes part in the chat. The chat is the user's own Genie session in the iframe.
- It never creates, edits or shares Genie spaces, even when it's allowed to.

### Service principal permissions

Reading a Genie agent's permission list requires **CAN MANAGE** on that agent, or workspace
admin. That's the only reason the service principal needs elevated access. Pick one per workspace:

| Option | Setup | New agents appear automatically? | Privilege | Notes |
|---|---|---|---|---|
| **A. Workspace admin** | Add the service principal to the workspace's `admins` group | ✅ Yes | High: admin of the whole workspace | Simplest to operate |
| **B. Group with CAN MANAGE on each agent** | Put the service principal in an account-level group (e.g. `genie-hub`) that's assigned to each workspace, and share each agent that should appear with that group at CAN MANAGE | ❌ Only when someone shares each new agent with the group. **Databricks doesn't add it automatically** | Medium | Add "share with the group" to your agent publishing checklist. Missed agents silently don't appear |

Notes:
- In B, the group can also handle workspace membership. An account-level group assigned to
  a workspace makes its members (including the service principal) users of that workspace.
- If an agent grants CAN MANAGE to the built-in `users` group, any workspace member, including the
  service principal, can already read its permissions.
- An agent the service principal can't see or read is **silently left out** of the dropdown.
  Users can still open it directly in Genie if they have access.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "⚠ N workspaces unavailable" (hover for details) | The service principal couldn't reach or read that workspace. `401` on sign-in: the service principal isn't in the workspace. `403`: missing permissions. Timeout: outbound network access blocked |
| An agent is missing from the dropdown | The service principal can't read that agent's permissions (see [options A and B](#service-principal-permissions)), or the user isn't on it directly or through a direct group (see [Known limitations](#known-limitations)) |
| The dropdown shows only one agent, or very few | The service principal isn't admin in that workspace and can only read agents shared with `users` (see step 4) |
| The iframe asks the user to log in | Expected the first time per workspace per session. Use **Open in new tab ↗** if the sign-in pop-up doesn't return to the app |
| The iframe is blank or refuses to load | That workspace doesn't allow embedding from the app's domain (step 4.3) |

Logs: `databricks apps logs genie-picker -p $PROFILE`, or `<app URL>/logz`.

---

## Known limitations

### Nested groups are not followed

The app matches an agent's permission list against the user and their **direct** group
memberships only.

**Example:** an agent is shared with `finance-genie-users`, and that group contains the group
`fpa-analysts`. A user who is only in `fpa-analysts` has access to the agent, but
**the agent won't appear in their dropdown.**

- **Effect:** an agent is missing from the dropdown. Users never get access to something they
  shouldn't have, because Databricks enforces the real permissions, nested groups included, when the
  agent opens in the iframe. The user can still reach it through **Open in new tab** or a direct link.
- **Workaround:** share agents with the groups users are *direct* members of, rather than with parent groups.

### Sign-in inside the iframe

The embedded agent uses the user's Databricks session for its workspace. Browsers with strict
third-party-cookie settings can interfere with signing in inside an iframe. Test with your
organization's standard browser; **Open in new tab ↗** is the fallback.

---

## Local development

```bash
uv venv && uv pip install fastapi uvicorn httpx databricks-sdk
DATABRICKS_PROFILE=<profile> .venv/bin/uvicorn app:app --reload --port 8765
```

Without service principal credentials (`DATABRICKS_CLIENT_ID` / `DATABRICKS_CLIENT_SECRET`), your
CLI profile stands in for the service principal in that profile's workspace. Other workspaces show
as unavailable.
