"""Schematic editor button (KiCad 10 IPC plugin). The PCB editor button comes from __init__.py."""

import importlib.util
import os
import sys

here = os.path.dirname(os.path.abspath(__file__))
sys.modules["pcbnew"] = None  # the SWIG half only belongs inside the PCB editor
spec = importlib.util.spec_from_file_location("kicad_importer", os.path.join(here, "__init__.py"),
                                              submodule_search_locations=[here])
sys.modules["kicad_importer"] = pkg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pkg)

try:
    import wx
except ImportError:
    sys.exit("Part Importer: wxPython isn't available to KiCad's plugin Python. Use the PCB editor button instead.")

from kicad_importer.importer import ImportDialog

app = wx.App()
project = os.environ.get("KIPRJMOD", "")
if not project or not os.path.isdir(project):
    wx.MessageBox("Open a KiCad project first so I know where to put the library.", "Part Importer")
    sys.exit(1)
dlg = ImportDialog(None, project)
dlg.Raise()
dlg.ShowModal()
dlg.Destroy()
