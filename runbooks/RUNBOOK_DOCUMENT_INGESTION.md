# Runbook: ingesting scanned documents (OCR)

Scanned paper charts, faxed referrals, outside-hospital records and
signed forms are part of the clinical record and have to survive an EMR
retirement like anything else. This runbook covers getting them into the
object store as searchable, patient-linked FHIR resources.

Read `docs/COMPLIANCE.md` → "Retention and integrity" first if you have
not: OCR'd documents are stored under the same posture as everything
else, which is detective rather than preventive.

---

## What happens to a document

One ingestion produces **two** stored objects:

| Object | Key | What it is |
|---|---|---|
| Source document | `documents/source/<id>.<ext>` | The scan itself, encrypted. **This is the record.** |
| `DocumentReference` | `fhir/DocumentReference/<id>.json` | FHIR R4 resource carrying the OCR text and a pointer to the source |

The `DocumentReference` goes through the ordinary ingestion path, so it is
encrypted, audited, indexed and patient-linked exactly like a resource
pulled from the source EMR. Restore, reconcile and disposition all treat it as the
`DocumentReference` it genuinely is — there is no separate document
subsystem to remember.

**The OCR text is derived, not authoritative.** CMS requires hospital
records be retained "in their original or legally reproduced form" (42
CFR 482.24(b)(1)). The text is there so records are searchable and
reviewable; the scan is what a records request is entitled to.

---

## Before you start

The OCR engine is [Tesseract](https://github.com/tesseract-ocr/tesseract),
Apache 2.0, running **entirely inside your own container**. No document
byte is sent anywhere. That is deliberate: every hosted OCR API would
mean transmitting PHI to a third party and needing a BAA under 45 CFR
164.502(e).

It needs native packages that pip cannot install. The Dockerfile already
installs them; a bare virtualenv will not have them.

```bash
python -m core.healthcheck
```

Look for `ocr.tesseract` and `ocr.poppler`. Both should be `PASS`. If
either warns, document ingestion will fail even though everything else
looks healthy — that is precisely why the check exists.

On Debian/Ubuntu:

```bash
apt-get install tesseract-ocr tesseract-ocr-eng poppler-utils
```

Supported input: PDF, PNG, JPEG, TIFF (including multi-page, common for
faxes), BMP, GIF, WebP.

---

## Ingest one document

Always dry-run first. It performs **real OCR** and shows you what would
be stored and at what quality, but writes nothing:

```bash
python -m core.fhir.documents --file ./referral.pdf --patient Patient/eAB12cd3
```

Then commit it:

```bash
python -m core.fhir.documents --file ./referral.pdf --patient Patient/eAB12cd3 --title "Cardiology referral" --confirm
```

Neither command prints the extracted text. It is PHI, and stdout ends up
in terminal scrollback, log files and CI output.

---

## You must supply the patient. The system will not guess.

`--patient` is required, and it is the operator's assertion about whose
record this is. **Nothing reads the OCR text to work it out**, and
nothing should ever be added that does.

The reason is concrete rather than theoretical. In a verification run
during development, Tesseract read a clean, machine-rendered date of
`2026-05-01` as `2028-05-01` — a two-year error, at 82% confidence, well
above the threshold that would have flagged it for review. Character
confusions between `0`/`O`, `1`/`l` and `5`/`S` are routine.

A misread digit in an MRN files one patient's record under a **different
patient**. That is a clinical safety incident and a HIPAA disclosure at
once, and nothing downstream would catch it. Deciding whose record a scan
is belongs to the person who had the document.

A malformed reference is refused outright rather than coerced, because a
document stored against no patient is invisible to restore-by-patient
and is typically discovered only when someone requests those records.

---

## Quality flags — what to do about them

Tesseract reports a mean per-word confidence. Below **60**, or when no
text is extracted at all, the resource is written with
`docStatus: "preliminary"` instead of `"final"`.

That is a standard FHIR value meaning *not verified*. It is not a
rejection — the document is fully stored either way, because a poor
scan of a real record is still that record. It is a marker so a human
reviews it, rather than a garbled page entering the object store looking
identical to a clean transcription.

**Treat `preliminary` as a work queue.** Find them after a batch:

```sql
-- Structural query only; the index holds no clinical content by design.
SELECT resource_id, patient_reference, stored_at
FROM stored_resources
WHERE resource_type = 'DocumentReference'
ORDER BY stored_at DESC;
```

`stored_resources` and its `stored_at` column are the names in
`core/db/schema.sql`; the query above is correct as printed.

`docStatus` lives in the resource itself, not the index, so confirm by
restoring the specific resource — see `RUNBOOK_DATA_RESTORE.md`.

An empty extraction is a legitimate outcome, not a bug: photographs,
blank fax cover sheets and handwriting all produce it. The source is
still stored and can be re-OCR'd later with a better engine.

---

## Re-running is safe

Document ids are derived from the patient reference plus the document
bytes, so re-ingesting the same scan for the same patient produces the
same id and the same keys. A batch that failed halfway can simply be
re-run; the duplicate index write is absorbed the same way it is for any
other resource.

The same blank form scanned for two different patients correctly yields
two distinct records — the patient reference is part of the id.

---

## Provenance, and re-OCR

Every `DocumentReference` records which engine and version produced its
text, the language, page count, mean confidence, and the SHA-256 of the
source document.

This is what makes trustworthiness answerable years later, and it means
that if a Tesseract version is ever found to have a systematic defect,
you can identify exactly which stored documents it touched and re-OCR
only those — the sources are all still there.

---

## Limits, and why they exist

| Limit | Value | Why |
|---|---|---|
| Document size | 100 MB | Untrusted bytes go to a native C++ library |
| Pages | 500 | Bounds rasterisation memory |
| OCR timeout | 300s/page | A malformed image should not hang ingestion |

Every byte here arrived from outside — a scanner, a fax gateway, another
organization's records department — and is handed to Tesseract and
poppler. The limits are enforced **before** either sees the bytes.
Raise them deliberately if a legitimate document needs it.

---

## Troubleshooting

**`OCREngineUnavailable: the tesseract binary is not installed`**
The Python wrapper is installed but the engine is not. Install the system
packages above, or run in the provided container.

**`OCREngineUnavailable: poppler is not installed`**
Images will OCR; PDFs will not. `apt-get install poppler-utils`.

**`tesseract has no language data for 'xxx'`**
Install the matching pack, e.g. `tesseract-ocr-spa`, then pass
`--language spa`.

**Everything stores but text is gibberish**
Check the source scan resolution. Below roughly 200 DPI, accuracy on body
text degrades sharply. PDFs are rasterised at 300 DPI, but that cannot
recover detail a low-resolution scan never captured. Re-scan if you can;
the stored source is unaffected either way.
