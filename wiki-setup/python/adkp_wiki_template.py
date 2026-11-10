"""
Reusable template for creating and updating Synapse Wiki pages (root pages and
sub-pages) for the ADKP backend, while preserving contributor-submitted HTML
formatting (links, <sup>, <sub>, etc.) exactly as written.

Based on the synapseclient `WikiPage` model (synapseclient.models.wiki), per
the official tutorial (https://python-docs.synapse.org/en/stable/tutorials/python/wiki/,
fetched 2026-09-21) and the installed synapseclient==4.11.0 source
(site-packages/synapseclient/models/wiki.py, read 2026-09-21).

HOW FORMATTING IS PRESERVED
    `WikiPage.markdown` accepts either an inline string or a path to a file on
    disk. When given a file path, the client gzips the file's raw bytes and
    uploads them as-is - there is no markdown escaping, HTML escaping, or
    text-mode re-encoding in between. This template always passes a FILE PATH
    (never an inline string) for exactly that reason: it's the only mode that
    guarantees byte-for-byte preservation of the contributor's original HTML
    (their <a href>, <sup>, <sub>, etc. tags survive untouched).

    Do not run the contributor's content through html.escape() or ad-hoc
    string manipulation before saving it to the content file - edit the file
    directly if changes are needed. The one deliberate exception is
    html_to_synapse_markdown(), used by build_study_wiki() (see STUDY WIKIS).

    Synapse's wiki renderer does apply its own server-side HTML sanitizer
    when the page is rendered (this is server behavior we can't inspect or
    reproduce from the client). Standard inline tags like <a>, <sup>, <sub>,
    <b>, <i>, <u>, <span> are expected to pass through and render correctly,
    but always spot-check the rendered page in the Synapse UI after the
    first upload of a new content pattern. verify_wiki_markdown() below only
    confirms the client didn't corrupt anything in transit/storage - it
    can't confirm how the renderer will display it.

STUDY WIKIS (study description + methods)
    build_study_wiki() assembles a study's wiki page in a fixed layout:

        # <study name>                 <- always first; asked for if not given
        ## Study Description:
        <description>
        ## Methods:
        <methods>

    The study name is usually the studies table's Study_Name followed by the
    abbreviation in parentheses, e.g. "The EXAMPLE_STUDY Study (EXAMPLE_STUDY)".
    Type it with normal underscores: they're written as &lowbar; for you,
    because the portal's study page otherwise reads them as italics.

    Contributor text pasted as raw HTML (<p>, <h3>, ...) renders with very
    large gaps between paragraphs on Synapse, and other study wikis are
    written in Synapse markdown. So build_study_wiki() converts HTML input
    with html_to_synapse_markdown(): paragraphs become blank-line-separated
    text, <h1>-<h6> become # headings, and links, bold, italics, <sup>/<sub>
    and bullet lists are kept (as markdown). Figures/images stay as raw HTML.
    Plain-text or markdown input (.md/.txt) is used as-is.

    Contributors often start their text with their own labels (e.g. a first
    paragraph "Study Description (<700 char): ..." or "Methods: METHODS").
    Remove those from the description/methods files before building, since
    the headings are added for you.

Setup:
    pip install -r requirements.txt
    Authenticate once via `synapse config` (~/.synapseConfig) or by setting the
    SYNAPSE_AUTH_TOKEN env var to a personal access token - do not hardcode
    credentials in this file.

Use as a library:
    from adkp_wiki_template import login, create_or_update_wiki

    syn = login()

    # Preview first: opens the content file in your browser and prints the
    # create/update plan, writes nothing to Synapse.
    create_or_update_wiki(
        syn,
        owner_id="syn00000000",
        title="Acknowledgement - EXAMPLE",
        content_path="acknowledgement_example.html",
        parent_id="00000000",  # only used when creating a brand-new page
        dry_run=True,
    )

    # Looks right -> push for real.
    create_or_update_wiki(
        syn, owner_id="syn00000000", title="Acknowledgement - EXAMPLE",
        content_path="acknowledgement_example.html", parent_id="00000000",
    )

Or from the command line:
    python adkp_wiki_template.py syn00000000 "Acknowledgement - EXAMPLE" \\
        acknowledgement_example.html --parent-id 00000000 --dry-run

    Study wiki (study name on top, Study Description + Methods as h2). The
    built markdown is written to content_path, then uploaded. Leave out
    --study-name to be asked for it. Use --wiki-id to update a page that has
    no title (e.g. a folder's existing root page):
    python adkp_wiki_template.py syn00000000 "" study_wiki_example.md \\
        --description description.html --methods methods.html \\
        --study-name "The EXAMPLE_STUDY Study (EXAMPLE_STUDY)" \\
        --wiki-id 000000 --dry-run

GETTING A CONTENT FILE FROM A CONTRIBUTOR'S JIRA TICKET
    Contributors often submit this text as a Jira ticket field rather than a
    ready-made HTML file. Three ways to turn that into a content file, in
    order of preference:

    1. Jira REST API (best fidelity, no copy-paste) - see
       fetch_jira_rendered_fields() below. Pulls the field's server-rendered
       HTML directly - the same markup Jira itself displays - straight to a
       file. Needs a Jira API token.

    2. Browser DevTools copy (best fidelity without an API token) - open the
       ticket, right-click the rendered field -> Inspect -> in the Elements
       panel right-click the containing element -> Copy -> Copy outerHTML.
       Paste that into a .html file. Captures exactly what Jira rendered.

    3. Word or Google Docs round-trip (fallback - needs cleanup) - paste the
       rendered ticket text into Word or Google Docs, then:
         - Word: File -> Save As -> Web Page (.htm)
         - Google Docs: File -> Download -> Web Page (.html, zipped)
       Both convert superscript/subscript/links to real HTML tags, but both
       also inject wrapper markup (Word: `mso-*` styles and XML namespaces;
       Docs: inline `<span style="...">` clutter) that should be stripped
       before use, and Word's autocorrect can silently swap characters
       (straight quotes -> curly, hyphens -> em dashes) including inside
       URLs. Diff the result against the original ticket text before trusting
       it, and prefer option 1 or 2 whenever either is available.

    Whichever method produces the file, run create_or_update_wiki() with
    verify=True (the default) and then spot-check the rendered page in the
    Synapse UI - see the HOW FORMATTING IS PRESERVED note above for why that
    manual check still matters even after verification passes.
"""

import argparse
import hashlib
import html
import os
import re
import tempfile
from html.parser import HTMLParser

import synapseclient
from synapseclient.core.exceptions import SynapseHTTPError
from synapseclient.models import WikiPage


def login(auth_token=None):
    """Log in to Synapse. Falls back to ~/.synapseConfig / SYNAPSE_AUTH_TOKEN if auth_token is None."""
    syn = synapseclient.Synapse()
    syn.login(authToken=auth_token)
    return syn


def find_existing_wiki(syn, owner_id, title):
    """
    Look up an existing wiki page (root or sub-page) on `owner_id` by exact
    title match, searching the whole wiki tree for that owner. Returns the
    matching WikiPage (with .id populated), or None if no wiki page with that
    title exists yet (including the case where the owner has no wiki at all).
    """
    try:
        return WikiPage(owner_id=owner_id, title=title).get(synapse_client=syn)
    except ValueError:
        # WikiPage.get() raises ValueError when the header tree has no page
        # matching this title (but the owner has at least one wiki page).
        return None
    except SynapseHTTPError as e:
        # Raised when the owner has no wiki page at all yet (404 on the
        # underlying header-tree lookup).
        if getattr(e, "response", None) is not None and e.response.status_code == 404:
            return None
        raise


def heading_html(text, level=2, bold=True):
    """
    Build a simple HTML heading snippet for a section title within wiki
    content - e.g. to turn a plain-text field label like "Disease focus."
    into a bold, larger sub-heading before saving it to a content file.

    `level` is an HTML heading level, 1 (largest) through 6 (smallest).
    `bold` additionally wraps the text in <strong>, so it stays bold even if
    a wiki's stylesheet ever overrides heading weight.

    This returns a snippet, not a full page - insert it into the content
    file where you want the heading to appear (e.g. via find/replace on the
    plain-text label, or by prepending it before you write the file).
    """
    if not 1 <= level <= 6:
        raise ValueError("level must be between 1 and 6")
    inner = f"<strong>{text}</strong>" if bold else text
    return f"<h{level}>{inner}</h{level}>"


def _escape_underscores(text):
    """
    Write underscores as &lowbar; so they show as underscores everywhere.

    Synapse's own wiki view displays a plain "_" fine, but the AD Knowledge
    Portal's study page (which shows the same wiki) reads pairs of
    underscores as italics, so a name like "The A_B_C Study" comes out as
    "The A" + italic "B" + "C Study". &lowbar; displays as "_" in both.
    Only use this on display text, never on URLs.
    """
    return text.replace("_", "&lowbar;")


class _HtmlToSynapseMarkdown(HTMLParser):
    """HTML -> Synapse markdown. See html_to_synapse_markdown()."""

    _INLINE = {"strong": "**", "b": "**", "em": "*", "i": "*", "sup": "^", "sub": "~"}
    # characters that would otherwise be read as markdown formatting
    # (underscores are handled separately - see _escape_underscores())
    _ESCAPE = re.compile(r"([\\`*\[\]^~])")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []        # finished blocks of markdown
        self.buf = []           # current block's pieces
        self.links = []         # stack of hrefs for open <a> tags
        self.list_depth = 0
        self.raw_depth = 0      # >0 while inside <figure> (kept as raw HTML)
        self.raw = []

    def _flush(self):
        raw = "".join(self.buf)
        indent = re.match(r" *", raw).group(0) if raw.lstrip().startswith("* ") else ""
        text = indent + re.sub(r"[ \t\r\n]+", " ", raw).strip()
        if text:
            self.blocks.append(text)
        self.buf = []

    def handle_starttag(self, tag, attrs):
        if self.raw_depth or tag == "figure":
            if tag == "figure":
                self._flush()
                self.raw_depth += 1
            self.raw.append(self.get_starttag_text())
            return
        if tag == "img":  # stray image outside a <figure>: keep as raw HTML
            self._flush()
            self.blocks.append(self.get_starttag_text())
        elif tag in ("p", "div", "br"):
            # a <p> right inside an <li> belongs on the bullet's line
            if not ("".join(self.buf).strip() == "*" and tag != "br"):
                self._flush()
        elif re.fullmatch(r"h[1-6]", tag):
            self._flush()
            self.buf.append("#" * int(tag[1]) + " ")
        elif tag in ("ul", "ol"):
            self._flush()
            self.list_depth += 1
        elif tag == "li":
            self._flush()
            self.buf.append("  " * (self.list_depth - 1) + "* ")
        elif tag == "a":
            self.links.append(dict(attrs).get("href", ""))
            self.buf.append("[")
        elif tag in self._INLINE:
            self.buf.append(self._INLINE[tag])

    def handle_endtag(self, tag):
        if self.raw_depth:
            self.raw.append(f"</{tag}>")
            if tag == "figure":
                self.raw_depth -= 1
                if not self.raw_depth:
                    self.blocks.append("".join(self.raw))
                    self.raw = []
            return
        if tag in ("p", "div", "li") or re.fullmatch(r"h[1-6]", tag):
            # a <p> inside an <li> must not end the list item's line early
            if not (tag == "p" and self.buf and self.buf[0].lstrip().startswith("* ")):
                self._flush()
        elif tag in ("ul", "ol"):
            self._flush()
            self.list_depth = max(0, self.list_depth - 1)
        elif tag == "a":
            self.buf.append(f"]({self.links.pop() if self.links else ''})")
        elif tag in self._INLINE:
            self.buf.append(self._INLINE[tag])

    def handle_data(self, data):
        if self.raw_depth:
            self.raw.append(html.escape(data, quote=False))
        else:
            data = html.escape(data, quote=False)
            self.buf.append(_escape_underscores(self._ESCAPE.sub(r"\\\1", data)))

    def result(self):
        self._flush()
        out, prev_list = [], False
        for block in self.blocks:
            is_list = block.lstrip().startswith("* ")
            # list items sit on consecutive lines; everything else gets a blank line
            out.append(("\n" if (is_list and prev_list) else "\n\n") + block)
            prev_list = is_list
        return "".join(out).strip() + "\n"


def html_to_synapse_markdown(html_text):
    """
    Convert contributor HTML (e.g. text copied from a Jira ticket) to Synapse
    wiki markdown, so it renders with normal paragraph spacing.

    Kept: paragraphs (blank line between), <h1>-<h6> as # headings, links as
    [text](url), <strong>/<b> as **bold**, <em>/<i> as *italic*, <sup> as
    ^sup^, <sub> as ~sub~, bullet lists as "* item". <figure> blocks and
    <img> tags are kept as raw HTML. Literal *, _, [, ], ^, ~ in the text
    are escaped so they don't turn into formatting. Other tags are dropped
    but their text is kept.
    """
    parser = _HtmlToSynapseMarkdown()
    parser.feed(html_text)
    parser.close()
    return parser.result()


def ask_study_name(study_name=None):
    """Return study_name, or ask for it on the command line until one is given."""
    while not (study_name and study_name.strip()):
        study_name = input("Study name for the top of the wiki "
                           "(e.g. 'The EXAMPLE_STUDY Study (EXAMPLE_STUDY)'): ")
    return study_name.strip()


def _read_section(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if path.lower().endswith((".md", ".txt")):
        return text.strip()
    if path.lower().endswith((".html", ".htm")) or re.search(r"<(p|h[1-6]|a|div)[\s>]", text):
        return html_to_synapse_markdown(text).strip()
    return text.strip()


def build_study_wiki(out_path, description_path, methods_path, study_name=None):
    """
    Write a study wiki page to `out_path` in the standard layout:

        # <study name>
        ## Study Description:
        <description>
        ## Methods:
        <methods>

    `study_name` is always the first line; if it's not given, you're asked
    for it. `description_path` / `methods_path` are files holding the
    contributor's text - HTML is converted with html_to_synapse_markdown(),
    markdown/plain text is used as-is. Returns out_path, ready to pass to
    create_or_update_wiki() as content_path.
    """
    study_name = _escape_underscores(ask_study_name(study_name))
    page = (
        f"# {study_name}\n\n"
        f"## Study Description:\n\n{_read_section(description_path)}\n\n"
        f"## Methods:\n\n{_read_section(methods_path)}\n"
    )
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        f.write(page)
    return out_path


def preview_content(content_path):
    """
    Open a content file directly in the default web browser, so you can
    visually check bold/heading/superscript/subscript/link formatting before
    it's pushed to Synapse. This renders the raw HTML as a browser sees it,
    which is a useful sanity check but won't exactly match Synapse's own
    wiki theme/CSS - it confirms the tags are structured the way you intend,
    not the final on-page appearance.
    """
    import webbrowser

    path = os.path.abspath(content_path)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Content file not found: {content_path}")
    webbrowser.open(f"file://{path}")
    return path


def create_or_update_wiki(syn, owner_id, title, content_path, parent_id=None, verify=True, dry_run=False,
                          wiki_id=None):
    """
    Create a new wiki page, or update it in place if a page with this exact
    title already exists on this owner. `content_path` must be a path to a
    file on disk (not an inline string) so its contents are uploaded
    byte-for-byte - see the module docstring.

    `parent_id` only matters for a brand-new page: leave it None for the
    owner's root wiki page, or set it to an existing page's id to create a
    new sub-page nested under it. It's ignored (the page's real parent is
    preserved) when updating an existing page.

    `wiki_id` updates that exact page instead of looking one up by title.
    Use it for pages with no title (e.g. a folder's existing root page),
    which the title lookup can't find - without it, the script would try to
    create a second root page and fail.

    `dry_run=True` prints the create-vs-update plan, opens the content file
    in your browser for a visual check (see preview_content()), and returns
    None WITHOUT writing anything to Synapse. Run it this way first; rerun
    with dry_run=False once the preview looks right.

    Returns the stored WikiPage (or None if dry_run). Raises
    FileNotFoundError if content_path doesn't exist.
    """
    if not os.path.isfile(content_path):
        raise FileNotFoundError(f"Content file not found: {content_path}")

    if wiki_id:
        existing = WikiPage(owner_id=owner_id, id=str(wiki_id)).get(synapse_client=syn)
    else:
        existing = find_existing_wiki(syn, owner_id, title)

    if dry_run:
        if existing:
            print(f"[DRY RUN] Would UPDATE wiki page {existing.id!r} ('{title}') on {owner_id}.")
        elif parent_id:
            print(f"[DRY RUN] Would CREATE new sub-page ('{title}') under {parent_id} on {owner_id}.")
        else:
            print(f"[DRY RUN] Would CREATE ('{title}') as the root wiki page of {owner_id}.")
        print(f"Content file: {os.path.abspath(content_path)}")
        preview_content(content_path)
        print("Nothing was written to Synapse. Rerun with dry_run=False to push this.")
        return None

    if existing:
        wiki = WikiPage(owner_id=owner_id, id=existing.id, title=title, markdown=content_path)
        action = "Updated"
    else:
        wiki = WikiPage(owner_id=owner_id, title=title, markdown=content_path, parent_id=parent_id)
        action = "Created"

    stored = wiki.store(synapse_client=syn)
    print(f"{action} wiki page {stored.id!r} ('{title}') on {owner_id}.")
    print(f"View at: https://www.synapse.org/Synapse:{owner_id}/wiki/{stored.id}")

    if verify:
        verify_wiki_markdown(syn, owner_id, stored.id, content_path)

    return stored


def verify_wiki_markdown(syn, owner_id, wiki_id, local_content_path):
    """
    Re-download the markdown just stored on `wiki_id` and compare it
    byte-for-byte against the local content file, to confirm the client
    didn't alter anything in transit (encoding issues, truncation, etc.).
    This does NOT tell you how Synapse's renderer will display the HTML -
    always spot-check that separately in the Synapse UI.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        downloaded_path = WikiPage(owner_id=owner_id, id=wiki_id).get_markdown_file(
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


def fetch_jira_rendered_fields(issue_key, field_ids, base_url="https://sagebionetworks.jira.com",
                                email=None, api_token=None):
    """
    Fetch one or more fields from a Jira issue as server-rendered HTML (the
    same HTML the Jira UI displays), via the REST API's `renderedFields`
    expansion. This is the highest-fidelity way to pull a contributor's
    ticket text into a wiki content file - it goes straight from Jira's own
    renderer to a file, with no clipboard/copy-paste step in between.

    Needs a Jira API token: https://id.atlassian.com/manage-profile/security/api-tokens
    Falls back to the JIRA_EMAIL / JIRA_API_TOKEN env vars if email/api_token
    aren't given - do not hardcode credentials in this file.

    Arguments:
        issue_key: e.g. "PROJ-123"
        field_ids: Jira field ids to fetch, e.g. ["customfield_10050"]. To find
            the field id for a given field label on your Jira instance, fetch
            any issue with expand="names" (or check your project's field
            configuration) and read off its `names` mapping - field ids are
            per-Jira-instance/project, not per-ticket, so look them up once
            and reuse them.
        base_url: the Jira Cloud site
        email, api_token: Jira Cloud API auth (HTTP Basic: email + token)

    Returns:
        dict of {field_id: rendered_html_string_or_None}. A None value means
        that field exists on the ticket but is empty/unpopulated.
    """
    import requests

    email = email or os.environ.get("JIRA_EMAIL")
    api_token = api_token or os.environ.get("JIRA_API_TOKEN")
    if not email or not api_token:
        raise ValueError(
            "Need a Jira email + API token - pass them in, or set the JIRA_EMAIL / "
            "JIRA_API_TOKEN env vars. Create a token at "
            "https://id.atlassian.com/manage-profile/security/api-tokens"
        )

    resp = requests.get(
        f"{base_url}/rest/api/3/issue/{issue_key}",
        params={"fields": ",".join(field_ids), "expand": "renderedFields"},
        auth=(email, api_token),
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    rendered = resp.json().get("renderedFields", {})
    return {field_id: rendered.get(field_id) for field_id in field_ids}


def save_jira_field(html, path):
    """
    Write a rendered-HTML field value (from fetch_jira_rendered_fields) to a
    content file exactly as returned - no reformatting. Raises ValueError if
    the field came back empty, so a blank ticket field can't silently produce
    a blank wiki page.
    """
    if not html:
        raise ValueError("No rendered content for this field - check it's actually populated on the ticket.")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(html)
    return path


def _parse_args():
    parser = argparse.ArgumentParser(
        description="Create or update a Synapse wiki page (root or sub-page), "
                     "preserving contributor HTML formatting exactly."
    )
    parser.add_argument("owner_id", help="Synapse entity ID that owns the wiki (e.g. syn12345)")
    parser.add_argument("title", help="Wiki page title. Also used to find an existing page to update - "
                                       "keep titles unique/descriptive within an owner's wiki tree.")
    parser.add_argument("content_path", help="Path to the contributor's HTML/markdown content file")
    parser.add_argument("--parent-id", default=None,
                         help="Existing wiki page id to nest a NEW page under (ignored when updating "
                              "an existing page). Omit to create/update the owner's root wiki page.")
    parser.add_argument("--no-verify", action="store_true",
                         help="Skip re-downloading and byte-comparing the stored content after upload.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Preview only: print the create/update plan and open the content file in "
                              "your browser, but write nothing to Synapse.")
    parser.add_argument("--wiki-id", default=None,
                         help="Update this exact wiki page id instead of looking it up by title "
                              "(needed for pages with no title).")
    study = parser.add_argument_group(
        "study wiki", "Build a study wiki (study name, Study Description, Methods) into content_path "
                      "before uploading. Give both --description and --methods.")
    study.add_argument("--description", default=None, help="File with the study description (HTML or markdown)")
    study.add_argument("--methods", default=None, help="File with the methods (HTML or markdown)")
    study.add_argument("--study-name", default=None,
                       help="Shown as the top heading. If left out, you'll be asked for it.")
    args = parser.parse_args()
    if bool(args.description) != bool(args.methods):
        parser.error("--description and --methods must be given together")
    return args


if __name__ == "__main__":
    args = _parse_args()
    if args.description:
        build_study_wiki(args.content_path, args.description, args.methods, args.study_name)
        print(f"Built study wiki: {os.path.abspath(args.content_path)}")
    syn = login()
    create_or_update_wiki(
        syn,
        owner_id=args.owner_id,
        title=args.title,
        content_path=args.content_path,
        parent_id=args.parent_id,
        verify=not args.no_verify,
        dry_run=args.dry_run,
        wiki_id=args.wiki_id,
    )
