"""Accessibility lawsuit-risk scan engine (ADA / WCAG triage).

Given a website URL, fetches the page server-side and runs 20 weighted
checks across six categories, producing a 0-100 score, an A-F grade, a
lawsuit-risk level, per-check evidence, and the top 3 prioritized fixes
as machine-readable JSON.

What this audits: the failure classes that dominate ADA website
lawsuits — plaintiff firms pick targets with automated scanners, and the
same class of scan is what this tool sells as triage: missing image alt
text, unlabeled form fields, low contrast, keyboard barriers, missing
landmarks, and empty links/buttons (these six classes appear in ~96% of
ADA complaints).

What this does NOT do: certify WCAG or ADA compliance. Automated scans
catch roughly 30% of WCAG issues — keyboard flows, screen-reader
behavior, and cognitive accessibility need human testing. The report
says so, in the report. Checks that cannot be evaluated return
"unknown", never guesses.

Shared fetch/check/scoring primitives come from common.auditlib.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# Make the shared lib importable both locally (repo layout) and in Docker
# (WORKDIR layout: /srv/a11y with ./common copied in).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.auditlib import (  # noqa: E402
    OVERALL_DEADLINE,
    STATUS_FAIL,
    STATUS_PARTIAL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    Check,
    compute_score,
    fetch,
    host_of,
    normalize_url,
)

ENGINE_VERSION = "a11y-audit/1.0"

DISCLAIMER = (
    "Automated triage scan only — NOT a WCAG/ADA compliance certification. "
    "Automated tools detect roughly 30% of WCAG issues; keyboard flows, "
    "screen-reader behavior, and cognitive accessibility require manual "
    "testing. A high score lowers lawsuit risk; it does not eliminate it."
)

# The failure classes plaintiff-firm scanners look for (present in ~96%
# of ADA complaints). Failing several of these drives the risk level.
PLAINTIFF_CHECKS = [
    "img_alt_text", "form_labels", "color_contrast", "keyboard_operability",
    "landmarks", "link_text_quality", "button_names",
]

GENERIC_LINK_TEXT = {
    "click here", "here", "read more", "more", "learn more", "link",
    "this", "go", "continue", "details", "more info", "info",
}

VALID_ROLES = {
    "alert", "alertdialog", "application", "article", "banner", "button",
    "caption", "cell", "checkbox", "code", "columnheader", "combobox",
    "complementary", "contentinfo", "definition", "deletion", "dialog",
    "directory", "document", "emphasis", "feed", "figure", "form",
    "generic", "grid", "gridcell", "group", "heading", "img", "insertion",
    "link", "list", "listbox", "listitem", "log", "main", "marquee",
    "math", "menu", "menubar", "menuitem", "menuitemcheckbox",
    "menuitemradio", "meter", "navigation", "none", "note", "option",
    "paragraph", "presentation", "progressbar", "radio", "radiogroup",
    "region", "row", "rowgroup", "rowheader", "scrollbar", "search",
    "searchbox", "separator", "slider", "spinbutton", "status",
    "strong", "subscript", "superscript", "switch", "tab", "table",
    "tablist", "tabpanel", "term", "textbox", "time", "timer", "toolbar",
    "tooltip", "tree", "treegrid", "treeitem",
}

NAMED_COLORS = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "green": (0, 128, 0), "blue": (0, 0, 255), "gray": (128, 128, 128),
    "grey": (128, 128, 128), "silver": (192, 192, 192),
    "maroon": (128, 0, 0), "navy": (0, 0, 128), "teal": (0, 128, 128),
    "olive": (128, 128, 0), "purple": (128, 0, 128),
    "fuchsia": (255, 0, 255), "lime": (0, 255, 0), "aqua": (0, 255, 255),
    "yellow": (255, 255, 0), "orange": (255, 165, 0),
}


# ---------------------------------------------------------------------------
# Check definitions (weights sum to 100)
# ---------------------------------------------------------------------------

CHECK_DEFS: list[tuple[str, str, int]] = [
    # images & media (17)
    ("img_alt_text", "images_media", 9),
    ("img_alt_quality", "images_media", 4),
    ("media_captions", "images_media", 4),
    # forms (18)
    ("form_labels", "forms", 10),
    ("form_input_purpose", "forms", 4),
    ("form_required_indication", "forms", 4),
    # structure & landmarks (18)
    ("lang_attribute", "structure", 5),
    ("heading_order", "structure", 5),
    ("landmarks", "structure", 5),
    ("skip_link", "structure", 3),
    # links & interactive (20)
    ("link_text_quality", "interactive", 7),
    ("button_names", "interactive", 5),
    ("target_size", "interactive", 4),
    ("keyboard_operability", "interactive", 4),
    # contrast & visual (12)
    ("color_contrast", "visual", 8),
    ("link_distinction", "visual", 4),
    # document basics (15)
    ("page_title", "document", 4),
    ("viewport_zoom", "document", 5),
    ("focus_indicator", "document", 4),
    ("aria_sanity", "document", 2),
]
assert sum(w for _, _, w in CHECK_DEFS) == 100


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------

def _build_ctx(url: str, deadline: float) -> dict | None:
    """Fetch homepage + up to 3 linked stylesheets. None if unreachable."""
    home = fetch(url, deadline)
    if home is None or home.status >= 400 or not home.text:
        return None
    final_url = home.url
    soup = BeautifulSoup(home.text, "lxml")

    css_chunks = [t.get_text() for t in soup.find_all("style")]
    n_css_files = 0
    for link in soup.find_all("link", rel=lambda v: v and "stylesheet" in str(v).lower()):
        if n_css_files >= 3:
            break
        href = link.get("href")
        if not href:
            continue
        css_url = urljoin(final_url, href)
        if urlparse(css_url).scheme not in ("http", "https"):
            continue
        fr = fetch(css_url, deadline)
        if fr is not None and fr.status == 200 and fr.text:
            css_chunks.append(fr.text)
            n_css_files += 1
    css_text = "\n".join(css_chunks)

    return {
        "url": url, "final_url": final_url, "host": host_of(url),
        "home": home, "soup": soup, "css_text": css_text,
        "css_files": n_css_files, "deadline": deadline,
    }


# ---------------------------------------------------------------------------
# Small helpers shared by checks
# ---------------------------------------------------------------------------

def _style_props(style: str) -> dict[str, str]:
    props = {}
    for decl in (style or "").split(";"):
        if ":" in decl:
            k, _, v = decl.partition(":")
            props[k.strip().lower()] = v.strip().lower()
    return props


def _accessible_name(el, soup) -> str:
    """Best-effort accessible name: aria-label, aria-labelledby targets,
    title, value (input buttons), contained text, or contained img alt."""
    aria = (el.get("aria-label") or "").strip()
    if aria:
        return aria
    labelledby = (el.get("aria-labelledby") or "").strip()
    if labelledby:
        parts = []
        for ref_id in labelledby.split():
            target = soup.find(id=ref_id)
            if target:
                parts.append(target.get_text(" ", strip=True))
        if " ".join(parts).strip():
            return " ".join(parts).strip()
    if el.name == "input":
        val = (el.get("value") or "").strip()
        if val:
            return val
    title = (el.get("title") or "").strip()
    if title:
        return title
    text = el.get_text(" ", strip=True)
    if text:
        return text
    img = el.find("img") if hasattr(el, "find") else None
    if img is not None:
        return (img.get("alt") or "").strip()
    return ""


def _labelled_control_ids(soup) -> set[str]:
    return {lab.get("for") for lab in soup.find_all("label") if lab.get("for")}


def _control_is_labelled(el, soup, labelled_ids: set[str]) -> bool:
    if (el.get("aria-label") or "").strip():
        return True
    if (el.get("aria-labelledby") or "").strip():
        return True
    cid = el.get("id")
    if cid and cid in labelled_ids:
        return True
    if el.find_parent("label") is not None:
        return True
    if (el.get("title") or "").strip():
        return True
    return False


def _form_controls(soup) -> list:
    controls = []
    skip_types = {"hidden", "submit", "button", "reset", "image"}
    for el in soup.find_all(["input", "select", "textarea"]):
        if el.name == "input" and (el.get("type") or "text").lower() in skip_types:
            continue
        controls.append(el)
    return controls


# ---------------------------------------------------------------------------
# Color parsing + WCAG contrast math
# ---------------------------------------------------------------------------

def _parse_color(value: str):
    """Parse a CSS color to (r, g, b) 0-255, or None if not evaluable."""
    v = (value or "").strip().lower()
    v = v.replace("!important", "").strip()
    if v in NAMED_COLORS:
        return NAMED_COLORS[v]
    m = re.fullmatch(r"#([0-9a-f]{3,8})", v)
    if m:
        h = m.group(1)
        if len(h) == 3:
            return tuple(int(c * 2, 16) for c in h)
        if len(h) in (6, 8):
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        return None
    m = re.fullmatch(r"rgba?\(([^)]+)\)", v)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        try:
            rgb = []
            for p in parts[:3]:
                if p.endswith("%"):
                    rgb.append(int(round(float(p[:-1]) * 2.55)))
                else:
                    rgb.append(int(float(p)))
            return tuple(rgb)
        except ValueError:
            return None
    return None


def _luminance(rgb) -> float:
    def channel(c: int) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast_ratio(fg, bg) -> float:
    l1, l2 = _luminance(fg), _luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _css_rules(css_text: str) -> list[tuple[str, dict[str, str]]]:
    """Crude CSS rule extraction: (selector, props) pairs. Good enough for
    signal detection (focus styles, link decoration, explicit color pairs);
    not a cascade engine — the evidence strings say so."""
    css = re.sub(r"/\*.*?\*/", "", css_text or "", flags=re.S)
    rules = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selector = m.group(1).strip().lower()
        props = _style_props(m.group(2))
        if selector and props:
            rules.append((selector, props))
    return rules


# ---------------------------------------------------------------------------
# Individual checks — each returns (status, evidence)
# ---------------------------------------------------------------------------

def check_img_alt_text(ctx) -> tuple[str, str]:
    imgs = ctx["soup"].find_all("img")
    if not imgs:
        return STATUS_UNKNOWN, "No <img> elements on the page; not evaluated."
    missing = [i for i in imgs if i.get("alt") is None]
    if not missing:
        return STATUS_PASS, f"All {len(imgs)} image(s) carry an alt attribute (empty alt = decorative, which is valid)."
    ratio = len(missing) / len(imgs)
    src = (missing[0].get("src") or "?")[:60]
    if ratio > 0.10:
        return STATUS_FAIL, f"{len(missing)}/{len(imgs)} images have NO alt attribute (e.g. {src}) — the single most common failure in ADA complaints."
    return STATUS_PARTIAL, f"{len(missing)}/{len(imgs)} images missing the alt attribute (e.g. {src})."


def check_img_alt_quality(ctx) -> tuple[str, str]:
    imgs = [i for i in ctx["soup"].find_all("img") if (i.get("alt") or "").strip()]
    if not imgs:
        return STATUS_UNKNOWN, "No images with non-empty alt text; quality not evaluated."
    bad, vague = [], []
    for i in imgs:
        alt = i.get("alt").strip()
        src_base = (i.get("src") or "").rsplit("/", 1)[-1].split("?")[0].lower()
        if alt.lower() == src_base or re.search(r"\.(jpe?g|png|gif|webp|svg)$", alt.lower()):
            bad.append(alt)
        elif alt.lower() in ("image", "picture", "photo", "graphic", "img", "image of", "picture of") \
                or alt.lower().startswith(("image of ", "picture of ", "photo of ")):
            vague.append(alt)
    if bad:
        return STATUS_FAIL, f"{len(bad)} image(s) use the file name as alt text (e.g. '{bad[0][:40]}') — screen readers announce the file name, not the content."
    if vague:
        return STATUS_PARTIAL, f"{len(vague)} image(s) have vague alt text (e.g. '{vague[0][:40]}') — describe the content, not the medium."
    return STATUS_PASS, f"{len(imgs)} image(s) with descriptive alt text; no file-name or boilerplate alt detected."


def check_media_captions(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    videos = soup.find_all("video")
    audios = soup.find_all("audio")
    if not videos and not audios:
        return STATUS_UNKNOWN, "No <video>/<audio> elements on the page; captions not evaluated."
    problems = []
    for v in videos:
        tracks = v.find_all("track")
        if not any((t.get("kind") or "").lower() in ("captions", "subtitles") for t in tracks):
            problems.append("video without a captions/subtitles <track>")
    for a in audios:
        # audio-only media needs a transcript; a linked transcript can't be
        # verified from markup, so presence of any <track> is the only signal.
        if not a.find_all("track"):
            problems.append("audio without a <track> (transcript not verifiable from markup)")
    if problems:
        return STATUS_FAIL, f"{len(problems)} media issue(s): {problems[0]}. Prerecorded media without captions/transcripts is a standard ADA complaint item."
    return STATUS_PASS, "All media elements declare caption/subtitle tracks."


def check_form_labels(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    controls = _form_controls(soup)
    if not controls:
        return STATUS_UNKNOWN, "No form controls on the page; labels not evaluated."
    labelled_ids = _labelled_control_ids(soup)
    unlabeled = []
    placeholder_only = []
    for el in controls:
        if _control_is_labelled(el, soup, labelled_ids):
            continue
        unlabeled.append(el)
        if (el.get("placeholder") or "").strip():
            placeholder_only.append(el)
    total = len(controls)
    if not unlabeled:
        return STATUS_PASS, f"All {total} form control(s) have a programmatic label (label/aria) — screen readers can name every field."
    ratio = len(unlabeled) / total
    name0 = unlabeled[0].get("name") or unlabeled[0].get("id") or unlabeled[0].name
    extra = f"; {len(placeholder_only)} rely on placeholder text only" if placeholder_only else ""
    if ratio > 0.20:
        return STATUS_FAIL, f"{len(unlabeled)}/{total} form controls have NO label (first: <{unlabeled[0].name}> '{name0}'){extra} — unlabeled fields are a top-3 ADA complaint."
    return STATUS_PARTIAL, f"{len(unlabeled)}/{total} form controls unlabeled (first: '{name0}'){extra}."


_PURPOSE_HINTS = [
    (re.compile(r"e-?mail", re.I), "email"),
    (re.compile(r"tel|phone|mobile", re.I), "tel"),
    (re.compile(r"full[-_ ]?name|first[-_ ]?name|last[-_ ]?name|(^|[-_])name($|[-_])", re.I), "name"),
    (re.compile(r"address|street", re.I), "address"),
    (re.compile(r"city|town", re.I), "address-level2"),
    (re.compile(r"zip|postal", re.I), "postal-code"),
    (re.compile(r"cc-|card", re.I), "cc-number"),
    (re.compile(r"username|user[-_ ]?name", re.I), "username"),
]


def check_form_input_purpose(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    controls = _form_controls(soup)
    if not controls:
        return STATUS_UNKNOWN, "No form controls on the page; input purpose not evaluated."
    identifiable, missing = [], []
    for el in controls:
        hay = " ".join(str(el.get(a) or "") for a in ("name", "id", "type", "placeholder"))
        purpose = None
        for rx, p in _PURPOSE_HINTS:
            if rx.search(hay):
                purpose = p
                break
        if purpose is None and (el.get("type") or "").lower() in ("email", "tel"):
            purpose = el.get("type").lower()
        if purpose:
            identifiable.append(el)
            if not (el.get("autocomplete") or "").strip():
                missing.append((el, purpose))
    if not identifiable:
        return STATUS_UNKNOWN, "No personally-identifiable form fields detected (name/email/phone/address/payment); autocomplete purpose not evaluated."
    if not missing:
        return STATUS_PASS, f"All {len(identifiable)} identifiable field(s) declare autocomplete purpose — WCAG 1.3.5 satisfied for these fields."
    el0, p0 = missing[0]
    if len(missing) == len(identifiable):
        return STATUS_FAIL, f"None of the {len(identifiable)} identifiable field(s) declare autocomplete (first: '{el0.get('name') or el0.get('id') or '?'}' looks like {p0}) — WCAG 1.3.5 (Identify Input Purpose) failure."
    return STATUS_PARTIAL, f"{len(missing)}/{len(identifiable)} identifiable field(s) lack autocomplete (first: looks like {p0})."


def check_form_required_indication(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    controls = _form_controls(soup)
    if not controls:
        return STATUS_UNKNOWN, "No form controls on the page; required-field indication not evaluated."
    labelled_ids = _labelled_control_ids(soup)
    labels_by_for = {lab.get("for"): lab for lab in soup.find_all("label") if lab.get("for")}
    marked_not_required, properly_required = [], 0
    for el in controls:
        is_required = el.has_attr("required") or (el.get("aria-required") or "").lower() == "true"
        label_text = ""
        cid = el.get("id")
        if cid and cid in labels_by_for:
            label_text = labels_by_for[cid].get_text(" ", strip=True)
        parent_label = el.find_parent("label")
        if parent_label is not None:
            label_text += " " + parent_label.get_text(" ", strip=True)
        star_marked = "*" in label_text or "(required)" in label_text.lower()
        if is_required:
            properly_required += 1
        elif star_marked:
            marked_not_required.append(el)
    if marked_not_required:
        el0 = marked_not_required[0]
        return STATUS_FAIL, f"{len(marked_not_required)} field(s) are marked required visually (* in the label) but NOT programmatically (no required/aria-required; first: '{el0.get('name') or el0.get('id') or '?'}') — screen-reader users can't tell the field is required."
    if properly_required:
        return STATUS_PASS, f"{properly_required} required field(s) are indicated programmatically (required/aria-required) as well as visually."
    return STATUS_UNKNOWN, "No required-field signals found (no required attributes, no * markers); not evaluated."


def check_lang_attribute(ctx) -> tuple[str, str]:
    html = ctx["soup"].find("html")
    if html is None:
        return STATUS_UNKNOWN, "No <html> element parsed; lang not evaluated."
    lang = (html.get("lang") or "").strip()
    if not lang:
        return STATUS_FAIL, "The <html> element has no lang attribute — screen readers will pronounce the page with the wrong speech rules (WCAG 3.1.1)."
    if re.fullmatch(r"[a-zA-Z]{2,3}(-[a-zA-Z0-9]+)*", lang):
        return STATUS_PASS, f"Page language declared: lang=\"{lang}\" — screen readers use the correct pronunciation rules."
    return STATUS_PARTIAL, f"lang attribute present but malformed: '{lang[:20]}' (expected e.g. 'en' or 'en-US')."


def check_heading_order(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    headings = [(int(t.name[1]), t.get_text(" ", strip=True)) for t in soup.find_all(re.compile(r"^h[1-6]$"))]
    if not headings:
        return STATUS_FAIL, "No headings at all — screen-reader users navigate by headings; this page gives them no map."
    h1s = [h for h in headings if h[0] == 1]
    skips = []
    prev = 0
    for level, _text in headings:
        if prev and level > prev + 1:
            skips.append(f"h{prev} -> h{level}")
        prev = level
    if skips:
        return STATUS_FAIL, f"Heading levels skip: {', '.join(skips[:3])} — a skipped level breaks the document outline screen readers present (WCAG 1.3.1/2.4.6)."
    if len(h1s) > 1:
        return STATUS_PARTIAL, f"{len(h1s)} <h1> elements and no skipped levels — one h1 is the cleaner outline."
    if not h1s:
        return STATUS_PARTIAL, f"{len(headings)} heading(s) but no <h1> — the page's main heading isn't identified."
    return STATUS_PASS, f"Clean heading outline: 1 h1 + {len(headings) - 1} subheading(s), no skipped levels."


def check_landmarks(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    found = set()
    tag_map = {"header": "banner", "nav": "navigation", "main": "main",
               "footer": "contentinfo", "aside": "complementary"}
    for tag, role in tag_map.items():
        if soup.find(tag):
            found.add(role)
    for el in soup.find_all(attrs={"role": True}):
        r = (el.get("role") or "").strip().lower()
        if r in ("banner", "navigation", "main", "contentinfo", "complementary", "search", "form"):
            found.add(r)
    mains = len(soup.find_all("main")) + len(soup.find_all(attrs={"role": "main"}))
    if "main" not in found:
        return STATUS_FAIL, f"No <main> landmark (found: {', '.join(sorted(found)) or 'none'}) — assistive tech can't jump to the primary content; missing landmarks are a named ADA complaint class."
    if mains > 1:
        return STATUS_PARTIAL, f"{mains} main landmarks — exactly one <main> is valid; screen readers can't tell which is primary."
    if len(found) >= 3:
        return STATUS_PASS, f"Landmarks present: {', '.join(sorted(found))} — screen-reader users can jump between page regions."
    return STATUS_PARTIAL, "Only the main landmark is present — header/nav/footer landmarks help screen-reader navigation."


def check_skip_link(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    first_links = soup.find_all("a", href=True)[:3]
    for a in first_links:
        href = a.get("href") or ""
        text = a.get_text(" ", strip=True).lower()
        if href.startswith("#") and len(href) > 1 and "skip" in text:
            target = soup.find(id=href[1:])
            if target is not None:
                return STATUS_PASS, f"Skip link present ('{a.get_text(' ', strip=True)[:40]}' -> {href}) — keyboard users can bypass repeated navigation (WCAG 2.4.1)."
            return STATUS_PARTIAL, "A 'skip' link exists but its target doesn't resolve to an element on the page."
    if soup.find("main") or soup.find(attrs={"role": "main"}):
        return STATUS_PARTIAL, "No skip link, but a <main> landmark exists — landmark navigation partially covers the bypass requirement."
    return STATUS_FAIL, "No skip link and no main landmark — keyboard users must tab through every navigation item on every page (WCAG 2.4.1)."


def check_link_text_quality(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    links = [a for a in soup.find_all("a", href=True)
             if not (a.get("href") or "").strip().lower().startswith("javascript:")]
    if not links:
        return STATUS_UNKNOWN, "No links on the page; link text not evaluated."
    empty, generic = [], []
    for a in links:
        name = _accessible_name(a, soup)
        if not name:
            empty.append(a)
        elif name.strip().lower() in GENERIC_LINK_TEXT:
            generic.append(a)
    if empty:
        href0 = (empty[0].get("href") or "?")[:50]
        return STATUS_FAIL, f"{len(empty)}/{len(links)} links have NO accessible name (first href: {href0}) — empty links are a top-6 ADA complaint class; screen readers announce just 'link'."
    if generic:
        if len(generic) / len(links) > 0.15:
            return STATUS_FAIL, f"{len(generic)}/{len(links)} links use generic text ('{generic[0].get_text(' ', strip=True)[:30]}') — out of context, screen-reader link lists become meaningless."
        return STATUS_PARTIAL, f"{len(generic)} link(s) use generic text (e.g. '{generic[0].get_text(' ', strip=True)[:30]}') — link purpose should be clear out of context."
    return STATUS_PASS, f"All {len(links)} links have descriptive accessible names."


def check_button_names(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    buttons = list(soup.find_all("button"))
    buttons += [i for i in soup.find_all("input")
                if (i.get("type") or "").lower() in ("button", "submit", "reset", "image")]
    buttons += list(soup.find_all(attrs={"role": "button"}))
    if not buttons:
        return STATUS_UNKNOWN, "No button elements on the page; button names not evaluated."
    unnamed = [b for b in buttons if not _accessible_name(b, soup)]
    if unnamed:
        b0 = unnamed[0]
        desc = f"<{b0.name}>" + (f" type={b0.get('type')}" if b0.get("type") else "")
        return STATUS_FAIL, f"{len(unnamed)}/{len(buttons)} buttons have NO accessible name (first: {desc}) — an unnamed (usually icon-only) button is announced as just 'button'; empty buttons are a top-6 ADA complaint class."
    return STATUS_PASS, f"All {len(buttons)} button(s) have accessible names."


def _px_value(v: str):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)px", (v or "").strip())
    return float(m.group(1)) if m else None


def check_target_size(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    targets = []
    targets += soup.find_all("a", href=True)
    targets += soup.find_all("button")
    targets += soup.find_all("input")
    targets += soup.find_all("select")
    measured, small, tiny = 0, [], []
    for el in targets:
        w = h = None
        props = _style_props(el.get("style") or "")
        if "width" in props:
            w = _px_value(props["width"])
        if "height" in props:
            h = _px_value(props["height"])
        if w is None and el.get("width"):
            try:
                w = float(el.get("width"))
            except (TypeError, ValueError):
                pass
        if h is None and el.get("height"):
            try:
                h = float(el.get("height"))
            except (TypeError, ValueError):
                pass
        if w is not None and h is not None:
            measured += 1
            smallest = min(w, h)
            if smallest < 24:
                tiny.append((el, smallest))
            elif smallest < 44:
                small.append((el, smallest))
    if measured == 0:
        return STATUS_UNKNOWN, "No interactive elements declare explicit sizes in markup (inline style/width/height); target size can't be estimated without rendered layout."
    if tiny:
        el0, s0 = tiny[0]
        return STATUS_FAIL, f"{len(tiny)} interactive element(s) measure under 24px in their smallest dimension (first: <{el0.name}> at {s0:.0f}px, from explicit markup sizes) — below the WCAG 2.5.8 AA minimum of 24x24 CSS px."
    if small:
        return STATUS_PARTIAL, f"{len(small)} interactive element(s) are 24-43px in their smallest dimension — above the 24px AA floor but under the 44px AAA/touch guideline."
    return STATUS_PASS, f"All {measured} measurable interactive element(s) are >=44px in both dimensions (explicit markup sizes)."


def check_keyboard_operability(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    positive_tab = [el for el in soup.find_all(attrs={"tabindex": True})
                    if str(el.get("tabindex")).strip().lstrip("+").isdigit()
                    and int(str(el.get("tabindex")).strip()) > 0]
    fake_buttons = []
    for el in soup.find_all(attrs={"onclick": True}):
        if el.name in ("a", "button", "input", "select", "textarea"):
            continue
        if (el.get("role") or "").strip().lower() in ("button", "link"):
            continue
        if el.has_attr("tabindex"):
            continue
        fake_buttons.append(el)
    if positive_tab and fake_buttons:
        return STATUS_FAIL, f"Two keyboard barriers: {len(positive_tab)} element(s) with positive tabindex (scrambles the natural tab order) and {len(fake_buttons)} clickable <{fake_buttons[0].name}> element(s) with no role/tabindex (mouse-only — unreachable by keyboard)."
    if positive_tab:
        return STATUS_FAIL, f"{len(positive_tab)} element(s) use positive tabindex (first: <{positive_tab[0].name}> tabindex={positive_tab[0].get('tabindex')}) — positive tabindex scrambles tab order and is a classic keyboard-trap ingredient."
    if fake_buttons:
        return STATUS_FAIL, f"{len(fake_buttons)} clickable element(s) are mouse-only (first: <{fake_buttons[0].name}> with onclick but no role/tabindex) — keyboard users cannot reach or activate them."
    return STATUS_PASS, "No positive-tabindex ordering hazards and no mouse-only clickable elements detected in markup."


def check_color_contrast(ctx) -> tuple[str, str]:
    pairs = []  # (fg_rgb, bg_rgb, where)
    soup = ctx["soup"]
    # Inline styles: elements declaring both text color and background.
    for el in soup.find_all(attrs={"style": True}):
        props = _style_props(el.get("style") or "")
        fg = _parse_color(props.get("color", ""))
        bg = _parse_color(props.get("background-color", "") or props.get("background", ""))
        if fg and bg:
            pairs.append((fg, bg, f"inline style on <{el.name}>"))
    # Stylesheet rules declaring both in one rule (e.g. body, .btn).
    for selector, props in _css_rules(ctx["css_text"]):
        fg = _parse_color(props.get("color", ""))
        bg = _parse_color(props.get("background-color", ""))
        if fg and bg:
            pairs.append((fg, bg, f"CSS rule '{selector[:40]}'"))
    if not pairs:
        return STATUS_UNKNOWN, "No element or CSS rule declares both a text color and a background color in evaluable form (hex/rgb/named) — contrast can't be estimated from markup alone; rendered-DOM measurement is needed."
    ratios = [(r, where) for (_, _, where), r in
              zip(pairs, [_contrast_ratio(fg, bg) for fg, bg, _ in pairs])]
    worst_ratio, worst_where = min(ratios, key=lambda t: t[0])
    n_fail = sum(1 for r, _ in ratios if r < 4.5)
    basis = (f"estimated from {len(pairs)} explicit color/background pair(s) in inline styles "
             f"and the page's own CSS ({ctx['css_files']} stylesheet(s) + style blocks); "
             "browser-default and inherited colors are not evaluated")
    if worst_ratio < 3.0:
        return STATUS_FAIL, f"Worst contrast {worst_ratio:.2f}:1 ({worst_where}) — far below the WCAG AA 4.5:1 minimum for text; {n_fail}/{len(pairs)} pair(s) under 4.5:1 ({basis}). Low contrast is a top-6 ADA complaint class."
    if worst_ratio < 4.5:
        return STATUS_PARTIAL, f"Worst contrast {worst_ratio:.2f}:1 ({worst_where}) — under the WCAG AA 4.5:1 minimum for normal text ({basis})."
    return STATUS_PASS, f"All {len(pairs)} evaluable color pair(s) meet WCAG AA 4.5:1 (worst {worst_ratio:.2f}:1; {basis})."


def check_link_distinction(ctx) -> tuple[str, str]:
    rules = _css_rules(ctx["css_text"])
    bare_a_rules = [p for sel, p in rules
                    if re.fullmatch(r"a", sel) or re.fullmatch(r"a:link", sel)]
    soup = ctx["soup"]
    inline_stripped = 0
    for a in soup.find_all("a", href=True):
        props = _style_props(a.get("style") or "")
        if props.get("text-decoration", "").startswith("none"):
            inline_stripped += 1
    css_strips = any(p.get("text-decoration", "").startswith("none") for p in bare_a_rules)
    if not css_strips and inline_stripped == 0:
        return STATUS_PASS, "Links keep their underline (no CSS or inline style strips it) — links are identifiable without relying on color alone."
    # Underline stripped: is there a non-color cue instead?
    alt_cues = any(any(k in p for k in ("border", "border-bottom", "background",
                                       "background-color", "font-weight", "outline"))
                   for p in bare_a_rules)
    if alt_cues:
        return STATUS_PARTIAL, "Link underlines are removed, but link rules add another visual cue (border/background/weight) — verify it survives in body text, not just nav."
    where = "the stylesheet's bare `a` rule" if css_strips else f"{inline_stripped} inline-styled link(s)"
    return STATUS_FAIL, f"Underlines are stripped by {where} with no non-color cue (border/background/weight) — links are then identifiable by color alone, a WCAG 1.4.1 failure."


def check_page_title(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    tag = soup.find("title")
    title = tag.get_text(strip=True) if tag else ""
    if not title:
        return STATUS_FAIL, "No <title> (or it's empty) — the title is the first thing a screen reader announces and the label in every tab/result list (WCAG 2.4.2)."
    low = title.lower()
    if low in ("home", "untitled", "document", "index", "welcome") or len(title) < 8:
        return STATUS_PARTIAL, f"Title is generic: '{title[:50]}' — it doesn't describe this page's topic or purpose."
    return STATUS_PASS, f"Descriptive page title: '{title[:60]}'."


def check_viewport_zoom(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    tag = soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)})
    if tag is None:
        return STATUS_PARTIAL, "No viewport meta tag — the page likely doesn't adapt to mobile, where zoom/pinch is an accessibility feature, not a luxury."
    content = (tag.get("content") or "").lower()
    if "user-scalable=no" in content.replace(" ", "") or "user-scalable=0" in content.replace(" ", ""):
        return STATUS_FAIL, "Viewport disables zoom (user-scalable=no) — low-vision users can't magnify the page; a standard ADA complaint item (WCAG 1.4.4/1.4.10)."
    m = re.search(r"maximum-scale\s*=\s*([0-9.]+)", content)
    if m and float(m.group(1)) < 2.0:
        return STATUS_FAIL, f"Viewport caps zoom at maximum-scale={m.group(1)} (<2x) — low-vision users can't magnify enough (WCAG 1.4.4)."
    return STATUS_PASS, "Viewport meta present and zoom is not restricted."


def check_focus_indicator(ctx) -> tuple[str, str]:
    css = (ctx["css_text"] or "").lower()
    rules = _css_rules(ctx["css_text"])
    has_focus_style = any(":focus" in sel for sel, _ in rules)
    outline_removed = bool(re.search(r"outline\s*:\s*(none|0)\b", css))
    soup = ctx["soup"]
    inline_removed = any(
        _style_props(el.get("style") or "").get("outline", "") in ("none", "0")
        for el in soup.find_all(attrs={"style": True}))
    if has_focus_style and not (outline_removed or inline_removed):
        return STATUS_PASS, "The page's CSS defines :focus styles and doesn't strip outlines — keyboard users can see where they are."
    if has_focus_style and (outline_removed or inline_removed):
        return STATUS_PARTIAL, "CSS defines :focus styles but also removes outlines somewhere — if the removal wins on interactive elements, keyboard focus becomes invisible."
    if outline_removed or inline_removed:
        return STATUS_FAIL, "CSS removes the focus outline (outline: none/0) and defines no replacement :focus style — keyboard users tab blind. Missing visible focus is a core ADA complaint."
    if not ctx["css_text"].strip():
        return STATUS_UNKNOWN, "No CSS found on the page (no style blocks, no fetchable stylesheets); focus visibility not evaluated."
    return STATUS_PARTIAL, "No :focus styles in the page's CSS — focus visibility depends entirely on browser defaults, which many CSS resets silently remove."


def check_aria_sanity(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    focusable_sel = "a[href], button, input, select, textarea, [tabindex]"
    hidden_focusable = []
    for el in soup.find_all(attrs={"aria-hidden": "true"}):
        if el.name in ("a", "button", "input", "select", "textarea") or el.has_attr("tabindex"):
            hidden_focusable.append(el)
        elif el.select(focusable_sel):
            hidden_focusable.append(el)
    bad_roles = []
    for el in soup.find_all(attrs={"role": True}):
        for r in (el.get("role") or "").lower().split():
            if r and r not in VALID_ROLES:
                bad_roles.append(r)
    any_aria = bool(soup.find_all(
        lambda tag: any(str(k).startswith("aria-") for k in tag.attrs))) \
        or bool(soup.find_all(attrs={"role": True}))
    if hidden_focusable:
        return STATUS_FAIL, f"{len(hidden_focusable)} aria-hidden element(s) contain focusable content — keyboard focus lands on things screen readers can't see (a focus-trap class bug, WCAG 4.1.3/2.1.1)."
    if bad_roles:
        return STATUS_PARTIAL, f"Invalid ARIA role value(s): {', '.join(sorted(set(bad_roles))[:4])} — unknown roles are ignored by assistive tech, so the intended semantics vanish."
    if not any_aria:
        return STATUS_UNKNOWN, "No ARIA attributes or roles used on the page; nothing to validate."
    return STATUS_PASS, "ARIA usage is sane: no aria-hidden focus traps and all role values are valid."


CHECK_FNS = {
    "img_alt_text": check_img_alt_text,
    "img_alt_quality": check_img_alt_quality,
    "media_captions": check_media_captions,
    "form_labels": check_form_labels,
    "form_input_purpose": check_form_input_purpose,
    "form_required_indication": check_form_required_indication,
    "lang_attribute": check_lang_attribute,
    "heading_order": check_heading_order,
    "landmarks": check_landmarks,
    "skip_link": check_skip_link,
    "link_text_quality": check_link_text_quality,
    "button_names": check_button_names,
    "target_size": check_target_size,
    "keyboard_operability": check_keyboard_operability,
    "color_contrast": check_color_contrast,
    "link_distinction": check_link_distinction,
    "page_title": check_page_title,
    "viewport_zoom": check_viewport_zoom,
    "focus_indicator": check_focus_indicator,
    "aria_sanity": check_aria_sanity,
}
assert set(CHECK_FNS) == {cid for cid, _, _ in CHECK_DEFS}


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

RECOMMENDATION_FIXES = {
    "img_alt_text": ("Add alt text to every image", "Missing alt text is the #1 failure in ADA complaints — screen readers announce the file name or nothing. Describe content images; use alt=\"\" for purely decorative ones."),
    "img_alt_quality": ("Rewrite boilerplate alt text", "File-name or 'image of' alt text wastes the attribute — describe what the image shows and why it's there."),
    "media_captions": ("Add captions/transcripts to media", "Prerecorded video needs captions and audio needs a transcript; plaintiff scans flag bare <video>/<audio> automatically."),
    "form_labels": ("Label every form field programmatically", "Use <label for> or aria-label on every control — placeholders are not labels. Unlabeled checkout/contact fields are a top-3 complaint."),
    "form_input_purpose": ("Add autocomplete attributes", "autocomplete=\"email\"/\"tel\"/\"street-address\" etc. (WCAG 1.3.5) lets browsers and assistive tech identify each field's purpose."),
    "form_required_indication": ("Mark required fields in code, not just visually", "A red asterisk sighted users see must exist for assistive tech too: add required or aria-required=\"true\"."),
    "lang_attribute": ("Set the page language", "Add lang=\"en\" (or the correct code) to <html> so screen readers pronounce content correctly (WCAG 3.1.1)."),
    "heading_order": ("Fix the heading outline", "One h1, then h2/h3 in order without skipping levels — headings are the table of contents screen readers navigate by."),
    "landmarks": ("Add landmark regions", "Wrap content in <header>, <nav>, <main>, <footer> so assistive tech can jump between regions; exactly one <main>."),
    "skip_link": ("Add a skip-to-content link", "First focusable element on the page: a visible-on-focus link to #main (WCAG 2.4.1) so keyboard users skip the nav."),
    "link_text_quality": ("Give every link a descriptive name", "Empty and 'click here' links fail WCAG 2.4.4 — the link text (or aria-label) must say where it goes."),
    "button_names": ("Name every button", "Icon-only buttons need aria-label — an unnamed button is announced as just 'button' and is a top-6 complaint class."),
    "target_size": ("Enlarge small tap targets", "Interactive elements should be at least 24x24 CSS px (WCAG 2.5.8 AA); 44px is the safer touch guideline."),
    "keyboard_operability": ("Make everything keyboard-operable", "Remove positive tabindex, and give clickable elements real semantics: a <button>, or role + tabindex + key handler. If you can't tab to it, it doesn't exist for keyboard users."),
    "color_contrast": ("Raise text contrast to 4.5:1", "WCAG AA requires 4.5:1 for normal text (3:1 for large). Darken the text or lighten the background until the pair passes."),
    "link_distinction": ("Don't identify links by color alone", "Keep underlines in body copy, or add a non-color cue (border, background, weight) — WCAG 1.4.1."),
    "page_title": ("Write a descriptive page title", "The <title> is the first thing announced on page load and the label everywhere the page is listed (WCAG 2.4.2)."),
    "viewport_zoom": ("Stop disabling zoom", "Remove user-scalable=no and any maximum-scale under 2 from the viewport meta — low-vision users must be able to magnify."),
    "focus_indicator": ("Restore a visible focus indicator", "Never outline:none without a replacement :focus style (outline, border, or shadow) at least as visible — keyboard users must see where they are."),
    "aria_sanity": ("Fix ARIA misuse", "Never put aria-hidden on focusable content, and use only valid role values — bad ARIA is worse than no ARIA."),
}


# ---------------------------------------------------------------------------
# Risk + report
# ---------------------------------------------------------------------------

def risk_level(score: float, checks: list[Check], unreachable: bool = False) -> dict:
    """Lawsuit-risk framing: what a plaintiff firm's scanner sees.

    Deterministic mapping from the score + failures in the six complaint
    classes (PLAINTIFF_CHECKS). High score lowers risk; nothing here
    certifies compliance (see DISCLAIMER).
    """
    if unreachable:
        return {"level": "unknown",
                "plaintiff_scan_failures": [],
                "basis": "Site unreachable — risk not evaluated."}
    by_id = {c.id: c for c in checks}
    failures = [cid for cid in PLAINTIFF_CHECKS
                if by_id.get(cid) is not None and by_id[cid].status == STATUS_FAIL]
    if score >= 90:
        level = "Low"
    elif score >= 75:
        level = "Moderate"
    elif score >= 55:
        level = "Elevated"
    else:
        level = "High"
    # Failing 3+ of the complaint classes floors the level at Elevated —
    # those are exactly the patterns demand letters cite.
    if len(failures) >= 3 and level in ("Low", "Moderate"):
        level = "Elevated"
    if len(failures) >= 5 and level != "High":
        level = "High"
    return {
        "level": level,
        "plaintiff_scan_failures": failures,
        "basis": (f"Score {score} (A-F grade bands) plus {len(failures)} failing "
                  f"check(s) in the six classes that dominate ADA complaints "
                  "(alt text, form labels, contrast, keyboard, landmarks, "
                  "empty links/buttons)."),
    }


def build_report(url: str, final_url: str, checks: list[Check],
                 t_start: float, unreachable: bool = False) -> dict:
    score, grade, denom, unknown_count = compute_score(checks)
    failed = [c for c in checks if c.status in (STATUS_FAIL, STATUS_PARTIAL)]
    failed.sort(key=lambda c: -c.weight)
    recommendations = []
    for c in failed[:3]:
        title, why = RECOMMENDATION_FIXES[c.id]
        recommendations.append({
            "check_id": c.id,
            "title": title,
            "why": why,
            "evidence": c.evidence,
            "weight": c.weight,
        })
    return {
        "url": url,
        "final_url": final_url,
        "graded_at": datetime.now(timezone.utc).isoformat(),
        "score": score,
        "grade": grade,
        "risk": risk_level(score, checks, unreachable=unreachable),
        "unreachable": unreachable,
        "checks": [
            {"id": c.id, "category": c.category, "weight": c.weight,
             "status": c.status, "evidence": c.evidence}
            for c in checks
        ],
        "recommendations": recommendations,
        "disclaimer": DISCLAIMER,
        "meta": {
            "evaluated_weight": denom,
            "total_weight": 100,
            "unknown_count": unknown_count,
            "elapsed_ms": int((time.time() - t_start) * 1000),
            "engine": ENGINE_VERSION,
        },
    }


def _run_checks(ctx: dict) -> list[Check]:
    checks = []
    for cid, cat, w in CHECK_DEFS:
        try:
            status, evidence = CHECK_FNS[cid](ctx)
        except Exception as exc:  # a check must never kill the run
            status, evidence = STATUS_UNKNOWN, f"Check errored and was not evaluated: {exc}"
        checks.append(Check(id=cid, category=cat, weight=w, status=status, evidence=evidence))
    return checks


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def audit_a11y(url: str) -> dict:
    """Full paid accessibility lawsuit-risk scan. Never raises."""
    t_start = time.time()
    deadline = t_start + OVERALL_DEADLINE
    url = normalize_url(url)
    ctx = _build_ctx(url, deadline)
    if ctx is None:
        checks = [Check(id=cid, category=cat, weight=w, status=STATUS_UNKNOWN,
                        evidence="Site unreachable or returned no content; check not evaluated.")
                  for cid, cat, w in CHECK_DEFS]
        return build_report(url, url, checks, t_start, unreachable=True)
    return build_report(url, ctx["final_url"], _run_checks(ctx), t_start)


def audit_a11y_lite(url: str) -> dict:
    """Free lite scan: score + grade + risk level only.

    Runs the same 20 checks but strips per-check evidence and detailed
    recommendations — the funnel for the paid full scan. Never raises.
    """
    t_start = time.time()
    try:
        full = audit_a11y(url)
    except Exception as exc:
        return {"url": url, "error": f"Audit failed: {exc}",
                "graded_at": datetime.now(timezone.utc).isoformat()}
    if full.get("unreachable"):
        return {
            "url": full["url"], "final_url": full["final_url"],
            "graded_at": full["graded_at"],
            "score": 0.0, "grade": "F", "risk_level": "unknown",
            "unreachable": True,
            "note": "Site unreachable — no scan possible.",
        }
    by_status: dict[str, int] = {"pass": 0, "partial": 0, "fail": 0, "unknown": 0}
    for c in full["checks"]:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
    top_gaps = [r["title"] for r in full["recommendations"]]
    return {
        "url": full["url"],
        "final_url": full["final_url"],
        "graded_at": full["graded_at"],
        "score": full["score"],
        "grade": full["grade"],
        "risk_level": full["risk"]["level"],
        "checks_summary": by_status,
        "top_gaps": top_gaps,
        "note": ("Free lite scan: score, grade, and lawsuit-risk level only. "
                 "The full audit_a11y report ($1.50/call) adds per-check "
                 "evidence and the prioritized fixes a developer needs. "
                 "Automated scan — not a compliance certification."),
    }


__all__ = ["audit_a11y", "audit_a11y_lite", "CHECK_DEFS", "CHECK_FNS",
           "build_report", "compute_score", "risk_level", "DISCLAIMER",
           "ENGINE_VERSION"]
