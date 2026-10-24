# kicad-importer

A KiCad 9/10 plugin I made because I got tired of downloading zips, unzipping them, and fixing up library paths every time I needed a part. You paste a link, hit Import, and the symbol, footprint and 3D model land in your project.

## What it does

- Takes an LCSC, JLCPCB, DigiKey, Mouser or Newark/Farnell/element14 link, or just an LCSC number like `C2040`
- For DigiKey/Mouser/Newark links it pulls the manufacturer part number out of the URL and looks it up on LCSC
- It only auto-imports when the LCSC part number matches exactly (case, dashes, slashes and spaces don't count). If there's no exact match it shows you the closest hits and you pick one, or cancel. I added that because searching `LM358DR` gives you `XL358D` as the top result, which is not the same chip
- Grabs the symbol, footprint and 3D model through [easyeda2kicad.py](https://github.com/uPesy/easyeda2kicad.py)
- Puts everything in one library per project and adds it to the project's lib tables for you
- Keeps a `parts.csv` so you know where each part came from

## Install

1. Grab `kicad-importer-<version>.zip` from the [releases page](https://github.com/notaroomba/kicad-importer/releases)
2. In KiCad open the Plugin and Content Manager
3. Click "Install from File..." and pick the zip
4. Restart the PCB editor if the button doesn't show up

Don't unzip it, KiCad wants the zip as is.

## Usage

1. Open your board in the PCB editor (save it first, the plugin uses the board's folder as the project folder)
2. Click the Part Importer button in the toolbar, or go to Tools > External Plugins > Part Importer
3. Paste a link or an LCSC number and hit Import
4. Watch the log. When it says Done, the part is in the `parts` library in the schematic editor and footprint browser

Links that work:

```
C2040
https://www.lcsc.com/product-detail/C2040.html
https://jlcpcb.com/partdetail/RaspberryPi-RP2040/C2040
https://www.digikey.com/en/products/detail/stmicroelectronics/STM32F405RGT6/2754208
https://www.mouser.com/ProductDetail/Texas-Instruments/LM358DR?qs=...
https://www.mouser.com/ProductDetail/595-LM358DR
https://www.newark.com/stmicroelectronics/stm32f405rgt6/32-bit-microcontroller-advanced/dp/71T9854
```

Other country sites (digikey.de, mouser.co.uk, uk.farnell.com, etc.) work too.

## Folder layout

Everything goes in your project folder:

```
my-project/
  my-project.kicad_pro
  sym-lib-table          <- "parts" entry added (file gets created if missing)
  fp-lib-table           <- same
  lib/parts/
    parts.kicad_sym      <- all symbols
    parts.pretty/        <- all footprints
    parts.3dshapes/      <- .wrl and .step models
    parts.csv
```

Paths use `${KIPRJMOD}`, so you can move the project around or commit it to git and it still works on another machine. Each symbol's Footprint field points at `parts:<footprint>`. Importing the same part again just overwrites it.

## parts.csv

One row per LCSC part, with these columns:

| column | what it is |
| --- | --- |
| source | lcsc, digikey, mouser, newark, farnell or element14 |
| url | the link you pasted (empty for a bare LCSC number) |
| mpn | manufacturer part number |
| lcsc_id | LCSC number, this is the key, reimporting replaces the row |
| symbol | `parts:<symbol>` |
| footprint | `parts:<footprint>` |
| imported_at | UTC timestamp |

Handy for a BOM or for JLCPCB assembly.

## Credit

All the actual EasyEDA to KiCad conversion is done by [easyeda2kicad.py](https://github.com/uPesy/easyeda2kicad.py) by uPesy. A copy of it lives in `plugins/easyeda2kicad/`. It's AGPL-3.0, so this plugin is AGPL-3.0 too (see [LICENSE](LICENSE)). The KiCad PCM metadata says `open-source` because the PCM schema doesn't have an AGPL option.

## Other importers

If this one doesn't fit what you need, check these out:

- [Impart (Import-LIB-KiCad-Plugin)](https://github.com/Steffen-W/Import-LIB-KiCad-Plugin): imports the zips you download from Octopart, SamacSys, Ultra Librarian, SnapEDA and EasyEDA. It's in the PCM, it shows up as impartGUI
- [SamacSys Library Loader / Component Search Engine](https://componentsearchengine.com/): search on the site, the Library Loader app drops the parts in a KiCad library. Mouser and a bunch of other distributors link to it
- [Ultra Librarian](https://www.ultralibrarian.com/cad-vendors/kicad): big library of models, you can export straight to KiCad. DigiKey links to it on their product pages
- [SnapEDA](https://www.snapeda.com/kicad): another big free library (now called SnapMagic), download KiCad files per part
