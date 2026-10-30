"""
Move data that already exists somewhere in Synapse (e.g. a contributor's own
workspace project) into a destination folder in the AD Knowledge Portal
backend - most often a study's Staging folder during Stages 1-2 of data
ingress, but the same steps apply to any Synapse-to-Synapse move (e.g.
Staging -> released Data folder in Stage 4).

Each row of the move plan CSV says "move everything inside ORIGIN into
DESTINATION". CONFIG["transfer_method"] picks how:

  "migrate"  - relocate the bytes into the destination's storage location
               server-side, then move (same synIDs). Fast, but you must be the
               creator of the destination's storage location.
  "reupload" - download each file and upload it into the destination (new
               synIDs), then - only once every copy is verified - move the
               originals to the Synapse trash. Works without owning the
               destination storage, but every byte passes through the machine
               running this, so run it somewhere with a fast connection (EC2).
  "move"     - only change the parent folder; the bytes stay where they are.

For "migrate" and "move", for every row this script:

  1. Audits before touching anything: both IDs exist, you have permission to
     move/add, the destination is private (no PUBLIC / all-registered-users
     access), which items in the origin (files and folders, at any depth)
     have their own local sharing settings that would follow them, no name
     collisions, and which storage location the files live in vs. the
     destination folder's storage location.
  2. Writes a "before" manifest (one CSV row per file: synID, name, MD5, size,
     storage location) so there's a local record of exactly what was moved.
  3. (If the storage locations differ and transfer_method is "migrate") Migrates the
     file bytes into the destination's storage location using synapseutils'
     index_files_for_migration() / migrate_indexed_files(). This keeps every
     synID the same - only the underlying storage changes.
  4. Moves the origin's children (not the origin folder itself) into the
     destination by changing each entity's parent. synIDs stay the same, and
     the moved items now inherit the destination's sharing settings.
  5. (If reset_local_sharing is True) Removes the local sharing settings found
     in step 1, so those items inherit the destination's too. Each one is
     saved to the run folder before it's removed.
  6. Optionally leaves a Link in the origin folder pointing to the destination,
     so the contributor's team can see where the data went.
  7. Verifies every file: correct parent, same MD5 as before, (if migrated)
     now in the destination's storage location, and every moved file/folder
     gets its sharing settings from the destination. Writes an "after"
     manifest.

For "reupload", steps 1-2 are the same (plus: which files are already copied
from an earlier run, so a re-run picks up where it stopped), then for each file
it downloads it, uploads it to the same relative path under the destination
with the same name and annotations, checks the copy's MD5 and storage
location, and deletes the local download before the next file. Only the
current version of each file is copied, and provenance isn't carried over. If
CONFIG["delete_originals"] is True and every copy verifies, the originals are
moved to the Synapse trash (restorable from there) and the optional redirect
Link is left. Copies are new entities, so they inherit the destination's
sharing settings.

Why both a move AND a migration? Changing an entity's parent only changes
where it shows up in Synapse - it does NOT move the bytes. If a contributor
uploaded to their own project (Synapse default storage) and the ADKP Staging
folder is backed by an ADKP S3 bucket, a plain move leaves the bytes in the
old storage. (synapseutils.copy() doesn't fix this either - it copies the file
handle, which still points at the same stored object, and gives every file a
new synID.) The migration step is what actually relocates the bytes.

Setup:
    pip install -r requirements.txt
    Authenticate once via `synapse config` (~/.synapseConfig) or by setting the
    SYNAPSE_AUTH_TOKEN env var to a personal access token - do not hardcode
    credentials in this file.

To use: put your move plan in a CSV under ../plans/ (that folder is
git-ignored, so real synIDs never get committed - see
../plans/example_move_plan.csv for the format), point CONFIG["plan_csv"] at
it, then run this file directly (`python move_data.py`). It always prints the
audit report and writes the "before" manifest; it only changes anything in
Synapse if CONFIG["dry_run"] is False and there are no ERROR-level issues.
Migration also asks for a final y/n confirmation in the terminal before each
row, unless CONFIG["confirm_migration"] is False.

Run outputs (manifests, migration progress databases, audit log) go to
../runs/<ticket>_<timestamp>/, which is also git-ignored.
"""

import csv
import json
import os
import re
import shutil
from datetime import datetime, timezone

import synapseclient
import synapseutils
from synapseclient.core.exceptions import SynapseHTTPError
from synapseclient.models import File, Folder, Link

# ==============================================================================
# CONFIG - the only section you should need to edit.
# ==============================================================================

_HERE = os.path.dirname(os.path.abspath(__file__))

CONFIG = {
    "dry_run": True,  # True: only audit + write the "before" manifest, change nothing.
                       # False: transfer and verify - if there are no ERROR-level issues.

    "ticket_id": "EXAMPLE-000",  # used to name the run folder and in the audit log

    # CSV with columns origin_id, destination_id, and (optional) destination_path,
    # which is just a human-readable note. Keep real plans in ../plans/ (git-ignored).
    "plan_csv": os.path.join(_HERE, "..", "plans", "example_move_plan.csv"),

    # "migrate": if the files' storage location differs from the destination folder's, migrate
    #            the bytes into the destination's storage location, then move (same synIDs).
    #            You must be the creator of the destination's storage location.
    # "reupload": download each file and upload it into the destination (new synIDs). Use when
    #            you can't migrate. All the data passes through this machine - run it on EC2.
    # "move":    only change the parent folder (bytes stay where they are).
    "transfer_method": "migrate",

    # True: after moving, remove any local sharing settings on the moved files/folders
    # so everything inherits the destination's (each one is saved to the run folder first).
    # False: leave them in place - they keep the origin's settings, and the audit only warns.
    "reset_local_sharing": True,

    # True: ask y/n in the terminal before migrating each row (the run waits until you answer).
    # False: migrate without asking, so a multi-row run can finish unattended - only after a
    # clean dry run.
    "confirm_migration": True,

    # "reupload" only. False: copy and verify, leave the originals alone.
    # True: once every copy verifies, move the originals to the Synapse trash.
    "delete_originals": False,

    # "reupload" only. Where each file is downloaded to (one at a time, deleted after its
    # upload). None: a _downloads folder inside the run folder.
    "download_dir": None,

    # True: after moving (for "reupload": after deleting the originals), leave a Link in each
    # origin folder pointing at its destination.
    "leave_redirect_link": False,
}

# ==============================================================================
# Reference values
# ==============================================================================

# Synapse's reserved principal IDs - used to make sure the destination isn't
# shared with the public or with every registered Synapse user.
PUBLIC_PRINCIPAL_ID = 273949
AUTHENTICATED_USERS_PRINCIPAL_ID = 273948
_BROAD_PRINCIPAL_IDS = {PUBLIC_PRINCIPAL_ID, AUTHENTICATED_USERS_PRINCIPAL_ID}
_EXPOSING_ACCESS_TYPES = {"READ", "DOWNLOAD"}

_FILE_TYPE = "org.sagebionetworks.repo.model.FileEntity"
_FOLDER_TYPE = "org.sagebionetworks.repo.model.Folder"
_PROJECT_TYPE = "org.sagebionetworks.repo.model.Project"
_CONTAINER_TYPES = {_FOLDER_TYPE, _PROJECT_TYPE}

_SYN_ID = re.compile(r"^syn\d+$")

_TRANSFER_METHODS = {"migrate", "reupload", "move"}

# "all" migrates every existing version of each file in place, so no old
# version is left behind in the previous storage location. See
# synapseutils.index_files_for_migration() for the other options.
FILE_VERSION_STRATEGY = "all"

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
    """Append one JSON line to this run's audit log. Never pass file contents in here."""
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
    """Read the move plan CSV. Returns (rows, issues)."""
    rows, issues = [], []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        missing = {"origin_id", "destination_id"} - set(reader.fieldnames or [])
        if missing:
            return [], [_issue("ERROR", "plan_csv", f"missing required column(s) {sorted(missing)}", path)]
        for line_no, raw in enumerate(reader, start=2):
            row = {k: (v or "").strip() for k, v in raw.items()}
            for col in ("origin_id", "destination_id"):
                if not _SYN_ID.match(row[col]):
                    issues.append(_issue("ERROR", col, f"line {line_no}: not a Synapse ID", row[col]))
            if row["origin_id"] == row["destination_id"]:
                issues.append(_issue("ERROR", "origin_id", f"line {line_no}: origin and destination are the same",
                                     row["origin_id"]))
            if any(r["origin_id"] == row["origin_id"] for r in rows):
                issues.append(_issue("ERROR", "origin_id", f"line {line_no}: origin is listed more than once",
                                     row["origin_id"]))
            rows.append(row)
    if not rows:
        issues.append(_issue("ERROR", "plan_csv", "plan has no rows", path))
    return rows, issues


# ==============================================================================
# Synapse lookups
# ==============================================================================

def get_upload_storage(syn, container_id):
    """Return (storage_location_id, sts_enabled) for uploads into this container."""
    dest = syn.restGET(f"/entity/{container_id}/uploadDestination", endpoint=syn.fileHandleEndpoint)
    return dest.get("storageLocationId"), bool(dest.get("stsEnabled", False))


def get_benefactor(syn, entity_id):
    """The entity whose sharing settings apply here (itself, if it has a local ACL)."""
    return syn.restGET(f"/entity/{entity_id}/benefactor")["id"]


def get_permissions(syn, entity_id):
    return syn.restGET(f"/entity/{entity_id}/permissions")


def check_destination_private(syn, dest_id):
    """ERROR if the destination is visible to the public or to every registered Synapse user."""
    benefactor_id = get_benefactor(syn, dest_id)
    acl = syn.restGET(f"/entity/{benefactor_id}/acl")
    issues = []
    for entry in acl.get("resourceAccess", []):
        if entry["principalId"] in _BROAD_PRINCIPAL_IDS and _EXPOSING_ACCESS_TYPES & set(entry["accessType"]):
            issues.append(_issue(
                "ERROR", "destination_id",
                f"destination is shared with principal {entry['principalId']} "
                f"({sorted(entry['accessType'])}) via benefactor {benefactor_id} - a Staging destination "
                "must be private. Fix its sharing settings before moving data in.",
                dest_id))
    return issues


def list_items_to_move(syn, origin):
    """
    The top-level things that get re-parented: the origin's direct children if
    it's a Folder/Project, or the origin itself if it's a single File.
    """
    if origin["concreteType"] == _FILE_TYPE:
        return [{"id": origin["id"], "name": origin["name"], "type": _FILE_TYPE}]
    return [{"id": c["id"], "name": c["name"], "type": c["type"]} for c in syn.getChildren(origin["id"])]


def files_under(syn, item):
    """
    (synID, name, path relative to the origin) for every file at or below one item
    (a file is just itself).
    """
    if item["type"] == _FILE_TYPE:
        return [(item["id"], item["name"], item["name"])]
    found = []
    for dirpath, _dirs, files in synapseutils.walk(syn, item["id"], includeTypes=["folder", "file"]):
        found.extend((file_id, name, f"{dirpath[0]}/{name}") for name, file_id in files)
    return found


def dest_files_by_path(syn, dest_id):
    """file_info() of every file already under the destination, keyed by path relative to it."""
    found = {}
    for dirpath, _dirs, files in synapseutils.walk(syn, dest_id, includeTypes=["folder", "file"]):
        prefix = dirpath[0].split("/")[1:]  # walk paths start with the destination's own name
        for name, file_id in files:
            found["/".join([*prefix, name])] = file_info(file_id)
    return found


def subfolders_under(syn, item):
    if item["type"] != _FOLDER_TYPE:
        return []
    found = []
    for _dirpath, dirs, _files in synapseutils.walk(syn, item["id"], includeTypes=["folder"]):
        found.extend(folder_id for _name, folder_id in dirs)
    return found


def file_info(file_id):
    """MD5/size/storage location/parent of a file's current version, without downloading it."""
    f = File(id=file_id, download_file=False).get()
    fh = f.file_handle
    return {"file_id": file_id, "file_name": f.name, "parent_id": f.parent_id,
            "storage_location_id": fh.storage_location_id, "content_md5": fh.content_md5,
            "content_size": fh.content_size}


def check_storage_owner(syn, storage_location_id, label):
    """ERROR unless you created this storage location - Synapse only lets its creator migrate into it."""
    try:
        syn.restGET(f"/storageLocation/{storage_location_id}")
        return []
    except SynapseHTTPError as e:
        return [_issue("ERROR", "transfer_method",
                       f"{label}: you can't migrate into storage location {storage_location_id} - only its creator "
                       f"can ({e}). Use transfer_method 'reupload', or ask its creator to run this.",
                       storage_location_id)]


def match_existing_copies(syn, dest_id, manifest, claimed, label):
    """
    "reupload" only. Sets each manifest row's copy_id if an earlier run already copied it
    (same relative path and MD5 under the destination), so a re-run picks up where it
    stopped. ERROR if a different file already has that path, or two rows would copy to it.
    """
    existing = dest_files_by_path(syn, dest_id)
    issues = []
    for m in manifest:
        rel = m["relative_path"]
        if rel in claimed:
            issues.append(_issue("ERROR", "destination_id",
                                 f"{label}: '{rel}' is also being copied here from {claimed[rel]}", m["file_id"]))
        claimed[rel] = m["origin_id"]
        copy = existing.get(rel)
        if copy is None:
            m["copy_id"] = ""
        elif copy["content_md5"] == m["content_md5"]:
            m["copy_id"] = copy["file_id"]
        else:
            m["copy_id"] = ""
            issues.append(_issue("ERROR", "destination_id",
                                 f"{label}: '{rel}' already exists in the destination ({copy['file_id']}) with "
                                 "different contents", m["file_id"]))
    return issues


# ==============================================================================
# Audit (read-only)
# ==============================================================================

def audit_row(syn, row, config, names_claimed):
    """
    Read-only checks for one plan row. Returns (row_plan, issues, manifest_rows).
    names_claimed maps destination_id -> {name: origin_id} across all rows, so two
    rows moving same-named items into one destination are caught too.
    """
    origin_id, dest_id = row["origin_id"], row["destination_id"]
    issues, manifest = [], []
    label = f"{origin_id} -> {dest_id}"

    origin = syn.restGET(f"/entity/{origin_id}")
    dest = syn.restGET(f"/entity/{dest_id}")
    if dest["concreteType"] not in _CONTAINER_TYPES:
        issues.append(_issue("ERROR", "destination_id", f"{label}: destination is not a Folder/Project", dest_id))
        return None, issues, manifest
    if origin["concreteType"] not in _CONTAINER_TYPES | {_FILE_TYPE}:
        issues.append(_issue("ERROR", "origin_id", f"{label}: origin must be a Folder, Project, or File", origin_id))
        return None, issues, manifest

    dest_path_ids = [p["id"] for p in syn.restGET(f"/entity/{dest_id}/path")["path"]]
    if origin_id in dest_path_ids:
        issues.append(_issue("ERROR", "destination_id", f"{label}: destination is inside the origin", dest_id))

    # Permissions: you need to add children to the destination and move things out of the origin.
    if not get_permissions(syn, dest_id).get("canAddChild"):
        issues.append(_issue("ERROR", "destination_id", f"{label}: you can't add items to the destination", dest_id))
    origin_perms = get_permissions(syn, origin_id)
    if not (origin_perms.get("canEdit") and origin_perms.get("canMove", True)):
        issues.append(_issue(
            "ERROR", "origin_id",
            f"{label}: you can't edit/move items in the origin - ask the owner to grant your Sage team "
            "edit (or admin) access on the origin folder", origin_id))

    issues += check_destination_private(syn, dest_id)

    dest_storage, dest_sts = get_upload_storage(syn, dest_id)
    if dest_sts:
        issues.append(_issue("ERROR", "destination_id",
                             f"{label}: destination uses an STS-enabled storage location - move manually "
                             "after confirming with Sage IT/DPE", dest_id))

    items = list_items_to_move(syn, origin)
    if not items:
        issues.append(_issue("WARNING", "origin_id", f"{label}: origin is empty - nothing to move", origin_id))

    method = config["transfer_method"]
    reupload = method == "reupload"
    claimed = names_claimed.setdefault(dest_id, {})
    for item in items:
        if item["type"] not in {_FILE_TYPE, _FOLDER_TYPE}:
            issues.append(_issue("ERROR", "origin_id",
                                 f"{label}: '{item['name']}' is a {item['type'].split('.')[-1]} - this script "
                                 "only moves Files and Folders; move it by hand or remove it from the origin",
                                 item["id"]))
    if reupload and config["delete_originals"]:
        for item in items:
            if not get_permissions(syn, item["id"]).get("canDelete"):
                issues.append(_issue("ERROR", "origin_id",
                                     f"{label}: you can't delete '{item['name']}' from the origin (needed for "
                                     "delete_originals)", item["id"]))
    if not reupload:  # "reupload" checks names file by file below, so a re-run can skip finished copies
        existing_dest_names = {c["name"] for c in syn.getChildren(dest_id)}
        for item in items:
            if item["name"] in existing_dest_names:
                issues.append(_issue("ERROR", "destination_id",
                                     f"{label}: '{item['name']}' already exists in the destination", item["id"]))
            elif item["name"] in claimed:
                issues.append(_issue("ERROR", "destination_id",
                                     f"{label}: '{item['name']}' is also being moved here from "
                                     f"{claimed[item['name']]}", item["id"]))
            claimed[item["name"]] = origin_id

    # Anything with its own sharing settings (a file or folder at any depth) keeps
    # them after the move instead of inheriting the destination's. Re-uploaded copies
    # are new entities, so they always inherit it and this doesn't apply.
    print(f"  {label}: checking {len(items)} item(s) for local sharing settings and file storage...")
    files_by_item = {item["id"]: files_under(syn, item) for item in items}
    tree_ids = [] if reupload else list(dict.fromkeys(
        entity_id for item in items
        for entity_id in [item["id"], *subfolders_under(syn, item),
                          *(file_id for file_id, _name, _rel in files_by_item[item["id"]])]))
    local_acl_ids = []
    for entity_id in tree_ids:
        if get_benefactor(syn, entity_id) != entity_id:
            continue
        local_acl_ids.append(entity_id)
        if not config["reset_local_sharing"]:
            issues.append(_issue(
                "WARNING", "origin_id",
                f"{label}: {entity_id} has its own local sharing settings, which will move with it and "
                "NOT inherit the destination's - set reset_local_sharing=True or remove them after the move",
                entity_id))
        elif not get_permissions(syn, entity_id).get("canChangePermissions"):
            issues.append(_issue(
                "ERROR", "origin_id",
                f"{label}: {entity_id} has its own local sharing settings and you can't change them - ask the "
                "owner for admin access so they can be removed", entity_id))
        else:
            issues.append(_issue(
                "WARNING", "origin_id",
                f"{label}: {entity_id} has its own local sharing settings - they'll be saved to the run folder "
                "and removed after the move so it inherits the destination's", entity_id))

    needs_migration = 0
    for item in items:
        for file_id, _name, rel_path in files_by_item[item["id"]]:
            info = file_info(file_id)
            info.update({"origin_id": origin_id, "destination_id": dest_id, "top_level_item_id": item["id"],
                         "relative_path": rel_path})
            manifest.append(info)
            if info["storage_location_id"] != dest_storage:
                needs_migration += 1

    if reupload:
        issues += match_existing_copies(syn, dest_id, manifest, claimed, label)
    elif needs_migration and method == "move":
        issues.append(_issue(
            "WARNING", "transfer_method",
            f"{label}: {needs_migration} file(s) are in a different storage location than the destination "
            f"({dest_storage}). With transfer_method 'move' they will show up in the destination, but the bytes "
            "will stay in their current storage.", needs_migration))
    elif needs_migration:
        issues += check_storage_owner(syn, dest_storage, label)

    row_plan = {"origin_id": origin_id, "origin_name": origin["name"],
                "origin_is_container": origin["concreteType"] in _CONTAINER_TYPES,
                "destination_id": dest_id, "destination_name": dest["name"],
                "destination_path": row.get("destination_path", ""),
                "destination_storage_location_id": dest_storage,
                "destination_benefactor_id": get_benefactor(syn, dest_id),
                "tree_ids": tree_ids, "local_acl_ids": local_acl_ids,
                "items": items, "file_count": len(manifest), "needs_migration": needs_migration,
                "total_bytes": sum(m["content_size"] or 0 for m in manifest),
                "already_copied": sum(1 for m in manifest if m.get("copy_id")),
                "bytes_to_copy": sum(m["content_size"] or 0 for m in manifest if not m.get("copy_id"))}
    return row_plan, issues, manifest


_MANIFEST_COLUMNS = ["origin_id", "destination_id", "top_level_item_id", "relative_path", "file_id", "file_name",
                     "parent_id", "storage_location_id", "content_md5", "content_size",
                     # "reupload" only: the copy, and (after verification) what it looks like
                     "copy_id", "copy_parent_id", "copy_storage_location_id", "copy_content_md5"]


def write_manifest(path, manifest):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_MANIFEST_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(manifest)


def print_report(issues):
    if not issues:
        print("No issues found.")
        return
    for issue in issues:
        print(f"{issue['level']}: [{issue['column']}] {issue['message']} (value: {issue['value']!r})")


# ==============================================================================
# Changes (only run when dry_run is False)
# ==============================================================================

def migrate_row(syn, row_plan, run_dir, ticket_id, confirm):
    """
    Migrate the origin's files into the destination's storage location, in place
    (same synIDs). Done before the move, so if it fails nothing has moved yet.
    Returns True if every indexed file migrated. Re-running with the same db_path
    resumes where it left off.
    """
    db_path = os.path.join(run_dir, f"migration_{row_plan['origin_id']}.db")
    dest_storage = str(row_plan["destination_storage_location_id"])
    print(f"\nIndexing {row_plan['origin_id']} for migration to storage location {dest_storage}...")
    synapseutils.index_files_for_migration(
        syn, row_plan["origin_id"], dest_storage, db_path, file_version_strategy=FILE_VERSION_STRATEGY,
    )
    # force=False (confirm=True): synapseutils prints the file count and asks y/n before copying anything.
    result = synapseutils.migrate_indexed_files(syn, db_path, create_table_snapshots=False, force=not confirm)
    if result is None:
        print("Migration not confirmed - stopping before the move.")
        return False

    counts = result.get_counts_by_status()
    result.as_csv(os.path.join(run_dir, f"migration_{row_plan['origin_id']}.csv"))
    _log_action(run_dir, ticket_id, "migrate", origin_id=row_plan["origin_id"],
                storage_location_id=dest_storage, counts=counts)
    print(f"Migration result: {counts}")
    return counts.get("ERRORED", 0) == 0 and counts.get("INDEXED", 0) == 0


def move_items(syn, row_plan, run_dir, ticket_id):
    """Re-parent each top-level item into the destination. synIDs don't change."""
    dest_id = row_plan["destination_id"]
    for item in row_plan["items"]:
        if item["type"] == _FILE_TYPE:
            entity = File(id=item["id"], download_file=False).get()
            entity.parent_id = dest_id
            entity.force_version = False  # a move shouldn't create a new file version
            entity.store()
        else:
            entity = Folder(id=item["id"]).get()
            entity.parent_id = dest_id
            entity.store()
        _log_action(run_dir, ticket_id, "move", entity_id=item["id"], name=item["name"],
                    from_parent=row_plan["origin_id"], to_parent=dest_id)
    print(f"Moved {len(row_plan['items'])} item(s) from {row_plan['origin_id']} to {dest_id}.")


def remove_local_sharing(syn, row_plan, run_dir, ticket_id):
    """
    Delete the local sharing settings (ACL) on each moved item that had one, so it
    inherits the destination's. Runs after the move, so the item inherits straight
    from the destination. Each ACL is saved to the run folder before it's deleted.
    """
    for entity_id in row_plan["local_acl_ids"]:
        acl = syn.restGET(f"/entity/{entity_id}/acl")
        with open(os.path.join(run_dir, f"removed_acl_{entity_id}.json"), "w") as f:
            json.dump(acl, f, indent=2)
        syn.restDELETE(f"/entity/{entity_id}/acl")
        _log_action(run_dir, ticket_id, "remove_local_sharing", entity_id=entity_id,
                    removed_principal_ids=[e["principalId"] for e in acl.get("resourceAccess", [])])
    print(f"Removed local sharing settings from {len(row_plan['local_acl_ids'])} item(s).")


def ensure_folders(rel_dir, folder_ids):
    """synID of rel_dir under the destination (folder_ids[""]), creating any missing folders."""
    if rel_dir not in folder_ids:
        parent_id = ensure_folders(os.path.dirname(rel_dir), folder_ids)
        # create_or_update (the default) returns the existing folder if one has this name
        folder_ids[rel_dir] = Folder(name=os.path.basename(rel_dir), parent_id=parent_id).store().id
    return folder_ids[rel_dir]


def reupload_row(row_plan, manifest, run_dir, ticket_id, download_dir):
    """
    Download each not-yet-copied file in this row and upload it to the same relative
    path under the destination, with the same name, annotations, and description.
    One file at a time; the local download is deleted after each upload. Stops at the
    first copy whose MD5 or storage location doesn't match. Returns True if all copied.
    """
    dest_id, dest_storage = row_plan["destination_id"], str(row_plan["destination_storage_location_id"])
    to_copy = [m for m in manifest if m["origin_id"] == row_plan["origin_id"] and not m["copy_id"]]
    folder_ids = {"": dest_id}
    os.makedirs(download_dir, exist_ok=True)
    for n, m in enumerate(to_copy, start=1):
        print(f"  [{n}/{len(to_copy)}] {m['relative_path']} ({(m['content_size'] or 0) / 1e9:.2f} GB)")
        parent_id = ensure_folders(os.path.dirname(m["relative_path"]), folder_ids)
        source = File(id=m["file_id"], path=download_dir, if_collision="overwrite.local").get()
        try:
            # create_or_update=False: fail rather than add a version to a same-named file
            copy = File(path=source.path, name=source.name, parent_id=parent_id, annotations=source.annotations,
                        description=source.description, create_or_update=False).store()
        finally:
            if source.path and os.path.isfile(source.path):
                os.remove(source.path)
        info = file_info(copy.id)
        if info["content_md5"] != m["content_md5"] or str(info["storage_location_id"]) != dest_storage:
            _log_action(run_dir, ticket_id, "reupload_mismatch", file_id=m["file_id"], copy_id=copy.id,
                        content_md5=m["content_md5"], copy_content_md5=info["content_md5"],
                        copy_storage_location_id=info["storage_location_id"])
            print(f"Stopping: copy {copy.id} of {m['file_id']} has MD5 {info['content_md5']} (expected "
                  f"{m['content_md5']}) and storage location {info['storage_location_id']} (expected {dest_storage}).")
            return False
        m["copy_id"] = copy.id
        _log_action(run_dir, ticket_id, "reupload", file_id=m["file_id"], copy_id=copy.id,
                    relative_path=m["relative_path"], content_md5=m["content_md5"])
    print(f"Copied {len(to_copy)} file(s) from {row_plan['origin_id']} to {dest_id}.")
    return True


def trash_originals(syn, row_plan, run_dir, ticket_id):
    """Move the origin's top-level items to the Synapse trash (they can be restored from there)."""
    for item in row_plan["items"]:
        syn.delete(item["id"])
        _log_action(run_dir, ticket_id, "trash_original", entity_id=item["id"], name=item["name"],
                    origin_id=row_plan["origin_id"])
    print(f"Moved {len(row_plan['items'])} original item(s) in {row_plan['origin_id']} to the trash.")


def leave_redirect_link(row_plan, run_dir, ticket_id):
    name = f"MOVED - see {row_plan['destination_name']} ({row_plan['destination_id']})"
    link = Link(name=name, parent_id=row_plan["origin_id"], target_id=row_plan["destination_id"]).store()
    _log_action(run_dir, ticket_id, "redirect_link", link_id=link.id, origin_id=row_plan["origin_id"],
                target_id=row_plan["destination_id"])
    print(f"Left redirect link {link.id} in {row_plan['origin_id']}.")


def verify(syn, row_plans, manifest_before, migrated_origins, reset_local_sharing):
    """Re-check every file after the run. Returns (manifest_after, issues)."""
    by_origin = {p["origin_id"]: p for p in row_plans}
    after, issues = [], []
    for before in manifest_before:
        plan = by_origin[before["origin_id"]]
        info = file_info(before["file_id"])
        info.update({k: before[k] for k in ("origin_id", "destination_id", "top_level_item_id")})
        after.append(info)
        top = syn.restGET(f"/entity/{before['top_level_item_id']}")
        if top["parentId"] != plan["destination_id"]:
            issues.append(_issue("ERROR", "verify", f"{before['top_level_item_id']} is not under the destination",
                                 top["parentId"]))
        if info["content_md5"] != before["content_md5"]:
            issues.append(_issue("ERROR", "verify", f"{before['file_id']} MD5 changed",
                                 (before["content_md5"], info["content_md5"])))
        if before["origin_id"] in migrated_origins and \
                info["storage_location_id"] != plan["destination_storage_location_id"]:
            issues.append(_issue("ERROR", "verify", f"{before['file_id']} is not in the destination storage location",
                                 info["storage_location_id"]))
    # Every moved file/folder should get its sharing settings from the destination. With
    # reset_local_sharing=False, the ones flagged in the audit are expected not to.
    for plan in row_plans:
        for entity_id in plan["tree_ids"]:
            benefactor_id = get_benefactor(syn, entity_id)
            if benefactor_id != plan["destination_benefactor_id"]:
                issues.append(_issue(
                    "ERROR" if reset_local_sharing else "WARNING", "verify",
                    f"{entity_id} gets its sharing settings from {benefactor_id}, not the destination's "
                    f"({plan['destination_benefactor_id']})", benefactor_id))
    return after, issues


def verify_copies(syn, row_plans, manifest):
    """
    "reupload" only. Every original has a copy with the same MD5, in the destination's
    storage location, getting its sharing settings from the destination.
    Returns (manifest_after, issues).
    """
    by_origin = {p["origin_id"]: p for p in row_plans}
    after, issues = [], []
    for m in manifest:
        plan = by_origin[m["origin_id"]]
        row = dict(m)
        after.append(row)
        if not m["copy_id"]:
            issues.append(_issue("ERROR", "verify", f"{m['file_id']} ('{m['relative_path']}') has no copy", ""))
            continue
        info = file_info(m["copy_id"])
        row.update({"copy_parent_id": info["parent_id"], "copy_storage_location_id": info["storage_location_id"],
                    "copy_content_md5": info["content_md5"]})
        if info["content_md5"] != m["content_md5"]:
            issues.append(_issue("ERROR", "verify", f"copy {m['copy_id']} of {m['file_id']} has a different MD5",
                                 (m["content_md5"], info["content_md5"])))
        if str(info["storage_location_id"]) != str(plan["destination_storage_location_id"]):
            issues.append(_issue("ERROR", "verify", f"copy {m['copy_id']} is not in the destination storage location",
                                 info["storage_location_id"]))
        benefactor_id = get_benefactor(syn, m["copy_id"])
        if benefactor_id != plan["destination_benefactor_id"]:
            issues.append(_issue("ERROR", "verify",
                                 f"copy {m['copy_id']} gets its sharing settings from {benefactor_id}, not the "
                                 f"destination's ({plan['destination_benefactor_id']})", benefactor_id))
    return after, issues


def run_reupload(syn, config, row_plans, manifest, run_dir):
    """The "reupload" changes: copy, verify, then (if delete_originals) trash the originals."""
    ticket_id = config["ticket_id"]
    download_dir = config["download_dir"] or os.path.join(run_dir, "_downloads")
    for plan in row_plans:
        print(f"\nCopying {plan['origin_id']} -> {plan['destination_id']}...")
        if not reupload_row(plan, manifest, run_dir, ticket_id, download_dir):
            print(f"\nStopped. Copies made so far are kept and logged in {run_dir}; re-running skips them.")
            return

    print("\n=== Verification ===")
    manifest_after, verify_issues = verify_copies(syn, row_plans, manifest)
    write_manifest(os.path.join(run_dir, "manifest_after.csv"), manifest_after)
    print_report(verify_issues)

    if not config["delete_originals"]:
        print("\nOriginals left in place. Once you've checked the copies, set CONFIG['delete_originals'] = True "
              "and run again to move the originals to the trash.")
    elif any(i["level"] == "ERROR" for i in verify_issues):
        print("\nNot deleting the originals: fix the verification ERROR(s) above first.")
    else:
        for plan in row_plans:
            trash_originals(syn, plan, run_dir, ticket_id)
            if config["leave_redirect_link"] and plan["origin_is_container"]:
                leave_redirect_link(plan, run_dir, ticket_id)
    print(f"\nDone. Run folder (manifests + audit log): {run_dir}")


# ==============================================================================
# Entry point
# ==============================================================================

def main(config):
    rows, plan_issues = load_plan(config["plan_csv"])
    method = config["transfer_method"]
    if method not in _TRANSFER_METHODS:
        plan_issues.append(_issue("ERROR", "transfer_method", f"must be one of {sorted(_TRANSFER_METHODS)}", method))
    print(f"=== Move plan: {len(rows)} row(s) from {config['plan_csv']} ===")
    for r in rows:
        print(f"  {r['origin_id']} -> {r['destination_id']}  {r.get('destination_path', '')}")
    if any(i["level"] == "ERROR" for i in plan_issues):
        print_report(plan_issues)
        return

    syn = login_once()
    run_dir = make_run_dir(config["ticket_id"])

    print("\n=== Audit (read-only) ===")
    row_plans, all_issues, manifest_before, names_claimed = [], list(plan_issues), [], {}
    for row in rows:
        row_plan, issues, manifest = audit_row(syn, row, config, names_claimed)
        all_issues += issues
        manifest_before += manifest
        if row_plan:
            row_plans.append(row_plan)
            status = (f"{row_plan['already_copied']} already copied, {row_plan['bytes_to_copy'] / 1e9:.1f} GB to copy"
                      if method == "reupload" else f"{row_plan['needs_migration']} need storage migration")
            print(f"  {row_plan['origin_id']} '{row_plan['origin_name']}' -> {row_plan['destination_id']} "
                  f"'{row_plan['destination_name']}': {len(row_plan['items'])} item(s), "
                  f"{row_plan['file_count']} file(s), {row_plan['total_bytes'] / 1e9:.1f} GB, {status}")

    if method == "reupload":
        # One file is downloaded at a time, so there must be room for the largest one.
        download_dir = config["download_dir"] or os.path.join(run_dir, "_downloads")
        os.makedirs(download_dir, exist_ok=True)
        largest = max((m["content_size"] or 0 for m in manifest_before if not m["copy_id"]), default=0)
        free = shutil.disk_usage(download_dir).free
        if free < largest * 1.5:
            all_issues.append(_issue("ERROR", "download_dir",
                                     f"only {free / 1e9:.1f} GB free in {download_dir}; the largest file to copy "
                                     f"is {largest / 1e9:.1f} GB", download_dir))

    manifest_path = os.path.join(run_dir, "manifest_before.csv")
    write_manifest(manifest_path, manifest_before)
    print(f"\nWrote {len(manifest_before)} file(s) to {manifest_path}")

    print("\n=== Validation report ===")
    print_report(all_issues)

    if config["dry_run"]:
        print("\ndry_run is True - nothing was changed. Set CONFIG['dry_run'] = False to make the changes once "
              "the report above looks right.")
        return
    if any(i["level"] == "ERROR" for i in all_issues):
        print("\nNot applying changes: fix the ERROR-level issue(s) above first.")
        return

    if method == "reupload":
        run_reupload(syn, config, row_plans, manifest_before, run_dir)
        return

    migrated_origins = set()
    for plan in row_plans:
        if method == "migrate" and plan["needs_migration"]:
            if not migrate_row(syn, plan, run_dir, config["ticket_id"], config["confirm_migration"]):
                print(f"\nStopping: migration for {plan['origin_id']} did not finish cleanly. Nothing from this "
                      f"row was moved. See {run_dir} for details; re-running resumes the migration.")
                return
            migrated_origins.add(plan["origin_id"])
        move_items(syn, plan, run_dir, config["ticket_id"])
        if config["reset_local_sharing"] and plan["local_acl_ids"]:
            remove_local_sharing(syn, plan, run_dir, config["ticket_id"])
        if config["leave_redirect_link"] and plan["origin_is_container"]:
            leave_redirect_link(plan, run_dir, config["ticket_id"])

    print("\n=== Verification ===")
    manifest_after, verify_issues = verify(syn, row_plans, manifest_before, migrated_origins,
                                           config["reset_local_sharing"])
    write_manifest(os.path.join(run_dir, "manifest_after.csv"), manifest_after)
    print_report(verify_issues)
    print(f"\nDone. Run folder (manifests + audit log): {run_dir}")


if __name__ == "__main__":
    main(CONFIG)
