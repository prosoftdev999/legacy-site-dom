"""What Apache httpd 2.4 sent for a URL: an emulation of the parts of core, mod_dir, mod_mime,
mod_headers and mod_include that this site's configuration uses.

The document root comes from site.tar (file bytes, permission bits and modification times);
server-wide settings come from httpd.conf and mime.types; per-directory settings come from the
.htaccess files on the way down to the file (AllowOverride All).
"""
import os, re, shlex, tarfile, time

DOCROOT_IN_TAR = "htdocs"


# ------------------------------------------------------------------ config reading
def directive_lines(text):
    """Logical lines of an Apache config file (comments dropped, continuations joined)."""
    out, buf = [], ""
    for line in text.splitlines():
        if line.rstrip().endswith("\\"):
            buf += line.rstrip()[:-1] + " "
            continue
        line = (buf + line).strip()
        buf = ""
        if line and not line.startswith("#"):
            out.append(line)
    return out


def words(line):
    lex = shlex.shlex(line, posix=True)
    lex.whitespace_split = True
    lex.commenters = ""
    lex.escapedquotes = '"'
    return list(lex)


class Server:
    def __init__(self, conf_text, mime_text, docroot_path="/usr/local/apache2/htdocs"):
        self.types = {}
        for line in mime_text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split()
                for ext in parts[1:]:
                    self.types[ext.lower()] = parts[0]
        self.add_type, self.add_charset, self.add_filter = {}, {}, {}
        self.default_charset = None
        self.directory_index = ["index.html"]
        self.options = set()
        stack = []
        for line in directive_lines(conf_text):
            if line.startswith("</"):
                stack.pop()
                continue
            if line.startswith("<"):
                stack.append(line.strip("<>"))
                continue
            w = words(line)
            name = w[0].lower()
            in_dir = [s for s in stack if s.lower().startswith(("directory", "files", "location"))]
            if in_dir:
                if in_dir[-1].strip() == 'Directory "%s"' % docroot_path and name == "options":
                    self.options = apply_options(set(), w[1:])
                continue
            if name == "addtype":
                for ext in w[2:]:
                    self.add_type[ext.lstrip(".").lower()] = w[1]
            elif name == "addcharset":
                for ext in w[2:]:
                    self.add_charset[ext.lstrip(".").lower()] = w[1].lower()
            elif name == "addoutputfilter":
                for ext in w[2:]:
                    self.add_filter.setdefault(ext.lstrip(".").lower(), set()).update(w[1].upper().split(";"))
            elif name == "adddefaultcharset":
                self.default_charset = None if w[1].lower() == "off" else ("iso-8859-1" if w[1].lower() == "on" else w[1])
            elif name == "directoryindex":
                self.directory_index = w[1:]


def apply_options(cur, args):
    if args and not any(a[0] in "+-" for a in args):
        cur = set()
    cur = set(cur)
    for a in args:
        if a.startswith("+"):
            cur.add(a[1:].lower())
        elif a.startswith("-"):
            cur.discard(a[1:].lower())
        else:
            cur.add(a.lower())
    if "all" in cur:
        cur |= {"indexes", "includes", "followsymlinks", "execcgi", "multiviews"}
    return cur


class DirConf:
    """Merged per-directory settings for one file."""

    def __init__(self, server):
        self.options = set(server.options)
        self.add_type = dict(server.add_type)
        self.add_charset = dict(server.add_charset)
        self.add_filter = {k: set(v) for k, v in server.add_filter.items()}
        self.default_charset = server.default_charset
        self.directory_index = list(server.directory_index)
        self.xbithack = "off"
        self.headers = []          # (action, name, value) in the order Apache applies them
        self.files_headers = []    # from <Files>/<FilesMatch> sections, applied after the directory ones

    def read_htaccess(self, text, filename):
        stack = []
        for line in directive_lines(text):
            if line.startswith("</"):
                stack.pop()
                continue
            if line.startswith("<"):
                w = words(line.strip("<>"))
                kind = w[0].lower()
                if kind == "filesmatch":
                    active = re.search(w[1], filename) is not None
                elif kind == "files":
                    active = w[1] == filename
                else:
                    raise ValueError("unsupported section " + line)
                stack.append(active)
                continue
            if stack and not all(stack):
                continue
            w = words(line)
            name = w[0].lower()
            if name == "header":
                args = w[1:]
                if args[0].lower() in ("always", "onsuccess"):
                    args = args[1:]
                act = args[0].lower()
                val = args[2] if len(args) > 2 else None
                (self.files_headers if stack else self.headers).append((act, args[1], val))
            elif name == "adddefaultcharset":
                self.default_charset = None if w[1].lower() == "off" else ("iso-8859-1" if w[1].lower() == "on" else w[1])
            elif name == "addcharset":
                for ext in w[2:]:
                    self.add_charset[ext.lstrip(".").lower()] = w[1].lower()
            elif name == "addtype":
                for ext in w[2:]:
                    self.add_type[ext.lstrip(".").lower()] = w[1]
            elif name == "xbithack":
                self.xbithack = w[1].lower()
            elif name == "options":
                self.options = apply_options(self.options, w[1:])
            elif name == "directoryindex":
                self.directory_index = w[1:]
            else:
                raise ValueError("unsupported directive " + line)


# ------------------------------------------------------------------ the document root
class Site:
    def __init__(self, tar_path, server):
        self.server = server
        self.files = {}
        self.dirs = set([""])
        with tarfile.open(tar_path) as tf:
            for m in tf.getmembers():
                name = m.name.lstrip("./")
                if not (name == DOCROOT_IN_TAR or name.startswith(DOCROOT_IN_TAR + "/")):
                    continue
                rel = name[len(DOCROOT_IN_TAR):].lstrip("/")
                if m.isdir():
                    self.dirs.add(rel)
                elif m.isfile():
                    self.files[rel] = (tf.extractfile(m).read(), m.mode, m.mtime)

    def dirconf(self, rel_file):
        parts = rel_file.split("/")
        conf = DirConf(self.server)
        fname = parts[-1]
        for i in range(len(parts)):
            d = "/".join(parts[:i])
            ht = (d + "/.htaccess") if d else ".htaccess"
            if ht in self.files:
                conf.read_htaccess(self.files[ht][0].decode("utf-8"), fname)
        return conf

    def resolve(self, url_path):
        rel = url_path.lstrip("/")
        if rel == "" or rel.endswith("/"):
            d = rel.rstrip("/")
            conf = self.dirconf((d + "/x") if d else "x")
            for name in conf.directory_index:
                cand = (d + "/" + name) if d else name
                if cand in self.files:
                    return cand
            return None
        return rel if rel in self.files else None

    def mime(self, rel, conf):
        exts = [e.lower() for e in rel.split("/")[-1].split(".")[1:]]
        ctype, charset, filters = None, None, set()
        for e in exts:
            if e in conf.add_type:
                ctype = conf.add_type[e]
            elif e in self.server.types:
                ctype = self.server.types[e]
            if e in conf.add_charset:
                charset = conf.add_charset[e]
            filters |= conf.add_filter.get(e, set())
        return ctype, charset, filters

    def parses_ssi(self, rel, conf, ctype, filters):
        if "includes" not in conf.options and "includesnoexec" not in conf.options:
            return False
        if "INCLUDES" in filters:
            return True
        mode = self.files[rel][1]
        return conf.xbithack in ("on", "full") and ctype == "text/html" and bool(mode & 0o100)

    def get(self, url_path):
        rel = self.resolve(url_path)
        if rel is None:
            return None
        conf = self.dirconf(rel)
        ctype, charset, filters = self.mime(rel, conf)
        body = self.files[rel][0]
        if self.parses_ssi(rel, conf, ctype, filters):
            body = SSI(self, rel).run()
        headers = []
        if ctype:
            if charset is None and conf.default_charset and ctype in ("text/html", "text/plain"):
                charset = conf.default_charset
            headers.append(("content-type", ctype + ("; charset=" + charset if charset else "")))
        table = {}
        order = []
        for act, name, val in conf.headers + conf.files_headers:
            key = name.lower()
            if act == "set":
                table[key] = (name, val)
                if key not in order:
                    order.append(key)
            elif act == "unset":
                table.pop(key, None)
            elif act in ("append", "merge"):
                if key in table:
                    cur = table[key][1]
                    if act == "merge" and val in [t.strip() for t in cur.split(",")]:
                        continue
                    table[key] = (table[key][0], cur + ", " + val)
                else:
                    table[key] = (name, val)
                    if key not in order:
                        order.append(key)
        for key in order:
            if key in table:
                headers.append((key, table[key][1]))
        return headers, body


# ------------------------------------------------------------------ mod_include
DIRECTIVE = re.compile(rb"<!--#\s*([a-zA-Z]+)((?:[^-]|-(?!->))*?)\s*-->", re.S)
ATTR = re.compile(rb"\s*([a-zA-Z_]+)\s*=\s*(\"((?:[^\"\\]|\\.)*)\"|'((?:[^'\\]|\\.)*)')", re.S)
DEFAULT_TIMEFMT = "%A, %d-%b-%Y %H:%M:%S %Z"
DEFAULT_ERRMSG = b"[an error occurred while processing this directive]"


def html_escape(b):
    return b.replace(b"&", b"&amp;").replace(b"<", b"&lt;").replace(b">", b"&gt;").replace(b'"', b"&quot;")


class SSI:
    def __init__(self, site, rel):
        self.site, self.rel = site, rel
        self.vars = {}
        self.timefmt = DEFAULT_TIMEFMT
        self.errmsg = DEFAULT_ERRMSG
        self.main_mtime = site.files[rel][2]
        self.vars["DOCUMENT_URI"] = ("/" + rel).encode()
        self.vars["DOCUMENT_NAME"] = rel.split("/")[-1].encode()

    def strftime(self, t):
        return time.strftime(self.timefmt, time.gmtime(t)).replace("GMT", "UTC").encode()

    def var(self, name):
        if name == "LAST_MODIFIED":
            return self.strftime(self.main_mtime)
        return self.vars.get(name)

    def substitute(self, val):
        out = b""
        i = 0
        while i < len(val):
            c = val[i:i + 1]
            if c == b"\\" and i + 1 < len(val):
                out += val[i + 1:i + 2]
                i += 2
                continue
            if c == b"$":
                m = re.match(rb"\$(?:\{([A-Za-z0-9_]+)\}|([A-Za-z0-9_]+))", val[i:])
                if m:
                    v = self.var((m.group(1) or m.group(2)).decode())
                    out += v if v is not None else b""
                    i += m.end()
                    continue
            out += c
            i += 1
        return out

    def expr(self, e, rel):
        e = e.decode().strip()
        request_uri = "/" + rel

        def operand(s):
            s = s.strip()
            m = re.fullmatch(r"v\('([^']*)'\)", s)
            if m:
                v = self.var(m.group(1))
                return v.decode("latin-1") if v is not None else ""
            m = re.fullmatch(r"%\{([A-Z_]+)\}", s)
            if m:
                return request_uri if m.group(1) == "REQUEST_URI" else ""
            m = re.fullmatch(r"'([^']*)'", s)
            if m:
                return m.group(1)
            raise ValueError("expr operand " + s)

        m = re.fullmatch(r"-n\s+(.+)", e)
        if m:
            return operand(m.group(1)) != ""
        m = re.fullmatch(r"(.+?)\s*=~\s*(?:m(.)(.*)\2|/(.*)/)", e)
        if m:
            pat = m.group(3) if m.group(2) else m.group(4)
            return re.search(pat, operand(m.group(1))) is not None
        m = re.fullmatch(r"(.+?)\s*(==|!=)\s*(.+)", e)
        if m:
            a, b = operand(m.group(1)), operand(m.group(3))
            return (a == b) if m.group(2) == "==" else (a != b)
        raise ValueError("expr " + e)

    def include(self, rel):
        if rel not in self.site.files:
            return self.errmsg
        conf = self.site.dirconf(rel)
        ctype, charset, filters = self.site.mime(rel, conf)
        data = self.site.files[rel][0]
        if self.site.parses_ssi(rel, conf, ctype, filters):
            return self.process(data, rel)
        return data

    def process(self, data, rel):
        out = []
        pos = 0
        conds = []  # stack of [taken_any, active]
        active = lambda: all(c[1] for c in conds)
        for m in DIRECTIVE.finditer(data):
            if active():
                out.append(data[pos:m.start()])
            pos = m.end()
            cmd = m.group(1).decode().lower()
            attrs = [(a.group(1).decode().lower(), a.group(3) if a.group(3) is not None else a.group(4)) for a in ATTR.finditer(m.group(2))]
            if cmd == "if":
                ok = active() and self.expr(dict(attrs)["expr"], rel)
                conds.append([ok, ok])
                continue
            if cmd == "elif":
                c = conds[-1]
                outer = all(x[1] for x in conds[:-1])
                if c[0] or not outer:
                    c[1] = False
                else:
                    c[1] = self.expr(dict(attrs)["expr"], rel)
                    c[0] = c[1]
                continue
            if cmd == "else":
                c = conds[-1]
                c[1] = not c[0]
                c[0] = True
                continue
            if cmd == "endif":
                conds.pop()
                continue
            if not active():
                continue
            if cmd == "set":
                d = dict(attrs)
                self.vars[d["var"].decode()] = self.substitute(d["value"])
            elif cmd == "echo":
                enc = b"entity"
                for k, v in attrs:
                    if k == "encoding":
                        enc = v.lower()
                    elif k == "var":
                        val = self.var(v.decode())
                        if val is None:
                            val = b"(none)"
                        out.append(html_escape(val) if enc == b"entity" else val)
            elif cmd == "config":
                for k, v in attrs:
                    if k == "timefmt":
                        self.timefmt = v.decode()
                    elif k == "errmsg":
                        self.errmsg = v
            elif cmd == "include":
                for k, v in attrs:
                    if k == "virtual":
                        target = v.decode().split("?")[0]
                        target = target.lstrip("/") if target.startswith("/") else os.path.normpath(os.path.join(os.path.dirname(rel), target))
                    elif k == "file":
                        target = os.path.normpath(os.path.join(os.path.dirname(rel), v.decode()))
                    else:
                        continue
                    out.append(self.include(target))
            else:
                out.append(self.errmsg)
        if active():
            out.append(data[pos:])
        return b"".join(out)

    def run(self):
        return self.process(self.site.files[self.rel][0], self.rel)
