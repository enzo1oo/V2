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

### Environment variables

`app/settings.py` is the one place that lists every variable the service reads, and
`.env.example` documents each of them (a test fails if one is missing). Locally they come from
`.env`; on Cloud Run the non-secret ones come from `deploy/env.prod.yaml` and the secrets from
Secret Manager (`deploy/secrets.txt`), set by Cloud Run as the same variables.

If `SERVICE_URL`, `SCHEDULER_SA_EMAIL` or `CHAT_PROJECT_NUMBER` is empty, every protected call is
rejected (fail closed), and `/health` answers 503 with the names of the missing variables.

**Gemini key locally:** put `GEMINI_API_KEY=...` in `.env` (git-ignored). Never commit it, paste
it into chats or tickets, or pass it on a command line. To check the key, model and region with
one live call (fake message), run from Cloud Shell or the Jakarta service, not from Hong Kong:

```bash
python scripts/smoke_gemini.py
```

## Deploy to Cloud Run

Everything runs from Cloud Shell (or any machine with `gcloud`), in the repo folder, with the
project selected (`gcloud config set project YOUR-PROJECT-ID`). Names (service `v2-git`, region
`asia-southeast2`, accounts `grc-tracker-run` and `grc-tracker-scheduler`) live in
`deploy/config.sh` only.

1. **One-time Google Cloud setup** (APIs, the two service accounts and their roles, empty secrets,
   Artifact Registry, the Cloud Tasks queue `wa-process`). Safe to run again:
```bash
deploy/setup_gcp.sh
```
2. **Secrets.** For each one in `deploy/secrets.txt` it asks for a file path or a hidden value
   (type `s` to skip one, e.g. the WhatsApp ones before Meta is set up). Nothing is echoed or saved:
```bash
deploy/setup_secrets.sh
```
3. **Production settings.** Copy the example, fill in the values (none are secret), and store it
   as the secret `grc-env-prod` that every deploy reads:
```bash
cp deploy/env.prod.yaml.example deploy/env.prod.yaml
```
```bash
deploy/setup_secrets.sh env
```
4. **Deploy** (tests, build, deploy; prints the service URL, the WhatsApp webhook URL and the
   health result):
```bash
deploy/deploy.sh
```
5. **Scheduler jobs** (creates or updates all six):
```bash
deploy/setup_scheduler.sh
```
6. **WhatsApp (when Meta is ready):** in the Meta app's webhook settings, paste the webhook URL
   printed by `deploy.sh` and `WA_VERIFY_TOKEN` from `deploy/env.prod.yaml`, then subscribe to
   **messages** (README §9b). Re-run steps 2 (WhatsApp secrets) and 4 if they were skipped.
7. **Share the sheet** with `grc-tracker-run@YOUR-PROJECT-ID.iam.gserviceaccount.com` as Editor.
8. **Apps Script:** install `apps_script/audit.gs` (§10 below).
9. **Check:** `/health` must answer `{"status":"ok"}`. A 503 lists the missing variable names and
   sheet problems (names only, never values):
```bash
curl -s https://v2-git-1085688466076.asia-southeast2.run.app/health
```

**Deploy on merge to `main`:** point the Cloud Build trigger at `cloudbuild.yaml` (Configuration:
"Cloud Build configuration file"). It runs the tests, builds, and deploys with
`deploy/deploy.sh --image …`, so it uses the same settings and secrets; no substitutions to fill.

**Update one secret later** (e.g. a new Gmail token or WhatsApp token): add a version, then
deploy again, because Cloud Run reads secrets when a revision starts:
```bash
deploy/setup_secrets.sh GMAIL_REFRESH_TOKEN
```
```bash
deploy/deploy.sh --skip-tests
```
Changed `deploy/env.prod.yaml`? Run `deploy/setup_secrets.sh env`, then `deploy/deploy.sh`.

## Go-live checklist (walk through with IT)

Most Cloud steps below are done by the `deploy/` scripts (section "Deploy to Cloud Run");
what remains here is what a person has to decide or click.

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
      `Builder email`, `File storage` (`None` = no Drive, the default; `Drive` = task folders),
      `AI reads PDFs` (blank or `Yes` = email PDFs go to Gemini unmasked; `No` = names only).
      `Gmail intake label` is no longer used and can be deleted.
- [ ] Create hidden tabs `_state` (`ID, Status, Owner, Doer, Notified doer, Notified at`) and
      `_failures` (`Key, Count, Last error, Last at`).
- [ ] Protect `AuditLog`, `_state`, `_failures`: editors = the runtime service account and the
      person who installs the Apps Script trigger.
- [ ] Recommended: data validation on Master `Status` with the custom formula
      `=OR(V2<>"Done",AND(Q2<>"",X2<>""))` (adjust letters to Status/Submitted date/Link) as a
      second guard next to the Apps Script check.

### 2. Google Cloud project and APIs
- [ ] Done by `deploy/setup_gcp.sh` (APIs, Artifact Registry `cloud-run-source-deploy`, Cloud Tasks
      queue `wa-process`). For `AI provider = Vertex` also enable `aiplatform.googleapis.com`.

### 3. Service accounts and roles (least privilege, DESIGN section 9)
`deploy/setup_gcp.sh` creates them and grants:
- `grc-tracker-run`: Secret Accessor on the secrets in `deploy/secrets.txt` only; Cloud Tasks
  Enqueuer; Service Account User on itself (WhatsApp tasks run as it).
- `grc-tracker-scheduler`: `run.invoker` on the service (`deploy/setup_scheduler.sh`).
- The build account (default: the Compute Engine default account; override with `BUILD_SA=`):
  Cloud Run Admin, Artifact Registry Writer, Logs Writer, Storage Object Viewer, Service Account
  User on `grc-tracker-run`, Secret Accessor on `grc-env-prod`.
By hand:
- [ ] Share the GRC sheet with `grc-tracker-run` as **Editor**; with `File storage` = Drive, add it
      to the Shared Drive as **Content manager**.
- [ ] Only with `AI provider = Vertex`: Vertex AI User for `grc-tracker-run`.

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
- [ ] Store the client JSON: `deploy/setup_secrets.sh OAUTH_CLIENT_SECRET`, and give it the path
      to the downloaded file.
- [ ] **One-time consent as the bot:** on a trusted laptop, in a browser signed in as the bot
      account (not as a person), mint the refresh token (it is printed, never saved), store it,
      then clear the terminal:
```bash
pip install google-auth-oauthlib && python scripts/mint_gmail_token.py oauth_client.json
```
```bash
deploy/setup_secrets.sh GMAIL_REFRESH_TOKEN
```
(It checks nothing about the value: paste only the line starting with `1//`. The mint script
prints which mailbox the token belongs to; it must be the bot.)
- [ ] Fill Config `Allowed senders`. No labels need to be created: `GRC/Logged`, `GRC/Ignored`,
      `GRC/Reply` and `GRC/Failed` are created by the service.
- [ ] Tell the team: forward or send requests to the bot address; the sender gets a reply in the
      same thread with the task ID. Replies in that thread are added to the task's Notes, not
      logged as new tasks.

### 6b. Gemini API key (DESIGN D6, D17, D18)
- [ ] **Before go-live, Compliance/IT confirm** (D18): using the AI Studio Gemini API from a
      Jakarta server for Hong Kong staff, and (§14) a key from BRI Indonesia's project. If not
      approved, use `AI provider = Vertex` or `Off`.
- [ ] Store the key (hidden prompt; it never touches a file or the shell history):
```bash
deploy/setup_secrets.sh GEMINI_API_KEY
```
- [ ] Check it once from Cloud Shell, then set Config `AI provider` =
      `Gemini API key`:
```bash
python scripts/smoke_gemini.py --secret grc-gemini-api-key --project "$(gcloud config get-value project)"
```

### 7. Deploy
- [ ] `deploy/deploy.sh` (section "Deploy to Cloud Run"), or the Cloud Build trigger on `main`
      with configuration file `cloudbuild.yaml`. Both deploy `v2-git` in `asia-southeast2` with
      `max-instances=1`, `min-instances=0`, CPU always allocated, the `grc-tracker-run` account,
      public ingress (every caller's token or signature is checked in the app), the settings from
      `grc-env-prod` and the secrets from `deploy/secrets.txt`. Settings changed by hand in the
      console are replaced by the next deploy: change `deploy/env.prod.yaml` instead.
- [ ] Check:
```bash
curl https://v2-git-1085688466076.asia-southeast2.run.app/health
```

### 8. Cloud Scheduler (all times Asia/Hong_Kong)
- [ ] `deploy/setup_scheduler.sh` creates or updates: `grc-gmail` (every 10 min), `grc-changes`
      (every 5 min, 08:00–19:55), `grc-recurring` (daily 07:00), `grc-digest` (weekdays 09:00),
      `grc-wa-reminders` (weekdays every 30 min, 07:00–19:30; the app sends once after Config
      `Reminder time`) and `grc-wa-drafts` (every 10 min). Each sends an OIDC token of
      `grc-tracker-scheduler` whose audience is `SERVICE_URL`.

### 9. Google Chat app
In the Cloud console → Google Chat API → Configuration:
- [ ] App name, avatar, description; enable *Receive 1:1 messages* and *Join spaces*.
- [ ] Connection settings: **HTTP endpoint URL** =
      `https://v2-git-1085688466076.asia-southeast2.run.app/chat`.
- [ ] Authentication audience: **Project number** (→ `CHAT_PROJECT_NUMBER`).
- [ ] Visibility: only the GRC team (people or a group in the domain).
- [ ] Use the classic Chat app (HTTP) setup, not a Workspace add-on; the bot expects that event
      format.

### 9b. WhatsApp bot (optional, DESIGN section 17)
BRI does this part in Meta; the service only needs the IDs and two secrets afterwards.
- [ ] Meta Business Manager (BRI's business, verified) → create an app of type *Business* → add the
      **WhatsApp** product. Register a **dedicated phone number** for the bot (not one used in the
      normal WhatsApp app).
- [ ] Note the **Phone number ID** (`WA_PHONE_NUMBER_ID`), the **WhatsApp Business Account ID**
      (`WA_WABA_ID`) and the Graph API version the app uses (`WA_GRAPH_VERSION`, e.g. `v23.0`).
- [ ] Business settings → System users → create one, give it the app and the WhatsApp account,
      and generate a **permanent token** with `whatsapp_business_messaging` and
      `whatsapp_business_management` → `deploy/setup_secrets.sh WA_ACCESS_TOKEN`.
- [ ] App settings → Basic → **App secret** → `deploy/setup_secrets.sh WA_APP_SECRET`.
- [ ] Pick a long random **verify token** (`WA_VERIFY_TOKEN`; not a secret, but not guessable).
- [ ] WhatsApp → Configuration → Webhook: callback URL
      `https://v2-git-1085688466076.asia-southeast2.run.app/wa/webhook`, the verify token above,
      then subscribe to the **messages** field.
- [ ] WhatsApp → Message templates → create `grc_daily_reminder`, category *Utility*, language
      *English*, body:
      `Hi {{1}}, your GRC tasks today: {{2}} overdue, {{3}} due within 7 days, {{4}} in progress
      but not updated recently. Open the tracker: {{5}}` — and wait for Meta's approval.
- [ ] Sheet: add a `WhatsApp` column to **Staff** (international format, e.g. `+852 9123 4567`)
      for everyone who may use the bot, and a hidden tab **`_wa_pending`** with the headers
      `Key`, `Kind`, `Sender`, `Text`, `Prompt`, `Media`, `Message IDs`, `Task ID`, `Status`,
      `Created at`, `Updated at`.
- [ ] Google Cloud: `deploy/setup_gcp.sh` already made the queue and roles. Store the two
      secrets with `deploy/setup_secrets.sh WA_ACCESS_TOKEN` and `… WA_APP_SECRET`, fill the `WA_*`
      values and `RUNTIME_SA_EMAIL` in `deploy/env.prod.yaml` (then `deploy/setup_secrets.sh env`),
      and run `deploy/deploy.sh`. The Scheduler jobs `grc-wa-drafts` and `grc-wa-reminders` come
      from `deploy/setup_scheduler.sh`.
- [ ] Test: send `help` to the bot from a Staff number, then the WhatsApp tests in
      `docs/LIVE_TEST.md`.

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

Set Config `AI provider`; no redeploy is needed (read at every job run). Masked text, file names
and PDFs (unmasked, Config `AI reads PDFs`, DESIGN section 16 item 11) are sent; requests are sent
with `store=False`.
- `Off` — rules only; everything works, titles are the message's first sentence.
- `Gemini API key` — Google AI Studio key (`GEMINI_API_KEY`, injected from Secret Manager). Needs the
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
