"""Create or update the Render web service that hosts this app.

Reads the settings already in .env and copies the hosted ones to Render, so there is no
second place to keep them in step. Run it again after changing .env to push the new values.

The service is deliberately a single instance: automatic lookup is a background thread and
saves rewrite the whole workbook, so a second instance would race the first.

`UH_TRUSTED_HOSTS` can only be known once Render has assigned a hostname, so the service is
created first and the variables are written afterwards, which also triggers the real deploy.
No secret value is ever printed; only the names of the variables that were sent.
"""
import os
import re

import requests

API = "https://api.render.com/v1"
TIMEOUT = (5, 30)
NAME = os.getenv("RENDER_SERVICE_NAME", "uh-selections")
REPO = "https://github.com/SaiSowjanya9/UH-Template"
BRANCH = "main"
BUILD = "pip install -r requirements.txt"
START = "waitress-serve --listen=0.0.0.0:$PORT --threads=8 wsgi:application"

# Copied from .env when present. Anything empty is skipped rather than sent blank.
COPIED = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_ANON_KEY", "SUPABASE_BUCKET",
          "UH_WORKBOOK_NAME", "UH_SECRET_KEY", "SEARCH_PROVIDER", "SERPAPI_API_KEY",
          "SERPAPI_KEY", "BRAVE_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_MODEL"]
# Without these the app would either refuse to start or have nothing to serve.
REQUIRED = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_ANON_KEY", "UH_SECRET_KEY"]


class DeployError(RuntimeError):
    pass


class Render:
    def __init__(self, token, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json",
                                     "User-Agent": "UH-Homes-Selections/1.0"})

    def _call(self, method, path, action, **kwargs):
        try:
            response = self.session.request(method, f"{API}{path}", timeout=TIMEOUT, **kwargs)
        except requests.RequestException:
            raise DeployError(f"Could not reach Render to {action}.") from None
        if response.status_code == 401:
            raise DeployError("Render rejected the API key. Check RENDER_API_KEY in .env.")
        if response.status_code == 402:
            raise DeployError("Render requires payment details for this plan. Use the free plan, "
                              "or add a card in the Render dashboard.")
        if response.status_code >= 400:
            message = ""
            try:
                message = (response.json() or {}).get("message", "")
            except ValueError:
                message = ""
            raise DeployError(f"Render could not {action} (HTTP {response.status_code}). {message}".strip())
        return response.json() if response.content else {}

    def workspace(self, email=None):
        owners = self._call("GET", "/owners", "list your workspaces",
                            params={"email": email} if email else None)
        found = [entry["owner"] for entry in owners if entry.get("owner")]
        if not found:
            raise DeployError("That Render API key can see no workspaces.")
        if len(found) > 1:
            names = ", ".join(f"{o['name']} <{o['email']}>" for o in found)
            raise DeployError(f"The key can see several workspaces ({names}). Re-run with --owner-email.")
        return found[0]

    def find_service(self, name, owner_id):
        services = self._call("GET", "/services", "look for an existing service",
                              params={"name": name, "type": "web_service", "ownerId": owner_id})
        for entry in services:
            service = entry.get("service") or entry
            if service.get("name") == name:
                return service
        return None

    def create_service(self, owner_id, region, plan):
        body = {"type": "web_service", "name": NAME, "ownerId": owner_id, "repo": REPO,
                "branch": BRANCH,
                "serviceDetails": {"runtime": "python", "plan": plan, "region": region,
                                   "healthCheckPath": "/healthz", "numInstances": 1,
                                   "envSpecificDetails": {"buildCommand": BUILD, "startCommand": START}}}
        created = self._call("POST", "/services", "create the web service", json=body)
        return created.get("service") or created

    def set_env_vars(self, service_id, variables):
        body = [{"key": key, "value": value} for key, value in variables.items()]
        self._call("PUT", f"/services/{service_id}/env-vars", "set the environment variables", json=body)


def hostname_of(service):
    url = ((service.get("serviceDetails") or {}).get("url") or "").strip()
    if url:
        return re.sub(r"^https?://", "", url).rstrip("/")
    slug = service.get("slug") or NAME
    return f"{slug}.onrender.com"


def hosted_variables(hostname):
    """The environment the hosted app needs, taken from .env plus the two host-specific values."""
    missing = [key for key in REQUIRED if not (os.getenv(key) or "").strip()]
    if missing:
        raise DeployError("Fill these in .env before deploying: " + ", ".join(missing))
    variables = {key: os.environ[key].strip() for key in COPIED if (os.getenv(key) or "").strip()}
    variables["UH_HOST"] = "0.0.0.0"            # a non-local bind, so the app demands sign-in
    variables["UH_TRUSTED_HOSTS"] = hostname
    variables["PYTHON_VERSION"] = os.getenv("RENDER_PYTHON_VERSION", "3.12")
    return variables


def deploy(token=None, owner_email=None, region="oregon", plan="free", session=None):
    token = token or (os.getenv("RENDER_API_KEY") or "").strip()
    if not token:
        raise DeployError("Add RENDER_API_KEY to .env (Render dashboard -> Account Settings -> API Keys).")
    api = Render(token, session=session)
    workspace = api.workspace(owner_email)
    service = api.find_service(NAME, workspace["id"])
    created = service is None
    if created:
        service = api.create_service(workspace["id"], region, plan)
    hostname = hostname_of(service)
    variables = hosted_variables(hostname)
    api.set_env_vars(service["id"], variables)
    return {"workspace": workspace["name"], "service_id": service["id"], "created": created,
            "hostname": hostname, "url": f"https://{hostname}", "variables": sorted(variables)}


if __name__ == "__main__":
    import argparse

    import common     # loads .env

    parser = argparse.ArgumentParser(description="Create or update the Render web service for this app.")
    parser.add_argument("--owner-email", help="pick a workspace when the key can see several")
    parser.add_argument("--region", default="oregon",
                        choices=["oregon", "ohio", "virginia", "frankfurt", "singapore"],
                        help="match your Supabase project's region")
    parser.add_argument("--plan", default="free", choices=["free", "starter"],
                        help="free sleeps when idle; starter stays awake")
    args = parser.parse_args()
    try:
        result = deploy(owner_email=args.owner_email, region=args.region, plan=args.plan)
    except DeployError as error:
        raise SystemExit(f"\n{error}\n")
    print(f"\nWorkspace:  {result['workspace']}")
    print(f"Service:    {result['service_id']} ({'created' if result['created'] else 'already existed'})")
    print(f"URL:        {result['url']}")
    print(f"Variables:  {', '.join(result['variables'])}")
    print("\nRender is building now; the first deploy takes a few minutes.")
    print(f"When it is live, open {result['url']} and sign in.")
