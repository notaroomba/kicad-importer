"""Import parts from LCSC / DigiKey / Mouser / Newark links into a KiCad project library."""

import csv
import json
import logging
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .easyeda2kicad.easyeda.easyeda_api import EasyedaApi
from .easyeda2kicad.easyeda.easyeda_importer import (
    Easyeda3dModelImporter,
    EasyedaFootprintImporter,
    EasyedaSymbolImporter,
)
from .easyeda2kicad.kicad.export_kicad_3d_model import Exporter3dModelKicad
from .easyeda2kicad.kicad.export_kicad_footprint import ExporterFootprintKicad
from .easyeda2kicad.kicad.export_kicad_symbol import (
    KICAD_SYM_VERSIONS_SORTED,
    ExporterSymbolKicad,
)

LIB = "parts"
LIB_URI = "${KIPRJMOD}/lib/parts/parts"
MODEL_DIR = LIB_URI + ".3dshapes"
CSV_FIELDS = ["source", "url", "mpn", "lcsc_id", "symbol", "footprint", "imported_at"]
LCSC_RE = re.compile(r"(?<![A-Za-z0-9])(C\d+)(?![A-Za-z0-9])", re.I)

log = logging.getLogger("kicad-importer")


# ---------------- link parsing ----------------


def _guess(text):
    """Bare part numbers: LCSC C-number, DigiKey ...-ND, Mouser 595-..., or a plain MPN."""
    q = quote(text)
    if re.fullmatch(r"[Cc]\d+", text):
        return dict(source="lcsc", url="", mpn="", lcsc_id=text.upper())
    if re.search(r"-ND$", text, re.I):
        mpn = re.sub(r"(CT|TR|DKR)?-ND$", "", re.sub(r"^\d+-", "", text), flags=re.I)
        return dict(source="digikey", url="https://www.digikey.com/en/products/result?keywords=" + q, mpn=mpn, lcsc_id="")
    if re.fullmatch(r"\d{2,3}-\S+", text):
        return dict(source="mouser", url="https://www.mouser.com/c/?q=" + q, mpn=text.split("-", 1)[1], lcsc_id="")
    return dict(source="mpn", url="https://www.digikey.com/en/products/result?keywords=" + q, mpn=text, lcsc_id="")


def parse_link(text):
    """Return dict(source, url, mpn, lcsc_id) for a pasted link or part number."""
    text = text.strip()
    if not text:
        raise ValueError("Paste a link or a part number first")
    if "/" not in text and " " not in text and not re.search(r"\.(com|net|org|[a-z]{2})$", text, re.I):
        return _guess(text)
    url = text if "://" in text else "https://" + text
    p = urlparse(url)
    host = p.netloc.lower().split(":")[0]
    parts = [unquote(s) for s in p.path.split("/") if s]
    low = [s.lower() for s in parts]

    def site(name):
        return re.search(r"(^|\.)" + name + r"\.", host + ".")

    if site("lcsc") or site("jlcpcb"):
        ids = LCSC_RE.findall(p.path)
        if ids:
            return dict(source="lcsc", url=text, mpn="", lcsc_id=ids[-1].upper())
    elif site("digikey"):
        # the trailing number is DigiKey's product id, its CAD models live at /en/models/<id>
        pid = parts[-1] if parts[-1].isdigit() else ""
        cad = "https://%s/en/models/%s" % (host, pid) if pid else ""
        # /en/products/detail/<mfr>/<mpn>/<id>
        if "detail" in low and low.index("detail") + 2 < len(parts):
            return dict(source="digikey", url=text, mpn=parts[low.index("detail") + 2], lcsc_id="", cad_url=cad)
        # old format: /product-detail/en/<mfr>/<mpn>/<dk-pn>/<id>
        if "product-detail" in low and low.index("product-detail") + 3 < len(parts):
            return dict(source="digikey", url=text, mpn=parts[low.index("product-detail") + 3], lcsc_id="", cad_url=cad)
    elif site("mouser"):
        # /ProductDetail/<mfr>/<mpn>?qs=...  or  /ProductDetail/595-LM358DR
        if "productdetail" in low:
            rest = parts[low.index("productdetail") + 1:]
            if len(rest) >= 2:
                return dict(source="mouser", url=text, mpn=rest[1], lcsc_id="")
            if rest:
                return dict(source="mouser", url=text, mpn=re.sub(r"^\d{2,3}-", "", rest[0]), lcsc_id="")
    elif site("newark") or site("farnell") or site("element14"):
        # /<mfr>/<mpn>/<desc>/dp/<sku>
        if "dp" in low and low.index("dp") >= 3:
            return dict(source=host.split(".")[-2], url=text, mpn=parts[low.index("dp") - 2], lcsc_id="")
    raise ValueError("Don't know how to read this link: " + text)


# ---------------- LCSC lookup ----------------


def _norm(s):
    return re.sub(r"[\s/#-]", "", s or "").lower()


def find_lcsc(mpn, api):
    """Search JLCPCB for an MPN. Returns (exact_match_or_None, all_candidates)."""
    results = api.search_jlcpcb_components(mpn).get("results", [])
    exact = [r for r in results if _norm(r["model"]) == _norm(mpn)]
    exact.sort(key=lambda r: -(r.get("stock") or 0))
    return (exact[0] if exact else None), results


# ---------------- DigiKey API (optional) ----------------

DK_CONFIG = Path.home() / ".config" / "kicad-importer" / "digikey.json"


def _digikey_creds():
    """(client_id, client_secret) from DIGIKEY_CLIENT_ID/SECRET or ~/.config/kicad-importer/digikey.json."""
    cid, secret = os.environ.get("DIGIKEY_CLIENT_ID"), os.environ.get("DIGIKEY_CLIENT_SECRET")
    if not (cid and secret) and DK_CONFIG.is_file():
        try:
            cfg = json.loads(DK_CONFIG.read_text(encoding="utf-8"))
            cid, secret = cfg.get("client_id"), cfg.get("client_secret")
        except ValueError:
            pass
    return (cid, secret) if cid and secret else None


def _json_call(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _digikey_product(mpn):
    """(auth headers, product) for the exact MPN via DigiKey's official product API, or (None, None)."""
    creds = _digikey_creds()
    if not creds or not mpn:
        return None, None
    try:
        token = _json_call("https://api.digikey.com/v1/oauth2/token",
                           urllib.parse.urlencode(dict(client_id=creds[0], client_secret=creds[1],
                                                       grant_type="client_credentials")).encode(),
                           {"Content-Type": "application/x-www-form-urlencoded"})["access_token"]
        headers = {"Authorization": "Bearer " + token, "X-DIGIKEY-Client-Id": creds[0],
                   "Content-Type": "application/json"}
        found = _json_call("https://api.digikey.com/products/v4/search/keyword",
                           json.dumps({"Keywords": mpn, "Limit": 10}).encode(), headers)
    except Exception as e:  # network, auth, bad JSON: fall back to the search page
        log.warning("DigiKey API lookup failed: %s", e)
        return None, None
    for prod in found.get("Products") or []:
        name = prod.get("ManufacturerProductNumber") or prod.get("ManufacturerPartNumber") or ""
        if _norm(name) == _norm(mpn):
            return headers, prod
    return None, None


def digikey_models_url(mpn, host="www.digikey.com"):
    """DigiKey's CAD models page for an exact MPN, or "".

    Needs your own free API app (developer.digikey.com): put its client id/secret in
    DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET or in ~/.config/kicad-importer/digikey.json.
    """
    _, prod = _digikey_product(mpn)
    pid = re.search(r"/(\d+)(?:[/?#].*)?$", (prod or {}).get("ProductUrl") or "")
    return "https://%s/en/models/%s" % (host, pid.group(1)) if pid else ""


STEP_RE = re.compile(r"\.(stp|step)(\?.*)?$", re.I)


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def digikey_step_links(mpn):
    """https links to .stp/.step files that DigiKey's official API lists for this MPN."""
    headers, prod = _digikey_product(mpn)
    if not prod:
        return []
    found = list(_walk_strings(prod))
    dk = ((prod.get("ProductVariations") or [{}])[0]).get("DigiKeyProductNumber")
    if dk:
        try:
            found += list(_walk_strings(_json_call("https://api.digikey.com/products/v4/search/%s/media"
                                                   % urllib.parse.quote(dk, safe=""), None, headers)))
        except Exception as e:
            log.warning("DigiKey media lookup failed: %s", e)
    return list(dict.fromkeys(u for u in found if u.startswith("https://") and STEP_RE.search(u)))


def fetch_file(url, dest_dir, limit=100 * 1024 * 1024):
    """Download url into dest_dir (https only, size capped). Returns the saved path."""
    if not url.startswith("https://"):
        raise ValueError("not an https link: " + url)
    name = unquote(Path(urlparse(url).path).name) or "model.step"
    dest = Path(dest_dir) / name
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "kicad-importer"}), timeout=60) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("file too big: " + url)
    dest.write_bytes(data)
    return dest


# ---------------- library writing ----------------


class NoCadData(RuntimeError):
    pass


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


def get_cad(lcsc_id, api):
    """EasyEDA CAD data, with a clear error for rate limiting vs. no data."""
    cap, root = _Capture(), logging.getLogger()
    root.addHandler(cap)
    try:
        cad = api.get_cad_data_of_component(lcsc_id=lcsc_id)
    finally:
        root.removeHandler(cap)
    if cad:
        return cad
    if any(re.search(r"HTTP Error (403|429)", m) for m in cap.msgs):
        raise RuntimeError("EasyEDA is refusing requests right now (rate limit). Wait a few minutes and try again.")
    if any("API request failed" in m for m in cap.msgs):
        raise RuntimeError("Couldn't reach EasyEDA: " + cap.msgs[-1])
    raise NoCadData("EasyEDA has no CAD data for " + lcsc_id)


def _paths(project_dir):
    out = Path(project_dir) / "lib" / LIB / LIB
    return out, Path(str(out) + ".pretty"), Path(str(out) + ".3dshapes")


def _fix_wrl(path):
    if path.suffix.lower() == ".wrl" and path.is_file():
        path.write_text(path.read_text(encoding="utf-8", errors="replace").replace("transparency 1.0", "transparency 0.0"), encoding="utf-8")


def import_part(lcsc_id, project_dir, api):
    """Write symbol, footprint and 3D model for lcsc_id into <project>/lib/parts/.

    Returns (symbol_name, footprint_name, mpn, 3d_files).
    """
    out, pretty, shapes = _paths(project_dir)
    pretty.mkdir(parents=True, exist_ok=True)
    cad = get_cad(lcsc_id, api)

    models = []
    m3d = Easyeda3dModelImporter(easyeda_cp_cad_data=cad, download_raw_3d_model=True, api=api).output
    if m3d:
        shapes.mkdir(parents=True, exist_ok=True)
        model = Exporter3dModelKicad(model_3d=m3d)
        if model.output:
            model.export(output_dir=str(shapes), overwrite=True)
            _fix_wrl(shapes / (m3d.name + ".wrl"))
        elif m3d.step:  # OBJ download failed but STEP came through
            (shapes / (m3d.name + ".step")).write_bytes(m3d.step)
        models = [p.name for p in (shapes / (m3d.name + ".wrl"), shapes / (m3d.name + ".step")) if p.is_file()]
        if not models:
            log.warning("3D model download failed for %s", lcsc_id)

    fp = EasyedaFootprintImporter(easyeda_cp_cad_data=cad).get_footprint()
    ExporterFootprintKicad(footprint=fp).export(
        footprint_full_path=str(pretty / (fp.info.name + ".kicad_mod")),
        model_3d_path=MODEL_DIR,
        model_3d_extension="step" if models and not models[0].endswith(".wrl") else "wrl",
    )

    sym = EasyedaSymbolImporter(easyeda_cp_cad_data=cad).get_symbol()
    sym.info.package = fp.info.name
    lib_path = str(out) + ".kicad_sym"
    ExporterSymbolKicad(symbol=sym, lib_path=lib_path).save_to_lib(
        lib_path=lib_path, footprint_lib_name=LIB, overwrite=True
    )
    return sym.info.name, fp.info.name, sym.info.mpn, models


# ---------------- downloaded CAD files (Ultra Librarian, SnapMagic, manufacturer) ----------------

CAD_EXTS = (".kicad_sym", ".kicad_mod", ".step", ".stp", ".wrl")
MODEL_EXTS = (".step", ".stp", ".wrl")


def _top_level(text, head):
    """Top-level '(head ...)' blocks one level inside the outer s-expression."""
    out, depth, start, i, n = [], 0, None, 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
            if depth == 2 and re.match(r"\(" + head + r"\s", text[i:i + len(head) + 2]):
                start = i
        elif c == ")":
            if depth == 2 and start is not None:
                out.append(text[start:i + 1])
                start = None
            depth -= 1
        i += 1
    return out


def read_cad_files(path):
    """{filename: bytes} for KiCad/3D files in a zip, a folder, or a single file."""
    path = Path(path)
    files = {}
    if path.is_dir():
        for f in path.rglob("*"):
            if f.is_file() and f.suffix.lower() in CAD_EXTS and "__MACOSX" not in f.parts:
                files[f.name] = f.read_bytes()
    elif path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith(CAD_EXTS) and "__MACOSX" not in name:
                    files[Path(name).name] = z.read(name)
    elif path.suffix.lower() in CAD_EXTS:
        files[path.name] = path.read_bytes()
    return files


def _put_symbol(lib_path, name, block, version):
    """Insert or replace one symbol block, keeping the newer file format version."""
    path = Path(lib_path)
    text = path.read_text(encoding="utf-8") if path.is_file() else (
        "(kicad_symbol_lib\n  (version %d)\n  (generator kicad-importer)\n)\n" % (version or KICAD_SYM_VERSIONS_SORTED[0]))
    for old in _top_level(text, "symbol"):
        if re.match(r'\(symbol\s+"%s"' % re.escape(name), old):
            text = re.sub(r"\n[ \t]*" + re.escape(old), "", text, count=1)
    end = text.rstrip().rfind(")")
    text = text[:end].rstrip() + "\n  " + block + "\n)\n"
    current = int((re.search(r"\(version\s+(\d+)\)", text) or [0, 0])[1])
    if version > current:
        text = re.sub(r"\(version\s+\d+\)", "(version %d)" % version, text, count=1)
    path.write_text(text, encoding="utf-8")


def _is_part(name, mpn):
    """Does a symbol/footprint name belong to this MPN? Vendor libs use names like 4818_744042100."""
    m = _norm(mpn).replace("_", "")
    toks = re.split(r"[_ ]", name)
    return bool(m) and any(_norm("".join(toks[i:])) == m for i in range(len(toks)))


def import_cad_files(path, project_dir, attach_to=None, mpn=""):
    """Import symbols, footprints and 3D models from a downloaded zip/folder/file.

    Vendor zips can hold a whole part family. With an mpn, only that part's symbol
    plus the footprint and 3D model it uses get imported. A file with only a 3D
    model gets attached to the footprint named attach_to.
    Returns (symbols, footprints, models).
    """
    files = read_cad_files(path)
    if not files:
        raise ValueError("No KiCad (.kicad_sym/.kicad_mod) or 3D (.step/.wrl) files in %s. "
                         "If it's from Ultra Librarian or SnapMagic, pick the KiCad v6+ format." % Path(path).name)
    out, pretty, shapes = _paths(project_dir)
    pretty.mkdir(parents=True, exist_ok=True)

    syms = []  # (name, block, version)
    for name in sorted(n for n in files if n.lower().endswith(".kicad_sym")):
        text = files[name].decode("utf-8", errors="replace")
        version = int((re.search(r"\(version\s+(\d+)\)", text) or [0, 0])[1])
        for block in _top_level(text, "symbol"):
            syms.append((re.match(r'\(symbol\s+"([^"]+)"', block).group(1), block, version))
    fp_names = sorted(n[:-len(".kicad_mod")] for n in files if n.lower().endswith(".kicad_mod"))

    def fp_of(block):
        m = re.search(r'\(property\s+"Footprint"\s+"([^"]*)"', block)
        return m.group(1).split(":")[-1] if m else ""

    if mpn and len(syms) > 1:
        keep = {n for n, _, _ in syms if _is_part(n, mpn)}
        for n, block, _ in syms:  # derived symbols need their parent
            if n in keep:
                keep.update(re.findall(r'\(extends\s+"([^"]+)"', block))
        if keep:
            syms = [x for x in syms if x[0] in keep]
    if mpn and len(fp_names) > 1:
        wanted = [f for f in fp_names if f in {fp_of(b) for _, b, _ in syms}] or [f for f in fp_names if _is_part(f, mpn)]
        fp_names = wanted or fp_names

    models = sorted(n for n in files if n.lower().endswith(MODEL_EXTS))
    best = [m for m in models if not m.lower().endswith(".wrl")] or models  # prefer STEP

    def pick_model(ref=""):
        base = Path(ref.replace("\\", "/")).name
        if base in models:
            return base
        stem = Path(base).stem.lower()
        same = [m for m in best if Path(m).stem.lower() == stem]
        return (same or (best if len(best) == 1 or not fp_names else []) or [None])[0]

    def with_model(text, ref_name):
        if re.search(r"\(model\s", text):
            def repl(x):
                m = pick_model(x.group(2).strip('"'))
                return '%s"%s/%s"' % (x.group(1), MODEL_DIR, m) if m else x.group(0)
            return re.sub(r'(\(model\s+)("[^"]*"|[^\s()]+)', repl, text)
        m = pick_model(ref_name)
        if not m:
            return text
        end = text.rstrip().rfind(")")
        return (text[:end].rstrip() + '\n  (model "%s/%s"\n    (offset (xyz 0 0 0)) (scale (xyz 1 1 1)) (rotate (xyz 0 0 0)))\n)\n'
                % (MODEL_DIR, m))

    used = set()
    for stem in fp_names:
        text = with_model(files[stem + ".kicad_mod"].decode("utf-8", errors="replace"), stem)
        used.update(Path(m).name for m in re.findall(r'\(model\s+"([^"]+)"', text))
        (pretty / (stem + ".kicad_mod")).write_text(text, encoding="utf-8")

    if attach_to and models and not fp_names:
        fp_path = pretty / (attach_to + ".kicad_mod")
        if fp_path.is_file():
            text = with_model(fp_path.read_text(encoding="utf-8"), attach_to)
            used.update(Path(m).name for m in re.findall(r'\(model\s+"([^"]+)"', text))
            fp_path.write_text(text, encoding="utf-8")

    models = [m for m in models if m in used or not fp_names and not attach_to]
    if models:
        shapes.mkdir(parents=True, exist_ok=True)
        for m in models:
            (shapes / m).write_bytes(files[m])
            _fix_wrl(shapes / m)

    lib_path = str(out) + ".kicad_sym"
    for name, block, version in syms:
        def fp_ref(m):
            ref = m.group(2).split(":")[-1]
            if ref not in fp_names and len(fp_names) == 1:
                ref = fp_names[0]
            return '%s"%s"' % (m.group(1), LIB + ":" + ref if ref else "")

        _put_symbol(lib_path, name, re.sub(r'(\(property\s+"Footprint"\s+)"([^"]*)"', fp_ref, block, count=1), version)
    return [n for n, _, _ in syms], fp_names, models


def remove_parts(project_dir, patterns):
    """Remove symbols matching glob patterns, then footprints/3D models/CSV rows nothing uses anymore."""
    import fnmatch
    out, pretty, shapes = _paths(project_dir)
    lib_path = Path(str(out) + ".kicad_sym")
    text = lib_path.read_text(encoding="utf-8") if lib_path.is_file() else ""
    removed = []
    for block in _top_level(text, "symbol"):
        name = re.match(r'\(symbol\s+"([^"]+)"', block).group(1)
        if any(fnmatch.fnmatchcase(name, p) for p in patterns):
            text = re.sub(r"\n[ \t]*" + re.escape(block), "", text, count=1)
            removed.append(name)
    if removed:
        lib_path.write_text(text, encoding="utf-8")
    kept = set(re.findall(r'\(property\s+"Footprint"\s+"%s:([^"]*)"' % LIB, text))
    gone_fps = [f for f in pretty.glob("*.kicad_mod") if f.stem not in kept] if pretty.is_dir() else []
    for f in gone_fps:
        f.unlink()
    used = set()
    for f in pretty.glob("*.kicad_mod") if pretty.is_dir() else []:
        used.update(Path(m).name for m in re.findall(r'\(model\s+"([^"]+)"', f.read_text(encoding="utf-8")))
    used = {Path(u).stem for u in used}  # a .step next to a referenced .wrl stays
    gone_models = [m for m in shapes.glob("*") if m.stem not in used] if shapes.is_dir() else []
    for m in gone_models:
        m.unlink()
    csv_path = Path(str(out) + ".csv")
    if csv_path.is_file():
        symbols = {re.match(r'\(symbol\s+"([^"]+)"', b).group(1) for b in _top_level(text, "symbol")}
        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            r["symbol"] = " ".join(x for x in r["symbol"].split() if x.split(":")[-1] in symbols)
            r["footprint"] = " ".join(x for x in r["footprint"].split() if x.split(":")[-1] in kept)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(r for r in rows if r["symbol"] or r["footprint"])
    return removed, [f.stem for f in gone_fps], [m.name for m in gone_models]


def add_lib_tables(project_dir):
    """Add the parts library to sym-lib-table and fp-lib-table if it isn't there yet."""
    for table, kind, ext in (("sym-lib-table", "sym_lib_table", ".kicad_sym"), ("fp-lib-table", "fp_lib_table", ".pretty")):
        path = Path(project_dir) / table
        text = path.read_text(encoding="utf-8") if path.is_file() else "(%s\n  (version 7)\n)\n" % kind
        if re.search(r'\(name\s+"?%s"?\)' % LIB, text):
            continue
        entry = '  (lib (name "%s")(type "KiCad")(uri "%s%s")(options "")(descr "Imported parts"))\n' % (LIB, LIB_URI, ext)
        end = text.rstrip().rfind(")")
        path.write_text(text[:end].rstrip("\n") + "\n" + entry + text[end:].lstrip(" "), encoding="utf-8")


def upsert_csv(project_dir, row):
    """One row per part, keyed by LCSC number (or MPN for parts that aren't from LCSC)."""
    def key(r):
        return r.get("lcsc_id") or (r.get("mpn") or "").lower()

    path = Path(project_dir) / "lib" / LIB / (LIB + ".csv")
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if path.is_file():
        with open(path, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if key(r) != key(row)]
    rows.append(row)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_import(info, project_dir, api):
    """info is parse_link() output with lcsc_id filled in. Returns the CSV row."""
    symbol, footprint, mpn, models = import_part(info["lcsc_id"], project_dir, api)
    add_lib_tables(project_dir)
    row = dict(info, mpn=mpn or info["mpn"], symbol=LIB + ":" + symbol, footprint=LIB + ":" + footprint,
               imported_at=_now(), models=models)
    upsert_csv(project_dir, row)
    return row


def run_file_import(path, info, project_dir, attach_to=None):
    """Import a downloaded zip/folder/file. Returns (symbols, footprints, models)."""
    symbols, footprints, models = import_cad_files(path, project_dir, attach_to, (info or {}).get("mpn", ""))
    add_lib_tables(project_dir)
    if symbols or footprints:
        mpn = (info or {}).get("mpn") or (symbols or footprints)[0]
        mpn = next((s for s in symbols if _norm(s) == _norm(mpn)), mpn)  # DigiKey URLs turn # into -
        upsert_csv(project_dir, dict(info or {}, mpn=mpn,
                                     symbol=" ".join(LIB + ":" + s for s in symbols),
                                     footprint=" ".join(LIB + ":" + f for f in footprints), imported_at=_now()))
    return symbols, footprints, models


def resolve(info, api):
    """Fill in info["lcsc_id"] if there's an exact LCSC match. Returns the candidate list."""
    if info["lcsc_id"]:
        return []
    exact, candidates = find_lcsc(info["mpn"], api)
    if exact:
        info["lcsc_id"] = exact["lcsc"]
    return candidates


# ---------------- KiCad plugin ----------------

try:
    import wx
except ImportError:  # running outside KiCad
    wx = None
try:
    import pcbnew
except ImportError:
    pcbnew = None

DOWNLOADS = Path.home() / "Downloads"


def _short(names, n=5):
    return ", ".join(names[:n]) + (" and %d more" % (len(names) - n) if len(names) > n else "")
PARTIAL = (".crdownload", ".download", ".part", ".tmp")

if wx:

    class ImportDialog(wx.Dialog):
        def __init__(self, parent, project_dir):
            super().__init__(parent, title="Import part", size=(700, 440),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.project_dir = project_dir
            self.api = EasyedaApi()
            self.watch = None
            self.link = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
            self.link.SetHint("Link or part number: LCSC C2040, DigiKey ...-ND, Mouser 595-..., or an MPN")
            button = wx.Button(self, label="Import")
            zip_button = wx.Button(self, label="Import file...")
            self.out = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(self.link, 1, wx.EXPAND | wx.RIGHT, 5)
            row.Add(button, 0, wx.RIGHT, 5)
            row.Add(zip_button)
            box = wx.BoxSizer(wx.VERTICAL)
            box.Add(row, 0, wx.EXPAND | wx.ALL, 8)
            box.Add(self.out, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            self.SetSizer(box)
            button.Bind(wx.EVT_BUTTON, self.on_import)
            zip_button.Bind(wx.EVT_BUTTON, self.on_file)
            self.link.Bind(wx.EVT_TEXT_ENTER, self.on_import)
            self.timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self.on_tick, self.timer)
            self.Bind(wx.EVT_CLOSE, self.on_close)
            self.say("Library: " + os.path.join(project_dir, "lib", LIB))

        def say(self, msg):
            self.out.AppendText(msg + "\n")
            wx.GetApp().Yield(True)

        def on_close(self, event):
            self.timer.Stop()
            event.Skip()

        def pick(self, info, candidates):
            """Returns an LCSC id, "browser", or None."""
            labels = ["Get CAD files from the distributor in my browser instead"]
            labels += ["%s  %s  %s  %s" % (c["lcsc"], c["model"], c["brand"], c["package"]) for c in candidates]
            dlg = wx.SingleChoiceDialog(self, "No exact LCSC match for %s. Pick one, or cancel." % info["mpn"],
                                        "Pick a part", labels)
            try:
                if dlg.ShowModal() != wx.ID_OK:
                    return None
                i = dlg.GetSelection()
                return candidates[i - 1]["lcsc"] if i else "browser"
            finally:
                dlg.Destroy()

        def on_import(self, _event):
            self.stop_watch()
            try:
                info = parse_link(self.link.GetValue())
                if not info["lcsc_id"]:
                    self.say("Searching LCSC for %s (%s)..." % (info["mpn"], info["source"]))
                    candidates = resolve(info, self.api)
                    if not info["lcsc_id"]:
                        choice = self.pick(info, candidates)
                        if choice == "browser":
                            return self.start_watch(info)
                        if not choice:
                            return
                        info["lcsc_id"] = choice
                self.say("Importing %s..." % info["lcsc_id"])
                try:
                    row = run_import(info, self.project_dir, self.api)
                except NoCadData as e:
                    self.say(str(e) + ".")
                    return self.start_watch(info)
                self.say("Done: %s / %s" % (row["symbol"], row["footprint"]))
                self.say("3D: " + (", ".join(row["models"]) or "none, EasyEDA has no model or the download failed"))
                self.link.SetValue("")
            except Exception as e:
                self.say("Error: %s" % e)

        def on_file(self, _event):
            dlg = wx.FileDialog(self, "Pick a downloaded zip, .kicad_sym, .kicad_mod or .step", str(DOWNLOADS),
                                wildcard="CAD files|*.zip;*.kicad_sym;*.kicad_mod;*.step;*.stp;*.wrl|All files|*",
                                style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
            try:
                if dlg.ShowModal() == wx.ID_OK:
                    self.import_file(dlg.GetPath(), self.watch and self.watch["info"])
            finally:
                dlg.Destroy()

        def import_file(self, path, info):
            try:
                attach = self.watch and self.watch.get("footprint")
                symbols, footprints, models = run_file_import(path, info, self.project_dir, attach)
                self.say("Imported %s" % os.path.basename(path))
                if symbols:
                    self.say("  symbols: " + _short(symbols))
                if footprints:
                    self.say("  footprints: " + _short(footprints))
                    if self.watch:
                        self.watch["footprint"] = footprints[0]
                        step, self.watch["step"] = self.watch.get("step"), None
                        if step and not models:  # the zip had no 3D model of its own
                            self.import_file(step, info)
                if models:
                    self.say("  3D: " + ", ".join(models) + (" (added to %s)" % attach if attach and not footprints else ""))
                return True
            except Exception as e:
                self.say("Error importing %s: %s" % (os.path.basename(path), e))
                return False

        # --- browser downloads ---

        def start_watch(self, info):
            if not info.get("cad_url") and info["mpn"] and _digikey_creds():
                self.say("Asking DigiKey for the part's CAD models page...")
                info["cad_url"] = digikey_models_url(info["mpn"])
            url = (info.get("cad_url") or info["url"]
                   or "https://www.digikey.com/en/products/result?keywords=" + quote(info["mpn"] or info["lcsc_id"]))
            self.watch = dict(info=info, since=time.time() - 2, seen={}, done=set(), footprint=None, step=None)
            self.fetch_step(info)
            self.say("Opening %s" % url)
            self.say(("Pick a tab (Ultra Librarian, SnapMagic, Manufacturer Provided), choose KiCad or STEP and hit Download. "
                      if info.get("cad_url") else
                      "Click EDA/CAD Models, then download the KiCad files and/or the STEP. ")
                     + "I'll import whatever lands in Downloads for the next 10 minutes.")
            wx.LaunchDefaultBrowser(url)
            self.timer.Start(1000)

        def fetch_step(self, info):
            """Manufacturer STEP straight from DigiKey's API links, if it lists any."""
            if not (info["mpn"] and _digikey_creds()):
                return
            for url in digikey_step_links(info["mpn"]):
                try:
                    tmp = tempfile.mkdtemp(prefix="kicad-importer-")
                    self.watch["step"] = str(fetch_file(url, tmp))
                    self.say("Got the manufacturer's STEP from DigiKey: " + os.path.basename(self.watch["step"]))
                    return
                except Exception as e:
                    self.say("Couldn't download %s: %s" % (url, e))
            self.say("DigiKey's API doesn't list a STEP file for this part, so you'll download the 3D model yourself.")

        def stop_watch(self):
            if self.watch:
                self.timer.Stop()
                self.watch = None

        def on_tick(self, _event):
            w = self.watch
            if not w:
                return
            if time.time() - w["since"] > 600:
                self.say("Stopped watching Downloads.")
                return self.stop_watch()
            try:
                entries = list(DOWNLOADS.iterdir())
            except OSError as e:
                self.say("Can't read %s: %s (use Import file... instead)" % (DOWNLOADS, e))
                return self.stop_watch()
            for p in entries:
                if p.name in w["done"] or p.name.endswith(PARTIAL) or p.name.startswith("."):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_mtime < w["since"]:
                    continue
                if not (p.is_dir() or p.suffix.lower() in (".zip",) + CAD_EXTS):
                    continue
                size = st.st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                if w["seen"].get(p.name) != size:  # wait until it stops growing
                    w["seen"][p.name] = size
                    continue
                w["done"].add(p.name)
                if p.is_dir() and not read_cad_files(p):
                    continue
                self.import_file(str(p), w["info"])

    if pcbnew:

        class ImporterPlugin(pcbnew.ActionPlugin):
            def defaults(self):
                self.name = "Part Importer"
                self.category = "Libraries"
                self.description = "Import a part from an LCSC, DigiKey, Mouser or Newark link"
                self.show_toolbar_button = True
                self.icon_file_name = os.path.join(os.path.dirname(__file__), "icon.png")

            def Run(self):
                board = pcbnew.GetBoard().GetFileName()
                if not board:
                    wx.MessageBox("Save the board first so I know where the project is.", "Part Importer")
                    return
                dlg = ImportDialog(None, os.path.dirname(os.path.abspath(board)))
                dlg.ShowModal()
                dlg.Destroy()


def cli(argv):
    """Import without KiCad: python3 -m plugins.importer <link, part number or downloaded file>... [--project DIR]"""
    import argparse

    ap = argparse.ArgumentParser(prog="python3 -m plugins.importer", description=cli.__doc__)
    ap.add_argument("links", nargs="+")
    ap.add_argument("--project", default=".", help="KiCad project folder (default: current folder)")
    ap.add_argument("--mpn", default="", help="for downloaded zips: only import this part, not the whole family")
    ap.add_argument("--remove", action="store_true",
                    help="remove symbols matching the given names/globs (e.g. '2828_*'), plus footprints and 3D models nothing uses anymore")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    if args.remove:
        symbols, footprints, models = remove_parts(args.project, args.links)
        print("removed %d symbols, %d footprints, %d 3D models" % (len(symbols), len(footprints), len(models)))
        return 0
    api, failed = EasyedaApi(), 0
    for link in args.links:
        try:
            if os.path.exists(link):
                info = dict(source="file", url="", mpn=args.mpn, lcsc_id="") if args.mpn else None
                symbols, footprints, models = run_file_import(link, info, args.project)
                print("%s -> symbols: %s, footprints: %s, 3D: %s" % (link, ", ".join(symbols) or "none",
                                                                     ", ".join(footprints) or "none", ", ".join(models) or "none"))
                continue
            info = parse_link(link)
            candidates = resolve(info, api)
            if not info["lcsc_id"]:
                print("No exact LCSC match for %s. Candidates:" % info["mpn"])
                for c in candidates:
                    print("  %s  %s  %s  %s" % (c["lcsc"], c["model"], c["brand"], c["package"]))
                print("Rerun with the LCSC number you want, or download the CAD files and pass the zip.")
                failed += 1
                continue
            row = run_import(info, args.project, api)
            print("%s -> %s / %s, 3D: %s" % (info["lcsc_id"], row["symbol"], row["footprint"], ", ".join(row["models"]) or "none"))
        except Exception as e:
            print("%s: %s" % (link, e))
            failed += 1
    return 1 if failed else 0


def self_check():
    def check(link, **want):
        got = parse_link(link)
        for k, v in want.items():
            assert got[k] == v, (link, k, got[k], v)

    check("C2040", source="lcsc", lcsc_id="C2040")
    check("c15742", lcsc_id="C15742")
    check("https://www.lcsc.com/product-detail/Microcontroller-Units-MCUs-MPUs-SOCs_STMicroelectronics-STM32F405RGT6_C15742.html", lcsc_id="C15742")
    check("https://www.lcsc.com/product-detail/C2040.html", lcsc_id="C2040")
    check("https://jlcpcb.com/partdetail/RaspberryPi-RP2040/C2040", lcsc_id="C2040")
    check("jlcpcb.com/partdetail/RP2040/C2040", lcsc_id="C2040")
    check("https://www.digikey.com/en/products/detail/stmicroelectronics/STM32F405RGT6/2754208", source="digikey", mpn="STM32F405RGT6")
    check("https://www.digikey.de/de/products/detail/murata-electronics/GRM188R71H104KA93D/600088?s=abc", mpn="GRM188R71H104KA93D")
    check("https://www.digikey.com/en/products/detail/texas-instruments/SN74LVC1G04DBVR%2FT/1", mpn="SN74LVC1G04DBVR/T")
    check("https://www.digikey.com/product-detail/en/stmicroelectronics/STM32F405RGT6/497-11767-ND/2754208", mpn="STM32F405RGT6")
    check("https://www.mouser.com/ProductDetail/Texas-Instruments/LM358DR?qs=abc%3D%3D", source="mouser", mpn="LM358DR")
    check("https://www.mouser.de/ProductDetail/595-LM358DR", mpn="LM358DR")
    check("https://www.mouser.com/ProductDetail/81-GRM188R71H104KA3D", mpn="GRM188R71H104KA3D")
    check("https://www.newark.com/stmicroelectronics/stm32f405rgt6/32-bit-microcontroller-advanced/dp/71T9854", source="newark", mpn="stm32f405rgt6")
    check("https://uk.farnell.com/murata/grm188r71h104ka93d/cap-0-1-f-50v-10-x7r-0603/dp/8820023", source="farnell", mpn="grm188r71h104ka93d")
    check("https://sg.element14.com/stmicroelectronics/stm32f405rgt6/mcu/dp/2064363", source="element14")
    check("732-1025-1-ND", source="digikey", mpn="1025-1")
    check("505-LT1618EDD#TRPBFCT-ND", source="digikey", mpn="LT1618EDD#TRPBF")
    check("296-1395-1-ND", source="digikey")
    check("595-LM358DR", source="mouser", mpn="LM358DR")
    check("LT1618EDD#TRPBF", source="mpn", mpn="LT1618EDD#TRPBF")
    check("https://www.digikey.com/en/products/detail/w%C3%BCrth-elektronik/744042100/1638429", mpn="744042100",
          cad_url="https://www.digikey.com/en/models/1638429")
    check("https://www.digikey.ca/en/products/detail/analog-devices-inc/LT1618EDD-TRPBF/960480?s=x",
          cad_url="https://www.digikey.ca/en/models/960480")
    assert _norm("LT1618EDD-TRPBF") == _norm("LT1618EDD#TRPBF")
    assert _is_part("4818_744042100", "744042100") and _is_part("LT1618EDD_TRPBF", "LT1618EDD#TRPBF")
    assert not _is_part("4818_744042101", "744042100") and not _is_part("1744042100", "744042100")
    assert not _is_part("2828_74402500030", "744042100")
    for bad in ("https://example.com/foo", "https://www.digikey.com/en/products", ""):
        try:
            parse_link(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert _norm("LM358-DR / x") == _norm("lm358drX")
    assert _norm("XL358D") != _norm("LM358DR")
    import io
    real, urllib.request.urlopen = urllib.request.urlopen, None
    seen = []

    def fake(req, timeout=0):
        seen.append(req.full_url)
        body = ({"access_token": "t"} if "oauth2" in req.full_url else {"Products": [
            {"ManufacturerProductNumber": "NRVB130LSFT1G-X", "ProductUrl": "https://www.digikey.com/en/products/detail/a/b/1"},
            {"ManufacturerProductNumber": "NRVB130LSFT1G", "ProductUrl": "https://www.digikey.com/en/products/detail/onsemi/NRVB130LSFT1G/5258002?s=x"}]})
        return io.BytesIO(json.dumps(body).encode())

    urllib.request.urlopen = fake
    os.environ.update(DIGIKEY_CLIENT_ID="id", DIGIKEY_CLIENT_SECRET="secret")
    try:
        assert digikey_models_url("NRVB130LSFT1G") == "https://www.digikey.com/en/models/5258002", seen
        assert digikey_models_url("NRVB130LSFT1") == ""  # no exact match, no guessing
        urllib.request.urlopen = lambda req, timeout=0: io.BytesIO(json.dumps(
            {"access_token": "t"} if "oauth2" in req.full_url else
            {"Products": [{"ManufacturerProductNumber": "X1", "ProductUrl": "https://www.digikey.com/p/1",
                           "ProductVariations": [{"DigiKeyProductNumber": "X1-ND"}],
                           "Links": ["https://mfr.example/cad/X1.STP?v=2", "http://insecure/x.step", "https://mfr.example/x.pdf"]}]}
            if "keyword" in req.full_url else {"Media": [{"Url": "https://mfr.example/cad/X1.step"}]}).encode())
        assert digikey_step_links("X1") == ["https://mfr.example/cad/X1.STP?v=2", "https://mfr.example/cad/X1.step"], digikey_step_links("X1")
        assert digikey_step_links("Y9") == []
        del os.environ["DIGIKEY_CLIENT_ID"]
        assert digikey_models_url("NRVB130LSFT1G") == "" or DK_CONFIG.is_file()
    finally:
        urllib.request.urlopen = real
    print("all link checks passed")


if __name__ == "__main__":
    import sys

    sys.exit(cli(sys.argv[1:]) if len(sys.argv) > 1 else self_check())
