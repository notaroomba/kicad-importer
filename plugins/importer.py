"""Import parts from LCSC / DigiKey / Mouser / Newark links into a KiCad project library."""

import csv
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from .easyeda2kicad.easyeda.easyeda_api import EasyedaApi
from .easyeda2kicad.easyeda.easyeda_importer import (
    Easyeda3dModelImporter,
    EasyedaFootprintImporter,
    EasyedaSymbolImporter,
)
from .easyeda2kicad.kicad.export_kicad_3d_model import Exporter3dModelKicad
from .easyeda2kicad.kicad.export_kicad_footprint import ExporterFootprintKicad
from .easyeda2kicad.kicad.export_kicad_symbol import ExporterSymbolKicad

LIB = "parts"
LIB_URI = "${KIPRJMOD}/lib/parts/parts"
CSV_FIELDS = ["source", "url", "mpn", "lcsc_id", "symbol", "footprint", "imported_at"]
LCSC_RE = re.compile(r"(?<![A-Za-z0-9])(C\d+)(?![A-Za-z0-9])", re.I)

log = logging.getLogger("kicad-importer")


# ---------------- link parsing ----------------


def parse_link(text):
    """Return dict(source, url, mpn, lcsc_id) for a pasted link or bare LCSC id."""
    text = text.strip()
    if re.fullmatch(r"[Cc]\d+", text):
        return dict(source="lcsc", url="", mpn="", lcsc_id=text.upper())
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
        # /en/products/detail/<mfr>/<mpn>/<id>
        if "detail" in low and low.index("detail") + 2 < len(parts):
            return dict(source="digikey", url=text, mpn=parts[low.index("detail") + 2], lcsc_id="")
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
    return re.sub(r"[\s/-]", "", s or "").lower()


def find_lcsc(mpn, api):
    """Search JLCPCB for an MPN. Returns (exact_match_or_None, all_candidates)."""
    results = api.search_jlcpcb_components(mpn).get("results", [])
    exact = [r for r in results if _norm(r["model"]) == _norm(mpn)]
    exact.sort(key=lambda r: -(r.get("stock") or 0))
    return (exact[0] if exact else None), results


# ---------------- library writing ----------------


def import_part(lcsc_id, project_dir, api):
    """Write symbol, footprint and 3D model for lcsc_id into <project>/lib/parts/.

    Returns (symbol_name, footprint_name, mpn).
    """
    lib_dir = Path(project_dir) / "lib" / LIB
    out = lib_dir / LIB
    pretty = Path(str(out) + ".pretty")
    shapes = Path(str(out) + ".3dshapes")
    pretty.mkdir(parents=True, exist_ok=True)

    cad = api.get_cad_data_of_component(lcsc_id=lcsc_id)
    if not cad:
        raise RuntimeError("EasyEDA has no CAD data for " + lcsc_id)

    fp = EasyedaFootprintImporter(easyeda_cp_cad_data=cad).get_footprint()
    ExporterFootprintKicad(footprint=fp).export(
        footprint_full_path=str(pretty / (fp.info.name + ".kicad_mod")),
        model_3d_path="${KIPRJMOD}/lib/parts/parts.3dshapes",
    )

    sym = EasyedaSymbolImporter(easyeda_cp_cad_data=cad).get_symbol()
    sym.info.package = fp.info.name
    lib_path = str(out) + ".kicad_sym"
    ExporterSymbolKicad(symbol=sym, lib_path=lib_path).save_to_lib(
        lib_path=lib_path, footprint_lib_name=LIB, overwrite=True
    )

    model = Exporter3dModelKicad(
        model_3d=Easyeda3dModelImporter(easyeda_cp_cad_data=cad, download_raw_3d_model=True, api=api).output
    )
    if model.output and model.export(output_dir=str(shapes), overwrite=True):
        wrl = shapes / (model.output.name + ".wrl")
        if wrl.is_file():
            wrl.write_text(wrl.read_text(encoding="utf-8").replace("transparency 1.0", "transparency 0.0"), encoding="utf-8")
    else:
        log.warning("No 3D model for %s", lcsc_id)

    return sym.info.name, fp.info.name, sym.info.mpn


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
    path = Path(project_dir) / "lib" / LIB / (LIB + ".csv")
    rows = []
    if path.is_file():
        with open(path, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("lcsc_id") != row["lcsc_id"]]
    rows.append(row)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def run_import(info, project_dir, api):
    """info is parse_link() output with lcsc_id filled in. Returns the CSV row."""
    symbol, footprint, mpn = import_part(info["lcsc_id"], project_dir, api)
    add_lib_tables(project_dir)
    row = dict(info, mpn=mpn or info["mpn"], symbol=LIB + ":" + symbol, footprint=LIB + ":" + footprint,
               imported_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    upsert_csv(project_dir, row)
    return row


# ---------------- KiCad plugin ----------------

try:
    import pcbnew
    import wx
except ImportError:  # running outside KiCad
    pcbnew = wx = None

if pcbnew:

    class ImportDialog(wx.Dialog):
        def __init__(self, parent, project_dir):
            super().__init__(parent, title="Import part", size=(600, 400),
                             style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
            self.project_dir = project_dir
            self.api = EasyedaApi()
            self.link = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
            self.link.SetHint("LCSC / JLCPCB / DigiKey / Mouser / Newark link, or C2040")
            button = wx.Button(self, label="Import")
            self.out = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
            row = wx.BoxSizer(wx.HORIZONTAL)
            row.Add(self.link, 1, wx.EXPAND | wx.RIGHT, 5)
            row.Add(button)
            box = wx.BoxSizer(wx.VERTICAL)
            box.Add(row, 0, wx.EXPAND | wx.ALL, 8)
            box.Add(self.out, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
            self.SetSizer(box)
            button.Bind(wx.EVT_BUTTON, self.on_import)
            self.link.Bind(wx.EVT_TEXT_ENTER, self.on_import)
            self.say("Library: " + os.path.join(project_dir, "lib", LIB))

        def say(self, msg):
            self.out.AppendText(msg + "\n")
            wx.Yield()

        def pick(self, mpn, candidates):
            if not candidates:
                self.say("Nothing on LCSC for " + mpn)
                return None
            labels = ["%s  %s  %s  %s" % (c["lcsc"], c["model"], c["brand"], c["package"]) for c in candidates]
            dlg = wx.SingleChoiceDialog(self, "No exact LCSC match for %s. Pick one, or cancel." % mpn,
                                        "Pick a part", labels)
            try:
                return candidates[dlg.GetSelection()]["lcsc"] if dlg.ShowModal() == wx.ID_OK else None
            finally:
                dlg.Destroy()

        def on_import(self, _event):
            try:
                info = parse_link(self.link.GetValue())
                if not info["lcsc_id"]:
                    self.say("Searching LCSC for %s (%s)..." % (info["mpn"], info["source"]))
                    exact, candidates = find_lcsc(info["mpn"], self.api)
                    info["lcsc_id"] = exact["lcsc"] if exact else self.pick(info["mpn"], candidates)
                    if not info["lcsc_id"]:
                        return
                self.say("Importing %s..." % info["lcsc_id"])
                row = run_import(info, self.project_dir, self.api)
                self.say("Done: %s / %s" % (row["symbol"], row["footprint"]))
                self.link.SetValue("")
            except Exception as e:
                self.say("Error: %s" % e)

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


if __name__ == "__main__":
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
    check("https://www.mouser.com/ProductDetail/Texas-Instruments/LM358DR?qs=abc%3D%3D", source="mouser", mpn="LM358DR")
    check("https://www.mouser.de/ProductDetail/595-LM358DR", mpn="LM358DR")
    check("https://www.mouser.com/ProductDetail/81-GRM188R71H104KA3D", mpn="GRM188R71H104KA3D")
    check("https://www.newark.com/stmicroelectronics/stm32f405rgt6/32-bit-microcontroller-advanced/dp/71T9854", source="newark", mpn="stm32f405rgt6")
    check("https://uk.farnell.com/murata/grm188r71h104ka93d/cap-0-1-f-50v-10-x7r-0603/dp/8820023", source="farnell", mpn="grm188r71h104ka93d")
    check("https://sg.element14.com/stmicroelectronics/stm32f405rgt6/mcu/dp/2064363", source="element14")
    for bad in ("https://example.com/foo", "https://www.digikey.com/en/products", "hello"):
        try:
            parse_link(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert _norm("LM358-DR / x") == _norm("lm358drX")
    assert _norm("XL358D") != _norm("LM358DR")
    print("all link checks passed")
