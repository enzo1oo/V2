# grc-tracker

Logs GRC requests from Gmail and Google Chat into the GRC Google Sheet, proposes an owner by fixed
rules (never by AI), and emails the doer once the manager approves. Runs on Cloud Run for BRI
Global Financial Services, Hong Kong.

- **Spec (source of truth):** [`docs/DESIGN.md`](docs/DESIGN.md). Section 15 lists every change
  made to v1.0 during the build.
- **Business context:** `docs/PRD.docx`.

## How it works

```
Gmail label "GRC/To log" ─┐                         ┌─> Master row (status Proposed) + AuditLog
                          ├─> process_request() ────┤
Google Chat message ──────┘   clean · rules · (AI)  └─> Drive folder "G-0012 – <title>"
                              · route · write
Manager sets Status ──> /jobs/changes (every 5 min) ──> assignment email to the Doer
Cloud Scheduler ──> /jobs/recurring (07:00) · /jobs/digest (weekdays 09:00) · /jobs/gmail (10 min)
Hand edits in Master ──> Apps Script trigger ──> AuditLog, Last updated, "Done needs proof"
```

| Path | What it is |
|---|---|
| `app/main.py` | FastAPI routes: `/jobs/*` (Scheduler token), `/chat` (Chat token), `/healthz` |
| `app/pipeline.py` | `process_request`: the one path every request takes |
| `app/extract/` | cleaning, rule extraction, AI providers (`ai.py`) |
| `app/route.py` | routing rules 1–5 |
| `app/sheets.py`, `app/drive.py`, `app/gmail.py`, `app/chat.py` | Google APIs behind small classes |
| `app/jobs.py` | change detection, recurring reports, digests |
| `app/auth.py`, `app/logs.py`, `app/alerts.py` | token checks, JSON logs, alert emails |
| `apps_script/audit.gs` | edit trigger for the sheet (installed by hand, step 10) |
| `scripts/mint_gmail_token.py` | one-time Gmail consent for the intake mailbox |
| `tests/` | all tests; Google services are faked, nothing touches the network |

## Local development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Tests run with the clock frozen at Monday 26 Oct 2026 09:00 HKT. The Apps Script tests run through
`pytest` when Node.js is installed (`node --test tests/apps_script/` runs them directly).

To run against the **dev** sheet, create a `.env` (never committed) with the variables below, sign
in with `gcloud auth application-default login` as an account that can edit the dev sheet and dev
Drive folder, then:

```bash
.venv/bin/uvicorn app.main:app --reload --env-file .env
```

Locally there is no Scheduler or Chat token, so `/jobs/*` and `/chat` answer 401; call the job
functions from a Python shell instead, or test through the deployed dev service.

### Environment variables (IDs only, never secrets)

| Variable | Example | Used for |
|---|---|---|
| `GCP_PROJECT` | `bri-hk-grc` | Secret Manager, Vertex AI |
| `GCP_REGION` | `asia-east2` | informational |
| `SHEET_ID` | `1AbC…` (from the sheet URL) | the GRC sheet |
| `SHARED_DRIVE_FOLDER_ID` | `0AbC…` | parent folder for task folders |
| `GMAIL_TOKEN_SECRET` | `grc-gmail-refresh-token` | secret name: Gmail refresh token |
| `OAUTH_CLIENT_SECRET` | `grc-gmail-oauth-client` | secret name: OAuth client JSON |
| `SCHEDULER_SA_EMAIL` | `grc-tracker-scheduler@<project>.iam.gserviceaccount.com` | who may call `/jobs/*` |
| `SERVICE_URL` | `https://grc-tracker-xxxx.a.run.app` | expected Scheduler token audience |
| `CHAT_PROJECT_NUMBER` | `123456789012` | expected Chat token audience |
| `TZ` | `Asia/Hong_Kong` | |

If `SERVICE_URL`, `SCHEDULER_SA_EMAIL` or `CHAT_PROJECT_NUMBER` is empty, every protected call is
rejected (fail closed).

## Go-live checklist (walk through with IT)

Names below are suggestions; keep them consistent with `cloudbuild.yaml` substitutions.

### 1. Sheet
The service refuses to run until the sheet matches DESIGN section 3 and reports **every** gap in
one alert. Against `GRC_Tracker_Tabs_v2.xlsx` that means:
- [ ] Master: add columns `Original message`, `Files`, `AI summary`, `Deliverables`, `Contacts`,
      `Source ID` (at the right, so the Staff formulas keep working). Keep one data row with the
      `Weight` formula; delete the `[EXAMPLE]` rows only after the first real task exists.
- [ ] Catalog: add `Known contacts`; set `Confirmed by manager?` = Yes on the reports that should
      be created automatically (none are today, so `/jobs/recurring` creates nothing).
- [ ] Staff: fill `Email` for everyone (no email = no assignment emails), `Role` (blank counts as
      Staff, so mark the manager **Manager**), and interns' `Internship end date`.
- [ ] Config: fill `Manager email`, `Allowed email domain` (blank = the Chat bot refuses
      everyone); add `AI provider` (`Off` to start), `AI model`, `AI region`, `Sender name`,
      `Mask emails for AI`, `Requester domains` (e.g. `bri.co.id=BRI`), `Known requesters`
      (e.g. `BRI, OJK, HKMA`), `Builder email`.
- [ ] Create hidden tabs `_state` (`ID, Status, Owner, Doer, Notified doer, Notified at`) and
      `_failures` (`Key, Count, Last error, Last at`).
- [ ] Protect `AuditLog`, `_state`, `_failures`: editors = the runtime service account and the
      person who installs the Apps Script trigger.
- [ ] Recommended: data validation on Master `Status` with the custom formula
      `=OR(V2<>"Done",AND(Q2<>"",X2<>""))` (adjust letters to Status/Submitted date/Link) as a
      second guard next to the Apps Script check.

### 2. Google Cloud project and APIs
```bash
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com secretmanager.googleapis.com sheets.googleapis.com \
  drive.googleapis.com gmail.googleapis.com chat.googleapis.com aiplatform.googleapis.com
```
```bash
gcloud artifacts repositories create grc --repository-format=docker --location=asia-east2
```

### 3. Service accounts and roles (least privilege, DESIGN section 9)
```bash
gcloud iam service-accounts create grc-tracker-run --display-name="GRC tracker runtime"
```
```bash
gcloud iam service-accounts create grc-tracker-scheduler --display-name="GRC tracker scheduler"
```
- [ ] `grc-tracker-run`: Secret Manager Secret Accessor **on the two secrets only**; Vertex AI
      User (only if `AI provider = Vertex`); share the GRC sheet with it as **Editor**; add it to
      the Shared Drive as **Content manager**.
- [ ] `grc-tracker-scheduler`: no project roles needed (the app checks its token itself);
      optionally `roles/run.invoker` on the service.
- [ ] Cloud Build service account: Cloud Run Admin, Artifact Registry Writer, and Service Account
      User on `grc-tracker-run`.

### 4. Org policy
- [ ] Google Chat cannot send an IAM token, so the service is deployed with
      `--allow-unauthenticated` and checks every token in the app. If the organisation enforces
      *Domain restricted sharing* (`iam.allowedPolicyMemberDomains`), IT needs an exception for this
      service, or Chat intake is blocked (Gmail and the timed jobs still work).

### 5. Shared Drive
- [ ] Create (or pick) the folder for task folders and note its ID → `SHARED_DRIVE_FOLDER_ID`.

### 6. Gmail intake mailbox (DESIGN D3)
- [ ] Decide option A (manager's mailbox) or B (e.g. `grc-intake@`).
- [ ] OAuth consent screen: **Internal** (an external app in Testing expires refresh tokens after
      7 days). Create an OAuth client of type *Desktop app*; download its JSON.
- [ ] Workspace admin: allow this OAuth client for `gmail.modify` and `gmail.send` (API controls).
- [ ] Store the client JSON:
```bash
gcloud secrets create grc-gmail-oauth-client --data-file=oauth_client.json
```
- [ ] On a trusted laptop, signed in as the intake mailbox, mint the refresh token (it is printed,
      never saved), then store it and clear the terminal:
```bash
pip install google-auth-oauthlib && python scripts/mint_gmail_token.py oauth_client.json
```
```bash
printf '%s' '<refresh-token>' | gcloud secrets create grc-gmail-refresh-token --data-file=-
```
- [ ] In the mailbox, create the label `GRC/To log` (`GRC/Logged` and `GRC/Failed` are created
      automatically).

### 7. Deploy
The first deploy has no URL yet, so deploy twice:
```bash
gcloud builds submit --config cloudbuild.yaml --substitutions=_SHEET_ID=<sheet-id>,_SHARED_DRIVE_FOLDER_ID=<folder-id>,_SERVICE_URL=pending,_CHAT_PROJECT_NUMBER=<project-number>
```
```bash
gcloud run services describe grc-tracker --region asia-east2 --format='value(status.url)'
```
```bash
gcloud builds submit --config cloudbuild.yaml --substitutions=_SHEET_ID=<sheet-id>,_SHARED_DRIVE_FOLDER_ID=<folder-id>,_SERVICE_URL=<url-from-above>,_CHAT_PROJECT_NUMBER=<project-number>
```
The build runs ruff and the tests first and stops on any failure. It deploys with
`max-instances=1`, `min-instances=0` and **CPU always allocated** (slow Chat requests finish in
the background).

### 8. Cloud Scheduler (all times Asia/Hong_Kong)
Every job posts to the service with an OIDC token whose **audience is the service URL itself**
(not the job path). Replace `<url>` and `<project>`:
```bash
for job in "gmail|*/10 * * * *" "changes|*/5 8-19 * * *" "recurring|0 7 * * *" "digest|0 9 * * 1-5"; do
  name="${job%%|*}"; cron="${job#*|}"
  gcloud scheduler jobs create http "grc-$name" --location=asia-east2 --schedule="$cron" \
    --time-zone="Asia/Hong_Kong" --http-method=POST --uri="<url>/jobs/$name" \
    --oidc-service-account-email="grc-tracker-scheduler@<project>.iam.gserviceaccount.com" \
    --oidc-token-audience="<url>" --attempt-deadline=300s
done
```
(`*/5 8-19` runs 08:00–19:55; DESIGN says 08:00–20:00.)

### 9. Google Chat app
In the Cloud console → Google Chat API → Configuration:
- [ ] App name, avatar, description; enable *Receive 1:1 messages* and *Join spaces*.
- [ ] Connection settings: **HTTP endpoint URL** = `<url>/chat`.
- [ ] Authentication audience: **Project number** (→ `CHAT_PROJECT_NUMBER`).
- [ ] Visibility: only the GRC team (people or a group in the domain).
- [ ] Use the classic Chat app (HTTP) setup, not a Workspace add-on; the bot expects that event
      format.

### 10. Apps Script audit trigger (DESIGN D8)
- [ ] Open the sheet → Extensions → Apps Script → paste `apps_script/audit.gs` → save.
- [ ] Run `installAuditTrigger` once as a person who can edit the protected AuditLog; accept the
      permissions. Do not rename `onMasterEdit` to `onEdit`.

### 11. Monitoring
- [ ] Log-based alert policies (Cloud Monitoring) on `jsonPayload.message` =
      `gmail_auth_error` (the broken mailbox cannot email its own alert), `schema_error`,
      `request_gave_up`. Alert emails for the last two are also sent by the service itself, at
      most once an hour per kind, to `Manager email` and `Builder email`.

### 12. Smoke test
- [ ] `curl <url>/healthz` → `{"status":"ok"}`; `curl -X POST <url>/jobs/gmail` → 401.
- [ ] Run `grc-changes` once from the Scheduler console (first run only records `_state`).
- [ ] Label a test email `GRC/To log`; run `grc-gmail`; a `Proposed` row and a Drive folder appear,
      the email moves to `GRC/Logged`.
- [ ] Set that row to `Not Started`; run `grc-changes`; the Doer gets the assignment email.
- [ ] Message the Chat bot `help`, then paste a request.
- [ ] Set a row to `Done` without proof in the sheet: it flips back and AuditLog shows `Blocked`.

## Switching the AI provider

Set Config `AI provider`; no redeploy is needed (read at every job run):
- `Off` — rules only (default; everything works, titles are the message's first sentence).
- `Vertex` — Gemini on Vertex AI: set `AI model` (e.g. `gemini-2.5-flash`) and `AI region`
  (check the model is offered there and that the region meets data-residency rules), and give the
  runtime service account Vertex AI User. Masked text and file names only are sent; never files.
- `BRI agent` — stub until the agent's API is known; requests are flagged `AI unavailable`.

## Operations

- **Logs:** Cloud Run → Logs. JSON entries carry task IDs, counts, timings and error types only;
  `app/logs.py` drops any other text field by design.
- **A request in `GRC/Failed`:** it failed 3 times. Fix the cause (see logs), then put the label
  `GRC/To log` back; the next run retries it (a `Source ID` already in Master is never logged
  twice).
- **Re-running a job** is always safe: rows are keyed by `Source ID`, emails by `_state`.
