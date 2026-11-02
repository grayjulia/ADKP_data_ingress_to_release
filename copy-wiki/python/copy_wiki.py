"""
Copy the text of one or more Synapse wikis into another entity's wiki - e.g.
a contributor's methods write-up on their own project folders, copied into
the matching ADKP Staging folder's wiki during Stage 2.

Each row of the copy plan CSV says "copy SOURCE's wiki into DESTINATION's
wiki". Rows that share a destination are combined, in the order they appear
in the CSV, so two sources can land one after the other on the same page
instead of the second overwriting the first. Each row can have a
section_label, which is added as a heading above that source's text (useful
when two sources use the same section headings).

For every destination this script:

  1. Audits before touching anything: both IDs exist, each source has a wiki
     with text in it, you can edit the destination, and nothing would be
     lost - wikis with attachments (images, files) are not supported, and
     only each source's top page is copied (sub-pages are flagged).
  2. Saves the destination's current wiki text to the run folder
     (before_<destination>.md) and writes the new text it plans to store
     (after_<destination>.md), so you can read exactly what will change.
  3. (Only if CONFIG["dry_run"] is False) Stores the new text on the
     destination's top wiki page, creating the page if there isn't one.
  4. Verifies the stored text matches after_<destination>.md exactly.

The sources are never changed. CONFIG["destination_mode"] picks what happens
to text already on the destination page: "replace" (e.g. a "To be provided"
placeholder) or "append" (keep it, add the copied text after it). Re-running
in "replace" mode skips destinations that already match; in "append" mode it
stops if the copied text is already there, so it can't be added twice.

To undo: Synapse keeps every version of a wiki page (Wiki -> "Wiki History"
in the Synapse UI), and before_<destination>.md has the old text.

Setup:
    pip install -r requirements.txt
    Authenticate once via `synapse config` (~/.synapseConfig) or by setting the
    SYNAPSE_AUTH_TOKEN env var to a personal access token - do not hardcode
    credentials in this file.

To use: put your copy plan in a CSV under ../plans/ (that folder is
git-ignored, so real synIDs never get committed - see
../plans/example_copy_plan.csv for the format), point CONFIG["plan_csv"] at
it, then run this file directly (`python copy_wiki.py`). It always prints the
audit report and writes the before/after files; it only changes anything in
Synapse if CONFIG["dry_run"] is False and there are no ERROR-level issues.
"""

import csv
import json
import os
import re
import tempfile
from datetime import datetime, timezone

import synapseclient
from synapseclient.core.exceptions import SynapseHTTPError
from synapseclient.models import WikiHeader, WikiPage

# ==============================================================================
# Settings
# ==============================================================================

_HERE = os.path.dirname(os.path.abspath(__file__))

CONFIG = {
    "dry_run": True,  # True: only audit + write the before/after files, change nothing.
                       # False: store the new wiki text - if there are no ERROR-level issues.

    "ticket_id": "EXAMPLE-000",  # used to name the run folder and in the audit log

    # CSV with columns source_id, destination_id, and (optional) section_label and
    # destination_path (a human-readable note). Keep real plans in ../plans/ (git-ignored).
    "plan_csv": os.path.join(_HERE, "..", "plans", "example_copy_plan.csv"),

    # "replace": the destination page ends up with only the copied text.
    # "append":  keep the destination's current text and add the copied text after it.
    "destination_mode": "replace",

    # Heading level for each row's section_label: 2 -> "## label". Pick one level above the
    # headings inside the copied text so the copied sections sit under the label.
    "label_heading_level": 2,
}

# ==============================================================================
# Reference values
# ==============================================================================

_SYN_ID = re.compile(r"^syn\d+$")

_DESTINATION_MODES = {"replace", "append"}

RUNS_DIR = os.path.normpath(os.path.join(_HERE, "..", "runs"))


# ==============================================================================
# Setup
# ==============================================================================

def login_once(auth_token=None):
    """
    Log in to Synapse once. Falls back to ~/.synapseConfig or the
    SYNAPSE_AUTH_TOKEN env var if auth_token is None. Asserts the connection is
    HTTPS rather than assuming it.
    """
    syn = synapseclient.Synapse()
    syn.login(authToken=auth_token)
    if not syn.repoEndpoint.startswith("https://"):
        raise RuntimeError(f"Refusing to proceed: Synapse repoEndpoint is not HTTPS ({syn.repoEndpoint!r}).")
    return syn


def make_run_dir(ticket_id):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = os.path.join(RUNS_DIR, f"{ticket_id}_{stamp}")
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def _log_action(run_dir, ticket_id, action, **fields):
    """Append one JSON line to this run's audit log. Never pass wiki text in here."""
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "ticket": ticket_id,
              "action": action, **fields}
    with open(os.path.join(run_dir, "audit_log.jsonl"), "a") as f:
        f.write(json.dumps(record) + "\n")


def _issue(level, column, message, value):
    return {"level": level, "column": column, "message": message, "value": value}


# ==============================================================================
# Plan loading
# ==============================================================================

def load_plan(path):
    """Read the copy plan CSV. Returns (rows, issues)."""
    rows, issues = [], []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        missing = {"source_id", "destination_id"} - set(reader.fieldnames or [])
        if missing:
            return [], [_issue("ERROR", "plan_csv", f"missing required column(s) {sorted(missing)}", path)]
        for line_no, raw in enumerate(reader, start=2):
            row = {k: (v or "").strip() for k, v in raw.items()}
            row.setdefault("section_label", "")
            for col in ("source_id", "destination_id"):
                if not _SYN_ID.match(row[col]):
                    issues.append(_issue("ERROR", col, f"line {line_no}: not a Synapse ID", row[col]))
            if row["source_id"] == row["destination_id"]:
                issues.append(_issue("ERROR", "source_id", f"line {line_no}: source and destination are the same",
                                     row["source_id"]))
            if any((r["source_id"], r["destination_id"]) == (row["source_id"], row["destination_id"]) for r in rows):
                issues.append(_issue("ERROR", "source_id", f"line {line_no}: this source -> destination pair is "
                                     "listed more than once", row["source_id"]))
            rows.append(row)
    if not rows:
        issues.append(_issue("ERROR", "plan_csv", "plan has no rows", path))
    return rows, issues


def group_by_destination(rows):
    """{destination_id: [rows in CSV order]}, destinations in order of first appearance."""
    groups = {}
    for row in rows:
        groups.setdefault(row["destination_id"], []).append(row)
    return groups


# ==============================================================================
# Synapse lookups
# ==============================================================================

def wiki_headers(syn, owner_id):
    """Every page header in owner_id's wiki tree, or [] if it has no wiki."""
    try:
        return list(WikiHeader.get(owner_id=owner_id, synapse_client=syn))
    except SynapseHTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return []
        raise


def root_page(syn, headers, owner_id):
    """The top (root) WikiPage of owner_id's wiki, or None if it has no wiki."""
    root = next((h for h in headers if not h.parent_id), None)
    if root is None:
        return None
    return WikiPage(owner_id=owner_id, id=root.id).get(synapse_client=syn)


def page_text(syn, page):
    """A wiki page's markdown text ("" if empty). Wiki text is stored as a separate file in Synapse."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        path = page.get_markdown_file(download_file=True, download_location=tmp_dir, synapse_client=syn)
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()


def can_edit(syn, entity_id):
    return syn.restGET(f"/entity/{entity_id}/permissions").get("canEdit", False)


# ==============================================================================
# Audit (read-only)
# ==============================================================================

def build_text(existing, sections, mode, level):
    """
    The destination's new wiki text: each source's text, under its label if
    it has one, joined in plan order - after the existing text in "append"
    mode.
    """
    blocks = []
    for label, text in sections:
        body = text.strip()
        blocks.append(f"{'#' * level} {label}\n\n{body}" if label else body)
    copied = "\n\n".join(blocks) + "\n"
    if mode == "append" and existing.strip():
        return existing.rstrip() + "\n\n" + copied, copied
    return copied, copied


def audit_destination(syn, dest_id, rows, config, run_dir):
    """Check one destination and everything going into it. Returns (dest_plan or None, issues)."""
    issues = []
    try:
        dest = syn.restGET(f"/entity/{dest_id}")
    except SynapseHTTPError as e:
        return None, [_issue("ERROR", "destination_id", f"can't read destination: {e}", dest_id)]
    if not can_edit(syn, dest_id):
        issues.append(_issue("ERROR", "destination_id", f"you don't have edit permission on '{dest['name']}'",
                             dest_id))

    dest_headers = wiki_headers(syn, dest_id)
    dest_page = root_page(syn, dest_headers, dest_id)
    existing = page_text(syn, dest_page) if dest_page else ""
    if dest_page and dest_page.attachment_file_handle_ids:
        # WikiPage.store() replaces the attachment list, so they'd be lost.
        issues.append(_issue("ERROR", "destination_id", f"destination wiki has "
                             f"{len(dest_page.attachment_file_handle_ids)} attachment(s); this tool doesn't "
                             "copy or keep wiki attachments - update this page by hand", dest_id))

    sections = []
    for row in rows:
        src_id = row["source_id"]
        try:
            src = syn.restGET(f"/entity/{src_id}")
        except SynapseHTTPError as e:
            issues.append(_issue("ERROR", "source_id", f"can't read source: {e}", src_id))
            continue
        src_headers = wiki_headers(syn, src_id)
        src_page = root_page(syn, src_headers, src_id)
        if src_page is None:
            issues.append(_issue("ERROR", "source_id", f"'{src['name']}' has no wiki", src_id))
            continue
        text = page_text(syn, src_page)
        if not text.strip():
            issues.append(_issue("ERROR", "source_id", f"'{src['name']}' wiki is empty", src_id))
        if src_page.attachment_file_handle_ids:
            issues.append(_issue("ERROR", "source_id", f"'{src['name']}' wiki has "
                                 f"{len(src_page.attachment_file_handle_ids)} attachment(s); this tool doesn't "
                                 "copy wiki attachments - copy this page by hand", src_id))
        if len(src_headers) > 1:
            issues.append(_issue("WARNING", "source_id", f"'{src['name']}' wiki has {len(src_headers) - 1} "
                                 "sub-page(s); only the top page is copied", src_id))
        sections.append((row["section_label"], text))
        print(f"    from {src_id} '{src['name']}': {len(text)} characters"
              + (f", labeled '{row['section_label']}'" if row["section_label"] else ""))

    new_text, copied = build_text(existing, sections, config["destination_mode"], config["label_heading_level"])
    up_to_date = existing == new_text
    if config["destination_mode"] == "append" and not up_to_date and copied.strip() in existing:
        issues.append(_issue("ERROR", "destination_id", "the copied text is already on the destination page - "
                             "running in append mode again would add it twice", dest_id))

    before_path = os.path.join(run_dir, f"before_{dest_id}.md")
    after_path = os.path.join(run_dir, f"after_{dest_id}.md")
    for path, text in ((before_path, existing), (after_path, new_text)):
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    plan = {"destination_id": dest_id, "destination_name": dest["name"],
            "wiki_id": dest_page.id if dest_page else None, "source_ids": [r["source_id"] for r in rows],
            "existing_chars": len(existing), "new_chars": len(new_text), "up_to_date": up_to_date,
            "before_path": before_path, "after_path": after_path}
    return plan, issues


def print_report(issues):
    if not issues:
        print("No issues found.")
        return
    for issue in issues:
        print(f"{issue['level']}: [{issue['column']}] {issue['message']} (value: {issue['value']!r})")


# ==============================================================================
# Changes (only run when dry_run is False)
# ==============================================================================

def store_wiki(syn, plan, run_dir, ticket_id):
    """Store after_<destination>.md as the destination's top wiki page (created if missing)."""
    # Passing a file path (not a string) uploads the text byte-for-byte - see wiki-setup.
    page = WikiPage(owner_id=plan["destination_id"], id=plan["wiki_id"], markdown=plan["after_path"])
    if plan["wiki_id"] is None:
        page.title = ""
    stored = page.store(synapse_client=syn)
    action = "update_wiki" if plan["wiki_id"] else "create_wiki"
    _log_action(run_dir, ticket_id, action, destination_id=plan["destination_id"], wiki_id=stored.id,
                source_ids=plan["source_ids"], before_file=os.path.basename(plan["before_path"]),
                after_file=os.path.basename(plan["after_path"]))
    print(f"  {'Updated' if plan['wiki_id'] else 'Created'} wiki {stored.id} on {plan['destination_id']} "
          f"'{plan['destination_name']}': https://www.synapse.org/Synapse:{plan['destination_id']}/wiki/{stored.id}")
    return stored.id


def verify(syn, plan, wiki_id):
    """Re-download the stored text and compare it to after_<destination>.md."""
    stored_text = page_text(syn, WikiPage(owner_id=plan["destination_id"], id=wiki_id).get(synapse_client=syn))
    with open(plan["after_path"], encoding="utf-8", newline="") as f:
        if stored_text == f.read():
            return None
    return _issue("ERROR", "destination_id", f"stored wiki text differs from {plan['after_path']}",
                  plan["destination_id"])


def main(config):
    rows, plan_issues = load_plan(config["plan_csv"])
    if config["destination_mode"] not in _DESTINATION_MODES:
        plan_issues.append(_issue("ERROR", "destination_mode", f"must be one of {sorted(_DESTINATION_MODES)}",
                                  config["destination_mode"]))
    groups = group_by_destination(rows)
    print(f"=== Copy plan: {len(rows)} row(s) into {len(groups)} destination(s) from {config['plan_csv']} ===")
    for r in rows:
        print(f"  {r['source_id']} -> {r['destination_id']}  {r.get('destination_path', '')}")
    if any(i["level"] == "ERROR" for i in plan_issues):
        print_report(plan_issues)
        return

    syn = login_once()
    run_dir = make_run_dir(config["ticket_id"])

    print(f"\n=== Audit (read-only, destination_mode: {config['destination_mode']}) ===")
    dest_plans, all_issues = [], list(plan_issues)
    for dest_id, dest_rows in groups.items():
        print(f"  {dest_id}:")
        plan, issues = audit_destination(syn, dest_id, dest_rows, config, run_dir)
        all_issues += issues
        if plan:
            dest_plans.append(plan)
            status = "already up to date" if plan["up_to_date"] else \
                f"{plan['existing_chars']} -> {plan['new_chars']} characters"
            print(f"    '{plan['destination_name']}': {status}")
            print(f"    read the new text here: {plan['after_path']}")

    print("\n=== Validation report ===")
    print_report(all_issues)

    if config["dry_run"]:
        print("\ndry_run is True - nothing was changed. Read the after_*.md files above, then set "
              "CONFIG['dry_run'] = False to store them.")
        return
    if any(i["level"] == "ERROR" for i in all_issues):
        print("\nNot applying changes: fix the ERROR-level issue(s) above first.")
        return

    print("\n=== Storing ===")
    verify_issues = []
    for plan in dest_plans:
        if plan["up_to_date"]:
            print(f"  {plan['destination_id']}: already up to date, skipped")
            continue
        wiki_id = store_wiki(syn, plan, run_dir, config["ticket_id"])
        issue = verify(syn, plan, wiki_id)
        if issue:
            verify_issues.append(issue)

    print("\n=== Verification ===")
    print_report(verify_issues)
    print(f"\nDone. Run folder (before/after text + audit log): {run_dir}")


if __name__ == "__main__":
    main(CONFIG)
