# Legacy Site DOM

A legacy-web migration and browser-DOM reconstruction project focused on reproducing the exact document trees Chromium built from an old Apache HTTP Server site.

The original site used Apache server-side includes, historical server configuration, directory-specific behavior, multiple character encodings, and browser-side scripting. During a migration to static hosting, the objective is to reconstruct the DOM that users' browsers actually received rather than simply copying the source files.

## Overview

A legacy page can pass through several transformations before it becomes a browser DOM:

```text
source file
    ↓
Apache configuration
    ↓
server-side includes
    ↓
response headers / charset
    ↓
CSP and scripting state
    ↓
HTML byte decoding
    ↓
Chromium HTML parser
    ↓
templates / shadow DOM / frames
    ↓
final DOM tree
```

The task reconstructs this complete path for every archived URL.

## Repository Structure

```text
legacy-site-dom/
├── environment/
├── solution/
├── tests/
├── instruction.md
└── task.toml
```

### `instruction.md`

Defines the migration objective, input evidence, DOM serialization format, and required output.

### `environment/`

Contains the archived site, server configuration, request history, browser evidence, and reproducible runtime.

### `solution/`

Contains the reference reconstruction implementation.

### `tests/`

Contains the independent verifier.

### `task.toml`

Defines task metadata and execution configuration.

## Input Evidence

The reconstruction uses several evidence sources:

```text
/app/data/pages.txt
/app/data/site.tar
/app/data/conf/
/app/data/access.log
/app/data/README.md
/app/data/known_trees.json
```

`pages.txt` lists the URLs that must be reconstructed.

`site.tar` contains the historical document root.

`conf/` contains the Apache server configuration and MIME information.

`access.log` contains request evidence from the browser-capture session.

`known_trees.json` contains reference DOM trees for the first set of URLs and provides useful calibration evidence.

## Apache Server Behavior

The historical site ran on Apache httpd and used server-side includes.

Reconstructing the response body can depend on behavior such as:

- SSI enablement
- `XBitHack`
- file permissions
- MIME types
- directory-specific configuration
- response character sets
- include processing

The browser DOM can only be reproduced after the server response itself has been reconstructed correctly.

## Server-Side Includes

SSI directives can transform the HTML before it reaches Chromium.

Relevant operations may include:

```text
include
echo
set
if
elif
else
config
```

An included file can itself affect the resulting markup, text content, comments, or structure.

The final DOM therefore cannot always be derived directly from the archived `.html` file.

## Character Encoding

Different directories and file types may use different declared character sets.

Character decoding matters because Chromium parses bytes, not abstract Unicode source text.

Conceptually:

```text
response bytes
      +
declared / detected encoding
      ↓
decoded characters
      ↓
HTML tokenizer
      ↓
DOM
```

Choosing the wrong encoding can change:

- text nodes
- attribute values
- URLs
- percent-encoded requests
- parser behavior

The task includes historical evidence that helps recover the effective charset rules.

## Content Security Policy and Scripting

Some pages were served under scripting restrictions.

Whether scripting is enabled affects HTML parsing itself in cases such as:

```html
<noscript>
```

The same HTML bytes can therefore produce different document structures depending on the page's scripting state.

The reconstruction must reproduce the state Chromium used during the archived session.

## HTML Parsing

The expected result is the DOM Chromium built, not a cleaned or normalized interpretation of the source markup.

That distinction matters for malformed or legacy HTML.

Browser parsing behavior can involve:

- implied elements
- foster parenting
- adoption-agency behavior
- raw-text parsing
- `noscript`
- templates
- encoding sniffing
- HTML error recovery

Using a generic XML parser or simplistic HTML parser will not reliably reproduce Chromium's tree.

## Template Contents

A `<template>` element has a separate template contents fragment.

The serialized representation therefore distinguishes:

```json
{
  "tag": "template",
  "attrs": {},
  "children": [],
  "content": []
}
```

The template's DOM children belong under `content`, not the element's ordinary `children` array.

This is important for the site's client-side CMS templates.

## Declarative Shadow DOM

Some archived pages contain declarative shadow-root templates.

When Chromium creates a shadow root, the serialized element includes:

```json
{
  "shadow": {
    "mode": "open",
    "children": []
  }
}
```

or:

```json
{
  "shadow": {
    "mode": "closed",
    "children": []
  }
}
```

Internal browser-created shadow roots for controls are not part of the required output.

## `srcdoc` Frames

An iframe using `srcdoc` embeds another HTML document.

For those elements the result includes a separate `frame` tree:

```json
{
  "tag": "iframe",
  "attrs": {
    "srcdoc": "..."
  },
  "children": [],
  "frame": []
}
```

The embedded document must be parsed under its own HTML document semantics.

## DOM Serialization

The solver writes:

```text
/app/output/page_dom.json
```

with this top-level shape:

```json
{
  "pages": {}
}
```

Every URL from `pages.txt` must be present exactly as listed.

Each value is the list of child nodes belonging to that document.

### Elements

```json
{
  "tag": "p",
  "attrs": {
    "class": "lead"
  },
  "children": [
    "Example text"
  ]
}
```

### Text nodes

Text nodes are plain JSON strings:

```json
"Example text"
```

### Comments

```json
{
  "comment": " navigation "
}
```

### Doctypes

```json
{
  "doctype": "html",
  "public": "",
  "system": ""
}
```

The exact serialization contract is defined in `instruction.md`.

## Text Normalization

Before comparison, adjacent text nodes are joined.

Text nodes containing only:

```text
space
tab
line feed
form feed
carriage return
```

are then removed.

Other text remains significant and must match exactly.

## Historical Evidence Recovery

Not every page has a directly known DOM tree.

The task therefore combines several forms of evidence:

```text
known browser trees
        +
Apache configuration
        +
historical access log
        +
site files
        ↓
recover server behavior
        ↓
apply recovered rules
        ↓
reconstruct remaining pages
```

The captured access log can help distinguish server behaviors through response sizes and secondary resource requests.

## Scale

The archived site contains 1,649 target URLs.

The reference data includes known trees for the first 60 URLs, while historical server evidence is used to recover the behavior needed for the remaining pages.

The task also includes hundreds of pages with template content and declarative shadow-DOM-related behavior, making browser-accurate parsing important rather than optional.

## Common Failure Modes

### Parsing the raw file directly

Apache SSI may have changed the response before Chromium received it.

### Assuming one charset everywhere

Directory and extension-specific rules can change the response encoding.

### Ignoring scripting state

`noscript` parsing differs depending on whether scripting is enabled.

### Using an XML parser

HTML error recovery and implied-element rules differ substantially.

### Leaving `<template>` contents empty

Template contents live in a separate document fragment and remain part of the expected tree.

### Ignoring declarative shadow DOM

Some templates become shadow roots while others remain ordinary template elements.

### Treating every unlogged page with one default Apache configuration

Historical directory overrides can produce different responses.

### Ignoring `srcdoc`

The iframe's embedded document is part of the required serialized result.

## Reconstruction Pipeline

A high-level implementation can be viewed as:

```text
site archive
      +
Apache configuration
      +
request log
      +
known DOM trees
      ↓
recover server rules
      ↓
build historical response bytes
      ↓
determine charset and scripting state
      ↓
apply Chromium-compatible HTML parsing
      ↓
recover template / shadow / frame content
      ↓
normalize required text nodes
      ↓
write page_dom.json
```

## Technical Areas

This project exercises:

- HTML parsing
- browser DOM behavior
- Apache HTTP Server
- server-side includes
- legacy web migration
- character encoding
- Content Security Policy
- `noscript`
- template document fragments
- declarative shadow DOM
- `iframe srcdoc`
- Chrome DevTools Protocol
- historical-state reconstruction
- frontend reverse engineering
- Python

## Goal

Legacy Site DOM provides a way to verify a static-site migration against the browser-visible behavior of the original server.

The central principle is:

> Preserve the document Chromium actually built, not merely the HTML files that happened to exist on disk.

## License

No license is currently specified.
