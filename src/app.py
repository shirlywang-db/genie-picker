"""Genie Picker: one page to pick a Genie agent and chat with it via the native embedded UI.

Agents are discovered across the workspaces listed in GENIE_WORKSPACES, and each user
only sees the Genie spaces they have access to. In every workspace, the app's service
principal lists the spaces and checks each space's permissions against the signed-in
user and their groups in that workspace. The app never holds a user token; the chat
itself runs in the iframe under the user's own Databricks sign-in.
"""

import asyncio
import logging
import os
import time
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

log = logging.getLogger("genie-picker")
logging.basicConfig(level=logging.INFO)

IS_DATABRICKS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def normalize_host(host: str) -> str:
    """'adb-123.4.azuredatabricks.net/?o=123' -> 'https://adb-123.4.azuredatabricks.net'"""
    host = host.strip()
    if not host.startswith("http"):
        host = f"https://{host}"
    return f"https://{urlparse(host).netloc}"


def local_client():
    """CLI-profile client, for running the app on a laptop."""
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient(profile=os.environ.get("DATABRICKS_PROFILE", "DEFAULT"))


def workspace_host() -> str:
    if IS_DATABRICKS_APP:
        # DATABRICKS_HOST in Databricks Apps is a bare hostname
        return normalize_host(os.environ["DATABRICKS_HOST"])
    return normalize_host(local_client().config.host)


def configured_workspaces() -> list[tuple[str, str]]:
    """(label, host) pairs from GENIE_WORKSPACES, e.g. "Sales=https://adb-1.azuredatabricks.net, https://adb-2...".

    Entries are separated by commas, semicolons or newlines (semicolons survive
    `databricks bundle deploy --var`, which splits on commas); the "Label=" prefix is
    optional. "current" stands for the app's own workspace, which is also the default.
    """
    raw = os.environ.get("GENIE_WORKSPACES", "").replace("\n", ",").replace(";", ",")

    def host_of(value: str) -> str:
        return workspace_host() if value.strip().lower() == "current" else normalize_host(value)

    entries = []
    for item in filter(None, (i.strip() for i in raw.split(","))):
        label, sep, rest = item.partition("=")
        if sep and "://" not in label and "." not in label:
            entries.append((label.strip(), host_of(rest)))
        else:
            entries.append(("", host_of(item)))
    if not entries:
        entries = [("", workspace_host())]
    return [(label or urlparse(host).netloc.split(".")[0], host) for label, host in entries]


def user_email(request: Request) -> str:
    """The signed-in user's email, set by the Databricks Apps proxy (not by the browser)."""
    email = request.headers.get("x-forwarded-email")
    if email:
        return email
    if IS_DATABRICKS_APP:
        raise HTTPException(401, "Missing x-forwarded-email header.")
    return local_client().current_user.me().user_name


_sp_tokens: dict[str, tuple[str, float]] = {}


async def sp_token(client: httpx.AsyncClient, host: str) -> str:
    """OAuth M2M token for the app's service principal in `host`."""
    cached = _sp_tokens.get(host)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    client_id, secret = os.environ.get("DATABRICKS_CLIENT_ID"), os.environ.get("DATABRICKS_CLIENT_SECRET")
    if not (client_id and secret):
        if not IS_DATABRICKS_APP and host == workspace_host():
            # Local dev without service principal credentials: stand in with the CLI profile
            return local_client().config.authenticate()["Authorization"].removeprefix("Bearer ")
        raise RuntimeError("no service principal credentials (DATABRICKS_CLIENT_ID/SECRET)")
    resp = await client.post(
        f"{host}/oidc/v1/token",
        data={"grant_type": "client_credentials", "scope": "all-apis"},
        auth=(client_id, secret),
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"service principal can't sign in to {host} ({resp.status_code}: {resp.text[:200]}); is it added to that workspace?"
        )
    body = resp.json()
    _sp_tokens[host] = (body["access_token"], time.time() + body.get("expires_in", 3600))
    return body["access_token"]


async def get_json(client: httpx.AsyncClient, url: str, token: str, **params) -> dict:
    resp = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
    if resp.status_code != 200:
        raise RuntimeError(f"GET {urlparse(url).path} returned {resp.status_code}: {resp.text[:200]}")
    return resp.json()


async def list_spaces(client: httpx.AsyncClient, host: str, token: str) -> list[dict]:
    spaces, page_token = [], None
    while True:
        params = {"page_size": 100, **({"page_token": page_token} if page_token else {})}
        body = await get_json(client, f"{host}/api/2.0/genie/spaces", token, **params)
        spaces += body.get("spaces", [])
        page_token = body.get("next_page_token")
        if not page_token:
            return spaces


async def spaces_for_user(client: httpx.AsyncClient, host: str, email: str) -> list[dict]:
    """Spaces in `host` that `email` can access, as seen by the app's service principal.

    Matches the user and their direct groups against each space's permissions; access
    through nested groups is not followed.
    """
    token = await sp_token(client, host)
    users = await get_json(
        client, f"{host}/api/2.0/preview/scim/v2/Users", token,
        filter=f'userName eq "{email}"', attributes="userName,groups",
    )
    if not users.get("Resources"):
        return []  # user isn't assigned to this workspace
    # Every workspace member is implicitly in "users"; SCIM doesn't list it
    groups = {g["display"] for g in users["Resources"][0].get("groups", [])} | {"users"}

    async def can_access(space: dict) -> bool:
        acl = await get_json(client, f"{host}/api/2.0/permissions/genie/{space['space_id']}", token)
        return any(
            e.get("user_name", "").lower() == email.lower() or e.get("group_name") in groups
            for e in acl.get("access_control_list", [])
        )

    spaces = await list_spaces(client, host, token)
    allowed = await asyncio.gather(*(can_access(s) for s in spaces), return_exceptions=True)
    for s, ok in zip(spaces, allowed):
        if isinstance(ok, Exception):
            log.warning("Skipping space %s in %s: %s", s["space_id"], host, ok)
    return [s for s, ok in zip(spaces, allowed) if ok is True]


app = FastAPI(title="Genie Picker")


@app.on_event("startup")
async def log_workspaces():
    log.info("Searching Genie agents in: %s", ", ".join(f"{label} ({host})" for label, host in configured_workspaces()))


@app.get("/api/spaces")
async def get_spaces(request: Request):
    email = user_email(request)
    workspaces = configured_workspaces()
    async with httpx.AsyncClient(timeout=30) as client:
        results = await asyncio.gather(
            *(spaces_for_user(client, host, email) for _, host in workspaces), return_exceptions=True
        )

    agents, unavailable = [], []
    for (label, host), result in zip(workspaces, results):
        if isinstance(result, Exception):
            log.warning("Could not list Genie spaces in %s: %s", host, result)
            unavailable.append({"workspace": label, "error": str(result)})
            continue
        for s in sorted(result, key=lambda s: (s.get("title") or "").lower()):
            agents.append(
                {
                    "id": s["space_id"],
                    "title": s.get("title") or s["space_id"],
                    "description": s.get("description", ""),
                    "workspace": label,
                    "embed_url": f"{host}/embed/genie/rooms/{s['space_id']}",
                    "open_url": f"{host}/genie/rooms/{s['space_id']}",
                }
            )
    return {"user": email, "agents": agents, "unavailable": unavailable}


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))
