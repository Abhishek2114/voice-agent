"""CSV import pipeline: parse → map → validate → dedupe → preview → commit.

* Never treats an uploaded phone number as consent. Consent columns are only recorded
  when the operator maps them *and* each row carries explicit evidence.
* Formula-like cells are neutralised (CSV injection).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.contacts.csv_safety import is_formula_like, neutralize
from app.contacts.phone import normalize_phone
from app.database.models import ConsentRecord, Contact, ContactCustomField

MAX_ROWS = 50_000
TARGET_FIELDS = ("first_name", "last_name", "full_name", "phone", "email", "company", "tags", "notes",
                 "consent_purpose", "consent_source", "consent_evidence", "consent_obtained_at", "dnd_status")  # fmt: skip
CONSENT_PURPOSES = {"promotional", "service", "transactional", "inquiry_followup"}
CONSENT_SOURCES = {"verbal_call", "web_form", "written", "digital_consent", "customer_inquiry", "other"}

GUESSES = {
    "first_name": ["first name", "firstname", "first", "given name", "name"],
    "last_name": ["last name", "lastname", "surname", "family name"],
    "full_name": ["full name", "fullname", "contact name"],
    "phone": ["phone", "mobile", "phone number", "mobile number", "contact number", "number", "cell", "whatsapp"],
    "email": ["email", "e-mail", "email address", "mail"],
    "company": ["company", "organisation", "organization", "business"],
    "tags": ["tags", "labels", "segment"],
    "notes": ["notes", "note", "comments", "remarks"],
    "consent_purpose": ["consent purpose", "consent_purpose"],
    "consent_source": ["consent source", "consent_source"],
    "consent_evidence": ["consent evidence", "consent_evidence", "consent proof"],
    "consent_obtained_at": ["consent date", "consent_obtained_at", "consent timestamp", "consented at"],
    "dnd_status": ["dnd", "dnd status", "dnd_status"],
}


class CSVError(ValueError):
    pass


def parse_csv(data: bytes) -> tuple[list[str], list[list[str]]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise CSVError("File must be UTF-8 encoded CSV") from exc
    if "\x00" in text:
        raise CSVError("File contains binary data")
    sample = text[:8192]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        rows = [r for r in reader if any(c.strip() for c in r)]
    except csv.Error as exc:
        raise CSVError(f"Malformed CSV: {exc}") from exc
    if not rows:
        raise CSVError("CSV is empty")
    headers = [h.strip()[:80] or f"column_{i + 1}" for i, h in enumerate(rows[0])]
    if len(headers) > 100:
        raise CSVError("Too many columns (max 100)")
    if len(set(h.lower() for h in headers)) != len(headers):
        raise CSVError("Column headers must be unique")
    body = rows[1:]
    if len(body) > MAX_ROWS:
        raise CSVError(f"Too many rows (max {MAX_ROWS})")
    return headers, body


def guess_mapping(headers: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for h in headers:
        key = h.strip().lower().replace("_", " ")
        for target, aliases in GUESSES.items():
            if target in used:
                continue
            if key == target.replace("_", " ") or key in aliases:
                mapping[h] = target
                used.add(target)
                break
    if "first_name" in used and "full_name" not in used and "last_name" not in used:
        # a single "name" column is better treated as a full name
        for h, t in mapping.items():
            if t == "first_name" and h.strip().lower() == "name":
                mapping[h] = "full_name"
    return mapping


@dataclass
class RowResult:
    row: int
    status: str  # valid | invalid | duplicate_in_file | duplicate_existing
    data: dict[str, Any]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _parse_dt(v: str) -> datetime | None:
    v = v.strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(v.replace("Z", ""), fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(v)
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


async def validate_rows(db: AsyncSession, ws_id: str, headers: list[str], rows: list[list[str]],
                        mapping: dict[str, str], default_country: str,
                        custom_columns: list[str] | None = None) -> list[RowResult]:  # fmt: skip
    idx = {h: i for i, h in enumerate(headers)}
    custom_columns = [c for c in (custom_columns or []) if c in idx and c not in mapping]
    existing_phones = set((await db.execute(select(Contact.phone_e164).where(Contact.workspace_id == ws_id,
                           Contact.deleted_at.is_(None), Contact.phone_e164.is_not(None)))).scalars())  # fmt: skip
    existing_emails = set((await db.execute(select(func.lower(Contact.email)).where(Contact.workspace_id == ws_id,
                           Contact.deleted_at.is_(None), Contact.email.is_not(None)))).scalars())  # fmt: skip
    seen_phones: set[str] = set()
    seen_emails: set[str] = set()
    out: list[RowResult] = []
    for n, row in enumerate(rows, start=2):
        vals: dict[str, str] = {}
        warnings: list[str] = []
        for header, target in mapping.items():
            if header in idx and target in TARGET_FIELDS:
                raw = row[idx[header]].strip() if idx[header] < len(row) else ""
                if is_formula_like(raw) and target != "phone":
                    warnings.append(f"{target}: formula-like value neutralised")
                    raw = neutralize(raw)
                vals[target] = raw[:2000]
        errors: list[str] = []
        data: dict[str, Any] = {}
        if vals.get("full_name") and not (vals.get("first_name") or vals.get("last_name")):
            first, _, last = vals["full_name"].partition(" ")
            data["first_name"], data["last_name"] = first[:120], last[:120]
        else:
            data["first_name"], data["last_name"] = vals.get("first_name", "")[:120], vals.get("last_name", "")[:120]
        phone = normalize_phone(vals.get("phone"), default_country) if vals.get("phone") else None
        if phone is not None:
            if phone.valid:
                data["phone_e164"], data["phone_raw"] = phone.e164, vals.get("phone")
            else:
                errors.append(f"phone: {phone.error}")
        email = vals.get("email", "").lower()
        if email:
            if "@" not in email or "." not in email.split("@")[-1] or len(email) > 254 or " " in email:
                errors.append("email: invalid address")
            else:
                data["email"] = email
        if not (data.get("phone_e164") or data.get("email")) and not errors:
            errors.append("row has neither a valid phone nor an email")
        data["company"] = vals.get("company", "")[:160] or None
        data["notes"] = vals.get("notes", "")
        data["tags"] = [t.strip().lower()[:40] for t in vals.get("tags", "").replace(";", ",").split(",") if t.strip()][:30]
        dnd = vals.get("dnd_status", "").strip().lower()
        if dnd:
            if dnd in ("yes", "y", "registered", "true", "1"):
                data["dnd_status"] = "registered"
            elif dnd in ("no", "n", "not_registered", "not registered", "false", "0"):
                data["dnd_status"] = "not_registered"
            else:
                warnings.append("dnd_status: unrecognised value ignored")
        # consent: recorded only when purpose, source AND evidence are present and valid
        purpose = vals.get("consent_purpose", "").strip().lower()
        if purpose:
            source = vals.get("consent_source", "").strip().lower()
            evidence = vals.get("consent_evidence", "").strip()
            obtained = _parse_dt(vals.get("consent_obtained_at", ""))
            if purpose not in CONSENT_PURPOSES:
                warnings.append("consent ignored: unknown purpose")
            elif source not in CONSENT_SOURCES:
                warnings.append("consent ignored: missing/unknown source")
            elif len(evidence) < 3:
                warnings.append("consent ignored: no evidence provided")
            elif obtained is None:
                warnings.append("consent ignored: missing/invalid consent date")
            else:
                data["consent"] = {"purpose": purpose, "source": source, "evidence": evidence[:2000],
                                   "obtained_at": obtained.isoformat()}  # fmt: skip
        data["custom_fields"] = {}
        for col in custom_columns:
            v = row[idx[col]].strip() if idx[col] < len(row) else ""
            if v:
                key = "".join(ch if ch.isalnum() else "_" for ch in col.lower()).strip("_")[:64] or "field"
                if not key[0].isalpha():
                    key = f"f_{key}"[:64]
                data["custom_fields"][key] = neutralize(v)[:2000]
        status = "invalid" if errors else "valid"
        if status == "valid":
            p, e = data.get("phone_e164"), data.get("email")
            if (p and p in seen_phones) or (e and e in seen_emails):
                status = "duplicate_in_file"
            elif (p and p in existing_phones) or (e and e in existing_emails):
                status = "duplicate_existing"
            if p:
                seen_phones.add(p)
            if e:
                seen_emails.add(e)
        out.append(RowResult(n, status, data, errors, warnings))
    return out


def summarize(results: list[RowResult]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return {
        "total": len(results),
        "valid": counts.get("valid", 0),
        "invalid": counts.get("invalid", 0),
        "duplicate_in_file": counts.get("duplicate_in_file", 0),
        "duplicate_existing": counts.get("duplicate_existing", 0),
        "with_consent": sum(1 for r in results if r.status == "valid" and r.data.get("consent")),
        "warnings": sum(len(r.warnings) for r in results),
    }


async def commit_rows(db: AsyncSession, ws_id: str, results: list[RowResult], *, update_existing: bool,
                      extra_tags: list[str], user_id: str, source: str) -> dict[str, int]:  # fmt: skip
    created = updated = consents = 0
    for r in results:
        if r.status == "valid" or (r.status == "duplicate_existing" and update_existing):
            d = r.data
            contact = None
            if r.status == "duplicate_existing":
                conds = []
                if d.get("phone_e164"):
                    conds.append(Contact.phone_e164 == d["phone_e164"])
                if d.get("email"):
                    conds.append(func.lower(Contact.email) == d["email"])
                from sqlalchemy import or_

                contact = (await db.execute(select(Contact).where(Contact.workspace_id == ws_id,
                           Contact.deleted_at.is_(None), or_(*conds)).limit(1))).scalar_one_or_none()  # fmt: skip
            tags = list(dict.fromkeys([*d.get("tags", []), *extra_tags]))
            if contact is None:
                contact = Contact(workspace_id=ws_id, first_name=d["first_name"], last_name=d["last_name"],
                                  phone_raw=d.get("phone_raw"), phone_e164=d.get("phone_e164"), email=d.get("email"),
                                  company=d.get("company"), notes=d.get("notes", ""), tags=tags, source=source,
                                  status="new", custom_fields=[])  # fmt: skip
                db.add(contact)
                created += 1
            else:
                contact.first_name = d["first_name"] or contact.first_name
                contact.last_name = d["last_name"] or contact.last_name
                contact.company = d.get("company") or contact.company
                contact.tags = list(dict.fromkeys([*(contact.tags or []), *tags]))
                updated += 1
            if d.get("dnd_status"):
                contact.dnd_status = d["dnd_status"]
                contact.dnd_checked_at = datetime.now(UTC)
            await db.flush()
            for k, v in (d.get("custom_fields") or {}).items():
                existing = next((f for f in contact.custom_fields if f.key == k), None)
                if existing:
                    existing.value = v
                else:
                    contact.custom_fields.append(ContactCustomField(workspace_id=ws_id, key=k, value=v))
            c = d.get("consent")
            if c:
                db.add(ConsentRecord(workspace_id=ws_id, contact_id=contact.id, phone_e164=contact.phone_e164,
                                     purpose=c["purpose"], source=c["source"],
                                     evidence=f"[imported] {c['evidence']}", recorded_by=user_id,
                                     obtained_at=datetime.fromisoformat(c["obtained_at"])))  # fmt: skip
                consents += 1
    await db.flush()
    return {"created": created, "updated": updated, "consents_recorded": consents}
