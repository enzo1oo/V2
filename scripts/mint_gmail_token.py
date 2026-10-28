"""One-time OAuth consent for the intake mailbox; prints the refresh token for Secret Manager.

Run this on a trusted laptop, signed in to the browser as the intake mailbox (option A: the
manager's own mailbox; option B: e.g. grc-intake@). Nothing is written to disk.

    pip install google-auth-oauthlib        # only needed for this script
    python scripts/mint_gmail_token.py path/to/oauth_client.json

Then IT stores the printed token as the secret named by GMAIL_TOKEN_SECRET, e.g.:

    printf '%s' '<token>' | gcloud secrets versions add <secret-name> --data-file=-

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
    print("\nRefresh token (store it in Secret Manager, then clear your terminal):\n")
    print(credentials.refresh_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
