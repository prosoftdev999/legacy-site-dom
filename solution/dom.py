"""Turn an HTTP response (headers and body bytes) into the DOM tree Chromium built from it.

Per response: choose the encoding the way a browser does (byte order mark, then the
Content-Type charset, then a <meta> prescan of the first 1024 bytes, every label resolved
through the WHATWG Encoding Standard table, a utf-16 <meta> label meaning UTF-8, windows-1252
when nothing is declared), decode, normalise newlines, and run the HTML tree construction
algorithm (vendored html5lib 1.1, two bugs patched below) with the scripting flag the page
really had. Scripting is off in a document whose enforced Content-Security-Policy carries a
sandbox directive without allow-scripts (a header can hold several comma-separated policies);
a srcdoc document has it off when its iframe is sandboxed without allow-scripts or when its
parent document has it off. Declarative shadow roots are attached from the first
<template shadowrootmode=open|closed> child of an element that may host a shadow root; any
other such template stays an ordinary template. The output drops text nodes made only of
ASCII whitespace and joins adjacent text.
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vendor"))
import webencodings  # noqa: E402
import html5lib  # noqa: E402
from html5lib import _inputstream  # noqa: E402
from html5lib.html5parser import getPhases  # noqa: E402
from html5lib.treebuilders import base as tb  # noqa: E402


# ------------------------------------------------------------------ encoding
def lookup(label):
    if label is None:
        return None
    e = webencodings.lookup(label)
    return e.name if e is not None else None


def header_charset(headers):
    ct = [v for k, v in headers if k == "content-type"]
    if not ct:
        return None
    for part in ct[-1].split(";")[1:]:
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip().lower() == "charset":
                return v.strip().strip('"').strip("'")
    return None


def sniff(headers, body):
    if body.startswith(b"\xef\xbb\xbf"):
        return "utf-8", body[3:]
    if body.startswith(b"\xfe\xff"):
        return "utf-16be", body[2:]
    if body.startswith(b"\xff\xfe"):
        return "utf-16le", body[2:]
    name = lookup(header_charset(headers))
    if name is None:
        e = _inputstream.EncodingParser(body[:1024]).getEncoding()
        name = e.name if e is not None else None
        if name in ("utf-16be", "utf-16le"):
            name = "utf-8"
        if name == "x-user-defined":
            name = "windows-1252"
    return name or "windows-1252", body


def decode(name, body):
    if name in ("utf-8", "utf-16be", "utf-16le"):
        return body.decode(name, "replace")
    codec = webencodings.lookup(name).codec_info.name
    out = []
    for b in body:
        if b < 0x80:
            out.append(chr(b))
            continue
        try:
            out.append(bytes([b]).decode(codec))
        except UnicodeDecodeError:
            out.append(chr(b))  # bytes the index leaves unassigned map to the C1 control of the same value
    return "".join(out)


def newlines(s):
    return s.replace("\r\n", "\n").replace("\r", "\n")


# ------------------------------------------------------------------ tree builder
class Node(tb.Node):
    def __init__(self, name, kind="element"):
        super().__init__(name)
        self.kind = kind
        self.namespace = None
        self.shadow = None

    def _text(self, data):
        t = Node(None, "text")
        t.value = data
        t.parent = self
        return t

    def appendChild(self, node):
        if node.kind == "text" and self.childNodes and self.childNodes[-1].kind == "text":
            self.childNodes[-1].value += node.value
            return
        node.parent = self
        self.childNodes.append(node)

    def insertText(self, data, insertBefore=None):
        if insertBefore is None:
            if self.childNodes and self.childNodes[-1].kind == "text":
                self.childNodes[-1].value += data
            else:
                self.childNodes.append(self._text(data))
        else:
            i = self.childNodes.index(insertBefore)
            if i > 0 and self.childNodes[i - 1].kind == "text":
                self.childNodes[i - 1].value += data
            else:
                self.childNodes.insert(i, self._text(data))

    def insertBefore(self, node, refNode):
        i = self.childNodes.index(refNode)
        if node.kind == "text" and i > 0 and self.childNodes[i - 1].kind == "text":
            self.childNodes[i - 1].value += node.value
            return
        node.parent = self
        self.childNodes.insert(i, node)

    def removeChild(self, node):
        self.childNodes.remove(node)
        node.parent = None

    def reparentChildren(self, newParent):
        for c in list(self.childNodes):
            newParent.appendChild(c)
        self.childNodes = []

    def cloneNode(self):
        n = Node(self.name, self.kind)
        n.namespace = self.namespace
        n.attributes = dict(self.attributes)
        n.value = self.value
        return n

    def hasContent(self):
        return bool(self.childNodes)

    @property
    def nameTuple(self):
        return (self.namespace or "http://www.w3.org/1999/xhtml", self.name)


class Element(Node):
    def __init__(self, name, namespace=None):
        super().__init__(name, "element")
        self.namespace = namespace


class Comment(Node):
    def __init__(self, data):
        super().__init__(None, "comment")
        self.value = data


class Doctype(Node):
    def __init__(self, name, publicId, systemId):
        super().__init__(name, "doctype")
        self.publicId = publicId or ""
        self.systemId = systemId or ""


class Document(Node):
    def __init__(self):
        super().__init__(None, "document")


class Builder(tb.TreeBuilder):
    documentClass = Document
    elementClass = Element
    commentClass = Comment
    doctypeClass = Doctype
    fragmentClass = Document

    def testSerializer(self, node):
        raise NotImplementedError

    def getDocument(self):
        return self.document


# ------------------------------------------------------------------ html5lib patches
_Initial = getPhases(False)["initial"]
_orig_anything = _Initial.anythingElse


def _anything(self):
    # An iframe srcdoc document without a doctype is not put in quirks mode.
    if getattr(self.parser, "srcdoc_doc", False):
        self.parser.phase = self.parser.phases["beforeHtml"]
        return
    _orig_anything(self)


_Initial.anythingElse = _anything


# html5lib 1.1 inserts whitespace in table cells and captions without going through the
# "in body" rules, so it skips reconstructing formatting elements and the <pre> newline drop.
def _space_via_body(self, token):
    return self.parser.phases["inBody"].processSpaceCharacters(token)


for _name in ("inCell", "inCaption"):
    getPhases(False)[_name].processSpaceCharacters = _space_via_body


# ------------------------------------------------------------------ policies
def scripting_off_by_csp(headers):
    for k, v in headers:
        if k != "content-security-policy":
            continue
        for policy in v.split(","):
            for directive in policy.split(";"):
                toks = directive.strip().split()
                if toks and toks[0].lower() == "sandbox":
                    if "allow-scripts" not in [t.lower() for t in toks[1:]]:
                        return True
    return False


SHADOW_HOSTS = {"article", "aside", "blockquote", "body", "div", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
                "header", "main", "nav", "p", "section", "span"}
RESERVED = {"annotation-xml", "color-profile", "font-face", "font-face-src", "font-face-uri", "font-face-format",
            "font-face-name", "missing-glyph"}


def may_host_shadow(name):
    if name in SHADOW_HOSTS:
        return True
    return "-" in name and name[0].isalpha() and name.islower() and name not in RESERVED


def attach_declarative_shadow_roots(node):
    for c in list(node.childNodes):
        if c.kind == "element":
            attach_declarative_shadow_roots(c)
    if node.kind != "element":
        return
    for c in list(node.childNodes):
        if c.kind == "element" and c.name == "template":
            mode = c.attributes.get("shadowrootmode")
            if mode is None or mode.lower() not in ("open", "closed"):
                continue
            if not may_host_shadow(node.name) or node.shadow is not None:
                continue
            node.shadow = (mode.lower(), c.childNodes)
            node.removeChild(c)


# ------------------------------------------------------------------ parse + dump
def parse(text, scripting, srcdoc=False):
    p = html5lib.HTMLParser(tree=Builder, namespaceHTMLElements=False)
    p.srcdoc_doc = srcdoc
    doc = p.parse(text, scripting=scripting)
    attach_declarative_shadow_roots(doc)
    return doc


def is_ws(s):
    return all(c in " \t\n\f\r" for c in s)


def dump_children(nodes, scripting):
    out = []
    for n in nodes:
        if n.kind == "text":
            if is_ws(n.value):
                continue
            if out and isinstance(out[-1], str):
                out[-1] += n.value
            else:
                out.append(n.value)
        elif n.kind == "comment":
            out.append({"comment": n.value})
        elif n.kind == "doctype":
            out.append({"doctype": n.name, "public": n.publicId, "system": n.systemId})
        else:
            out.append(dump_element(n, scripting))
    return out


def dump_element(n, scripting):
    o = {"tag": n.name, "attrs": dict(n.attributes)}
    if n.name == "template":
        o["children"] = []
        o["content"] = dump_children(n.childNodes, scripting)
    else:
        o["children"] = dump_children(n.childNodes, scripting)
    if n.shadow is not None:
        o["shadow"] = {"mode": n.shadow[0], "children": dump_children(n.shadow[1], scripting)}
    if n.name == "iframe" and "srcdoc" in n.attributes:
        sandbox = n.attributes.get("sandbox")
        frame_scripting = scripting
        if sandbox is not None and "allow-scripts" not in sandbox.lower().split():
            frame_scripting = False
        doc = parse(newlines(n.attributes["srcdoc"]), frame_scripting, srcdoc=True)
        o["frame"] = dump_children(doc.childNodes, frame_scripting)
    return o


def solve_page(headers, body):
    name, body = sniff(headers, body)
    text = newlines(decode(name, body))
    scripting = not scripting_off_by_csp(headers)
    return dump_children(parse(text, scripting).childNodes, scripting)
