"""
Reusable helpers for secure ADKP data ingress: auditing a study's Synapse
folder tree and permissions, safely creating/renaming/moving/removing staging
folders, and uploading contributor files with checksum verification and
provenance.

Business rules come from:
  - SOP "ADTR 1-1.0 Data Ingress" (Confluence AD space, page 3380641793)
  - SOP "ADTR 1-2.0 Data Release" (Confluence AD space, page 2865233921) -
    notes AD Knowledge Portal uses a nested folder structure, and individual
    ADKP studies still vary (assay subfolders, versioned ingest folders).
    THIS SCRIPT MAKES NO ASSUMPTION ABOUT FOLDER ARRANGEMENT - it audits
    whatever tree actually exists under ROOT_SYN_ID and only changes what
    FOLDER_PLAN explicitly declares. Don't add logic elsewhere in this file
    that assumes a "Staging"/"Data" pair, a fixed depth, or any particular
    naming - confirm the real arrangement for a given ticket by running this
    script's audit mode first.
  - "Sage AWS Provisioner" (Confluence IT space, page 708083719) - AWS
    environments and STS credential requirements
  - "Configuration and Maintenance Standard" (Confluence SISP space, page
    2627895297) - Sage's InfoSec requirement for "dynamic encryption of
    connections" (TLS in transit) on any production/priority system
(all fetched 2026-09-17; re-check if conventions seem to have changed).

Setup:
    pip install -r requirements.txt   # synapseclient, and boto3 only if you
                                       # hit an external/STS storage location
                                       # (see check_storage_location() below)
    Authenticate once via `synapse config` (~/.synapseConfig) or by setting
    the SYNAPSE_AUTH_TOKEN env var to a personal access token - do not
    hardcode credentials in this file.

To use: edit the CONFIG dict below (the only thing you should need to
change), then run this file directly (`python folder_setup.py`). It
always prints a validation report (current tree + permission audit +
storage-location check + proposed folder/upload changes); it only writes to
Synapse if CONFIG["dry_run"] is False and there are no ERROR-level issues.

On security:
  - All Synapse API traffic goes over HTTPS by default; login_once() asserts
    this rather than assuming it, since Sage IT has had to fix at least one
    endpoint that allowed plain HTTP (SEC-514).
  - Synapse's own managed storage already lives on Sage-operated, encrypted
    S3 behind that HTTPS-only API - for the common case (default Synapse
    storage location) this script's normal upload path already satisfies
    "encrypted in transit + at rest" with no separate AWS setup needed.
  - Direct AWS/S3 access is only relevant if a folder is bound to an
    *external* STS-enabled storage location (see SYNPY-1049) rather than
    default Synapse storage. check_storage_location() detects this per
    folder; if found, CONFIG["external_storage_confirmed"] must be
    explicitly set True (after verifying with Sage IT that the bucket is
    provisioned in the 'scicomp' AWS environment, per the AWS Provisioner
    SOP - PHI/controlled data may never live in 'sandbox') before this
    script will upload to it.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

import synapseclient
from synapseclient import Folder, File, Activity

# ==============================================================================
# CONFIG - the only section you should need to edit. Everything below this is
# library code. Leave folder_plan / upload_plan empty to run in audit-only
# mode (prints the current tree + permission + storage-location report and
# changes nothing) - useful before the contributor has replied about what the
# folder structure should actually look like.
# ==============================================================================

CONFIG = {
    "dry_run": True,  # True: only print the validation report, change nothing.
                       # False: apply folder_plan/upload_plan if there are no ERROR-level issues.

    "ticket_id": "EXAMPLE-000",
    "ticket_url": "https://sagebionetworks.jira.com/browse/EXAMPLE-000",

    # Root of the subtree to audit/manage - any Synapse Project or Folder ID.
    # This script does not assume this is a "Staging" folder in any general
    # sense, or any particular depth/naming below it - it's just whatever
    # root you point it at.
    "root_syn_id": "syn00000000",

    "is_human_data": True,  # Set False only for non-human data - enforces the stricter private-folder check.

    # Set True only after confirming with Sage IT that an external storage
    # location this script is about to upload to is provisioned in the
    # 'scicomp' AWS environment with STS-only access. Leave False until
    # check_storage_location() actually reports one is in use - see the
    # module docstring's "On security" section.
    "external_storage_confirmed": False,

    # Declarative desired-state folder operations under root_syn_id. Each item is one of:
    #   {"action": "ensure",  "parent": "syn...", "name": "..."}                 - create if missing
    #   {"action": "rename",  "target": "syn...", "name": "..."}                 - rename in place
    #   {"action": "move",    "target": "syn...", "new_parent": "syn..."}        - reparent
    #   {"action": "remove",  "target": "syn..."}                                - delete, only if empty
    # Fill this in once the contributor responds about the folder structure they need.
    "folder_plan": [
        # {"action": "ensure", "parent": "syn00000000", "name": "example_folder_1"},
        # {"action": "ensure", "parent": "syn00000000", "name": "example_folder_2"},
    ],

    # Files to upload once target folders exist. Each item:
    #   {"target_folder_id": "syn...", "local_dir": "/path/to/files"}
    # or, to reference a folder created by folder_plan above by name:
    #   {"target_folder_name": "example_folder_1", "under": "syn00000000", "local_dir": "/path/to/files"}
    "upload_plan": [
        # {"target_folder_name": "example_folder_1", "under": "syn00000000", "local_dir": "/path/to/raw"},
    ],
}

# ==============================================================================
# SOP / SECURITY REFERENCE - condensed notes used in report messages below.
# ==============================================================================

# Synapse's reserved principal IDs (per Synapse REST API docs) - used to flag
# accidental public/broad exposure on what should be a private staging folder.
PUBLIC_PRINCIPAL_ID = 273949
AUTHENTICATED_USERS_PRINCIPAL_ID = 273948
_BROAD_PRINCIPAL_IDS = {PUBLIC_PRINCIPAL_ID, AUTHENTICATED_USERS_PRINCIPAL_ID}

# Access types that count as "can see/download the data" for the purposes of
# the private-staging-folder check below.
_EXPOSING_ACCESS_TYPES = {"READ", "DOWNLOAD"}

SOP_NOTES = {
    "staging_private": "Staging folders must always remain Private. ADKP's backend Project uses "
        "folder-level local sharing for the Staging folder regardless of whether the data is human "
        "or not - add the uploader's Synapse username directly to the Staging folder with 'edit' "
        "(or 'edit and delete') access. (ADTR 1-1.0 5.4.2)",
    "no_release_before_governance": "Never release (make public / apply Access Requirements) "
        "before the linked PCO governance ticket(s) are closed. Staging folders don't need "
        "ARs because they must always remain Private. (ADTR 1-1.0 5.3)",
    "remove_only_if_empty": "Never remove a folder that still has files or sub-folders in it - "
        "surface it as an issue for a human to resolve instead of silently deleting contents.",
    "aws_phi_environment": "PHI and other controlled-access data may only live in the 'scicomp' AWS "
        "environment, never 'sandbox'. Programmatic access must use short-lived STS credentials, "
        "never long-lived IAM keys, and MFA is required for any human AWS console/CLI access. "
        "(Sage AWS Provisioner)",
    "folder_arrangement_varies": "ADKP studies don't all share one folder layout (nested "
        "Staging/Data, assay subfolders, versioned vN_ingest folders). Don't assume this ticket's "
        "arrangement matches another one - always audit the actual current tree first. (ADTR 1-2.0 5)",
}

AUDIT_LOG_PATH = os.path.join(os.path.dirname(__file__), "ingress_audit_log.jsonl")


# ==============================================================================
# Setup - runs exactly once per invocation, from main() at the bottom.
# ==============================================================================

def login_once(auth_token=None):
    """
    Log in to Synapse exactly once for this run. Falls back to ~/.synapseConfig
    or the SYNAPSE_AUTH_TOKEN env var if auth_token is None. Asserts the
    connection is HTTPS rather than assuming it - see module docstring.
    """
    syn = synapseclient.Synapse()
    syn.login(authToken=auth_token)
    if not syn.repoEndpoint.startswith("https://"):
        raise RuntimeError(
            f"Refusing to proceed: Synapse repoEndpoint is not HTTPS ({syn.repoEndpoint!r}). "
            "Human data must only be transferred over an encrypted connection."
        )
    return syn


def _log_action(ticket_id, action, **fields):
    """Append one JSON line to the local audit log. Never pass file contents or PHI fields in here."""
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "ticket": ticket_id,
              "action": action, **fields}
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


# ==============================================================================
# Tree + permission audit
# ==============================================================================

def get_subtree(syn, root_id):
    """
    Recursively walk a Synapse container and return a tree dict:
    {"id", "name", "type", "children": [...], "file_count"}. Makes no
    assumption about depth or naming - just reports what's actually there.
    """
    root = syn.get(root_id, downloadFile=False)
    node = {"id": root_id, "name": root.name, "type": type(root).__name__, "children": [], "file_count": 0}
    for child in syn.getChildren(root_id):
        if child["type"] == "org.sagebionetworks.repo.model.Folder":
            node["children"].append(get_subtree(syn, child["id"]))
        else:
            node["file_count"] += 1
    return node


def print_tree(node, indent=0):
    label = f"{node['name']} ({node['id']})"
    if node["file_count"]:
        label += f" - {node['file_count']} file(s)"
    print("  " * indent + label)
    for child in node["children"]:
        print_tree(child, indent + 1)


def describe_arrangement(node):
    """
    Best-effort, informational-only description of the observed tree shape -
    NOT used to drive any branching logic elsewhere in this file. Just a
    sanity-check aid for the human reviewing the report, per
    SOP_NOTES['folder_arrangement_varies'].
    """
    depths = []

    def _walk(n, depth):
        depths.append(depth)
        for child in n["children"]:
            _walk(child, depth + 1)

    _walk(node, 0)
    max_depth = max(depths)
    child_names = [c["name"] for c in node["children"]]
    looks_versioned = all(n.lower().startswith("v") and any(ch.isdigit() for ch in n) for n in child_names) \
        if child_names else False

    if not node["children"] and node["file_count"] == 0:
        return "Empty container - no existing structure to infer from."
    if looks_versioned:
        return f"Top-level children look like versioned release folders ({child_names}) rather than a Staging/Data pair."
    if max_depth <= 1:
        return "Flat/shallow structure (children hold files directly, little or no nesting)."
    return f"Nested structure, {max_depth} level(s) deep under the root."


def _is_empty(node):
    return node["file_count"] == 0 and not node["children"]


def _find_by_id(node, entity_id):
    if node["id"] == entity_id:
        return node
    for child in node["children"]:
        found = _find_by_id(child, entity_id)
        if found:
            return found
    return None


def get_acl(syn, entity_id):
    """
    Return (benefactor_id, acl) for an entity - the benefactor is the entity that
    actually owns the ACL in effect here (itself, if it has a local ACL, otherwise
    the nearest ancestor that does). This is how Synapse resolves ACL inheritance.
    """
    benefactor_id = syn.restGET(f"/entity/{entity_id}/benefactor")["id"]
    acl = syn.restGET(f"/entity/{benefactor_id}/acl")
    return benefactor_id, acl


def audit_permissions(syn, root_id, expect_private=True):
    """
    Walk the subtree under root_id and flag any folder whose effective ACL grants
    READ/DOWNLOAD to PUBLIC or AUTHENTICATED_USERS - i.e. any folder that isn't
    actually private, when expect_private is True. Returns a list of issue dicts
    (column/level/value/message, matching the other tool's report format).
    """
    issues = []

    def _walk(entity_id, name):
        benefactor_id, acl = get_acl(syn, entity_id)
        for ra in acl.get("resourceAccess", []):
            if ra["principalId"] in _BROAD_PRINCIPAL_IDS and _EXPOSING_ACCESS_TYPES & set(ra["accessType"]):
                level = "ERROR" if expect_private else "WARNING"
                principal = "PUBLIC" if ra["principalId"] == PUBLIC_PRINCIPAL_ID else "AUTHENTICATED_USERS"
                issues.append({
                    "column": name, "level": level, "value": entity_id,
                    "message": f"{principal} has {sorted(_EXPOSING_ACCESS_TYPES & set(ra['accessType']))} "
                               f"via benefactor {benefactor_id} - {SOP_NOTES['staging_private']}",
                })
        for child in syn.getChildren(entity_id):
            if child["type"] == "org.sagebionetworks.repo.model.Folder":
                _walk(child["id"], child["name"])

    _walk(root_id, root_id)
    return issues


# ==============================================================================
# Storage location check (default Synapse storage vs. external/STS S3)
# ==============================================================================

def check_storage_location(syn, folder_id):
    """
    Ask Synapse what storage backs uploads to this folder. Returns a dict
    describing whether it's Synapse's own default storage (S3 behind the
    HTTPS API, already satisfying "encrypted in transit + at rest") or an
    external/STS-enabled storage location that this script would need
    explicit sign-off to use (see CONFIG["external_storage_confirmed"] and
    the module docstring's "On security" section).
    """
    dest = syn.restGET(f"/entity/{folder_id}/uploadDestination", endpoint=syn.fileHandleEndpoint)
    concrete_type = dest.get("concreteType", "")
    sts_enabled = bool(dest.get("stsEnabled", False))
    is_default_synapse_storage = concrete_type.endswith("S3UploadDestination") and not sts_enabled
    return {
        "folder_id": folder_id,
        "concrete_type": concrete_type,
        "sts_enabled": sts_enabled,
        "is_default_synapse_storage": is_default_synapse_storage,
    }


def audit_storage_locations(syn, folder_ids, external_storage_confirmed):
    """Run check_storage_location() over a set of folders and turn the results into report issues."""
    issues = []
    for folder_id in folder_ids:
        info = check_storage_location(syn, folder_id)
        if not info["is_default_synapse_storage"] and not external_storage_confirmed:
            issues.append({
                "column": folder_id, "level": "ERROR", "value": info["concrete_type"],
                "message": f"{folder_id} uses non-default storage (sts_enabled={info['sts_enabled']}) - "
                           f"confirm with Sage IT this is provisioned in 'scicomp' with STS-only access "
                           f"({SOP_NOTES['aws_phi_environment']}), then set "
                           f"CONFIG['external_storage_confirmed'] = True to proceed.",
            })
    return issues


# ==============================================================================
# Folder plan (create/rename/move/remove)
# ==============================================================================

def plan_folder_changes(tree, folder_plan):
    """
    Resolve folder_plan against the current tree. Returns (ops, issues):
    ops is the list of concrete actions to apply (in order), issues is a list of
    ERROR/WARNING dicts (e.g. "remove" targeting a non-empty folder).
    """
    ops = []
    issues = []
    name_index = {}  # (parent_id, name) -> node, populated as we go so "ensure" is idempotent

    def index_children(node):
        for child in node["children"]:
            name_index[(node["id"], child["name"])] = child
            index_children(child)

    index_children(tree)

    for step in folder_plan:
        action = step["action"]
        if action == "ensure":
            key = (step["parent"], step["name"])
            if key in name_index:
                continue  # already exists - nothing to do
            ops.append(step)
        elif action == "rename":
            target = _find_by_id(tree, step["target"])
            if target is None:
                issues.append({"column": "folder_plan", "level": "ERROR", "value": step,
                                "message": f"rename target {step['target']} not found under root"})
                continue
            ops.append(step)
        elif action == "move":
            target = _find_by_id(tree, step["target"])
            if target is None:
                issues.append({"column": "folder_plan", "level": "ERROR", "value": step,
                                "message": f"move target {step['target']} not found under root"})
                continue
            ops.append(step)
        elif action == "remove":
            target = _find_by_id(tree, step["target"])
            if target is None:
                issues.append({"column": "folder_plan", "level": "ERROR", "value": step,
                                "message": f"remove target {step['target']} not found under root"})
                continue
            if not _is_empty(target):
                issues.append({"column": "folder_plan", "level": "ERROR", "value": step,
                                "message": f"{step['target']} is not empty - {SOP_NOTES['remove_only_if_empty']}"})
                continue
            ops.append(step)
        else:
            issues.append({"column": "folder_plan", "level": "ERROR", "value": step,
                            "message": f"unknown action {action!r}"})

    return ops, issues


def apply_folder_changes(syn, ops, ticket_id):
    """Execute the resolved folder operations from plan_folder_changes(), in order."""
    created_by_name = {}
    for step in ops:
        action = step["action"]
        if action == "ensure":
            folder = syn.store(Folder(step["name"], parent=step["parent"]))
            created_by_name[(step["parent"], step["name"])] = folder.id
            _log_action(ticket_id, "folder_ensure", entity_id=folder.id, parent=step["parent"], name=step["name"])
        elif action == "rename":
            entity = syn.get(step["target"], downloadFile=False)
            entity.name = step["name"]
            syn.store(entity)
            _log_action(ticket_id, "folder_rename", entity_id=step["target"], new_name=step["name"])
        elif action == "move":
            entity = syn.get(step["target"], downloadFile=False)
            entity.parentId = step["new_parent"]
            syn.store(entity)
            _log_action(ticket_id, "folder_move", entity_id=step["target"], new_parent=step["new_parent"])
        elif action == "remove":
            syn.delete(step["target"])
            _log_action(ticket_id, "folder_remove", entity_id=step["target"])
    return created_by_name


def resolve_upload_targets(upload_plan, created_by_name, tree):
    """
    Resolve each upload_plan entry to a concrete target_folder_id, using either an
    explicit target_folder_id or a (target_folder_name, under) pair matched against
    folders just created by apply_folder_changes() or already present in tree.
    """
    resolved = []
    issues = []
    for entry in upload_plan:
        if "target_folder_id" in entry:
            resolved.append({**entry, "target_folder_id": entry["target_folder_id"]})
            continue
        key = (entry["under"], entry["target_folder_name"])
        folder_id = created_by_name.get(key)
        if folder_id is None:
            existing = _find_by_id(tree, entry["under"])
            match = next((c for c in (existing["children"] if existing else [])
                          if c["name"] == entry["target_folder_name"]), None)
            folder_id = match["id"] if match else None
        if folder_id is None:
            issues.append({"column": "upload_plan", "level": "ERROR", "value": entry,
                            "message": f"could not resolve folder {entry['target_folder_name']!r} "
                                       f"under {entry['under']} - check folder_plan created it"})
            continue
        resolved.append({**entry, "target_folder_id": folder_id})
    return resolved, issues


# ==============================================================================
# Upload
# ==============================================================================

def compute_md5(path, chunk_size=8 * 1024 * 1024):
    digest = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_files(syn, target_folder_id, local_dir, ticket_id, ticket_url):
    """
    Upload every file in local_dir (recursively) to target_folder_id via Synapse's
    normal storage path (synapseclient handles multipart upload for large files,
    and transparently uses STS/external storage under the hood when the folder is
    configured for it - see check_storage_location() for the pre-flight gate on
    that). Skips files whose name and MD5 already match an existing file in that
    folder (idempotent re-run). Verifies the checksum Synapse reports back matches
    the local one, and attaches provenance (Activity) referencing the ticket.
    Logs each action.
    """
    existing_by_name = {}
    for child in syn.getChildren(target_folder_id):
        if child["type"] == "org.sagebionetworks.repo.model.FileEntity":
            existing_by_name[child["name"]] = child["id"]

    results = []
    for dirpath, _, filenames in os.walk(local_dir):
        for filename in filenames:
            local_path = os.path.join(dirpath, filename)
            local_md5 = compute_md5(local_path)

            existing_id = existing_by_name.get(filename)
            if existing_id is not None:
                existing = syn.get(existing_id, downloadFile=False)
                if getattr(existing, "md5", None) == local_md5:
                    results.append({"file": filename, "status": "skipped_unchanged", "entity_id": existing_id})
                    continue

            activity = Activity(
                name=f"Data ingress for {ticket_id}",
                description=f"Uploaded per data contribution ticket {ticket_url}",
            )
            entity = syn.store(File(local_path, parent=target_folder_id), activity=activity)

            if entity.md5 != local_md5:
                raise RuntimeError(
                    f"Checksum mismatch after upload for {filename}: local={local_md5} synapse={entity.md5}"
                )

            _log_action(ticket_id, "file_upload", entity_id=entity.id, filename=filename, md5=local_md5,
                        target_folder_id=target_folder_id)
            results.append({"file": filename, "status": "uploaded", "entity_id": entity.id})

    return results


def print_report(issues):
    if not issues:
        print("No issues found.")
        return
    for issue in issues:
        print(f"{issue['level']}: [{issue['column']}] {issue['message']} (value: {issue['value']!r})")


# ==============================================================================
# Entry point - one pass: log in once, audit once, then act.
# ==============================================================================

def main(config):
    syn = login_once()

    print(f"=== Current tree under {config['root_syn_id']} ===")
    tree = get_subtree(syn, config["root_syn_id"])
    print_tree(tree)
    print(f"\nObserved arrangement: {describe_arrangement(tree)}")
    print(f"({SOP_NOTES['folder_arrangement_varies']})")

    print(f"\n=== Permission audit (expect_private={config['is_human_data']}) ===")
    perm_issues = audit_permissions(syn, config["root_syn_id"], expect_private=True)

    print(f"\n=== Folder plan ({len(config['folder_plan'])} step(s) configured) ===")
    folder_ops, folder_issues = plan_folder_changes(tree, config["folder_plan"])
    for step in folder_ops:
        print(f"  planned: {step}")

    # Storage-location check covers the root plus any folder already named as an
    # explicit upload target (folders that don't exist yet get checked after
    # folder_plan is applied, right before uploading to them).
    existing_upload_targets = [e["target_folder_id"] for e in config["upload_plan"] if "target_folder_id" in e]
    storage_issues = audit_storage_locations(
        syn, [config["root_syn_id"], *existing_upload_targets], config["external_storage_confirmed"]
    )

    all_issues = perm_issues + folder_issues + storage_issues
    print("\n=== Validation report ===")
    print_report(all_issues)

    has_errors = any(i["level"] == "ERROR" for i in all_issues)

    if config["dry_run"]:
        print("\ndry_run is True - nothing was changed. Set CONFIG['dry_run'] = False to apply "
              "folder_plan/upload_plan once the report above looks right.")
        return

    if has_errors:
        print("\nNot applying changes: fix the ERROR-level issue(s) above first.")
        return

    created_by_name = apply_folder_changes(syn, folder_ops, config["ticket_id"])
    print(f"\nApplied {len(folder_ops)} folder change(s).")

    # Re-fetch the tree so upload target resolution sees folders just created.
    tree = get_subtree(syn, config["root_syn_id"])
    resolved_uploads, upload_issues = resolve_upload_targets(config["upload_plan"], created_by_name, tree)
    if upload_issues:
        print("\n=== Upload target resolution issues ===")
        print_report(upload_issues)

    # Re-check storage location for any newly-created folders before uploading to them.
    new_targets = [e["target_folder_id"] for e in resolved_uploads
                   if e["target_folder_id"] not in existing_upload_targets]
    new_storage_issues = audit_storage_locations(syn, new_targets, config["external_storage_confirmed"])
    if new_storage_issues:
        print("\n=== Storage-location issues on newly created folders ===")
        print_report(new_storage_issues)
        print("\nNot uploading: resolve the storage-location issue(s) above first.")
        return

    for entry in resolved_uploads:
        print(f"\nUploading from {entry['local_dir']} to {entry['target_folder_id']}...")
        results = upload_files(syn, entry["target_folder_id"], entry["local_dir"],
                                config["ticket_id"], config["ticket_url"])
        for r in results:
            print(f"  {r['status']}: {r['file']} ({r['entity_id']})")

    print(f"\nDone. Audit log: {AUDIT_LOG_PATH}")


if __name__ == "__main__":
    main(CONFIG)
