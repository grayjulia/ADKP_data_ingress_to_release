"""
Add a new Acknowledgement Statement sub-page under the AD Knowledge Portal's
Acknowledgment Statements wiki page (owner syn12666371, wiki id 602387), then
re-alphabetize that page's sub-pages so the new one lands in the right spot.

Standalone: only needs synapseclient (see requirements.txt) - nothing else
from this repo, so it works even for someone who only has this one file.

Before writing anything to Synapse, this checks that the acknowledgement
text actually contains ADKP's required attribution sentence:
    "The results published here are in whole or in part based on data
    obtained from the AD Knowledge Portal"
Contributors sometimes submit a near-miss or omit this sentence entirely -
catching that here is cheaper than a public wiki page needing a fix later.

Content is always passed as a FILE PATH, never an inline string: the
underlying WikiPage.markdown field uploads a file's raw bytes as-is, with no
HTML/markdown escaping in between - the only way to preserve a contributor's
original HTML (their <a href>, <sup>, <sub>, etc.) byte-for-byte.

Setup:
    pip install -r requirements.txt
    Authenticate once via `synapse config` (~/.synapseConfig) or by setting the
    SYNAPSE_AUTH_TOKEN env var to a personal access token - do not hardcode
    credentials in this file.

To use: edit STUDY_ABBREVIATION, CONTENT_PATH, and DRY_RUN below, then run
this file directly (`python add_acknowledgement_statement.py`). It always
validates the content file first (this check runs even when DRY_RUN is
True); it only writes to Synapse - creating the sub-page, then re-sorting -
if DRY_RUN is False and validation passes.
"""

import hashlib
import os
import re
import tempfile
import webbrowser

from synapseclient import Synapse
from synapseclient.core.exceptions import SynapseHTTPError
from synapseclient.models import WikiOrderHint, WikiPage

OWNER_ID = "syn12666371"    # AD Knowledge Portal project
PARENT_WIKI_ID = "602387"   # "Acknowledgment Statements" wiki page

REQUIRED_PHRASE = (
    "The results published here are in whole or in part based on data "
    "obtained from the AD Knowledge Portal"
)

# ==============================================================================
# EDIT THIS SECTION, then run this file.
# ==============================================================================

DRY_RUN = True  # True: validate and preview only, write nothing to Synapse.
                # False: create the sub-page and re-alphabetize if validation passes.

STUDY_ABBREVIATION = "EXAMPLE"  # short study name; page title becomes "Acknowledgement - <name>"

CONTENT_PATH = "acknowledgement_example.html"  # contributor's acknowledgement HTML content file

# ==============================================================================
# Library code below - shouldn't need to edit past this point.
# ==============================================================================


def login(auth_token=None):
    """Log in to Synapse. Falls back to ~/.synapseConfig / SYNAPSE_AUTH_TOKEN if auth_token is None."""
    syn = Synapse()
    syn.login(authToken=auth_token)
    return syn


def validate_acknowledgement_text(text):
    """
    Raise ValueError if `text` doesn't contain ADKP's required attribution
    sentence (REQUIRED_PHRASE). HTML tags are stripped and whitespace
    collapsed first, so inline formatting mid-sentence (e.g. a contributor
    bolding part of it) doesn't cause a false negative.
    """
    stripped = re.sub(r"<[^>]+>", "", text)
    collapsed = re.sub(r"\s+", " ", stripped).strip()
    if REQUIRED_PHRASE not in collapsed:
        raise ValueError(
            "Acknowledgement text is missing ADKP's required attribution sentence:\n"
            f"  {REQUIRED_PHRASE!r}\n"
            "Got (HTML tags stripped, whitespace collapsed):\n"
            f"  {collapsed!r}"
        )


def find_existing_wiki(syn, title):
    """
    Look up an existing sub-page under PARENT_WIKI_ID by exact title match.
    Returns the matching WikiPage (with .id populated), or None if no wiki
    page with that title exists yet.
    """
    try:
        return WikiPage(owner_id=OWNER_ID, title=title).get(synapse_client=syn)
    except ValueError:
        # WikiPage.get() raises ValueError when the header tree has no page
        # matching this title.
        return None
    except SynapseHTTPError as e:
        if getattr(e, "response", None) is not None and e.response.status_code == 404:
            return None
        raise


def preview_content(content_path):
    """Open a content file in the default web browser, for a visual check before pushing."""
    path = os.path.abspath(content_path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Content file not found: {content_path}")
    webbrowser.open(f"file://{path}")
    return path


def verify_wiki_markdown(syn, wiki_id, local_content_path):
    """
    Re-download the markdown just stored on `wiki_id` and compare it
    byte-for-byte against the local content file, to confirm the upload
    wasn't altered in transit. Does NOT confirm how Synapse's renderer will
    display the HTML - spot-check that separately in the Synapse UI.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        downloaded_path = WikiPage(owner_id=OWNER_ID, id=wiki_id).get_markdown_file(
            download_file=True, download_location=tmp_dir, synapse_client=syn
        )
        with open(downloaded_path, "rb") as f:
            remote_bytes = f.read()

    with open(local_content_path, "rb") as f:
        local_bytes = f.read()

    if hashlib.sha256(remote_bytes).digest() == hashlib.sha256(local_bytes).digest():
        print("Verified: stored markdown matches the local content file exactly.")
        return True
    else:
        print("WARNING: stored markdown differs from the local content file - "
              "something altered it in transit. Compare the two files directly.")
        return False


def create_or_update_wiki(syn, title, content_path, dry_run=False, verify=True):
    """
    Create a new sub-page under PARENT_WIKI_ID, or update it in place if a
    page with this exact title already exists on OWNER_ID.

    dry_run=True prints the create-vs-update plan, opens the content file in
    the browser for a visual check, and returns None without writing
    anything to Synapse.

    Returns the stored WikiPage (or None if dry_run).
    """
    if not os.path.isfile(content_path):
        raise FileNotFoundError(f"Content file not found: {content_path}")

    existing = find_existing_wiki(syn, title)

    if dry_run:
        if existing:
            print(f"[DRY RUN] Would UPDATE wiki page {existing.id!r} ('{title}').")
        else:
            print(f"[DRY RUN] Would CREATE new sub-page ('{title}') under wiki id {PARENT_WIKI_ID}.")
        print(f"Content file: {os.path.abspath(content_path)}")
        preview_content(content_path)
        print("Nothing was written to Synapse. Rerun with dry_run=False to push this.")
        return None

    if existing:
        wiki = WikiPage(owner_id=OWNER_ID, id=existing.id, title=title, markdown=content_path)
        action = "Updated"
    else:
        wiki = WikiPage(owner_id=OWNER_ID, title=title, markdown=content_path, parent_id=PARENT_WIKI_ID)
        action = "Created"

    stored = wiki.store(synapse_client=syn)
    print(f"{action} wiki page {stored.id!r} ('{title}').")
    print(f"View at: https://www.synapse.org/Synapse:{OWNER_ID}/wiki/{stored.id}")

    if verify:
        verify_wiki_markdown(syn, stored.id, content_path)

    return stored


def alphabetize_children(syn):
    """
    Re-sort PARENT_WIKI_ID's children alphabetically by title, without
    disturbing the ordering of any other page in the tree.

    Synapse wiki pages have a single tree-wide order hint (a flat id_list of
    every page id) rather than a per-parent order field, so re-sorting just
    one parent's children means finding the contiguous slice of that list its
    current children occupy and replacing only that slice.

    Uses syn.getWikiHeaders() (the /wikiheadertree endpoint) rather than the
    newer WikiHeader.get() (/wikiheadertree2) - the v2 endpoint has been
    observed to lag behind on very recently created pages, silently omitting
    them from the tree and therefore from the re-sort.
    """
    headers = syn.getWikiHeaders(owner=OWNER_ID)
    children = [h for h in headers if h.get("parentId") == PARENT_WIKI_ID]
    if not children:
        print(f"No children found under wiki id {PARENT_WIKI_ID} - nothing to re-sort.")
        return

    order_hint = WikiOrderHint(owner_id=OWNER_ID).get(synapse_client=syn)
    id_list = order_hint.id_list

    child_ids = {h["id"] for h in children}
    title_by_id = {h["id"]: h["title"] for h in children}

    # Contiguous slice of id_list currently occupied by these children. Any
    # child missing from id_list entirely (e.g. a very recently created page
    # Synapse hasn't added to the order hint yet) is still included in the
    # sorted result and inserted into that slice.
    present_indices = sorted(i for i, wid in enumerate(id_list) if wid in child_ids)
    if not present_indices:
        print(f"None of {len(children)} children currently appear in the order hint - "
              "cannot determine where to splice the sorted block.")
        return
    lo, hi = present_indices[0], present_indices[-1]

    sorted_child_ids = sorted(child_ids, key=lambda wid: title_by_id[wid].strip().lower())

    order_hint.id_list = id_list[:lo] + sorted_child_ids + id_list[hi + 1:]
    order_hint.store(synapse_client=syn)
    print(f"Stored new order for {len(sorted_child_ids)} sub-pages under wiki id {PARENT_WIKI_ID}.")


def add_acknowledgement_statement(syn, study_abbreviation, content_path, dry_run=True):
    """
    Validate `content_path`'s acknowledgement text, then create (or update)
    it as a sub-page titled "Acknowledgement - <study_abbreviation>" under
    the Acknowledgment Statements page, and re-alphabetize that page's
    sub-pages.

    Raises ValueError (before touching Synapse) if the text is missing the
    required attribution sentence - see validate_acknowledgement_text().
    Raises FileNotFoundError if content_path doesn't exist.

    dry_run=True previews the create/update and skips the re-alphabetize
    step entirely (nothing was created to sort).

    Returns the stored WikiPage (or None if dry_run).
    """
    with open(content_path, "r", encoding="utf-8") as f:
        text = f.read()
    validate_acknowledgement_text(text)

    title = f"Acknowledgement - {study_abbreviation}"
    stored = create_or_update_wiki(syn, title=title, content_path=content_path, dry_run=dry_run)

    if dry_run:
        print("DRY_RUN is True - skipping re-alphabetize. Set DRY_RUN = False once the "
              "preview above looks right.")
        return None

    alphabetize_children(syn)
    return stored


if __name__ == "__main__":
    syn = login()
    add_acknowledgement_statement(
        syn,
        study_abbreviation=STUDY_ABBREVIATION,
        content_path=CONTENT_PATH,
        dry_run=DRY_RUN,
    )
