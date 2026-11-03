"""One-time OAuth consent for the GRC bot mailbox; prints the refresh token for Secret Manager.

Run this on a trusted laptop, signed in to the browser **as the bot mailbox** (the dedicated
Workspace account, e.g. grc-bot@<company domain>; DESIGN D3), never as a person. Nothing is
written to disk.

    pip install google-auth-oauthlib        # only needed for this script
    python scripts/mint_gmail_token.py path/to/oauth_client.json

It prints the mailbox the token belongs to, then the token. Store the token as a new version of
the secret named by GMAIL_TOKEN_SECRET: in the Cloud console (Security > Secret Manager > the
secret > New version, paste, Add) or with `gcloud secrets versions add`.

The OAuth client must belong to an *Internal* consent screen (DESIGN D3): an external app in
"Testing" status gets refresh tokens that expire after 7 days.
"""

from __future__ import annotations

import sys

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(argv[1], scopes=SCOPES)
    credentials = flow.run_local_server(
        port=0, access_type="offline", prompt="consent", open_browser=True
    )
    if not credentials.refresh_token:
        print("No refresh token returned. Revoke the app's access and run again.")
        return 1
    mailbox = _mailbox(credentials.token)
    print(f"\nThis token reads and sends as: {mailbox}")
    print("If that is not the bot mailbox, do NOT store it: run again in a private browser")
    print("window and sign in as the bot.\n")
    print("Refresh token: copy only the next line (it starts with 1//), store it in Secret")
    print("Manager as a new version of the Gmail token secret, then clear your terminal:\n")
    print(credentials.refresh_token)
    return 0


def _mailbox(access_token: str) -> str:
    """The address the token belongs to, so a token minted as the wrong account is caught here."""
    import json
    import urllib.request

    request = urllib.request.Request(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())["emailAddress"]
    except Exception as error:  # the token is still valid; only the check failed
        return f"(could not check: {type(error).__name__})"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
