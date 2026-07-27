#!/usr/bin/env python3
"""Structural checks for the docs site. Stdlib only, no deps, ~instant.

Guards the drift classes that actually bit this site on 2026-07-27, all of the
same shape: a page has TWO representations of its own contents (the visible
questions, the table of contents, the JSON-LD schema) and nothing kept them in
step, so editing one silently desynced the others.

  1. JSON-LD PARSES. A malformed block is invisible in the browser and simply
     ignored by search engines, so it fails silently forever.

  2. FAQPage SCHEMA == VISIBLE QUESTIONS. Google's structured-data rules require
     the markup to match what a reader sees. The schema had drifted BOTH ways at
     once: it still advertised two questions that had been deleted, and had
     never gained one that was added. Both halves were invisible on the page.

  3. TOC == HEADINGS. The FAQ's contents list was missing an entry for weeks
     (nobody noticed, because the entry itself rendered fine).

  4. ANCHORS RESOLVE, AND ARE UNIQUE. A '#foo' link to a heading that has been
     renamed or removed is a dead link that no build step would catch.

Usage:
    scripts/check-pages.py                # this repo's *.html
    scripts/check-pages.py path/to/*.html # explicit files
    scripts/check-pages.py --quiet        # only print failures
    scripts/check-pages.py --fix          # regenerate FAQPage schemas from the pages

Exit: 0 all clean · 1 one or more checks failed.

SHARED, NOT COPIED. This is the single copy for the whole Meridian docs family;
rucktrack-docs and packcal-docs both run it via the reusable workflow beside it
(.github/workflows/check-pages.yml). It is deliberately generic -- it keys on
the presence of a FAQPage schema and a .toc block, not on any one product.

Why shared rather than vendored: the family vendors source/shared/LayoutKit.mc
into each app repo because Monkey C cannot import across repos, so a copy plus a
sync guard is the only option there. GitHub Actions has no such limit for public
repos in one org, so vendoring here would import a workaround into a context
that does not need it -- and would need its own drift guard, which is more
machinery than the script. Sharing also compounds: running this against a SECOND
corpus (packcal-docs) immediately exposed two flaws in it -- heading numbering
treated as drift, and wording diffs printed twice instead of paired. Two copies
would each have stayed dumb.
"""
import glob
import html as htmllib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _text(fragment):
    """Visible text of an HTML fragment, entities decoded, whitespace collapsed."""
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def _qkey(s):
    """Comparison key for a question. Strips a leading "N. " because heading
    numbering is a display convention, not content: packcal-docs numbers its
    headings while its schema does not, which is a formatting choice rather than
    drift. Without this the check reports every question on such a page as
    missing and drowns the two or three that genuinely differ."""
    return re.sub(r"^\d+\.\s*", "", s).strip()


def expected_entities(src):
    """The mainEntity a page's visible content implies.

    This is the CANONICAL generator: --fix writes exactly this, and the check
    compares against exactly this, so there is one algorithm rather than a
    generator and a validator that can disagree. Answers flatten <p> and <li>
    in document order, which is what a reader sees.
    """
    body = src[src.find("<h2 id="):]
    out = []
    for chunk in [c for c in re.split(r"(?=<h2 id=)", body) if c.startswith("<h2")]:
        q = _text(re.search(r'<h2 id="[^"]+">(.*?)</h2>', chunk, re.S).group(1))
        ans = chunk[chunk.find("</h2>") + 5:]
        ans = re.split(r'<p class="back"|</div>', ans)[0]
        parts = [a or b for a, b in re.findall(r"<p>(.*?)</p>|<li>(.*?)</li>", ans, re.S)]
        txt = re.sub(r"\s+", " ", " ".join(_text(x) for x in parts)).strip()
        if txt:
            out.append({"@type": "Question", "name": q,
                        "acceptedAnswer": {"@type": "Answer", "text": txt}})
    return out



def check_page(path):
    """Returns (errors, notes) for one page."""
    errs, notes = [], []
    src = path.read_text(encoding="utf-8")
    name = path.name

    # --- 1. every JSON-LD block parses -------------------------------------
    blocks = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', src, re.S)
    schemas = []
    for i, raw in enumerate(blocks):
        try:
            schemas.append(json.loads(raw))
        except json.JSONDecodeError as e:
            errs.append(f"{name}: JSON-LD block {i + 1} is not valid JSON ({e})")

    # --- headings on the page ----------------------------------------------
    heads = [(m.group(1), _text(m.group(2)))
             for m in re.finditer(r'<h2 id="([^"]+)">(.*?)</h2>', src, re.S)]
    head_ids = [h[0] for h in heads]
    head_txt = [h[1] for h in heads]

    # --- 2. FAQPage schema must match the visible questions ----------------
    for s in schemas:
        if s.get("@type") != "FAQPage":
            continue
        q = [_text(e.get("name", "")) for e in s.get("mainEntity", [])]
        if not head_txt:
            errs.append(f"{name}: FAQPage schema present but no <h2 id> questions found")
            continue
        qk, hk = [_qkey(x) for x in q], [_qkey(x) for x in head_txt]
        if len(qk) == len(hk):
            # Same count: nothing was added or removed, so any difference is a
            # WORDING drift. Report it as an aligned PAIR -- set-based reporting
            # would print each one twice (once as "not on the page", once as
            # "missing from schema") and bury three real edits in six lines.
            for pg, sc in zip(hk, qk):
                if pg != sc:
                    errs.append(f"{name}: page and schema word this differently\n"
                                f"          page  : {pg}\n"
                                f"          schema: {sc}")
        else:
            # Counts differ: a question genuinely exists on one side only.
            for x in [x for x in qk if x not in hk]:
                errs.append(f"{name}: schema advertises a question NOT on the page: {x!r}")
            for x in [x for x in hk if x not in qk]:
                errs.append(f"{name}: page question MISSING from schema: {x!r}")

        # Question parity is not enough: an ANSWER can be rewritten on the page
        # while the schema keeps the old text, which is invisible to a reader and
        # still wrong in the structured data. Compare against the canonical
        # generation. (Hit for real on 2026-07-27: the install-stuck answer was
        # rewritten and only a manual regeneration kept the schema honest.)
        want = {e["name"]: e["acceptedAnswer"]["text"] for e in expected_entities(src)}
        for e in s.get("mainEntity", []):
            nm = _text(e.get("name", ""))
            have = _text((e.get("acceptedAnswer") or {}).get("text", ""))
            exp = want.get(nm)
            if exp is not None and have != exp:
                errs.append(f"{name}: schema ANSWER is stale for {nm!r}\n"
                            f"          run: scripts/check-pages.py --fix")

    # --- 3. a table of contents must list every heading --------------------
    toc = re.search(r'<div class="toc">(.*?)</div>', src, re.S)
    if toc:
        linked = re.findall(r'href="#([^"]+)"', toc.group(1))
        for hid, txt in heads:
            if hid not in linked:
                errs.append(f"{name}: heading #{hid} ({txt[:44]!r}) is missing from the contents")
        for a in linked:
            if a not in head_ids:
                errs.append(f"{name}: contents links #{a}, which is not a heading on the page")

    # --- 4. anchors resolve, and ids are unique ----------------------------
    all_ids = re.findall(r'\sid="([^"]+)"', src)
    dupes = {i for i in all_ids if all_ids.count(i) > 1}
    for d in sorted(dupes):
        errs.append(f"{name}: duplicate id {d!r}")
    for a in set(re.findall(r'href="#([^"]+)"', src)):
        if a and a not in all_ids:
            errs.append(f"{name}: dead in-page link #{a}")

    return errs, notes


def fix_page(path):
    """Rewrite a page's FAQPage mainEntity from its visible content."""
    src = path.read_text(encoding="utf-8")
    m = re.search(r'(<script type="application/ld\+json">)(.*?)(</script>)', src, re.S)
    if not m:
        return False
    try:
        doc = json.loads(m.group(2))
    except json.JSONDecodeError:
        return False
    if doc.get("@type") != "FAQPage":
        return False
    doc["mainEntity"] = expected_entities(src)
    body = m.group(1) + "\n" + json.dumps(doc, indent=2, ensure_ascii=False) + "\n" + m.group(3)
    path.write_text(src[:m.start()] + body + src[m.end():], encoding="utf-8")
    return True


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    quiet = "--quiet" in sys.argv
    files = ([Path(a) for a in args] if args
             else sorted(Path(p) for p in glob.glob(str(ROOT / "*.html"))))
    if not files:
        print("check-pages: no html files found", file=sys.stderr)
        return 1

    if "--fix" in sys.argv:
        for f in files:
            if fix_page(f):
                print(f"  fixed  {f.name} (schema regenerated from the page)")
        print()

    total_err = 0
    for f in files:
        errs, notes = check_page(f)
        total_err += len(errs)
        for e in errs:
            print(f"  FAIL  {e}")
        for n in notes:
            print(f"  note  {n}")
        if not errs and not quiet:
            print(f"  ok    {f.name}")
    print()
    if total_err:
        print(f"check-pages: {total_err} problem(s) across {len(files)} page(s)")
        return 1
    print(f"check-pages: OK — {len(files)} page(s) clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
