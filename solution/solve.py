"""Rebuild the DOM Chromium built for every URL in /app/data/pages.txt.

The document root survived without its .htaccess files and only the later part of the
capture session's access log survived, so the lost per-directory settings are recovered in
two steps.

1. Pages whose load is in the log. The log settles three things per page:
   * which file answered and whether mod_include ran on it: the logged response size,
     compared with each candidate body (index files for directory URLs; the raw file and the
     file run through mod_include, which server.py emulates);
   * how Chromium decoded the page: every page fetches a byline photo whose query holds a
     credit line written as character references, and Chromium percent-encodes a query in
     the document's encoding, so only the right decoding reproduces the logged request;
   * whether scripting was off (a sandboxing Content-Security-Policy): pixels inside
     <noscript> are fetched only when <noscript> is parsed as markup.
2. The other pages. The lost settings depended only on a file's directory, name and
   permission bits, so for each directory (and file extension) the rules are fitted on the
   logged pages there: SSI on .html files is either off, on, or tied to the owner-execute bit
   (XBitHack); the charset the server declared is one of a few labels or none, possibly
   per extension (AddCharset); scripting is on or off, possibly differing for one file-name
   prefix (a FilesMatch section). Every rule consistent with the logged pages is applied to
   an unlogged page, and the run aborts unless they all give the same tree.
"""
import json, os, re, sys
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import dom  # noqa: E402
import server  # noqa: E402

DATA = sys.argv[1] if len(sys.argv) > 1 else "/app/data"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/app/output/page_dom.json"
ENCODINGS = ["utf-8", "windows-1252", "windows-1254", "windows-1251", "iso-8859-2", "iso-8859-15"]
HEADER_CHARSETS = [None, "UTF-8", "ISO-8859-1", "windows-1252", "ISO-8859-9"]
PREFIXES = ["print-"]


# ------------------------------------------------------------------ access log
LOG = re.compile(r'^(\S+) \S+ \S+ \[[^\]]+\] "(\S+) (\S+) [^"]*" (\d{3}) (\S+)')


def read_log(path, urls):
    paths = {urlsplit(u).path: u for u in urls}
    groups, cur = {}, None
    for line in open(path, encoding="latin-1"):
        m = LOG.match(line)
        if not m:
            continue
        target, status, size = m.group(3), int(m.group(4)), m.group(5)
        if target in paths and status == 200:
            cur = paths[target]
            groups[cur] = {"size": int(size), "subs": []}
        elif cur is not None:
            groups[cur]["subs"].append(target)
    return groups


# ------------------------------------------------------------------ requests a tree implies
QUERY_SAFE = set(range(0x21, 0x7F)) - {0x22, 0x23, 0x3C, 0x3E, 0x27}


def encode_query(q, encoding):
    """Percent-encode a URL query the way the URL parser does for a document in `encoding`."""
    out = []
    for ch in q:
        if encoding.startswith("utf"):
            b = ch.encode("utf-8")
        else:
            codec = dom.webencodings.lookup(encoding).codec_info.name
            try:
                b = ch.encode(codec)
            except UnicodeEncodeError:
                # a character the encoding lacks becomes "&#N;" with "&", "#" and ";" percent-encoded
                out.append("%%26%%23%d%%3B" % ord(ch))
                continue
        for x in b:
            out.append(chr(x) if x in QUERY_SAFE else "%%%02X" % x)
    return "".join(out)


def request_path(src, encoding):
    src = src.strip(" \t\n\f\r")
    if src.startswith("http://"):
        rest = src[len("http://"):]
        src = rest[rest.index("/"):] if "/" in rest else "/"
    if "?" in src:
        path, q = src.split("?", 1)
        return encode_query(path, "utf-8") + "?" + encode_query(q, encoding)
    return encode_query(src, "utf-8")


def implied_images(nodes, encoding, acc):
    """Image fetches the tree causes (template contents are inert; frames are UTF-8 documents)."""
    for n in nodes:
        if not isinstance(n, dict) or "tag" not in n:
            continue
        if n["tag"] == "img" and "src" in n["attrs"]:
            acc.add(request_path(n["attrs"]["src"], encoding))
        implied_images(n["children"], encoding, acc)
        if "shadow" in n:
            implied_images(n["shadow"]["children"], encoding, acc)
        if "frame" in n:
            implied_images(n["frame"], "utf-8", acc)
    return acc


def evidence(subs):
    return {s for s in subs if s.startswith(("/staff/photo.jpg", "/px", "/imp", "/p.gif"))}


# ------------------------------------------------------------------ candidates
def candidate_files(site, url_path):
    rel = url_path.lstrip("/")
    if rel == "" or rel.endswith("/"):
        return [f for f in sorted(site.files) if f.startswith(rel) and "/" not in f[len(rel):] and f[len(rel):].startswith("index.")]
    return [rel] if rel in site.files else []


def bodies(site, rel):
    """(processed?, body) for the raw file and, when it has directives, the file after mod_include."""
    raw = site.files[rel][0]
    out = [(False, raw)]
    if b"<!--#" in raw:
        out.append((True, server.SSI(site, rel).run()))
    return out


def headers_for(ctype, charset, scripting):
    h = [("content-type", ctype + ("; charset=" + charset if charset else ""))]
    if not scripting:
        h.append(("content-security-policy", "sandbox"))
    return h


def same(trees):
    first = json.dumps(trees[0], sort_keys=True)
    return all(json.dumps(t, sort_keys=True) == first for t in trees)


def from_log(site, url, ev):
    """Trees and settings of every candidate that reproduces the logged page load."""
    found = []
    for rel in candidate_files(site, urlsplit(url).path):
        ctype = site.mime(rel, site.dirconf(rel))[0]
        for processed, body in bodies(site, rel):
            if len(body) != ev["size"]:
                continue
            for enc in ENCODINGS:
                for scripting in (True, False):
                    h = headers_for(ctype, enc, scripting)
                    name, _ = dom.sniff(h, body)
                    tree = dom.solve_page(h, body)
                    if evidence(implied_images(tree, name, set())) == evidence(ev["subs"]):
                        found.append({"rel": rel, "processed": processed, "decoded": name,
                                      "scripting": scripting, "tree": tree, "body": body})
    assert found, "no candidate explains the log for " + url
    assert same([f["tree"] for f in found]), "log leaves %s open" % url
    return found


# ------------------------------------------------------------------ rules for unlogged pages
def features(site, rel):
    name = rel.split("/")[-1]
    mode = site.files[rel][1]
    return {"dir": rel.rsplit("/", 1)[0] if "/" in rel else "", "name": name, "exts": tuple(name.split(".")[1:]),
            "user_x": bool(mode & 0o100), "prefix": next((p for p in PREFIXES if name.startswith(p)), "")}


SSI_RULES = {"off": lambda f: False, "on": lambda f: True, "xbithack": lambda f: f["user_x"]}


def fit(site, logged):
    """Per directory: the rule hypotheses consistent with every logged page there."""
    rules = {}
    for url, found in logged.items():
        rel = found[0]["rel"]
        f = features(site, rel)
        r = rules.setdefault(f["dir"], {"ssi": {(e, k) for e in {"html", "shtml", "htm"} for k in SSI_RULES},
                                        "charset": None, "scripting": None})
        ctype = site.mime(rel, site.dirconf(rel))[0]
        raw = site.files[rel][0]
        # SSI: only informative when processing changes the bytes
        if b"<!--#" in raw:
            seen = {x["processed"] for x in found}
            ext = f["exts"][-1]
            r["ssi"] = {(e, k) for (e, k) in r["ssi"] if e != ext or SSI_RULES[k](f) in seen}
        # declared charset, per extension group: which declarations give the decoding seen
        key = "tr" if "tr" in f["exts"] else ""
        cs = {c for c in HEADER_CHARSETS
              if any(dom.sniff(headers_for(ctype, c, True), x["body"])[0] == x["decoded"] for x in found)}
        r["charset"] = r["charset"] or {}
        r["charset"][key] = r["charset"].get(key, set(HEADER_CHARSETS)) & cs
        # scripting, per file-name prefix (only when the log tells on from off)
        trees_on_off = {x["scripting"] for x in found}
        if len(trees_on_off) == 1:
            r["scripting"] = r["scripting"] or {}
            r["scripting"].setdefault(f["prefix"], set()).add(trees_on_off.pop())
    return rules


def from_rules(site, url, rules):
    rel = candidate_files(site, urlsplit(url).path)
    assert len(rel) == 1, "directory index %s is not in the log" % url
    rel = rel[0]
    f = features(site, rel)
    r = rules.get(f["dir"])
    assert r is not None, "no logged page in " + f["dir"]
    ctype = site.mime(rel, site.dirconf(rel))[0]
    ext = f["exts"][-1]
    procs = {SSI_RULES[k](f) for (e, k) in r["ssi"] if e == ext}
    key = "tr" if "tr" in f["exts"] else ""
    charsets = r["charset"].get(key) or set(HEADER_CHARSETS)
    scripts = (r["scripting"] or {}).get(f["prefix"]) or {True, False}
    assert all(len(v) for v in (procs, charsets, scripts)), "contradictory rules for " + url
    trees = []
    for processed, body in bodies(site, rel):
        if processed not in procs and b"<!--#" in site.files[rel][0]:
            continue
        for c in charsets:
            for s in scripts:
                trees.append(dom.solve_page(headers_for(ctype, c, s), body))
    assert trees and same(trees), "rules leave %s open" % url
    return trees[0]


def main():
    srv = server.Server(open(os.path.join(DATA, "conf/httpd.conf")).read(),
                        open(os.path.join(DATA, "conf/mime.types")).read())
    site = server.Site(os.path.join(DATA, "site.tar"), srv)
    urls = open(os.path.join(DATA, "pages.txt")).read().split()
    log = read_log(os.path.join(DATA, "access.log"), urls)
    logged = {u: from_log(site, u, log[u]) for u in urls if u in log}
    rules = fit(site, logged)
    pages = {}
    for u in urls:
        pages[u] = logged[u][0]["tree"] if u in logged else from_rules(site, u, rules)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"pages": pages}, f, ensure_ascii=False)
    print("wrote", len(pages), "pages to", OUT, "(%d from the log, %d from fitted rules)" % (len(logged), len(urls) - len(logged)))


if __name__ == "__main__":
    main()
