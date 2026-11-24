# Compliance

voice-agent includes engineering controls for running calling campaigns responsibly, with defaults aimed at India. This document explains what those controls do, how they work in the code, and the regulatory background they are designed around.

> **This is not legal advice.** voice-agent does **not** guarantee compliance with TRAI regulations, the DPDP Act or any other law. The regulatory summary below is drawn mostly from secondary sources (law-firm and vendor articles) and may be incomplete, out of date or wrong. Before making real calls, verify the current rules against the official TRAI and Government of India texts, and talk to a lawyer and to your telecom access provider. You, the operator, are responsible for how you use the software.

## Contents

- [Principles built into the software](#principles-built-into-the-software)
- [Workspace compliance policy](#workspace-compliance-policy)
- [The eligibility engine (fails closed)](#the-eligibility-engine-fails-closed)
- [Consent records](#consent-records)
- [Suppression list and opt-outs](#suppression-list-and-opt-outs)
- [DND / NCPR scrub import](#dnd--ncpr-scrub-import)
- [Calling window](#calling-window)
- [Frequency caps and attempts](#frequency-caps-and-attempts)
- [Disclosures: AI identity and recording](#disclosures-ai-identity-and-recording)
- [Real-calling (PSTN) prerequisites](#real-calling-pstn-prerequisites)
- [Retention](#retention)
- [Erasure workflow](#erasure-workflow)
- [Audit log](#audit-log)
- [Regulatory background: India](#regulatory-background-india)
- [Data protection: DPDP Act 2023](#data-protection-dpdp-act-2023)
- [What remains your responsibility](#what-remains-your-responsibility)
- [Sources](#sources)

## Principles built into the software

* **A CSV upload is never treated as consent.** A phone number in an imported file means nothing about consent. The import wizard records consent only when the operator maps the consent columns *and* the row carries a valid purpose, source, date and evidence (`backend/app/imports/service.py`). The dashboard and API show this rule during import.
* **Simulated calls never affect real contacts.** In `SIMULATION` mode, opt-outs and lead captures are not applied to the database (they appear only in the call's tool log). Callback requests are flagged as simulated, and simulated outcomes never change a contact's CRM status (`DbToolHost` and `persist_conversation` in `backend/app/calls/service.py`). Frequency caps count calls per mode, so simulations do not use up a contact's real-call allowance.
* **Fail closed.** If any check cannot be completed (missing data, a corrupted policy, an internal error), the call is blocked. A corrupted stored policy falls back to the conservative defaults, never to looser settings.
* **Real calling is off by default** and requires several separate steps (see [Real-calling prerequisites](#real-calling-pstn-prerequisites)).
* **The agent cannot complete transactions.** Callback and appointment *requests* are recorded for a human to act on.

## Workspace compliance policy

Each workspace has its own policy (`CompliancePolicy` in `backend/app/compliance/policy.py`), edited under **Compliance** in the dashboard by workspace admins. Every change is audited with the changed fields.

| Setting | Default | Meaning |
|---|---|---|
| `ai_disclosure_required` | `true` | Add an AI-identity statement to the opening line if the agent's greeting does not already contain one. |
| `ai_disclosure_text` | "Hello, this is an AI assistant calling on behalf of {business}." | Used for phone and simulated calls. |
| `recording_enabled` | `false` | Record calls (stereo WAV). |
| `recording_disclosure_text` | "This call may be recorded for quality purposes." | Spoken whenever recording is on. |
| `calling_window_start` / `_end` | `09:00` / `21:00` | Permitted calling hours, in the workspace timezone (default `Asia/Kolkata`, i.e. IST). |
| `calling_days` | Mon–Sat (`[0..5]`) | Permitted weekdays (0 = Monday). |
| `max_calls_per_contact_per_day` | 1 | Per phone number, in the last 24 hours. |
| `max_calls_per_contact_per_week` | 3 | Per phone number, in the last 7 days. |
| `consent_required_for` | promotional, service, inquiry_followup | Purposes that need an active consent record. Transactional does not by default. |
| `inquiry_followup_days` | 7 | How long consent from a customer inquiry is valid for follow-up calls. |
| `dnd_check_required_for` | promotional | Purposes that need a recent DND scrub result. |
| `dnd_max_age_days` | 30 | A DND scrub older than this counts as unknown. |
| `retention_days_recordings` | 90 | Recordings are deleted after this many days. |
| `retention_days_transcripts` | 365 | Transcripts and per-turn messages are deleted after this many days. |
| `pstn_authorized`, `telemarketer_registration_id`, `registered_number_series`, `a2p_declared_to_provider` | unset | Operator attestations required for real calls. |

The policy is validated: times must be `HH:MM`, the start must be before the end, days must be 0–6, and the weekly limit must be at least the daily limit.

## The eligibility engine (fails closed)

Every campaign call runs through `check_eligibility` (`backend/app/compliance/service.py`) before a job is created. The same engine powers the **eligibility panel** on each contact's page and the campaign preview (`POST …/compliance/eligibility`, `POST …/campaigns/{id}/preview`). The checks:

1. **Phone**: the contact has a valid E.164 number (normalised with libphonenumber).
2. **Contact status**: not deleted, and not `do_not_call` or `wrong_number`.
3. **Suppression**: the number is not on the workspace suppression list (unexpired entries).
4. **Consent**: if the campaign purpose requires consent, there must be a `granted`, unrevoked, unexpired consent record for that purpose, matched by contact or phone number. `inquiry_followup` consent must also have been obtained within `inquiry_followup_days`.
5. **DND**: if the purpose requires a DND check:
   * `not_registered` passes only if the scrub is newer than `dnd_max_age_days`;
   * `registered` blocks, unless there is active consent for that purpose with source `digital_consent`;
   * `unknown` or stale blocks.
6. **Calling window**: the current time is inside the *effective* window (below), in the campaign's timezone.
7. **Frequency**: calls of the same mode to this number in the last 24 hours and 7 days are below the caps.
8. **Attempts**: the campaign's `max_attempts` has not been reached.
9. **PSTN prerequisites** (real calls only): `VA_REAL_TELEPHONY_ENABLED=true` and all four workspace attestations are recorded.

A call is allowed only if **every** check passes. Each result carries a human-readable reason. The scheduler handles failures as follows:

* blocked **only** by the calling window: the contact waits until the next window opens;
* blocked only by frequency caps: retried a day later;
* anything else: the contact is marked `skipped` (or `exhausted` when only attempts are used up), and the reasons are stored and written to the audit log as `compliance.call_blocked`.

## Consent records

A `ConsentRecord` (`consent_records` table) stores:

| Field | Values |
|---|---|
| `purpose` | `promotional`, `service`, `transactional`, `inquiry_followup` |
| `source` | `verbal_call`, `web_form`, `written`, `digital_consent`, `customer_inquiry`, `other` |
| `evidence` | Free text, required (where and how consent was captured, e.g. a form ID or call ID) |
| `obtained_at`, `expires_at` | Timestamps. `expires_at` must be after `obtained_at`. |
| `status`, `revoked_at` | `granted` or `revoked` |
| `recorded_by`, `call_id` | Who recorded it, and the call it came from (for verbal consent) |

Consent can be recorded in three ways:

* manually on a contact's page (editor role);
* through a CSV import with explicit evidence columns;
* during a real call, when the person explicitly agrees to follow-up and the agent sets `consent_to_contact: true` in `capture_lead`. That creates an `inquiry_followup` consent with source `verbal_call`, the call ID as evidence, and an expiry of `inquiry_followup_days`. The transcript is kept as supporting evidence, subject to retention.

Revocation (from the dashboard or API, or automatically on opt-out) sets `status = revoked` and `revoked_at`. Revoked records are kept for evidence.

## Suppression list and opt-outs

The suppression list (`suppression_entries`, one row per workspace and number) blocks every campaign in the workspace from calling a number. Reasons: `opt_out`, `dnd_registry`, `manual`, `wrong_number`, `erasure`.

An opt-out can come from:

* **During a real call.** The caller asks not to be contacted, and the agent calls `record_opt_out` (the system prompt instructs it to).
* **Manually.** An editor adds the number with reason `opt_out`.

Either way, `record_opt_out`:

1. adds the number to the suppression list;
2. sets the contact's status to `do_not_call`;
3. revokes every active consent record for that number in the workspace;
4. writes a `compliance.opt_out` audit entry;
5. ends the call (for an in-call opt-out).

Removing a suppression entry requires the **admin** role and is audited. Simulated calls never add real numbers to the suppression list.

## DND / NCPR scrub import

India's DND preferences (the National Customer Preference Register) cannot be queried through any public API, as far as this project knows. voice-agent therefore **does not look up DND status itself**. Instead, you obtain scrub results through your authorised channel (typically your access provider or the DLT platform you are registered on) and import them:

* Dashboard: **Compliance → Apply DND scrub results**
* API: `POST /api/workspaces/{id}/compliance/dnd-scrub` with `entries: [{phone, status: "registered" | "not_registered"}]`, an optional `checked_at` and a `source` label (admin role).

Each matching contact gets `dnd_status` and `dnd_checked_at`. The response reports how many contacts were updated and how many numbers were invalid or unmatched, and the import is audited. Results older than `dnd_max_age_days` count as unknown, and **unknown blocks** any purpose that requires a DND check.

## Calling window

* The **workspace window** (default 09:00–21:00, Monday–Saturday, in the workspace timezone, which defaults to IST) is the outer limit.
* A **campaign** has its own window (default 10:00–19:00, Monday–Saturday) and timezone. The effective window is the **intersection**: the later start, the earlier end, and only days allowed by both. A campaign can narrow the workspace window but never widen it.
* A campaign whose window does not overlap the workspace window cannot be started (`400 no_calling_window`).
* Simulation campaigns can be configured to ignore the calling window (`simulation.ignore_calling_window`), so they can be tested at any hour. This option has no effect on real calls.

The 09:00–21:00 default is a conservative engineering choice, not a statement of the legal limit. One secondary source describes 10:00–19:00 as an access-provider convention for promotional calls. Check what applies to you, and set the workspace window accordingly.

## Frequency caps and attempts

* At most `max_calls_per_contact_per_day` (default 1) and `max_calls_per_contact_per_week` (default 3) calls per number, counted per calling mode.
* Each campaign has `max_attempts` (1–10, default 2) and `retry_delay_minutes` (default 60) for no-answer, busy or failed outcomes. Attempts that fail because of a local provider outage (for example Ollama being down) are not counted, and the campaign pauses itself after three consecutive provider failures.

## Disclosures: AI identity and recording

* **AI identity.** When `ai_disclosure_required` is on and the agent's greeting does not already mention being an AI or automated assistant, an identity statement is added before the greeting (`compose_greeting` in `backend/app/agents/runtime.py`). Phone and simulated calls use `ai_disclosure_text`. Browser calls use "Hi, you're speaking with {agent}, an AI assistant for {business}." Independently of this, the system prompt tells every agent to say honestly that it is an AI if asked. Public link pages show "You will be speaking with an AI assistant for {business}." before the call starts.
* **Recording.** When recording is on, `recording_disclosure_text` is added to the opening line, and public link pages show that the call is recorded. Recordings are owner-only files, and every playback is audited.

## Real-calling (PSTN) prerequisites

Real phone calls need **all** of the following:

1. `VA_REAL_TELEPHONY_ENABLED=true` in `.env`, set by whoever runs the installation;
2. an Asterisk ARI provider configured and enabled by an instance admin (**Settings → Telephony**);
3. four **attestations** recorded by a workspace admin under **Compliance → Real phone calls**:
   * `pstn_authorized`: the workspace is authorised for real calling;
   * `telemarketer_registration_id`: your telemarketer or entity registration ID (for example your DLT registration);
   * `registered_number_series`: the calling number series registered for you (for example 140xx for promotional or 160xx for service/transactional calls);
   * `a2p_declared_to_provider`: automated (A2P) calling has been declared to your telecom provider.

These are **attestations**. voice-agent records what you state and refuses PSTN campaigns until every field is filled in. It cannot verify the registration with TRAI or your provider. The checks are applied when a PSTN campaign is created or started, for each call by the eligibility engine, and again immediately before dialling. See [TELEPHONY.md](TELEPHONY.md).

## Retention

Roughly every 30 minutes the scheduler applies each workspace's retention policy:

* recordings older than `retention_days_recordings` (default 90) are deleted from disk and marked deleted;
* transcripts and per-turn messages for calls older than `retention_days_transcripts` (default 365) are deleted.

Retention does **not** remove call records (date, outcome, summary), the tool log of a conversation (which can include details a caller gave, such as a phone number), contacts, consent records or audit entries. Remove those with deletion or erasure if needed. Backups keep whatever they contained when they were taken.

## Erasure workflow

`POST /api/workspaces/{id}/contacts/{contact_id}/erase` (admin role, also available on the contact page) handles a data-deletion request for one contact:

* deletes recording files and rows for the contact's calls;
* deletes transcripts and per-turn messages, and clears conversation tool logs and effects;
* clears call summaries and phone numbers on those calls, and unlinks them from the contact;
* deletes the contact's consent records and callback requests;
* deletes the contact;
* by default (`keep_suppression=true`) **keeps the phone number on the suppression list** with reason `erasure`, the minimum needed to make sure the person is never called again. Pass `keep_suppression=false` to remove that too;
* writes a `contact.erased` audit entry with counts (no personal data).

Known gaps:

* Erasure does **not** remove data already copied into backups.
* It does not clear the stored system prompt of past conversations, which can contain the contact's first name ("You are speaking with …").
* It does not remove mentions of the person inside other records, such as free-text notes on other contacts.

Rotate or delete old backups as part of your process.

## Audit log

Compliance-relevant events are written to the `audit_logs` table in the same transaction as the change. They include: consent recorded or revoked, suppression added or removed, opt-outs, DND scrub imports, blocked calls (with the failed checks), policy changes (with changed fields), lead captures, recording access, contact erasure, exports, and imports. Workspace admins can browse and filter it under **Compliance → Audit log**.

## Regulatory background: India

The summary below is a **best-effort orientation, not an authoritative statement of the law.** Most figures come from secondary summaries (listed under [Sources](#sources)), and those sources do not always agree. Verify everything against the official TRAI text and with your lawyer and access provider.

### TCCCPR 2018

The **Telecom Commercial Communications Customer Preference Regulations, 2018 (TCCCPR)**, issued by the Telecom Regulatory Authority of India (TRAI), regulate commercial voice calls and SMS. In outline:

* **Customer preferences / DND.** Subscribers can register preferences to block commercial communication through the National Customer Preference Register (NCPR), widely known as DND. Promotional communication to registered numbers is restricted unless the subscriber has given explicit consent in the prescribed (digital) form.
* **Registration on DLT.** Senders (principal entities) and telemarketers register with access providers on their Distributed Ledger Technology (DLT) platforms. Headers, consent templates and content templates are registered there.
* **Categories.** Communications are classified as promotional, service, transactional and (after the 2025 amendment) government. Different consent rules apply to each.
* **Unsolicited commercial communication (UCC)** is communication that does not match the recipient's consent or preferences, or comes from an unregistered sender.

### 2025 amendment (February 2025)

Secondary sources describe an amendment made on **12 February 2025** (Gazette publication reported as 18 February 2025; most provisions effective 30 days later, some after 60 days). The reported changes include:

* **Number series.** Promotional voice calls must use the **140 series**, and service and transactional voice calls the **160 series** (described as 1600xx, with a 1601 sub-prefix reported for RBI/SEBI/PFRDA/IRDAI-regulated entities). Using ordinary 10-digit numbers for commercial calls can lead to disconnection and blacklisting.
* **Auto-dialers and robocalls.** Senders must notify their originating access provider in advance about using auto-dialers or robocalls.
* **Complaints.** The complaint window is extended (reported as 3 → 7 days). Action against a sender is triggered at a lower threshold (reported as 5 complaints in 10 days, previously 10 in 7). Complaints have simpler validity requirements.
* **Consent.** Mandatory opt-out in promotional messages; a 90-day bar on asking again after an opt-out; inferred consent limited to the duration of the contract. A 7-day validity is reported for certain explicit consents, but sources disagree on which category this applies to.
* **Penalties** on access providers and escalating sanctions on senders, as listed in the sources.

### Third Amendment (reported, 2026)

TRAI press release **No. 119 of 2026** (a scanned PDF dated September 2026) and secondary coverage describe the **Telecom Commercial Communications Customer Preference (Third Amendment) Regulations, 2026**. As reported:

* **Application-to-person (A2P) calls** (calls started by software or an automated platform without direct human dialling, including autodialled, prerecorded and **artificial/AI-generated voice** calls) are brought explicitly under the framework.
* Entities making automated calls must **declare the activity and their originating numbers to their access provider**. **Undeclared automated calls are treated as UCC.**
* A **terminating charge of up to ₹0.05 (5 paise) per minute** may apply to A2P calls, with exemptions for calls from TRAI-designated commercial series (reported as **140xx, 1600xx and 1601xx**) and other authorised calls.
* Access providers must use **AI-based spam detection**, share intelligence across networks, and may act after fewer complaints (reported as three unique complaints when the AI system also flags the number).
* **Promotional follow-up after a consumer inquiry** is reported as permitted **for seven days only**, with the inquiry retained in verifiable form. voice-agent's `inquiry_followup_days` default of 7 mirrors this.
* We could not confirm a commencement date from the sources consulted.

These points come from a scanned press release and secondary summaries. Treat them as reported, not confirmed, until you have read the notified regulation.

### How this maps to voice-agent

| Reported requirement | What voice-agent provides | What it cannot do |
|---|---|---|
| Register as sender or telemarketer on DLT; use 140/160 series | Attestation fields; PSTN blocked until filled | Register you, verify registration, or obtain numbers |
| Declare automated (A2P) calls and originating numbers to the access provider | `a2p_declared_to_provider` attestation | Make or verify the declaration |
| Respect DND/NCPR preferences | DND scrub import, freshness check, block on unknown or registered | Query NCPR (no public API known) |
| Consent and opt-out | Consent records with evidence and expiry, opt-outs that suppress and revoke | Judge whether your consent wording or capture method is legally valid |
| Inquiry follow-up window | `inquiry_followup` consent limited to 7 days (configurable) | — |
| Identify the caller as automated | AI disclosure in the opening line | — |
| Charges | — | Calls to Indian numbers may incur carrier charges, including any A2P terminating charge. Check with your provider. |

**Never use voice-agent to bypass DND preferences, caller-ID or number-series rules, or consent requirements.**

## Data protection: DPDP Act 2023

India's **Digital Personal Data Protection Act, 2023** (and its rules, as notified) applies to processing digital personal data, such as contact details, call transcripts and recordings. Some general considerations for operators (again, not legal advice):

* **Notice and purpose.** Tell people what personal data you collect and why. Use it only for the stated purpose. voice-agent's consent records store a purpose for each consent, and the AI and recording disclosures support transparency during calls.
* **Consent and withdrawal.** Where you rely on consent, make it easy to withdraw. Opt-outs revoke consent and suppress the number immediately.
* **Data minimisation and retention.** Keep only what you need for as long as you need it. Recording is off by default, and retention periods are configurable.
* **Erasure and correction.** Use the erasure workflow for deletion requests and the contact editor for corrections. Note the gaps listed under [Erasure workflow](#erasure-workflow).
* **Security safeguards.** See [SECURITY.md](SECURITY.md). Data stays on your Mac, and no AI processing is sent to a cloud service, which reduces cross-border transfer questions. You remain responsible for securing the machine and any backups.

## What remains your responsibility

* Registering with your access provider or DLT platform, obtaining 140/160-series numbers, and declaring automated calling.
* Obtaining DND scrub results through your authorised channel and importing them regularly.
* Making sure consent was genuinely obtained, is recorded with evidence, and covers the purpose of each campaign.
* Choosing calling hours, frequency limits and retention periods that meet the law and your provider's terms.
* The content of your agents' scripts and knowledge (no misleading claims; prices must be accurate).
* Telling callers about recording, and handling data-subject requests.
* Checking for regulatory changes. The rules described here have changed several times since 2018.

## Sources

The regulatory summary above is based on these sources. The figures were taken from secondary summaries and must be verified against the official TRAI text and with your lawyer and access provider.

* TRAI press release No. 119 of 2026: <https://www.trai.gov.in/sites/default/files/2026-09/PR_No119of2026.pdf>
* PolicyEdge, "TRAI finalises new spam rules covering AI detection, robocalls and consumer appeals": <https://www.policyedge.in/p/trai-finalises-new-spam-rules-covering-ai-detection-robocalls-and-consumer-appeals>
* Securiti, "India spam rules: TRAI latest amendment": <https://securiti.ai/india-spam-rules-trai-latest-amendment/>
* Sigma Chambers, "2025 TCCCPR amendments: a renewed push by TRAI for order in commercial communications": <https://www.sigmachambers.in/post/2025-tcccpr-amendments-a-renewed-push-by-trai-for-order-in-commercial-communications-1>
* FreJun, "Auto-dialer, robocall and 140-series TRAI rules in India" (vendor article): <https://frejun.com/auto-dialer-robocall-140-series-trai-rules-india/>
