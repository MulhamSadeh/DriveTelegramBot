"""Authorize the bot's Drive account locally or build its Drive client."""
import json
import os
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def authorize():
    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        raise RuntimeError("Google did not issue a refresh token. Run authorization again.")
    # Replace the old credentials only after successful consent.
    with open("token.json", "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as f:
        f.write(creds.to_json())
    return creds


def drive_service():
    raw = os.environ.get("GOOGLE_TOKEN_JSON")
    if raw:
        creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    elif Path("token.json").exists():
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    else:
        creds = authorize()
    # Discovery is bundled. The authorized transport refreshes at request time.
    return build("drive", "v3", credentials=creds, static_discovery=True)


if __name__ == "__main__":
    authorize()
    print("Saved token.json. Set GOOGLE_TOKEN_JSON on the bot service and redeploy.")
