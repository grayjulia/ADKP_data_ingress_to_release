# ADKP_ingress_stage_1

Tooling for ADKP data ingress, stage 1.

## Layout

Code is grouped by tool first, then by language (for tools with equivalent
implementations in more than one language, kept in sync with each other -
see each tool's own section below for details):

```
<tool-name>/
  python/...
  r/...
```

## Tools

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
2. Authenticate via `synapse config` (~/.synapseConfig) or the
   `SYNAPSE_AUTH_TOKEN` env var - never hardcode credentials in the script.
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
location). Different portals/programs structure their Synapse folders very
differently (ADKP nested Staging/Data, ELITE flattened, GENIE
Test/Staging/Production projects, versioned `vN_ingest` folders elsewhere) -
this script never assumes a particular shape.

### Wiki Setup

Reusable tooling for creating and updating Synapse Wiki pages (root pages and
sub-pages) - e.g. a study's main wiki, its Methods sub-page, or an
Acknowledgement sub-page - while preserving contributor-submitted HTML
formatting (links, superscript, subscript, etc.) exactly as written. Includes
an optional helper to pull a contributor's text straight from a Jira ticket
field as Jira's own rendered HTML, avoiding a manual copy/paste step.

```
wiki-setup/
  python/adkp_wiki_template.py
  python/requirements.txt
```

#### Using the script

This one is meant to be imported, not edited-and-run (owner/title/content
vary per call, unlike the single-row script below):

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
re-alphabetize logic as Wiki Setup (above) and Alphabetize Wiki (below),
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

Standalone, edit-and-run, same pattern as Portal Studies Table below:

1. `pip install -r acknowledgement-statements/python/requirements.txt`
2. Authenticate via `synapse config` (~/.synapseConfig) or the
   `SYNAPSE_AUTH_TOKEN` env var - never hardcode credentials in the script.
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

### Portal Studies Table

Reusable tooling for entering and updating rows in the ADKP Portal Studies
Table ([syn17083367](https://www.synapse.org/Synapse:syn17083367/tables/)),
which drives the study detail pages on the AD Knowledge Portal.

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

#### Alphabetize Wiki

A separate script from `adkp_studies_table.py`/`.R` above, but part of the
same process: keeps the Acknowledgment Statements wiki page's sub-pages
alphabetized. The Acknowledgement Statements tool above already includes
this same re-sort logic and runs it automatically whenever it adds a new
statement (the two scripts are standalone, not linked - see that section's
note on duplicated logic) - use this script directly only when you need a
standalone re-sort without adding a new page (e.g. after manually editing a
sub-page's title). Unlike the other tools in this repo, it's a one-off
script hardcoded to that specific page (owner `syn12666371`, wiki id
`602387`) rather than a generic reusable tool - there's only one such page,
and neither id identifies any investigator or study-specific data.

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

##### Using the script

1. `pip install -r alphabetize-wiki/python/requirements.txt`
2. Authenticate via `synapse config` (~/.synapseConfig) or the
   `SYNAPSE_AUTH_TOKEN` env var - never hardcode credentials in the script.
3. Run it (`python alphabetize_acknowledgement_statements.py`).

Note: this uses `syn.getWikiHeaders()` (the `/wikiheadertree` endpoint)
rather than the newer `synapseclient.models.WikiHeader.get()`
(`/wikiheadertree2`) - the v2 endpoint has been observed to lag behind on
very recently created pages, silently omitting them from the tree and
therefore from the re-sort.
