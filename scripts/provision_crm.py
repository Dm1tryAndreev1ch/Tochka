"""Provision a limited Espo API user and indexed idempotency fields."""

import json
import subprocess
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent


def main():
    env = dotenv_values(ROOT / ".env")
    with httpx.Client(
        base_url=env["ESPO_URL"] + "/api/v1/",
        auth=("admin", env["CRM_ADMIN_PASSWORD"]),
        timeout=30,
        trust_env=False,
    ) as client:
        for attempt in range(60):
            try:
                response = client.get("App/user")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        else:
            raise RuntimeError("EspoCRM is not ready")
        for entity in ("Lead", "Call", "Task"):
            metadata = {
                "fields": {"cVoiceActionId": {"type": "varchar", "maxLength": 36}},
                "indexes": {"voiceActionId": {"columns": ["cVoiceActionId"], "unique": True}},
            }
            # The custom field is nullable, so manual CRM records remain supported.
            path = f"custom/Espo/Custom/Resources/metadata/entityDefs/{entity}.json"
            php = (
                "$p="
                + json.dumps(path)
                + "; @mkdir(dirname($p),0775,true); file_put_contents($p,stream_get_contents(STDIN));"
            )
            subprocess.run(
                ["docker", "compose", "exec", "-T", "crm", "php", "-r", php],
                input=json.dumps(metadata).encode(),
                cwd=ROOT,
                check=True,
            )
        subprocess.run(
            ["docker", "compose", "exec", "-T", "crm", "bin/command", "rebuild"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        access = {
            entity: {"read": "all", "create": "yes", "edit": "all", "delete": "no", "stream": "no"}
            for entity in ("Lead", "Call", "Task")
        }
        for entity in (
            "Account",
            "Contact",
            "Opportunity",
            "Case",
            "Meeting",
            "Campaign",
            "Document",
            "Email",
            "User",
        ):
            access[entity] = False
        # Espo validates assignedUser links through User.read, even for own user.
        # This grants no user writes, export or administration rights.
        access["User"] = {"read": "all", "create": "no", "edit": "no", "delete": "no"}
        existing = (
            client.get(
                "Role",
                params={
                    "where[0][type]": "equals",
                    "where[0][attribute]": "name",
                    "where[0][value]": "Voice Desk Integration",
                    "maxSize": 1,
                },
            )
            .json()
            .get("list", [])
        )
        body = {
            "name": "Voice Desk Integration",
            "data": access,
            "exportPermission": "no",
            "userPermission": "no",
            "assignmentPermission": "all",
            "massUpdatePermission": "no",
        }
        response = (
            client.put("Role/" + existing[0]["id"], json=body) if existing else client.post("Role", json=body)
        )
        response.raise_for_status()
        role = response.json()["id"]
        existing = (
            client.get(
                "User",
                params={
                    "where[0][type]": "equals",
                    "where[0][attribute]": "userName",
                    "where[0][value]": "voice-desk-api",
                    "maxSize": 1,
                },
            )
            .json()
            .get("list", [])
        )
        if existing:
            response = client.get("User/" + existing[0]["id"])
        else:
            response = client.post(
                "User",
                json={
                    "userName": "voice-desk-api",
                    "type": "api",
                    "isActive": True,
                    "authMethod": "ApiKey",
                    "rolesIds": [role],
                },
            )
        response.raise_for_status()
        data = response.json()
        key = data.get("apiKey")
        if not key:
            raise RuntimeError("CRM did not return the API key")
        text = (ROOT / ".env").read_text()
        import re

        text = re.sub(r"^ESPO_API_KEY=.*$", "ESPO_API_KEY=" + key, text, flags=re.M)
        (ROOT / ".env").write_text(text)
        user_response = client.get(
            "User",
            params={
                "where[0][type]": "equals",
                "where[0][attribute]": "userName",
                "where[0][value]": "admin",
                "maxSize": 1,
            },
        )
        user_response.raise_for_status()
        assigned_id = user_response.json()["list"][0]["id"]
        text = re.sub(r"^ESPO_ASSIGNED_USER_ID=.*\n?", "", text, flags=re.M)
        text += "\nESPO_ASSIGNED_USER_ID=" + assigned_id + "\n"
        (ROOT / ".env").write_text(text)
        (ROOT / ".env").chmod(0o600)
        print("EspoCRM API user ready (Lead / Call / Task only); key saved to .env")


if __name__ == "__main__":
    main()
