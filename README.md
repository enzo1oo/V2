# grc-tracker

Logs GRC requests from Gmail and Google Chat into the GRC Google Sheet, proposes an owner by fixed
rules (never by AI), and emails the doer once the manager approves. Runs on Cloud Run for BRI
Global Financial Services, Hong Kong.

- **Spec (source of truth):** [`docs/DESIGN.md`](docs/DESIGN.md) (v1.2). Sections 15 and 16 list
  the decisions made during the build.
- **Business context:** `docs/PRD.docx`.

## How it works

```
Email to the bot mailbox ─┐                         ┌─> Master row (status Proposed) + AuditLog
 (allowed senders only)   ├─> process_request() ────┤   + reply to the sender in the thread
Google Chat message ──────┘   clean · rules · (AI)  └─> Drive folder "G-0012 – <title>"
                              · route · write
Manager sets Status ──> /jobs/changes (every 5 min) ──> assignment email to the Doer
Cloud Scheduler ──> /jobs/recurring (07:00) · /jobs/digest (weekdays 09:00) · /jobs/gmail (10 min)
Hand edits in Master ──> Apps Script trigger ──> AuditLog, Last updated, "Done needs proof"
```

| Path | What it is |
|---|---|
| `app/main.py` | FastAPI routes: `/jobs/*` (Scheduler token), `/chat` (Chat token), `/health` |
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

### Live check against the real services

Copy `.env.example` to `.env` and fill it in (it is git-ignored), sign in for Sheets and Drive as
the runtime service account, then run the read-only check:

```bash
gcloud auth application-default login --impersonate-service-account=grc-tracker-run@$(gcloud config get-value project).iam.gserviceaccount.com
```
```bash
python scripts/live_check.py
```

It checks the sheet (tabs, columns, Config, Staff), the bot mailbox, one Gemini call with a fake
message, and the Drive folder, and never prints a secret. Steps that write are opt-in:
`--write-drive` (creates and deletes a test folder), `--send-to <address>` (one test email) and
`--gmail-once` (real intake of one unread email: writes to the sheet, so use the dev sheet).

### Environment variables (IDs only, never secrets)

| Variable | Example | Used for |
|---|---|---|
| `GCP_PROJECT` | `bri-hk-grc` | Secret Manager, Vertex AI |
| `GCP_REGION` | `asia-southeast2` | informational |
| `SHEET_ID` | `1AbC…` (from the sheet URL) | the GRC sheet |
| `SHARED_DRIVE_FOLDER_ID` | `0AbC…` | parent folder for task folders (only with `File storage` = `Drive`) |
| `GMAIL_TOKEN_SECRET` | `grc-gmail-refresh-token` | secret name: Gmail refresh token |
| `OAUTH_CLIENT_SECRET` | `grc-gmail-oauth-client` | secret name: OAuth client JSON |
| `GEMINI_KEY_SECRET` | `grc-gemini-api-key` | secret name: Gemini API key (AI Studio) |
| `SCHEDULER_SA_EMAIL` | `grc-tracker-scheduler@<project>.iam.gserviceaccount.com` | who may call `/jobs/*` |
| `SERVICE_URL` | `https://grc-tracker-xxxx.a.run.app` | expected Scheduler token audience |
| `CHAT_PROJECT_NUMBER` | `123456789012` | expected Chat token audience |
| `TZ` | `Asia/Hong_Kong` | |

If `SERVICE_URL`, `SCHEDULER_SA_EMAIL` or `CHAT_PROJECT_NUMBER` is empty, every protected call is
rejected (fail closed).

**Gemini key locally:** put `GEMINI_API_KEY=...` in `.env` (git-ignored); it takes precedence over
`GEMINI_KEY_SECRET`. Never commit it, paste it into chats or tickets, or pass it on a command line.
To check the key, model and region with one live call (fake message), run from Cloud Shell or the
Jakarta service, not from Hong Kong:

```bash
python scripts/smoke_gemini.py
```

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
      everyone); add **`Allowed senders`** (who may email the bot, e.g.
      `manager@bri.co.id, @bri.co.id`; blank = every email is ignored), `AI provider`
      (`Gemini API key`, `Vertex` or `Off`; start with `Off`), `AI model` (blank =
      `gemini-3.8-flash`), `AI region` (Vertex mode only), `Sender name`, `Mask emails for AI`,
      `Requester domains` (e.g. `bri.co.id=BRI`), `Known requesters` (e.g. `BRI, OJK, HKMA`),
      `Builder email`, `File storage` (`None` = no Drive, the default; `Drive` = task folders).
      `Gmail intake label` is no longer used and can be deleted.
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
The Artifact Registry repo `cloud-run-source-deploy` in `asia-southeast2` was created when Cloud
Run was connected to the repo; `cloudbuild.yaml` pushes there (`_REPO`).

### 3. Service accounts and roles (least privilege, DESIGN section 9)
```bash
gcloud iam service-accounts create grc-tracker-run --display-name="GRC tracker runtime"
```
```bash
gcloud iam service-accounts create grc-tracker-scheduler --display-name="GRC tracker scheduler"
```
- [ ] `grc-tracker-run`: Secret Manager Secret Accessor **on the three secrets only** (Gmail
      token, OAuth client, Gemini key); Vertex AI User (only if `AI provider = Vertex`); share the
      GRC sheet with it as **Editor**; add it to the Shared Drive as **Content manager**.
- [ ] `grc-tracker-scheduler`: no project roles needed (the app checks its token itself);
      optionally `roles/run.invoker` on the service.
- [ ] Cloud Build service account: Cloud Run Admin, Artifact Registry Writer, and Service Account
      User on `grc-tracker-run`.

### 4. Org policy
- [ ] Google Chat cannot send an IAM token, so the service is deployed with
      `--allow-unauthenticated` and checks every token in the app. If the organisation enforces
      *Domain restricted sharing* (`iam.allowedPolicyMemberDomains`), IT needs an exception for this
      service, or Chat intake is blocked (Gmail and the timed jobs still work).

### 5. Shared Drive (only if Config `File storage` = `Drive`)
- [ ] Create (or pick) the folder for task folders and note its ID → `SHARED_DRIVE_FOLDER_ID`.
      With `File storage` = `None` (default) skip this: attachments stay in the archived email in
      the bot mailbox and the sheet's `Files` column lists their names.

### 6. Bot mailbox (DESIGN D3, D4, D16)
- [ ] IT creates a **dedicated Google Workspace account** on the company domain, e.g.
      `grc-bot@<company domain>`, with a Gmail licence. Never a personal @gmail.com and never a
      person's own mailbox.
- [ ] OAuth consent screen: **Internal** (an external app in Testing expires refresh tokens after
      7 days). Create an OAuth client of type *Desktop app*; download its JSON.
- [ ] Workspace admin: allow this OAuth client for `gmail.modify` and `gmail.send` (API controls).
- [ ] Store the client JSON:
```bash
gcloud secrets create grc-gmail-oauth-client --data-file=oauth_client.json
```
- [ ] **One-time consent as the bot:** on a trusted laptop, in a browser signed in as the bot
      account (not as a person), mint the refresh token (it is printed, never saved), store it,
      then clear the terminal:
```bash
pip install google-auth-oauthlib && python scripts/mint_gmail_token.py oauth_client.json
```
```bash
printf '%s' '<refresh-token>' | gcloud secrets create grc-gmail-refresh-token --data-file=-
```
- [ ] Fill Config `Allowed senders`. No labels need to be created: `GRC/Logged`, `GRC/Ignored`,
      `GRC/Reply` and `GRC/Failed` are created by the service.
- [ ] Tell the team: forward or send requests to the bot address; the sender gets a reply in the
      same thread with the task ID. Replies in that thread are added to the task's Notes, not
      logged as new tasks.

### 6b. Gemini API key (DESIGN D6, D17, D18)
- [ ] **Before go-live, Compliance/IT confirm** (D18): using the AI Studio Gemini API from a
      Jakarta server for Hong Kong staff, and (§14) a key from BRI Indonesia's project. If not
      approved, use `AI provider = Vertex` or `Off`.
- [ ] Store the key in Secret Manager without it touching a file or shell history:
```bash
read -rsp 'Gemini key: ' k && printf '%s' "$k" | gcloud secrets create grc-gemini-api-key --data-file=- ; unset k
```
- [ ] Check it once from Cloud Shell, then set Config `AI provider` =
      `Gemini API key`:
```bash
python scripts/smoke_gemini.py --secret grc-gemini-api-key --project "$(gcloud config get-value project)"
```

### 7. Deploy (Cloud Build trigger on `main`)
The service is `v2-git` in `asia-southeast2` at
`https://v2-git-1085688466076.asia-southeast2.run.app`; `cloudbuild.yaml` already carries these.
- [ ] Cloud Build → Triggers → the trigger Cloud Run created for the repo → Edit:
      **Configuration: "Cloud Build configuration file"**, location `cloudbuild.yaml` (not
      "Dockerfile": that skips the tests and every setting below).
- [ ] On the trigger, add the substitution variable **`_SHEET_ID`** (and `_SHARED_DRIVE_FOLDER_ID`
      only with `File storage` = `Drive`). A build without `_SHEET_ID` stops before deploying.
- [ ] The trigger's service account needs Cloud Run Admin, Artifact Registry Writer, Logs Writer
      and Service Account User on `grc-tracker-run`.
- [ ] Push to `main` (or "Run" the trigger). The build runs ruff and the tests, builds, pushes
      and deploys with `max-instances=1`, `min-instances=0`, **CPU always allocated**, the
      `grc-tracker-run` service account, unauthenticated ingress (tokens are checked in the app)
      and all environment variables. Settings changed by hand in the console are replaced by the
      next deploy, so change them in `cloudbuild.yaml`.
- [ ] Check: Cloud Build → History shows the build green, then:
```bash
curl https://v2-git-1085688466076.asia-southeast2.run.app/health
```

### 8. Cloud Scheduler (all times Asia/Hong_Kong)
Every job posts to the service with an OIDC token whose **audience is the service URL itself**
(not the job path). Run as is; `URL` and `PROJECT` are filled in for you:
```bash
URL=https://v2-git-1085688466076.asia-southeast2.run.app
PROJECT=$(gcloud config get-value project)
for job in "gmail|*/10 * * * *" "changes|*/5 8-19 * * *" "recurring|0 7 * * *" "digest|0 9 * * 1-5"; do
  name="${job%%|*}"; cron="${job#*|}"
  gcloud scheduler jobs create http "grc-$name" --location=asia-southeast2 --schedule="$cron" \
    --time-zone="Asia/Hong_Kong" --http-method=POST --uri="$URL/jobs/$name" \
    --oidc-service-account-email="grc-tracker-scheduler@$PROJECT.iam.gserviceaccount.com" \
    --oidc-token-audience="$URL" --attempt-deadline=300s
done
```
(`*/5 8-19` runs 08:00–19:55; DESIGN says 08:00–20:00.)

### 9. Google Chat app
In the Cloud console → Google Chat API → Configuration:
- [ ] App name, avatar, description; enable *Receive 1:1 messages* and *Join spaces*.
- [ ] Connection settings: **HTTP endpoint URL** =
      `https://v2-git-1085688466076.asia-southeast2.run.app/chat`.
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
- [ ] `curl https://v2-git-1085688466076.asia-southeast2.run.app/health` → `{"status":"ok"}`;
      `curl -X POST https://v2-git-1085688466076.asia-southeast2.run.app/jobs/gmail` → 401. (Use
      `/health`: Cloud Run can intercept `/healthz`.) A 403 HTML page instead means Cloud Run
      itself is blocking callers: grant `allUsers` the `roles/run.invoker` role on the service.
- [ ] Run `grc-changes` once from the Scheduler console (first run only records `_state`).
- [ ] From an allowed sender, email the bot; run `grc-gmail`; a `Proposed` row appears (and a Drive
      folder, only if Config `File storage` = Drive), the sender gets a reply in the thread, and the email is labelled `GRC/Logged`,
      read and archived.
- [ ] From an address not in `Allowed senders`, email the bot; run `grc-gmail`; it is labelled
      `GRC/Ignored` and no row appears.
- [ ] Set that row to `Not Started`; run `grc-changes`; the Doer gets the assignment email.
- [ ] Message the Chat bot `help`, then paste a request.
- [ ] Set a row to `Done` without proof in the sheet: it flips back and AuditLog shows `Blocked`.

## Switching the AI provider

Set Config `AI provider`; no redeploy is needed (read at every job run). Only masked text and file
names are sent, never file contents; requests are sent with `store=False`.
- `Off` — rules only; everything works, titles are the message's first sentence.
- `Gemini API key` — Google AI Studio key from Secret Manager (`GEMINI_KEY_SECRET`). Needs the
  service in an available region (asia-southeast2) and Compliance sign-off (D18).
- `Vertex` — Gemini on Vertex AI with the runtime service account: set `AI region` and give the
  service account Vertex AI User. Google's recommendation for unlisted regions (D18).

`AI model` defaults to `gemini-3.8-flash`. Any failure (bad key, location not supported, timeout,
invalid JSON) is flagged `AI unavailable` on the task and the request is logged on rules alone.

## Operations

- **Logs:** Cloud Run → Logs. JSON entries carry task IDs, counts, timings and error types only;
  `app/logs.py` drops any other text field by design.
- **An email in `GRC/Failed`:** it failed 3 times and was marked read (it stays in the bot's
  inbox). Fix the cause (see logs), then mark it unread; the next run retries it (a `Source ID`
  already in Master is never logged twice).
- **An email in `GRC/Ignored`:** the sender is not in `Allowed senders`, or it was an auto-reply.
  Add the sender and mark it unread to have it logged.
- **Re-running a job** is always safe: rows are keyed by `Source ID`, emails by `_state`.
