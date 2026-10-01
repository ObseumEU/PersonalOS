"""Visual files made safe to show (docs/FILES.md, "Visuals"): SVG sanitising, Graphviz DOT
rendered to SVG, and the checks that tell an agent at once that its diagram or chart is broken.

- SVG (uploaded, or made by an agent) is rewritten on the way in: only drawing elements of
  the SVG namespace stay; scripts, foreignObject, animations (which can rewrite links), event
  handlers, `javascript:` and every reference outside the document (href, url(), @import) are
  dropped. A DOCTYPE with an internal subset (entities) is refused before parsing.
- DOT is rendered by the `dot` program (graphviz, in the api image) with a time limit, in an
  empty directory, refusing the attributes that read local files (image, shapefile, …);
  its output goes through the same SVG sanitiser. Renders are cached by content hash.
- Mermaid and Vega-Lite render in the browser; here they are only checked (a Vega-Lite spec
  must be JSON and carry its data inline, never a URL).
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XML_NS = "http://www.w3.org/XML/1998/namespace"
MAX_SVG = 5 * 1024 * 1024
MAX_DOT = 1024 * 1024
DOT_TIMEOUT = 20

ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", XLINK_NS)


class RenderError(ValueError):
    """The content cannot be shown: say why, in a line an agent can act on."""


# ------------------------------------------------------------------ SVG

ELEMENTS = {
    "svg", "g", "defs", "symbol", "use", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan", "textPath", "title", "desc", "marker", "linearGradient", "radialGradient", "stop",
    "clipPath", "mask", "pattern", "image", "style", "switch", "filter", "feBlend", "feColorMatrix",
    "feComponentTransfer", "feComposite", "feConvolveMatrix", "feDiffuseLighting", "feDisplacementMap",
    "feDistantLight", "feDropShadow", "feFlood", "feFuncA", "feFuncB", "feFuncG", "feFuncR", "feGaussianBlur",
    "feMerge", "feMergeNode", "feMorphology", "feOffset", "fePointLight", "feSpecularLighting", "feSpotLight",
    "feTile", "feTurbulence",
}
# A link is kept as a plain group: its content shows, nothing navigates (graphviz emits <a> for URL=).
UNWRAP = {"a"}
_DATA_IMAGE = re.compile(r"^data:image/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+$", re.I)
_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)
_BAD_CSS = re.compile(r"@import|expression\s*\(|javascript:|behavior\s*:|-moz-binding", re.I)


def _local(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        ns, local = tag[1:].split("}", 1)
        return ns, local
    return "", tag


def _clean_css(text: str) -> str:
    text = re.sub(r"@import[^;]*;?", "", text, flags=re.I)
    text = _URL.sub(lambda m: m.group(0) if m.group(2).strip().startswith("#") else "none", text)
    return "" if _BAD_CSS.search(text) else text


def _safe_ref(value: str, *, image: bool) -> bool:
    v = value.strip()
    return v.startswith("#") or (image and bool(_DATA_IMAGE.match(v)))


def _clean_attrs(el: ET.Element, local: str) -> None:
    for key in list(el.attrib):
        ns, name = _local(key)
        value = el.attrib[key]
        drop = False
        if ns not in ("", XLINK_NS, XML_NS):
            drop = True  # editor namespaces (inkscape, sodipodi) and anything unknown
        elif name.lower().startswith("on"):
            drop = True  # event handlers
        elif name == "href":
            drop = not _safe_ref(value, image=local == "image")
        elif "javascript:" in value.lower().replace(" ", "") or "data:text" in value.lower():
            drop = True
        elif name == "style":
            cleaned = _clean_css(value)
            if cleaned != value:
                el.attrib[key] = cleaned
        elif "url(" in value.lower():
            if any(not m.group(2).strip().startswith("#") for m in _URL.finditer(value)):
                drop = True
        if drop:
            del el.attrib[key]


def _walk(el: ET.Element, default_ns: str) -> None:
    for child in list(el):
        if not isinstance(child.tag, str):  # comments, processing instructions
            el.remove(child)
            continue
        ns, local = _local(child.tag)
        if ns not in (SVG_NS, default_ns) or (local not in ELEMENTS and local not in UNWRAP):
            el.remove(child)
            continue
        if local in UNWRAP:
            local = "g"
            for key in list(child.attrib):
                if _local(key)[1] in ("href", "target", "title") or key.startswith(f"{{{XLINK_NS}}}"):
                    del child.attrib[key]
        child.tag = f"{{{SVG_NS}}}{local}"
        _clean_attrs(child, local)
        if local == "style":
            child.text = _clean_css(child.text or "")
            for sub in list(child):
                child.remove(sub)
            continue
        _walk(child, default_ns)


def sanitize_svg(data: bytes) -> bytes:
    """A safe copy of an SVG document, or RenderError when it is not one."""
    if len(data) > MAX_SVG:
        raise RenderError(f"the SVG is larger than {MAX_SVG // (1024 * 1024)} MB")
    text = data.decode("utf-8-sig", errors="replace")
    if re.search(r"<!ENTITY", text, re.I) or re.search(r"<!DOCTYPE[^>]*\[", text, re.I):
        raise RenderError("the SVG declares entities (a DOCTYPE with an internal subset); remove the DOCTYPE")
    text = re.sub(r"<!DOCTYPE[^>]*>", "", text, flags=re.I)
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise RenderError(f"the SVG is not well-formed XML: {e}") from e
    ns, local = _local(root.tag)
    if local != "svg" or ns not in (SVG_NS, ""):
        raise RenderError("the document's root element is not <svg>")
    root.tag = f"{{{SVG_NS}}}svg"
    _clean_attrs(root, "svg")
    _walk(root, ns)
    out = ET.tostring(root, encoding="unicode")
    return out.encode("utf-8")


def looks_like_svg(text: str) -> bool:
    head = re.sub(r"^﻿?\s*(<\?xml[^>]*\?>\s*)?((<!--.*?-->|<!DOCTYPE[^>]*>)\s*)*", "", text[:4096], flags=re.S | re.I)
    return head.lower().startswith("<svg")


# ------------------------------------------------------------------ Graphviz DOT

_DOT_FILES = re.compile(r"\b(image|imagepath|imagescale|shapefile|fontpath|fontnames|stylesheet)\s*=", re.I)
_DOT_IMG = re.compile(r"<\s*img\b", re.I)


def dot_available() -> bool:
    return shutil.which("dot") is not None


def render_dot(source: bytes) -> bytes:
    """DOT source → a sanitised SVG. RenderError says what is wrong (graphviz's own message)."""
    if len(source) > MAX_DOT:
        raise RenderError(f"the DOT source is larger than {MAX_DOT // 1024} KB")
    text = source.decode("utf-8", errors="replace")
    if _DOT_FILES.search(text) or _DOT_IMG.search(text):
        raise RenderError("DOT attributes that read files (image, shapefile, fontpath, stylesheet, <IMG>) "
                          "are not allowed")
    exe = shutil.which("dot")
    if exe is None:
        raise RenderError("graphviz (dot) is not installed on this server")
    with tempfile.TemporaryDirectory(prefix="pos-dot-") as tmp:
        env = {"PATH": os.environ.get("PATH", ""), "GV_FILE_PATH": tmp, "SERVER_NAME": "pos", "HOME": tmp}
        try:
            proc = subprocess.run([exe, "-Tsvg"], input=source, capture_output=True, timeout=DOT_TIMEOUT,
                                  cwd=tmp, env=env, check=False)
        except subprocess.TimeoutExpired as e:
            raise RenderError(f"graphviz took longer than {DOT_TIMEOUT} s; make the graph smaller") from e
    if proc.returncode != 0 or not proc.stdout:
        msg = proc.stderr.decode("utf-8", errors="replace").strip().replace("\n", " ")
        raise RenderError(f"graphviz could not render it: {msg[:400] or 'no output'}")
    return sanitize_svg(proc.stdout)


def render_dot_cached(files_dir: Path, sha: str, path: Path) -> bytes:
    """The rendered SVG of a stored DOT file, cached under files_dir/.render by content hash."""
    cache = files_dir / ".render" / f"{sha}.svg"
    if cache.is_file():
        return cache.read_bytes()
    err = files_dir / ".render" / f"{sha}.err"
    if err.is_file():
        raise RenderError(err.read_text(encoding="utf-8"))
    cache.parent.mkdir(parents=True, exist_ok=True)
    try:
        svg = render_dot(path.read_bytes())
    except RenderError as e:
        if "not installed" not in str(e):
            err.write_text(str(e), encoding="utf-8")
        raise
    tmp = cache.with_suffix(".tmp")
    tmp.write_bytes(svg)
    os.replace(tmp, cache)
    return svg


# ------------------------------------------------------------------ checks for client-side renderers

MERMAID_KINDS = (
    "graph", "flowchart", "sequenceDiagram", "classDiagram", "stateDiagram", "stateDiagram-v2", "erDiagram",
    "journey", "gantt", "pie", "quadrantChart", "requirementDiagram", "gitGraph", "C4Context", "C4Container",
    "C4Component", "C4Dynamic", "C4Deployment", "mindmap", "timeline", "zenuml", "sankey-beta", "xychart-beta",
    "block-beta", "packet-beta", "kanban", "architecture-beta", "radar-beta", "treemap-beta",
)


def check_mermaid(text: str) -> str | None:
    """A warning when the text does not start like a Mermaid diagram (rendering happens in the browser)."""
    body = re.sub(r"^\s*---\n.*?\n---\s*\n", "", text, flags=re.S)  # front matter
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("%%"):
            continue
        first = s.split()[0]
        if first in MERMAID_KINDS:
            return None
        return (f"the first line {first!r} is not a Mermaid diagram type (e.g. 'flowchart LR', "
                f"'sequenceDiagram', 'architecture-beta')")
    return "the Mermaid diagram is empty"


def _urls(node, path="spec") -> list[str]:
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "url" and isinstance(v, str):
                out.append(f"{path}.url")
            out += _urls(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out += _urls(v, f"{path}[{i}]")
    return out


def check_vegalite(data: bytes) -> None:
    """A Vega-Lite spec must be a JSON object with a view (mark, layer, concat, …) and inline data."""
    try:
        spec = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise RenderError(f"the Vega-Lite spec is not valid JSON: {e}") from e
    if not isinstance(spec, dict):
        raise RenderError("the Vega-Lite spec must be a JSON object")
    if not {"mark", "layer", "concat", "hconcat", "vconcat", "facet", "repeat", "spec"} & set(spec):
        raise RenderError("the Vega-Lite spec has no view: give it a mark (or layer, concat, facet, repeat)")
    if found := _urls(spec):
        raise RenderError(f"put the data inline (data.values), not as a URL: {', '.join(found[:3])}")
