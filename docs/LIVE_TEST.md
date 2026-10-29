# Live test: the bot mailbox end to end

Run after any mailbox, token or deploy change. Everything here is done in Gmail, the sheet and
the Cloud console; no terminal. All test text below is fake.

**Before you start**
- Config `Allowed senders` includes the test sender (e.g. `@brifins.com`), and does **not** include
  the address in test 4.
- Send every test **to the bot mailbox**, from a person's account (never from the bot itself).
- Do not open the test emails in the bot's Gmail before the run: the bot only takes unread mail.
- To run now instead of waiting up to 10 minutes: Cloud console > Cloud Scheduler > `grc-gmail` >
  ⋮ > **Force run**.

| # | Send this | Expected within 10 min |
|---|-----------|------------------------|
| 1 | Subject `Permintaan data Manpower Planning`. Body: `Mohon dikirimkan Form MPP dan PPT Joint Planning Session paling lambat Jumat 13 November 2026. Terima kasih.` | New `Proposed` row in Master: English title, due date 2026-11-13, proposed owner filled. The sender gets an acknowledgement reply in the thread. The email gets `GRC/Logged`, is read and archived. |
| 2 | Reply to the acknowledgement from test 1: `Mohon tambahkan juga data intern.` | **No** new row. The test 1 row's `Notes` gets the reply text. The reply gets `GRC/Reply`. |
| 3 | Forward any email that has a PDF attached, with the note `Tolong dicek ya` above the forward. | New `Proposed` row. `Files` reads `In the original email in the GRC bot mailbox: <file name>`. `Notes` holds `Tolong dicek ya`. |
| 4 | From an address **not** in `Allowed senders` (e.g. a personal Gmail), any request. | No row, no reply. The email is marked read and labelled `GRC/Ignored`. |
| 5 | Subject `Tolong cek printer`, body `Printer lantai 3 rusak, tolong dicek.` | New `Proposed` row routed by Rule 5 (no catalog match). |
| 6 | In the sheet, set the test 1 row's `Status` to `Not Started` (with a Doer filled in). | Within 5 minutes (between 08:00 and 19:55 HK time) the Doer gets the assignment email. |

**When a test fails:** Cloud console > Logging > Logs Explorer, query

```
resource.type="cloud_run_revision" AND resource.labels.service_name="v2-git" AND (jsonPayload.message="gmail_run" OR jsonPayload.message="message_ignored" OR severity>=WARNING)
```

| Log line | Meaning |
|----------|---------|
| `message_ignored` reason `sender_not_allowed` | Add the sender or its `@domain` to Config `Allowed senders`. |
| `message_ignored` reason `sent_by_bot_mailbox` | The test was sent from the bot itself; send from a person. |
| `no_allowed_senders` | Config has no `Allowed senders` row, or it is spelled differently. |
| `gmail_auth_error` reason `RefreshError: invalid_grant` | The stored token is wrong or revoked; mint a new one. |
| `schema_problem` | The sheet is missing the tab or column named in `reason`. |

**Clean up:** delete the test rows from Master (or set them to `Cancelled`) when done.
