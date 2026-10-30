# ADKP_data_ingress_to_release

Tooling for moving a study's data into the AD Knowledge Portal (ADKP), from
data ingress through public release.

The README is organized by the four stages of the ADKP data ingress ticket
template in Jira. Each stage lists what its checklist covers and which tools
here help with it. Some processes can happen at any point rather than in one
stage, such as moving data between Synapse locations or setting up a
publication dataset. Those are documented separately at the end.

This repo covers ADKP processes only.

## Contents

- [Layout](#layout)
- [Before you run anything](#before-you-run-anything)
- [Stage 1: Set up/verify Synapse infrastructure](#stage-1-set-upverify-synapse-infrastructure)
  - [Folder Setup](#folder-setup)
  - [Portal Studies Table](#portal-studies-table)
- [Stage 2: Collect/verify study documentation](#stage-2-collectverify-study-documentation)
  - [Wiki Setup](#wiki-setup)
- [Stage 3: Validate/verify metadata](#stage-3-validateverify-metadata)
- [Stage 4: Release data](#stage-4-release-data)
  - [Acknowledgement Statements](#acknowledgement-statements)
  - [Alphabetize Wiki](#alphabetize-wiki)
- [Not tied to a stage](#not-tied-to-a-stage)
  - [Move Data](#move-data)
  - [Publication Dataset / DOI](#publication-dataset--doi)

## Layout

Code is grouped by tool first, then by language (for tools with equivalent
implementations in more than one language, kept in sync with each other -
see each tool's own section below for details):

```
<tool-name>/
  python/...
  r/...
```

## Before you run anything

Every tool follows the same basic setup:

1. Install the tool's requirements: `pip install -r <tool-name>/python/requirements.txt`
2. Authenticate via `synapse config` (~/.synapseConfig) or the
   `SYNAPSE_AUTH_TOKEN` env var - never hardcode credentials in a script.
3. Never commit real study or contributor details (study synIDs, ticket
   numbers, contributor names). Committed examples use placeholders like
   `syn00000000` and `EXAMPLE-000`. Tools that need a real per-ticket input
   file keep it in a git-ignored folder (see [Move Data](#move-data)).
4. Edit-and-run scripts default to a dry run. They print a report and change
   nothing until you set `dry_run`/`DRY_RUN` to `False`.

---

## Stage 1: Set up/verify Synapse infrastructure

Checklist highlights: confirm the contribution is covered by a DTA
(Governance/PCO ticket), confirm the ticket details with the contributor
(including expected data volume), decide whether new metadata templates need
an RFC, and have the contributor review the templates. Then create/verify the
study and Staging folders, bind Staging folders to the right JSON Schema for
Curator, make sure the study is in the data model and the Portal Studies
Table (folder name, abbreviation, and annotation must match exactly), grant
the uploader edit/delete access on Staging, and send upload instructions and
templates.

If the contributor has already uploaded data somewhere else in Synapse (for
example their own workspace project), use [Move Data](#move-data) to bring it
into the Staging folder.

### Folder Setup

Reusable tooling for auditing a study's Synapse folder tree and permissions,
safely creating/renaming/moving/removing folders, and uploading contributor
files with checksum verification and provenance. Not hardcoded to any
particular ticket or folder arrangement - it audits whatever tree actually
exists under the root you point it at and only changes what `folder_plan`
explicitly declares.

```
folder-setup/
  python/folder_setup.py
  python/requirements.txt
```

#### Using the script

1. `pip install -r folder-setup/python/requirements.txt`
2. Authenticate (see [Before you run anything](#before-you-run-anything)).
3. Edit the `CONFIG` dict near the top of `folder_setup.py` (ticket ID,
   root Synapse ID, and - once known - `folder_plan`/`upload_plan`). Leave
   `folder_plan`/`upload_plan` empty to run in audit-only mode.
4. Run it (`python folder_setup.py`). It always prints the current
   folder tree, a permission audit, and a storage-location check; it only
   writes to Synapse if `CONFIG["dry_run"]` is `False` and there are no
   `ERROR`-level issues.

#### Creating, renaming, moving, or removing folders

Folder changes are declarative - list what you want in `CONFIG["folder_plan"]`
and the script resolves it against the current tree and applies only what's
actually needed (e.g. `ensure` is a no-op if the folder already exists):

```python
"folder_plan": [
    {"action": "ensure", "parent": "syn...", "name": "example_folder"},   # create if missing
    {"action": "rename", "target": "syn...", "name": "new_name"},
    {"action": "move",   "target": "syn...", "new_parent": "syn..."},
    {"action": "remove", "target": "syn..."},                        # only if the folder is empty
],
```

File uploads work the same way via `CONFIG["upload_plan"]`, either against an
existing `target_folder_id` or a `target_folder_name` just created by
`folder_plan` above - see the module docstring for the full schema.

See the module docstring and the `SOP_NOTES` dict at the top of the file for
the SOP/security reasoning behind each check (private-folder enforcement,
HTTPS assertion, the `scicomp`-only rule for any external AWS storage
location). ADKP study folder layouts vary (nested Staging/Data folders, assay
subfolders, versioned ingest folders), so this script never assumes a
particular shape.

### Portal Studies Table

Reusable tooling for entering and updating rows in the ADKP Portal Studies
Table ([syn17083367](https://www.synapse.org/Synapse:syn17083367/tables/)),
which drives the study detail pages on the AD Knowledge Portal. Use it in
Stage 1 to add/verify the study's row, then again once Stage 2 has collected
the study name, description, `studyFocus`, and `specimenType`.

```
portal-studies-table/
  python/adkp_studies_table.py
  r/adkp_studies_table.R
  scripts/check_sync.py
  scripts/dump_r_constants.R
```

#### Using a script

Each file (`python/adkp_studies_table.py` or `r/adkp_studies_table.R`) is a
standalone, edit-and-run script:

1. Open the language version you want and edit the `STUDY_ROW`/`STUDY_UPDATES`
   and `DRY_RUN`/`MODE` values near the top (see the "SOP REFERENCE" section
   above them for what each field means and its valid values).
2. Run it. It always prints a validation report against the SOP's business
   rules and the live table; it only writes to Synapse if `DRY_RUN` is
   `False`/`FALSE` and there are no `ERROR`-level issues.

See the docstring/header comment at the top of each file for setup details
(package installation, Synapse authentication).

#### Keeping the Python and R versions in sync

Both files independently encode the same schema and SOP business rules
(column names/limits, controlled vocabularies, deprecated columns, the
`ACK_CONTEXT_*`/`STUDY_METADATA_*`/`ACCESS_REQS_*` reference IDs, etc.). If
one is edited without the other, they can silently drift apart.

`portal-studies-table/scripts/check_sync.py` guards against that: it
extracts the shared constants from both files (without needing
`synapseclient`/`pandas`/`synapser` installed) and fails if any differ. It
deliberately ignores `STUDY_ROW`, `STUDY_UPDATES`, `DRY_RUN`, `MODE`, and
`UPDATE_STUDY_ABBREVIATION`, since those are meant to be edited per use.

Run it locally with:

```bash
python3 portal-studies-table/scripts/check_sync.py
```

It also runs automatically in CI (`.github/workflows/check-sync.yml`) on any
push or PR that touches `portal-studies-table/`.

**When you change a shared constant (a column's max length, a controlled
vocabulary, the SOP guidance text, a reference ID, etc.), update it in both
`adkp_studies_table.py` and `adkp_studies_table.R`, then run the sync check
before committing.**

---

## Stage 2: Collect/verify study documentation

Checklist highlights: gather any terms missing from the data dictionary (and
start an RFC for new metadata templates if needed). Collect/verify the study
description, methods description, and acknowledgement statement, plus the
Portal Studies Table details (study abbreviation, `Study_Name`,
`Study_Description`, `studyFocus`, `specimenType`). Draft the study
description in the Staging folder's wiki for the contributor to review before
adding it to the top-level study folder's wiki, and make sure metadata
templates exist for every data type.

### Wiki Setup

Reusable tooling for creating and updating Synapse Wiki pages (root pages and
sub-pages) - e.g. a study's main wiki, its Methods sub-page, or the Staging
folder draft - while preserving contributor-submitted HTML formatting (links,
superscript, subscript, etc.) exactly as written. Includes an optional helper
to pull a contributor's text straight from a Jira ticket field as Jira's own
rendered HTML, avoiding a manual copy/paste step.

```
wiki-setup/
  python/adkp_wiki_template.py
  python/requirements.txt
```

#### Using the script

This one is meant to be imported, not edited-and-run (owner/title/content
vary per call):

```python
from adkp_wiki_template import login, create_or_update_wiki

syn = login()

# Preview first: opens the content file in your browser and prints the
# create/update plan: writes nothing to Synapse.
create_or_update_wiki(
    syn, owner_id="syn...", title="...", content_path="content.html",
    parent_id=None,  # only used when creating a brand-new page
    dry_run=True,
)

# Looks right -> push for real.
create_or_update_wiki(syn, owner_id="syn...", title="...", content_path="content.html")
```

It also works from the command line (`python adkp_wiki_template.py --help`),
and as a standalone Jira-to-Synapse pull via `fetch_jira_rendered_fields()` /
`save_jira_field()` for teams with a Jira API token. See the module
docstring for full details, including why content is always passed as a
**file path** rather than an inline string (the only mode that preserves the
contributor's HTML byte-for-byte), and the fallback import paths (browser
DevTools copy, Word/Google Docs round-trip) for when Jira API access isn't
available.

---

## Stage 3: Validate/verify metadata

Checklist highlights: verify the contributor has uploaded data and metadata
templates to Staging, validate the metadata, verify new metadata files are
uploaded and versioned (existing studies), and apply/verify file annotations,
requesting missing information from contributors as needed. Once the upload
is finished, change the contributors' Staging access from edit to download
and request release notes.

No tools in this repo for Stage 3 yet.

---

## Stage 4: Release data

Checklist highlights: scope the data to a release (fix version on the
epic), tell the contributor about the pending release, draft the public
release notes for their review, and verify annotations and the scope of
data/metadata in the released folders. Share and verify the acknowledgement
statement, have Governance confirm DTA issues are resolved and handle any
Access Restriction needs, migrate folders/files from Staging to released,
review the released data, mint the DOI, and notify the contributor. Remove
contributor permissions from Staging if they're no longer needed.

[Move Data](#move-data) can also handle the "migrate folders/files from
Staging to released" step: list each Staging folder as an origin and its
released Data folder as the destination.

### Acknowledgement Statements

Adds a new Acknowledgement Statement sub-page under the AD Knowledge
Portal's Acknowledgment Statements wiki page (owner `syn12666371`, wiki id
`602387`) and re-alphabetizes that page's sub-pages afterward, so the new
page lands in the right spot. Before writing anything to Synapse, it checks
that the submitted text actually contains ADKP's required attribution
sentence ("The results published here are in whole or in part based on
data obtained from the AD Knowledge Portal"), raising an error if it's
missing rather than publishing an incomplete statement.

Standalone by design: it covers the same create/update-wiki-page and
re-alphabetize logic as Wiki Setup (Stage 2) and Alphabetize Wiki (below),
inlined into a single file with no dependency on either - only
`synapseclient` is required. That means it's usable on its own (e.g. by
someone who only has this one file, not the rest of the repo), at the cost
of the two pieces of logic being duplicated rather than shared; if either
of those tools' underlying behavior changes, check whether this script
needs the same fix.

```
acknowledgement-statements/
  python/add_acknowledgement_statement.py
  python/requirements.txt
```

#### Using the script

Standalone, edit-and-run, same pattern as Portal Studies Table:

1. `pip install -r acknowledgement-statements/python/requirements.txt`
2. Authenticate (see [Before you run anything](#before-you-run-anything)).
3. Edit `STUDY_ABBREVIATION` (short study name - the sub-page title becomes
   `"Acknowledgement - <name>"`) and `CONTENT_PATH` (path to the
   contributor's acknowledgement HTML/text file) near the top of
   `add_acknowledgement_statement.py`.
4. Run it (`python add_acknowledgement_statement.py`). It always validates
   the content file first, raising `ValueError` if the required attribution
   sentence is missing - this check runs even with `DRY_RUN = True`. It
   only creates the sub-page and re-alphabetizes if `DRY_RUN` is `False`
   and validation passes.

To use it from other code instead, import
`add_acknowledgement_statement(syn, study_abbreviation, content_path,
dry_run=True)`.

### Alphabetize Wiki

Keeps the Acknowledgment Statements wiki page's sub-pages alphabetized. The
Acknowledgement Statements tool above already includes this same re-sort
logic and runs it automatically whenever it adds a new statement (the two
scripts are standalone, not linked - see that section's note on duplicated
logic) - use this script directly only when you need a standalone re-sort
without adding a new page (e.g. after manually editing a sub-page's title).
Unlike the other tools in this repo, it's a one-off script hardcoded to that
specific page (owner `syn12666371`, wiki id `602387`) rather than a generic
reusable tool - there's only one such page, and neither id identifies any
investigator or study-specific data.

Synapse wiki pages have a single tree-wide order hint (a flat list of every
page id) rather than a per-parent order field, so re-sorting just one
parent's children means finding the contiguous slice of that list its
current children occupy and replacing only that slice - this script does
that.

```
alphabetize-wiki/
  python/alphabetize_acknowledgement_statements.py
  python/requirements.txt
```

#### Using the script

1. `pip install -r alphabetize-wiki/python/requirements.txt`
2. Authenticate (see [Before you run anything](#before-you-run-anything)).
3. Run it (`python alphabetize_acknowledgement_statements.py`).

Note: this uses `syn.getWikiHeaders()` (the `/wikiheadertree` endpoint)
rather than the newer `synapseclient.models.WikiHeader.get()`
(`/wikiheadertree2`) - the v2 endpoint has been observed to lag behind on
very recently created pages, silently omitting them from the tree and
therefore from the re-sort.

---

## Not tied to a stage

These can come up at any point in a study's lifecycle.

### Move Data

Moves data that already exists somewhere in Synapse into a destination
folder in the ADKP backend. The most common case is in Stages 1-2: a
contributor has already uploaded files to their own workspace project, and
they need to go into the study's Staging folder. The same tool works for any
Synapse-to-Synapse move, such as Staging to the released Data folder in
Stage 4.

```
move-data/
  python/move_data.py
  python/requirements.txt
  plans/example_move_plan.csv   # format example (only committed file in plans/)
  plans/<your plan>.csv         # git-ignored - real synIDs stay local
  runs/                         # git-ignored - manifests, migration progress, audit log
```

#### What it does

For each row of a move plan ("move everything inside origin into
destination"), it:

1. **Audits first, read-only.** It checks that you can move items out of the
   origin and add items to the destination, and that the destination is
   private (not shared with the public or all registered users). It flags
   any file or folder in the origin, at any depth, with its own sharing
   settings (those would follow the item instead of inheriting Staging's),
   name clashes in the
   destination (including two rows moving same-named files into one folder),
   and unsupported item types.
2. **Writes a "before" manifest**: one row per file with synID, name, MD5,
   size, and storage location.
3. **Migrates storage if needed.** Changing a file's parent only changes
   where it shows up in Synapse, not where the bytes are stored. If the
   files are in a different storage location than the destination folder
   (e.g. Synapse default storage vs. the ADKP bucket behind Staging), it
   moves the bytes with `synapseutils.index_files_for_migration()` /
   `migrate_indexed_files()`. synIDs stay the same. This runs before the
   move, so if it fails, nothing has moved yet.
4. **Moves the origin's contents** (not the origin folder itself) into the
   destination. synIDs stay the same, and the moved items now inherit the
   destination's sharing settings.
5. **Removes leftover sharing settings** (if `reset_local_sharing` is
   `True`, the default). Anything flagged in step 1 has its own sharing
   settings removed, so it inherits the destination's like everything else.
   Each removed setting is saved to the run folder first
   (`removed_acl_<synID>.json`) in case it needs to be put back.
6. **Optionally leaves a redirect Link** in each origin folder that points
   to the destination, so the contributor's team can see where the data
   went.
7. **Verifies every file**: it's under the destination, its MD5 is
   unchanged, (if migrated) it's in the destination's storage location, and
   every moved file and folder gets its sharing settings from the
   destination. Then it writes an "after" manifest.

`synapseutils.copy()` is deliberately not used. Its copies point at the same
stored bytes as the originals, so the data never actually reaches the
destination's storage location, and it leaves duplicates behind to clean up.
Migration is the step that relocates the bytes; keeping the original synIDs is
a side effect of it, not a requirement.

#### Using the script

1. `pip install -r move-data/python/requirements.txt`
2. Authenticate (see [Before you run anything](#before-you-run-anything)).
   Your account needs edit access on the origin. If the origin is a
   contributor's own project, ask them to add the Sage curation team to its
   top-level folder.
3. Make a plan CSV in `move-data/plans/` (copy `example_move_plan.csv`). Each
   row has `origin_id`, `destination_id`, and an optional `destination_path`
   note for humans:

   ```
   origin_id,destination_path,destination_id
   syn00000001,example_study/Staging/example_assay/raw,syn00000010
   ```

4. In `move_data.py`, set `CONFIG["ticket_id"]` and `CONFIG["plan_csv"]`, and
   decide on `migrate_storage` (default `True`), `reset_local_sharing`
   (default `True`), `confirm_migration` (default `True`), and
   `leave_redirect_link` (default `False`).
5. Run it with `dry_run = True` (the default) and read the report. Expect it
   to take several minutes for a few thousand files, because it looks up
   every file.
6. When the report shows no `ERROR`s, set `dry_run = False` and run it again.
   The migration step shows how many files it will migrate and asks `y/n`
   before copying anything, once per plan row. The run waits at each
   question until you answer. To let a multi-row run finish unattended, set
   `confirm_migration = False` (only after a clean dry run).

Large migrations (hundreds of GB) can take hours. Consider running one plan
row at a time. If a run is interrupted, run it again: files that already
migrated are skipped, so it picks up where it stopped.

---

### Publication Dataset / DOI

Supports a contributor who wants a Synapse Dataset and DOI for a manuscript
or publication (SOP "ADTR 1-7.0 Publication Dataset/DOI Creation"). This is
separate from minting a DOI for a study page. A request can arrive at any
time, either as its own "Request for DOI for Publication" ticket or flagged
on a data ingress request, so it isn't tied to one ingress stage. It has its
own two-part checklist on the publication dataset ticket template:

- **Publication dataset set-up:** get the Synapse usernames of anyone who
  needs edit access, create an empty Dataset in the AD Portal backend,
  confirm whether any manuscript-related analysis files will be included and
  (if so) create a `LastName_FirstName` folder for them under "Manuscript-Related
  Analysis Files", and grant edit/delete to the contributor and collaborators.
- **Publication dataset upload and versioning:** share how to add items to
  the Dataset and edit its wiki, confirm any analysis files meet open-access
  governance criteria, have the contributor create a stable Dataset version
  and mint the DOI, and confirm the acknowledgement statement.

No tools in this repo for this process yet. [Wiki Setup](#wiki-setup) can
be used for the Dataset's wiki.
