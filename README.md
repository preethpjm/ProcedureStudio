# Procedure Studio

Engineering inputs in, reviewed technical publications out. Procedure Studio takes a product's **STEP model**, its
**MBOM / APL** and its **assembly drawing**, and proposes the **Illustrated Parts List** and the **STE disassembly
and assembly procedures**. A writer then checks every proposal against the 3D model. The outputs are S1000D data
modules, an ATA iSpec 2200 CMM, or the TechPub IPL Excel file. Studio **checks every document itself**: against the
installed schema of the standard, the BREX the document names, your own rule sets (Schematron or XPath rules), after
any transform stylesheets. Only then does it publish them.

When a new drawing or 3D model arrives, Studio compares it with the signed-off baseline. It lists what changed and
which procedure steps that touches, keeps the unaffected step text word for word, and writes a modification
procedure.

```
STEP ──► GLB + product model (occurrences, transforms, geometry, BOM tree)
            │
            ├─► contacts (FCL) ─► blocking directions (ray casting) ─► disassembly sequence
MBOM/APL ───┼─► 3D ↔ MBOM reconciliation (stp2glb reconciler: exact, variant, supersession, fuzzy, FR→EN)
Drawing ────┴─► torques, callouts, notes
                         │
                         ▼
               proposed IPL  ──► writer review (accept, edit, link to 3D)
                         │
                         ▼
               process graph (unfasten/remove/install/fasten/apply/lockwire/test …)
                         │
                         ▼
               STE procedures (templates; optional LLM rewording, always re-checked)
                         │
        ┌────────────────┼──────────────────┐
        ▼                ▼                  ▼
   S1000D 530/710/941   ATA CMM XML    TechPub IPL Excel + editor JSON
        │                │
        ▼                ▼
   transform stylesheets ─► check: integrity → well-formed → schema (XSD 1.0/1.1, DTD, IDs) → BREX + rule sets
                         │
                         ▼
               publish the checked files (folder, or any connector)
```

## Run it

```bash
cd backend
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp ../studio.example.yaml studio.yaml            # optional; environment variables also work
python -m studio --config studio.yaml            # http://127.0.0.1:8790
```

The built UI ships in `backend/studio/web`. To work on the UI, run `cd frontend && npm ci && npm run dev`. The dev
server is at http://127.0.0.1:5173 and proxies `/api` to the backend. `npm run build` typechecks the UI and writes it
back into `backend/studio/web`.

Docker: `docker build -t procedure-studio . && docker run -p 8790:8790 -v studio-data:/data procedure-studio`.

### Environment variables

| Variable | Use |
|---|---|
| `STUDIO_CONFIG` | path of `studio.yaml` |
| `STUDIO_DATA` | data folder (projects, runs, published files) |
| `STUDIO_CONVERTER` | `occ` (default) or `exe` |
| `STUDIO_LLM_PROVIDER`, `STUDIO_LLM_MODEL` | `none` (default), `ollama`, `openai`, `bedrock`, `anthropic`. The Settings screen overrides these |

## Using it

The left rail follows the work in order:

| Op | Screen | What the writer does |
|---|---|---|
| 010 | Inputs and run | Upload the STEP, MBOM/APL and drawing, then follow the nine stages. Files left out of a new run are kept from the previous run. |
| 020 | Match 3D to parts list | Check the reconciliation: exact, variant, superseded, fuzzy and translated matches, and what is left over on each side. |
| 030 | Illustrated parts list | Review rows next to the 3D model. Accept rows, correct nomenclature, units, effectivity, role and torque, and link rows to 3D parts with **Pick in 3D**. Every field shows its source. Flags explain what to check. |
| 040 | Disassembly sequence | Scrub through the removal order and explode the model. Reorder steps; the server checks the blocking constraints and rebuilds what follows. |
| 050 | Procedures | Read the disassembly and assembly procedures (and the modification procedure for a revision). Selecting a step shows its parts in 3D. Edit a step to have it re-checked for STE and facts. |
| 060 | Check documents | Each generated document with its four stages (integrity, well-formed XML, schema, business rules), every problem with its rule, line and a suggestion, the reading view through a stylesheet, and the XML with the problem lines marked. Choose which installed schema validates each output. |
| 070 | Publish | Choose S1000D, ATA or TechPub outputs and a destination, and set the DMC / CMM identity (including the BREX the data modules name) once per product. Documents with errors are held back unless you publish anyway; the check result is recorded with every published file. |
| 080 | Changes from baseline | Shown when the run has a baseline. Lists what changed and where, which steps are affected, and how much text was kept. |

**Schemas and rules** (below the numbered steps) holds what all products are checked against; see that section.
**Settings** holds the language model connection.

**Sign off as baseline** (bottom of the rail) freezes a run. Later runs of the product are compared with it. A
signed-off run cannot be edited.

Selection is shared by occurrence id. GLB node names, IPL rows, sequence steps and procedure steps all use the same
`PN_n` ids, so a click in any of them highlights the same parts everywhere.

## Products, issues, modifications and variants

Studio keeps each **product** (one assembly) with all its runs. Each run says what it is; the run form offers only
what fits the product's state.

| Run | When | Compared with | What you get |
|---|---|---|---|
| First issue | The first run of a new product | Nothing | IPL, procedures, documents. Sign it off: it becomes the **baseline**. |
| New issue | A new drawing, 3D model or parts list for the same product | Its baseline | The changes and the steps they touch. Unchanged steps keep their approved text. Item numbers continue. Documents get the next issue number (002, 003…). |
| Modification (SB / MOD / ECN) | A change applied to units in service | Its baseline | The same, plus a **modification procedure** titled with the reference ("SB 32-0042: …"), with the bulletin attached to the run. |
| Variant | A product derived from a shipped one (a seat with another cushion or tray table, a valve with another fitting) | The parent product's baseline | The new product's own IPL and documents (next variant code, A → B), and the differences from the parent. It reuses the parent's approved wording wherever nothing differs, and gives a conversion procedure from the parent configuration. |

**Upload only what changed.** Files you do not choose are carried over from the last run, or from the parent's
baseline for a variant's first run. A variant whose only difference is its parts list needs only the new parts list.

**Item numbers are stable.**
* A continuing part keeps its item number in every later issue, modification and variant.
* A new part gets a free number between its neighbours. A part that replaces another goes next to it: 225 next to
  the retired 220.
* A number retired by a removed part is never reused, and the IPL lists the retired numbers.

This keeps procedure text and readers' references valid.

To set up another product:
1. Add it under *Inputs and run → Add a product or a variant*: either a new product, or a variant of one that has a
   baseline.
2. Start its first run with the STEP, the parts list and, if you have it, the drawing.
3. Review *Match*, *IPL*, *Sequence* and *Procedures*, then check and publish.
4. Sign off the run. From then on, new drawings, models, parts lists and bulletins are new runs of that product.

The publication identity (DMC, ATA numbers, the BREX your data modules name) is set once per product under
*Publish*; a variant inherits it.

## How the disassembly is ordered

The 3D model gives the geometry: which part can come out, in which direction, once which others are out. Studio
turns that into a maintenance order, the way a CMM is written:

1. **Part by part.** Each part comes off right after its own attaching parts (screws, nuts, washers, lock wire),
   with the seals and back-up rings fitted on it: "Remove the four screws and the washers from the cover. Remove the
   cover. Remove and discard the packing." Attaching parts wait for their part unless something else needs them out
   first, and nothing moves ahead of a part that blocks it.
2. **Level by level.** Sub-assemblies come off whole. Each one is then taken apart in its own subtask ("Disassemble the
   rotary electro magnet"), from its own 3D plan, and its sub-assemblies after it. Without a 3D sub-assembly the
   parts-list order is used and flagged.
3. **Not taken apart.** Sub-assemblies that are joined for good (wound, shrunk, bonded, equipped bodies, circuit
   boards) get "Do not disassemble ... further". Inserts, expansion plugs and dowel pins stay installed.

Assembly is the reverse: sub-assemblies are assembled first, then installed, with new seals.

Machined parts are often modelled with their manufacturing phases or extra bodies (raw stock, phase bodies) as
sub-products. Studio reads them as one part: the finished body is shown and used, the others are hidden, so the
parts list and the viewer see one part. Files converted before this version need a new run from the start.

## Parts lists: Excel, CSV or PDF

The parts list can be an MBOM, APL or EBOM export: `.xlsx`, `.xls`, `.csv`, or a PDF with a text layer.

**How the file is read.** Studio finds the table: the right sheet in a multi-sheet workbook, or the table on every
page of a PDF, even when the column layout changes between pages. It then maps each column to a field and says how it
found it:
* from a known header ("Part Number", "Désignation EN", "Qty.");
* by guessing, from a similar header or from what the values look like (part numbers, small whole numbers, `*`
  levels, rising item numbers);
* or not at all.

**When it needs you.** If the part number, description or quantity was guessed, or another table could also be the
parts list, the run is marked *columns guessed: check the import*. Open *Match 3D to parts list → Parts list import*:
choose the table, set each field's column, check the first lines as they will be read, and use the mapping. It is
kept for that product and that file layout, so the next issue of the same export loads without asking. A scanned
(image-only) PDF needs OCR first.

## Language model (optional)

Procedures are complete without a language model. Templates write STE steps, and every step is checked for STE and
facts. A model only rewords. Each reworded step must pass the same STE checker and fact check (same items,
quantities and torques, no new numbers), or the template text stays. Steps are sent in small batches, and a batch
that fails keeps its template text.

Set it up under **Settings → Language model** in the app. *Test with two sample steps* shows what the model returns
before you save. Then *Procedures → Reword with the language model* applies it to a run; new runs use it
automatically.

**Offline with Ollama (for example Qwen)**
```bash
# on the Studio machine, or any machine on your network
ollama pull qwen2.5:7b            # or qwen2.5:14b / qwen3:8b; a 7–14B model is enough for rewording
ollama serve                      # listens on http://127.0.0.1:11434
```
In Settings, choose *Ollama (offline)*, with server address `http://127.0.0.1:11434/v1` (or `http://<host>:11434/v1`).
Press *List models*, pick the model, *Test*, *Save*.
* Qwen 3 "thinking" output is removed before the answer is read.
* Lower *Steps per request* if a small model times out or loses steps.
* To reach Ollama on another machine, start it with `OLLAMA_HOST=0.0.0.0`.

**Other local servers.** For vLLM, LM Studio, LocalAI or an internal gateway, choose *OpenAI-compatible server* and
give its `/v1` address.

**Amazon Bedrock**
* Choose *Amazon Bedrock*. Set the region and the model or inference-profile id (for example
  `eu.anthropic.claude-sonnet-4-5-20250929-v1:0`), and optionally an AWS profile.
* Inside a VPC without internet access, set *VPC endpoint URL* to the `bedrock-runtime` interface endpoint.
* Credentials come from the server's AWS credentials chain (instance role, profile, environment), never from the page.
* Install `boto3` (see `requirements.txt`).

The same settings can be given in `studio.yaml` (`llm:`). What is saved in the app takes precedence. It is stored
in `STUDIO_DATA/settings.json`, which is readable by its owner only.

## Configuration and adapters

Every external piece is an adapter chosen in `studio.yaml` (see `studio.example.yaml`):

* **Converter**: `occ` uses Open CASCADE in-process on any OS. `exe` uses the legacy `cad-step-to-glb.exe`; a product
  model is rebuilt from its GLB.
* **LLM**: `none`, `ollama`, `openai` (any OpenAI-compatible server), `bedrock` or `anthropic`. Normally set on the
  Settings screen; see *Language model*.
* **Connectors**: `folder` is built in. Any other destination (PLM, CMS hot folder, Git) plugs in from
  configuration with no change to Studio:

  ```yaml
  connectors:
    - kind: plm
      class: mycompany.studio_plm:PlmConnector   # subclass of studio.connectors.base.Connector
      url: https://plm.example/api
  ```

  A connector implements `status()` (shown in the header and on Publish) and `publish(bundle)`. It returns one
  result per document.
* **Role rules**: `roles.extra_rules` adds regex rules (by part number or name) ahead of the built-in rules. Roles
  drive the process: fastener, washer, seal, insert, lockwire, cap, and so on.

## Schemas and rules

Document checking is built into Studio (`backend/studio/conformance`). The schema layer is ASTHRA's validation
core, brought over without SGML and without ASTHRA's document store. Studio adds rule sets and stylesheets. Everything
lives under `STUDIO_DATA/conformance` and is managed on the **Schemas and rules** screen (or the
`/api/conformance/...` endpoints).

**Schemas.** Add an issue of a standard as you received it: the zip or folder of the S1000D schemas, an ATA or OEM DTD
set, any XSD set, or a ready-made package (`studio-package.json`; ASTHRA's `asthra-package.json` packages install
unchanged). Studio finds the document types and asks only for what the files do not say, such as the issue of a
DTD set.
* S1000D downloads with several copies (release, patches) are recognised, and the ISO entity sets are bundled.
* The default BREX shipped with an S1000D issue is added to the BREX library.
* Every schema is compiled at install. libxml2 is used first; the `xmlschema` library handles XSD 1.1.
* Validation is offline. Includes and entities resolve only inside the package.

**The check.** These are the stages, in order. A stage that did not run is reported as not checked, never as passed.
1. Integrity: encoding, size, empty file.
2. Well-formed XML. A broken file is still checked against its schema up to the break.
3. Schema, with explained messages. For example: "A required `<quantityPerNextHigherAssy>` is missing before
   `<partRef>`". Also duplicate IDs and dangling IDREFs.
4. Business rules: the BREX chain the data module names (`brexDmRef`), project BREX → S1000D default, including SNS
   rules. Then every enabled rule set that applies.

Studio picks the schema from what the document declares. When several installed schemas fit, you choose per output on
the Check documents screen.

**BREX.** Add BREX data modules (information code 022). A project BREX that builds on the S1000D default is checked as
a chain. A BREX you do not have can be given an installed stand-in, and the reports say so. Set the BREX your data
modules name under Publish → Publication identity → *BREX data module code*.

**Rule sets** are where your own XML rules go. There are two formats; see `examples/rules`:
* Schematron (`.sch`): `assert`/`report`, with `@id` as the rule id and `@role` as the severity.
* A YAML or JSON rule file, for rules without Schematron. Each rule has an `id`, a `context` XPath and one test:
  * `assert`: an XPath that must be true.
  * `forbid`: an XPath that must be false.
  * `allowed`: a list of allowed values, for an attribute or the text.
  * `pattern`: a regular expression.

  For example:

  ```yaml
  id: house-rules-s1000d
  applies_to: {standards: [S1000D], doc_types: [proced]}
  rules:
    - id: HR-002
      context: //para
      forbid: "contains(., ';')"
      message: Do not use semicolons in procedure text.
  ```

  Rules compile when the file is added, so a bad rule is refused with the reason. Rule sets can be enabled, disabled
  and limited to standards, document types or top elements. Adding a file with the same id replaces it.

**Stylesheets** are XSLT 1.0. A *view* renders a document for reading on the Check screen. Built-in views cover S1000D
data modules and the ATA CMM; your own views take precedence. A *transform* rewrites Studio's XML before it is checked
and published: house conventions, a customer's flavour of a schema, added boilerplate. Transforms run in order, and
`examples/stylesheets` has one. A stylesheet with includes is added as a zip. Stylesheets cannot reach the network or
write files.

**Try a document** checks any XML file against what is installed, without a run. Use it while writing rules or
stylesheets.

**Editing a document.** Each document on the Check screen opens in **Document**: the document as it reads (title
block, warnings, cautions and notes boxed, numbered steps, spares and supplies as labelled rows), editable in place.
Turn on **Tags** to see every element as a bracket with its name and attributes; select an element to change its
attributes, move, duplicate or delete it. **Show: Print preview** renders it through a view stylesheet (the built-in
one or yours), read only. **XML** is the source with syntax colours, folding, search (Ctrl+F) and undo, and
**Side by side** shows Document and XML together.

The check runs as you type, with the same colours everywhere: red for errors, amber for warnings, green when the
document is valid. Problems are underlined on their line in XML and outlined on their element in Document;
"line 128" in Problems jumps there. **Save and check** (Ctrl+S) keeps your edit: it replaces the generated document
when checking and publishing, and the edit is logged. A file that is not well-formed XML is not saved. If the
generated document changes later (a step reworded, the IPL changed), your edit is kept and the check warns that it
is out of date. **Back to generated** removes the edit. A signed-off baseline is read only.

`packages/` holds **evaluation subsets** of S1000D 4.2 (proced, ipd) and the ATA CMM, written for testing. Reports
label them as synthetic. Install the official schemas and your BREX for real use.

## TechPub

The TechPub output is the IPL in the column layout of TechPub's *Generate from Excel* import (`excel_utils` headers).
It also includes one JSON file per procedure in the shape of the TechPub procedure editor. Use the folder connector
to drop both where TechPub imports them, or download them from Publish.

## Data

Everything is plain files under `STUDIO_DATA`:

```
settings.json                                   settings changed in the app (language model)
projects/<project>/project.json                 name, kind (product / variant), parent, settings (publication identity,
                                                schema choices, parts-list mappings, role rules), baseline
projects/<project>/runs/<run>/run.json          inputs (sha256), what the run is (change kind, reference, reason),
                                                issue number, stage status and timings, sign-off
  mbom_import.json                              how the parts list was read (tables, mapping, what was guessed)
  edits.json                                    every writer edit, before and after
  inputs/  <root>.glb  product_model.json  meshes.npz  contacts.json  roles.json  sequence.json
  reconciliation.json  mbom.json  drawing.json  ipl.json  process.json
  procedure_disassembly.json  procedure_assembly.json
  changeset.json  procedure_modification.json   (runs with a baseline)
  documents/  check.json                        the documents as checked (and published), and their reports
  doc_edits/<file>  doc_edits/<file>.json       a writer's edit of a document, and the generated document it was made from
  publish/NNN_<connector>.json                  every publication result, with each file's check result
conformance/registry.sqlite3  packages/  brex/  rules/  stylesheets/    schemas and rules (shared by all products)
```

## Tests

```bash
cd backend
pytest tests/test_units.py tests/test_conformance.py        # rules, STE, fact check, model diff, connectors,
                                                            # schemas, BREX, rule sets, stylesheets (sandboxing)
STUDIO_SAMPLES=/path/to/samples pytest tests                # plus two full runs over the HTTP API (about a minute)
```

The end-to-end test runs the sample brake selector valve (82 occurrences) and checks the following:

* There are no sequence conflicts or broken precedence, and every drawing torque is assigned.
* IPL item numbers are unique, and every IPL field has a source.
* There are 0 STE errors, and all four XML outputs pass the check against the installed schemas.
* The example project BREX, rule file and Schematron find their issues, errors hold publishing back, and a transform
  is applied before the check. What is published is byte for byte what was checked.
* Writer edits are re-checked, the folder publication is written, and a signed-off baseline is read-only.
* The Rev B revision finds exactly the six screws re-identified C77004-10 → C77004-12, with a modification
  procedure.
* A variant created from the baseline with only a new parts list reports that one change, keeps every other item
  number, places the new screw next to the retired one, and gets variant data module codes (00B). An SB
  modification needs its reference, gets issue 002 documents and a modification procedure titled with the SB.
* A confirmed parts-list mapping is kept and used, and a saved API key is never returned by the API.

## Limits to know

* The S1000D and ATA schema packages here are evaluation subsets. Business rules (BREX) are checked only once the
  BREX the documents name is installed.
* SGML (legacy ATA iSpec 2200 SGML) is not validated. Studio writes XML.
* Stylesheets are XSLT 1.0 (libxslt). XSLT 2/3 would need Saxon.
* Sequence planning uses straight-line removal along the principal axes and the part's own fit axes. Parts that need
  a rotation or a path (bayonets, clips) are flagged as forced for the writer to confirm.
* Torques are read from text on the drawing. A drawing that is only a scanned image needs OCR first.
* The STE checker covers word count, approved verbs at the start, modal verbs, -ing forms, passive voice and
  semicolons. It is not the full ASD-STE100 dictionary.
