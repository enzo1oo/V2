# GRC Request Intake and Task Assignment — Build Spec v1.1

Status: final design for the build. Source of truth for Claude Code.
Business context: see `docs/PRD.docx` (PRD with the TRD as Appendix A).
Owner: Enzo Taslim (builder). Hosting: BRI HK IT (Google Cloud).

v1.1 = v1.0 plus the Phase 0 review decisions. Every change is listed in section 15 and marked
**(v1.1)** where it appears.

---

## 1. What the system does (one paragraph)

The GRC manager forwards requests from BRI Pusat, regulators and auditors to the system, either by
labelling an email `GRC/To log` in Gmail or by pasting WhatsApp text to a Google Chat bot. A Python
service on Cloud Run reads each request, saves its attachments to a Drive folder, extracts the
deadline, letter number and contacts by fixed rules, optionally asks an AI for an English title,
summary and deliverables, proposes an owner by fixed rules, and writes one row to the `Master` tab of
the GRC Google Sheet with status `Proposed`. When the manager approves (or someone hands the task
off), the doer gets an email with the full original message and file links. Daily digests remind
everyone. Every change is logged.

**The AI never chooses who does the work.** Fixed rules propose; the manager approves.

---

## 2. Final design decisions

| # | Decision |
|---|---|
| D1 | Python 3.12, FastAPI, one container on Cloud Run. `max-instances=1`, `min-instances=0`. All sheet writes go through one in-process lock. **(v1.1)** Google API client objects are created per thread (httplib2 is not thread-safe). CPU is always allocated (instance-based billing) so the Chat background completion in section 8 actually runs. |
| D2 | The Google Sheet is the only data store. The service reads and writes columns **by header name**, never by position. |
| D3 | Gmail is accessed with an **OAuth refresh token for one intake mailbox**, stored in Secret Manager. The same code serves Gmail option A (manager's own mailbox) and option B (a forwarding mailbox such as grc-intake@); only the token differs. Scopes: `gmail.modify`, `gmail.send`. No domain-wide delegation. **(v1.1)** The OAuth client must belong to an **Internal** consent screen (a "Testing" external app expires refresh tokens after 7 days) and be allowed by the Workspace admin for the restricted Gmail scope. |
| D4 | Assignment emails and digests are sent from that same mailbox. The display name comes from Config. |
| D5 | Sheets and Drive use the runtime service account (`grc-tracker-run`). Attachments go to a Shared Drive folder per task: `G-0012 – <title>`. |
| D6 | AI goes through one interface, `AIProvider.extract(text, catalog) -> AIResult`, with three implementations: `VertexGeminiProvider`, `BriAgentProvider` (stub until the agent owner shares the API), `NullProvider`. Chosen by the Config setting `AI provider`. |
| D7 | Google Chat app over HTTP to `POST /chat`; synchronous replies; Google-signed token verified on every call. **(v1.1)** This needs the Cloud Run service to allow unauthenticated invocation (`allUsers` invoker) with token checks done in the app; confirm the org policy allows it. |
| D8 | Edits made by hand in the sheet are audited by a small Apps Script edit trigger (`apps_script/audit.gs`), because only Apps Script sees the editor's identity. The service writes its own actions to AuditLog. |
| D9 | Approvals and handoffs are detected by comparing `Master` with a hidden `_state` tab every 5 minutes. |
| D10 | Timed jobs: Cloud Scheduler with OIDC tokens → `POST /jobs/*`. |
| D11 | Time zone `Asia/Hong_Kong` everywhere. Dates are stored as real sheet dates. |
| D12 | Original message: up to 45,000 characters in the `Original message` cell (Sheets limit is 50,000); the full text is always saved as `original.txt` in the task folder. |
| D13 | A weekday that does not match its date (e.g. "Rabu 06 November 2026" = Friday) keeps the written date as the deadline and adds a flag. Never silently change a deadline. |
| D14 | Routing can match on **known contacts** (emails or full names) stored per Catalog row, as well as keywords. |
| D15 | Failure never loses a request: the Gmail label is only changed after the row is written; a job retry is always safe (idempotent by `Source ID`). |

---

## 3. Data model (Google Sheet)

Tabs already exist (from `GRC_Tracker_Tabs_v2.xlsx`). The service must not depend on column order.
Missing required columns → the job stops and alerts (section 11).

### Master (one row per task)
Required columns (exact header text):

`ID`, `Type`, `Title`, `Catalog ID`, `Category`, `Division`, `Requester / Regulator`, `Owner`, `Doer`,
`Support`, `Whole team`, `Letter no.`, `Letter code`, `Request reference`, `Start / received`,
`Due date`, `Submitted date`, `Priority`, `Effort`, `Weight` (formula), `Progress`, `Status`,
`Last updated`, `Link`, `Send to`, `Notes`, `Flag`, `Source`, `Routing note`,
**new:** `Original message`, `Files`, `AI summary`, `Deliverables`, `Contacts`, `Source ID`.

- `ID`: `G-0001` upward. The next number is the highest existing `G-` number + 1, computed under the write lock.
- `Status` values: `Proposed`, `Not Started`, `In Progress`, `On Hold`, `Done`, `Cancelled`.
- `Source`: `Email`, `Google Chat`, `Recurring <CatalogID> <yyyy-mm>`, `Manual`.
- `Source ID`: Gmail message ID, Chat message name, or the recurring key. Used to skip duplicates.
- `Weight`: write the same formula the existing rows use (looks up Effort in Config; blank = Medium).
  **(v1.1)** The formula is read from the last data row (`valueRenderOption=FORMULA`) and its row
  number rewritten for the new row; if Master has no data rows yet, the job stops with a
  `schema_error` asking for one template row.
- `Link`: proof of submission (filled by people). `Files`: the Drive folder URL (filled by the service).
- `Flag`: `; `-separated short notes (e.g. `Weekday mismatch: Rabu = Wed, 06 Nov 2026 is Fri`).

### Staff
`Name`, `Email`, `Main division`, `Role` (Staff/Intern/Manager), `Internship end date`,
`Capacity (1 = full time)`, `Weighted open load` (formula; read its computed value).

### Catalog
`Catalog ID`, `Report type`, `Division`, `Regulator / Institution`, `Recurrence`, `Default owner`,
`Backup`, `Typical due day`, `Due months seen (2026)`, `Match keywords` (comma-separated),
**new:** `Known contacts` (comma-separated emails or full names), `Confirmed by manager?` (Yes/No).
Rows with IDs starting `ADHOC-` have no default owner and are never used by routing rule 1.

### LetterCodes
`Letter code`, `Route to division`.

### AuditLog (protected; service and Apps Script only)
`Timestamp`, `User`, `Task ID`, `Action`, `Field`, `Old value`, `New value`, `Reason`.

### Config (Setting → Value)
Existing: `Effort: Small/Medium/Large`, `Support share`, `Overload threshold`, `Stale after (days)`,
`Reminder time`, `Leaving warning (days)`, `Unknown letter code`, `Manager email`,
`Allowed email domain`, `Gmail intake label`, `Gmail done label`, `Recurring lead (days)`,
`Default due (days)`.
**New:** `AI provider` (`Vertex` / `BRI agent` / `Off`), `AI model`, `AI region`, `Sender name`,
`Mask emails for AI` (Yes/No, default No).
**New (v1.1):** `Requester domains` (`domain=Org` pairs separated by `;`, e.g.
`bri.co.id=BRI; ojk.go.id=OJK`), `Known requesters` (comma-separated organisation names, e.g.
`BRI, OJK, HKMA, Bank Indonesia`).

### _state (hidden, service only)
`ID`, `Status`, `Owner`, `Doer`, `Notified doer`, `Notified at`.

### _failures (hidden, service only) **(v1.1)**
`Key`, `Count`, `Last error`, `Last at`.
`Key` is a `Source ID` (failure counter for section 11) or `alert:<error type>` (alert throttling).
Needed because the instance scales to zero and in-memory counters are lost.

---

## 4. Request processing pipeline

Every request (email, chat, recurring) goes through the same function:
`process_request(raw: RawRequest) -> TaskResult`.

```
RawRequest
  source, source_id, received_at, subject, body_text, body_html,
  sender_name, sender_email, attachments[(filename, mime, bytes)], submitted_by
```

1. **Dedupe.** If `source_id` already exists in `Master.Source ID`, return the existing task.
2. **Unwrap forwards** (email only). If the body contains a forwarded block
   (`---------- Forwarded message ---------`, `-----Original Message-----`, `Dari:`/`From:` header
   block), the **forwarded part is the request**, its original `From` is the requester, and the
   manager's text above it is stored in `Notes` as `Manager note: …`. For nested forwards use the
   innermost block.
3. **Clean** (used for matching, titles and AI; the untouched original is stored separately):
   - drop greeting and sign-off lines: `Selamat (pagi|siang|sore|malam)`, `Yth`, `Dengan hormat`,
     `Dear`, `Hi/Halo`, `Terima kasih…`, `Salam`, `Regards`, `Best`, `Thanks`, signature blocks;
     **(v1.1)** a greeting only removes its own clause (up to the first `,` `!` `:` or line end), so
     "Halo, tolong kirim laporan X" keeps "tolong kirim laporan X"; a sign-off line (≤ 60 chars,
     no digits) drops itself and the rest of the message only when at most 12 non-blank lines
     follow it;
   - drop `**` and `*` emphasis marks, collapse whitespace;
   - drop quoted reply chains below the request (`On … wrote:`, `Pada … menulis:`).
   **(v1.1)** Rule extraction (step 5) runs on a *light* clean (emphasis, quoted replies and
   whitespace only) so contacts and dates in sign-off areas are not lost; titles, routing and AI
   use the full clean.
4. **Mask** (copy used only for AI): phone numbers → `[phone]`, account numbers (8+ digits) →
   `[account]`, HKID/KTP/passport patterns → `[id]`. Emails are kept unless Config says otherwise.
5. **Extract by rules** (`extract/rules.py`, pure functions, fully unit-tested):
   - `letter_no`, `letter_code`: regex for `B.91.e-EMR/IRM/10/2026` style → code `EMR/IRM`
     (first two unit parts, upper case). Spaces around `.` and `-` are allowed.
   - `due_date`: dates in `06 November 2026`, `6 Nov 2026`, `06/11/2026` (day first), `2026-11-06`,
     with Indonesian and English month names and abbreviations. **(v1.1)** Also `November 6, 2026`,
     two-digit years (`6-Nov-26`) and dates without a year (`6 November`: this year, or next year if
     that date has passed; flag `Deadline year assumed`). A date within 60 characters after a
     deadline word (`deadline`, `paling lambat`, `selambat-lambatnya`, `batas waktu`, `tenggat`,
     `sebelum`, `due`, `by`, `no later than`, `pengumpulan pada`, **(v1.1)** `before`) wins — if
     several, the earliest on or after today (if all are past: the latest, flag
     `Deadline already passed`). **(v1.1)** Else the **earliest** date on or after today, flag
     `Deadline guessed (no deadline word)`; else none.
   - `weekday_check`: if a weekday name (Senin…Minggu, Monday…Sunday) appears right before the due
     date and does not match, add the flag (D13).
   - `contacts`: email addresses and capitalised full names after `a.n.`, `PIC`, `menghubungi`,
     `contact`.
   - `title_rule`: first sentence of the cleaned text, max 90 characters, cut at a word boundary.
     **(v1.1)** A formal letter's `Perihal:` / `Hal:` / `Subject:` line is used instead when
     present; letter header lines (`Nomor:`, `Lampiran:`, `Kepada`, `di …`) are skipped; the first
     letter is capitalised.
   - **(v1.1)** `requester_org`: the sender's email domain looked up in Config `Requester domains`
     (sub-domains match their parent); else the first `Known requesters` name found as a whole word
     in the text; else blank. Fills `Requester / Regulator`.
6. **Extract by AI** (if `AI provider` ≠ `Off`), see section 6. Merge: rule values win for
   `letter_no` and `letter_code`; for `due_date`, use the rule date if present, else the AI date;
   if both exist and differ, keep the rule date and flag `AI deadline differs: <date>`; the AI title,
   summary and deliverables are used when present.
7. **Missing deadline:** today + `Default due (days)`, flag `No deadline found, default used`.
8. **Store files.** Create the task folder; save `original.txt` and every attachment; put the folder
   URL in `Files`. (A file over 25 MB: skip it and flag.) **(v1.1)** The folder carries the
   `Source ID` as a Drive app property; a retry reuses the existing folder instead of creating a
   second one, and files already in it are not uploaded again. The ID is only known at step 10,
   so the folder is created as `Pending – <title>` and renamed to `G-0012 – <title>` after the
   row is written. The service uses the `drive.file` scope (it only touches what it created).
9. **Route** (section 5) → `Owner`, `Doer` (= Owner), `Division`, `Catalog ID`, `Routing note`.
10. **Write** the row (status `Proposed`, `Last updated` = now, `Weight` formula), then AuditLog
    `Created` with the routing note as the reason.
    **(v1.1)** Every text value is written as text (leading `'`), so message content starting
    with `=`, `+` or digits never becomes a formula or number. `Contacts` lists the forwarded
    sender first, then the extracted contacts; the sender also counts as a known contact for
    routing. Once the row exists, a failure to write AuditLog or rename the folder is logged and
    alerted but does not fail the request (the row is the record; a retry would only dedupe).
11. **Acknowledge.** Chat: reply (section 8). Email: change label to the Config `Gmail done label`
    (`GRC/Logged`) **after** step 10.

---

## 5. Routing rules (`route.py`, pure function, fully unit-tested)

Inputs: cleaned text, contacts, letter code, due date, Catalog, Staff, LetterCodes, Config,
optional `ai_catalog_id`. Output: `RouteResult(owner, division, catalog_id, rule, note)`.

A person is **available** if: role is not Manager; and if they have an internship end date, it is
on or after both today and the due date. **Load ratio** = Weighted open load ÷ Capacity.

1. **Rule 1 — known report.** Score each non-ADHOC Catalog row: + length of every keyword found as a
   whole word (case-insensitive, hyphens and underscores treated as spaces), + 100 for every known
   contact found (email exact, or full name). **(v1.1)** Each keyword and each contact counts once,
   however often it appears. Highest score > 0 wins; **(v1.1)** ties go to the row confirmed by the
   manager, then Catalog ID A–Z. If its default owner is available and load ratio ≤
   `Overload threshold` → that owner.
   Note: `Rule 1: catalog "<Report type>" (<matched words>) → default owner`.
2. **Rule 2 — backup.** Same Catalog row, but the owner is unavailable or overloaded, and the backup
   is available → backup. Note says why.
3. **Rule 3 — division.** Division from the Catalog row (if matched) else from LetterCodes by
   letter code. Pick the available person in that division with the lowest load ratio (tie: name A–Z).
4. **Rule 4 — AI suggestion.** Only if rules 1–3 found nothing and `ai_catalog_id` is a real
   non-ADHOC Catalog ID: apply rules 1–2 to that row. Note starts `Rule 4 (AI suggested): …`.
5. **Rule 5 — needs review.** No owner. Division if known. Flag `Needs review: no owner`.

**(v1.1)** A letter code that is not in LetterCodes adds the flag
`<Config "Unknown letter code">: unknown letter code <code>` (e.g.
`Needs review: unknown letter code XYZ/ABC`); routing continues without a division from it.
A blank `Role` counts as `Staff`. A name in Catalog that is not in Staff is unavailable.

---

## 6. AI extraction (`extract/ai.py`)

Interface:
```python
class AIProvider(Protocol):
    name: str

    def extract(
        self, masked_text: str, catalog: list[CatalogEntry], attachment_names: list[str]
    ) -> AIResult | None: ...
```
`AIResult` (pydantic, strict): `title_en` (≤ 90 chars), `summary_en` (≤ 2 sentences),
`deliverables: list[str]`, `deadline: date | None`, `deadline_quote: str | None`,
`requester_org: str | None`, `contacts: list[{name, email}]`, `catalog_id: str` (an ID from the list
or `NONE`), `language: "id" | "en" | "mixed"`.
**(v1.1)** An over-long title or summary is trimmed (title to 90 characters at a word boundary,
summary to its first two sentences) rather than rejecting the whole result.

- Prompt lives in `prompts/extract_v1.md`; the version string is logged with each call.
- Input: masked cleaned text (max 8,000 characters), Catalog IDs with report names, attachment file
  names only. Never file contents.
- `VertexGeminiProvider`: `google-genai` SDK in Vertex mode, model and region from Config,
  temperature 0, JSON output with the schema enforced.
- `BriAgentProvider`: stub that raises `NotConfigured` until the endpoint, auth and payload format
  are known. Keep its interface identical.
- Guardrails: drop `deadline` unless `deadline_quote` appears word-for-word in the original text
  (**(v1.1)** compared ignoring case, line breaks, repeated spaces and `*`, because emails wrap
  lines and the AI sees the cleaned text);
  `catalog_id` must exist; 20-second timeout, one retry, then return `None` (the pipeline continues
  on rules and flags `AI unavailable`).
- Log provider, model, prompt version, latency, success. **Never log message text.**

---

## 7. Jobs and endpoints

| Endpoint | Caller | Schedule (HKT) | What it does |
|---|---|---|---|
| `POST /jobs/gmail` | Scheduler | every 10 min | Up to 20 threads with the intake label → `process_request` each |
| `POST /jobs/changes` | Scheduler | every 5 min, 08:00–20:00 | Compare Master with `_state`; send assignment emails (section 8); update `_state` |
| `POST /jobs/recurring` | Scheduler | daily 07:00 | Create confirmed Catalog reports due within `Recurring lead (days)`, once per period (`Source ID` = `Recurring <ID> <yyyy-mm>`) |
| `POST /jobs/digest` | Scheduler | weekdays 09:00 | Personal and manager digests |
| `POST /chat` | Google Chat | on event | Section 8 |
| `GET /healthz` | anyone | — | 200 OK, no data |

**(v1.1)** Gmail: in each labelled thread the **newest** message is the request and its message ID is
the `Source ID` (in option A the manager may label an older conversation).

Recurring rules: `Monthly` = every month; `Quarterly`/`Semester` = months listed in
`Due months seen (2026)`, else Jan/Apr/Jul/Oct and Jan/Jul; `Annual` = first listed month. Due day =
`Typical due day`, capped at the month's last day.

Change detection (`/jobs/changes`): an assignment email is sent when (a) Status moves from `Proposed`
to any open status and the doer has not been notified for this task, or (b) `Doer` changes on an
open task. Record `Notified doer` and `Notified at` in `_state` so each assignment emails once.
**(v1.1)** When `_state` is empty (first run after go-live) every task is recorded as it is and
**nothing is sent**. After that, a task with no `_state` row (a row added by hand) is treated as
coming from `Proposed`: if it is already open with a Doer, the Doer is emailed once.
**(v1.1)** Both triggers reduce to one rule: an open task whose `Doer` differs from
`Notified doer` gets the email. So a send that fails (Gmail error, or the Doer has no email in
Staff) stays pending and is retried on the next run; a missing email also adds the flag
`Assignment email not sent: doer has no email in Staff` once. `_state` is saved after every send,
so a crash later in the run cannot send twice. Each send is written to AuditLog
(`Assignment emailed`).

**(v1.1) Recurring details.** A recurring request carries its Catalog ID, so routing rule 1 uses
that row directly instead of keyword scoring (report names often lack their own keywords), and
the AI is not called. Title: `<Report type> – <Mon yyyy>`. Due dates are taken in
`[today, today + Recurring lead (days)]` inclusive, across month and year ends.

---

## 8. Notifications and Chat

**Assignment email** (to the Doer, cc the Owner if different): subject
`[GRC] G-0012 – <title> (due 06 Nov 2026)`; body: title, due date, flags, division, owner, routing
note, AI summary and deliverables (if any), the Files link, the sheet link, then the **full original
message** under a divider. Plain text plus simple HTML.

**Personal digest** (09:00, only if non-empty): overdue, due within 7 days, In Progress with
`Last updated` older than `Stale after (days)`. Proposed tasks are excluded.
**Manager digest:** awaiting approval, all overdue, people over the load threshold, interns leaving
within `Leaving warning (days)` who are still default owners in the Catalog, count of
`AI unavailable` flags in the last 24 h.
**(v1.1)** "Personal" means tasks where the person is the `Doer`; open statuses only. The manager
digest is also sent only when non-empty, to Config `Manager email` (skipped if blank). The
"last 24 h" AI count uses `Start / received` on or after yesterday (the sheet keeps no creation
time). Managers are left out of the load list.

**Chat bot:**
- Only users whose email ends with `@<Allowed email domain>`.
- `help` → usage. `my tasks` → the sender's open tasks (match by Staff email).
- Anything else → `process_request`; reply with ID, title, proposed owner, due date, flags, Files link,
  and "Waiting for manager approval". If the message has attachments, try to download them with
  the Chat API into the task folder; on failure, ask the user to drop files in the Files link.
- Must answer within 25 seconds: if processing exceeds that, reply "Logged as G-xxxx, details by
  email" and finish in the background (requires CPU always allocated, D1).

---

## 9. Security

- Verify on every request: Scheduler OIDC token (audience = service URL, email = scheduler SA);
  Chat bearer token (Google-signed for Chat, audience = project number). Reject everything else.
- Secrets only from Secret Manager (Gmail refresh token, OAuth client secret, BRI agent credential).
  Nothing secret in code, the sheet, env files committed to git, or logs.
- Least privilege: runtime SA (Sheets via sharing, Drive Shared Drive, Vertex, Secret accessor);
  scheduler SA (`run.invoker` only).
- Logs: task IDs, counts, timings and error types only. No message bodies, names, emails or phone
  numbers.
- **(v1.1)** Test fixtures use fake names and `example.*` domains only; no real person's name or
  contact goes into git.

---

## 10. Repository layout

```
grc-tracker/
├── CLAUDE.md
├── docs/DESIGN.md            # this file
├── docs/PRD.docx
├── app/
│   ├── main.py               # FastAPI routes
│   ├── settings.py           # env vars (IDs only)
│   ├── clock.py              # (v1.1) Asia/Hong_Kong now/today, freezable in tests
│   ├── models.py             # RawRequest, Task, CatalogEntry, StaffMember, RouteResult, AIResult
│   ├── sheets.py             # SheetStore: header-based read/write, write lock, batch calls
│   ├── drive.py              # task folders, file upload
│   ├── gmail.py              # label poll, message parse, send
│   ├── chat.py               # event handling, replies, attachment download
│   ├── pipeline.py           # process_request
│   ├── extract/clean.py      # unwrap forwards, clean, mask
│   ├── extract/rules.py      # dates, weekday, letter code, contacts, title, requester org
│   ├── extract/ai.py         # AIProvider + implementations
│   ├── route.py              # routing rules
│   ├── jobs.py               # changes, recurring, digests
│   ├── notify.py             # email templates
│   └── auth.py               # token verification
├── prompts/extract_v1.md
├── apps_script/audit.gs
├── scripts/mint_gmail_token.py   # one-time OAuth consent for the intake mailbox
├── tests/
│   ├── fixtures/             # sample messages (masked/fake), catalog.json, staff.json
│   ├── fakes.py              # FakeSheetStore, FakeDrive, FakeGmail, FakeAI
│   └── test_*.py
├── Dockerfile
├── cloudbuild.yaml
├── requirements.txt
└── README.md                 # local run, env vars, IT setup checklist, deploy
```

Environment variables (IDs only): `GCP_PROJECT`, `GCP_REGION`, `SHEET_ID`, `SHARED_DRIVE_FOLDER_ID`,
`GMAIL_TOKEN_SECRET`, `OAUTH_CLIENT_SECRET`, `SCHEDULER_SA_EMAIL`, `SERVICE_URL`, `CHAT_PROJECT_NUMBER`,
`TZ=Asia/Hong_Kong`.

---

## 11. Errors and monitoring

- Missing tab or column → stop the job, log `schema_error`, email the builder and manager (at most
  once per hour per error).
- Gmail auth failure → stop the Gmail job, alert within 1 hour; labels stay for retry.
- Any exception inside one request → log, add nothing to the sheet, leave the label, continue with
  the next request. After 3 failures for the same `source_id`, add the label `GRC/Failed` and alert.
  **(v1.1)** `GRC/Failed` replaces the intake label (otherwise the job would retry it forever).
  A failed label change after the row is written is logged; the next run finds the row by
  `Source ID` and changes the label then. `/jobs/gmail` answers 500 with `schema_error` or
  `gmail_auth_error` when it stops.
- **(v1.1)** Failure counts and the "once per hour" alert throttle are stored in `_failures`.
- Sheets quota errors (429) → exponential backoff, max 3 tries. Read each tab once per job run.

---

## 12. Acceptance tests (must pass before deploy)

Fixture `tests/fixtures/manpower_planning.txt` (the 26 Oct 2026 request, phones and email masked,
**(v1.1)** people's names replaced with fake ones), forwarded by the manager with the note
"Tolong dicek ya". With AI `Off`:
- requester org is BRI (from the forwarded sender, via `Requester domains`; also found in the text);
- title is **not** the greeting;
- due date 2026-11-06 with flag containing `Weekday mismatch`;
- Catalog `HR-DATA`, division HR, via Rule 1 known contact `Hana Setyaningsih` (fake name);
- `Original message` contains the full untouched text; two attachments saved.

With a fake AI returning a valid result: title contains "Manpower Planning", deliverables list has 2
items. With a fake AI returning a deadline whose quote is not in the text: AI deadline dropped.

Also required: letter `B.91.e-EMR/IRM/10/2026` with no known report → Rule 3 Policy and Risk;
"Tolong cek printer" → Rule 5; duplicate `source_id` → no second row; intern ending before the
deadline is never proposed; Done-without-proof is the Apps Script's job (not the service's).

---

## 13. Out of scope for v1

WhatsApp bot, web dashboard, automatic assignment without approval, migrating 2026 history,
reading attachment contents, multi-mailbox intake.

## 14. Open items (build around them, do not block)

| Item | How the code handles it now |
|---|---|
| BRI AI agent API details | `BriAgentProvider` stub; Vertex or Off works meanwhile |
| Gmail option A vs B | Same code; IT mints the token for whichever mailbox |
| Mask emails for AI? | Config switch `Mask emails for AI` (default No) |
| Retention period | Not enforced in v1 |
| Weekday/date conflict policy | Keep written date + flag (D13) |
| Public invoker for `/chat` (D7) | IT to confirm org policy; otherwise Chat intake is blocked |
| `Weighted open load` counts Proposed tasks? | Resolved: yes, the Staff formula counts every row that is not Done/Cancelled |

### Sheet changes needed before go-live (found in `GRC_Tracker_Tabs_v2.xlsx`, Phase 2)

The schema check stops every job until these exist:

| Tab | Change |
|---|---|
| Master | Add columns `Original message`, `Files`, `AI summary`, `Deliverables`, `Contacts`, `Source ID`. Keep at least one data row with the `Weight` formula (it is copied to new rows). |
| Catalog | Add column `Known contacts`. |
| Staff | Fill `Role` (blank counts as Staff, so the manager is proposed until marked Manager), `Email`, and interns' `Internship end date`. |
| Config | Add `AI provider`, `AI model`, `AI region`, `Sender name`, `Mask emails for AI`, `Requester domains`, `Known requesters` (all optional, defaults apply); fill `Manager email`, `Allowed email domain`. |
| `_state`, `_failures` | Create both tabs (hidden) with the headers in section 3. |

The Staff and Master formulas use column letters and rows `2:2000`; append new Master columns at
the right and widen the ranges before Master passes 2,000 rows.

## 15. Changes in v1.1 (Phase 0 review)

1. D1: per-thread Google clients; CPU always allocated for Chat background completion.
2. D3/D7: Internal OAuth consent screen; public invoker for `/chat` noted as an IT prerequisite.
3. New `_failures` tab for failure counts and alert throttling (sections 3, 11).
4. `requester_org` rule and Config `Requester domains` / `Known requesters` (sections 3, 4, 12).
5. Drive folder reused on retry via a `Source ID` app property (section 4 step 8).
6. Gmail: newest message in the labelled thread is the request (section 7).
7. Rule 1: each keyword/contact counts once; ties → confirmed row, then Catalog ID (section 5).
8. Deadline fallback: earliest future date, flagged; extra date formats; `before` added (section 4).
9. `/jobs/changes`: the first run baselines `_state` without email (section 7).
10. Greeting/sign-off cleaning made clause-based; rules run on a light clean (section 4 step 3).
11. Weight formula copied from the last data row (section 3).
12. `AIResult` trims over-long title/summary instead of rejecting (section 6).
13. `app/clock.py` added to the layout; fixtures use fake names (sections 9, 10, 12).
14. Unknown letter code flag, blank Role = Staff; sheet changes needed before go-live (sections 5, 14).
15. Drive folder renamed after the write; text written as text; sender in Contacts; quote check
    ignores whitespace and emphasis; audit/rename failures after the write do not fail the request.
16. `GRC/Failed` replaces the intake label; label-change failures are retried by the next run.
17. Change detection as one rule (open and Doer ≠ Notified doer) with retry; recurring requests
    carry their Catalog ID and skip the AI; digest details (section 8).
