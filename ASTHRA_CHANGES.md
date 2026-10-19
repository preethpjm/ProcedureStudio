# ASTHRA changes for Procedure Studio

These are additive changes. No existing endpoint, schema or behaviour changes. With neither of the new environment
variables set, ASTHRA behaves as before, plus the integration endpoints.

## Changed files

| File | Change |
|---|---|
| `backend/asthra/integrations/__init__.py` | new package |
| `backend/asthra/integrations/studio.py` | new: `StudioIntegration` (status, publish, links) and the `integration_link` table |
| `backend/asthra/api/app.py` | four endpoints under `/api/integrations/studio/`, optional token check, optional extra allowed hosts (see `app_py.diff`) |
| `frontend/src/App.tsx` | opens `?project=<id>&doc=<id>` deep links (see `App_tsx.diff`) |
| `backend/tests/test_studio_integration.py` | new: 5 tests |
| `backend/tests/fixtures/studio/` | new: the two evaluation schema packages, four Studio-generated documents and a reconciliation file |

## Endpoints

| Method and path | Purpose |
|---|---|
| `GET /api/integrations/studio/status` | ASTHRA version, installed packages (standard, issue, doc types, provenance), and the Studio targets each one can validate (`s1000d_proced`, `s1000d_ipd`, `ata_cmm`), plus whether a token is required |
| `POST /api/integrations/studio/publish` | multipart `manifest` (JSON) + `files`. Finds or creates the project by name. For each document: if its stable key (e.g. `S1000D-530`) is already linked, commits a new revision, or reports `unchanged` when the bytes are identical; otherwise imports it and selects the installed schema. Returns per-document validation statuses, counts and diagnostics. Optional `knowledge` entries (reconciliation JSON + GLB) go through the existing engineering-set import, so *locate* works on the Studio part numbers. One failing document never stops the others. |
| `GET /api/integrations/studio/links?project=` | Studio key → ASTHRA document links |
| `POST /api/integrations/studio/packages` | install a schema package zip (same path as the UI's package install) |

## Security (opt-in)

* `ASTHRA_INTEGRATION_TOKEN`: when set, the integration endpoints require the `x-asthra-token` header with this value.
  Set the same value in Studio.
* `ASTHRA_ALLOWED_HOSTS=asthra,asthra.internal`: extra host names accepted in addition to loopback, for when Studio
  reaches ASTHRA by a service name.
* Write requests still need ASTHRA's `x-asthra: 1` CSRF header; the Studio connector sends it.

## Verification

* `pytest tests/test_studio_integration.py`: 5 passed. The rest of the suite is unchanged by this work.
  Its pre-existing failures come from fixtures that were not in the codebase dump.
* Frontend: `tsc --noEmit` is clean and `vitest`: 50 passed. A Studio **Open in ASTHRA** link opens the published
  document in the editor, with its schema status.
* Live: Studio published 530, 710, 941 and the CMM into a running ASTHRA. All four were structurally valid. The only
  warning was that the S1000D BREX is not installed. Publishing again revised the same documents.
