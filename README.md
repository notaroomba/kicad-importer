# kicad-importer

A KiCad 9/10 plugin I made because I got tired of downloading zips, unzipping them, and fixing up library paths every time I needed a part. You paste a link, hit Import, and the symbol, footprint and 3D model land in your project.

## What it does

- Takes an LCSC, JLCPCB, DigiKey, Mouser or Newark/Farnell/element14 link, or a part number: LCSC (`C2040`), DigiKey (`505-LT1618EDD#TRPBFCT-ND`), Mouser (`595-LM358DR`) or just the manufacturer part number
- For anything that isn't LCSC it figures out the manufacturer part number and looks it up on LCSC
- It only auto-imports when the LCSC part number matches exactly (case, dashes, slashes, `#` and spaces don't count). If there's no exact match it shows you the closest hits and you pick one, or cancel. I added that because searching `LM358DR` gives you `XL358D` as the top result, which is not the same chip
- Grabs the symbol, footprint and 3D model through [easyeda2kicad.py](https://github.com/uPesy/easyeda2kicad.py)
- If LCSC doesn't have the part (or EasyEDA has no CAD for it), it opens the distributor page in your browser and imports whatever you download from there: Ultra Librarian or SnapMagic zips, the manufacturer's STEP file, loose `.kicad_sym`/`.kicad_mod` files
- Button in the PCB editor (KiCad 9 and 10) and in the schematic editor (KiCad 10)
- Puts everything in one library per project and adds it to the project's lib tables for you
- Keeps a `parts.csv` so you know where each part came from

## Install

### Option 1: add my repo to KiCad (easiest, and you get updates)

1. In KiCad open the Plugin and Content Manager
2. Click "Manage..." next to the repository dropdown, then "+" to add one
3. Paste this URL:
   ```
   https://github.com/notaroomba/kicad-importer/releases/latest/download/repository.json
   ```
4. Pick it in the repository dropdown, find Part Importer, click Install, then Apply Pending Changes

When I put out a new release, KiCad will show it as an update.

### Option 2: install the zip

1. Grab `kicad-importer-<version>.zip` from the [releases page](https://github.com/notaroomba/kicad-importer/releases)
2. In the Plugin and Content Manager click "Install from File..." and pick the zip. Don't unzip it, KiCad wants the zip as is

### Option 3: copy it in by hand (no Plugin Manager)

1. Clone or download this repo
2. In the PCB editor go to Tools > External Plugins > Open Plugin Directory. On my Mac that's `~/Documents/KiCad/9.0/scripting/plugins`
3. Make a folder in there called `kicad_importer` and copy everything inside `plugins/` into it (so `kicad_importer/__init__.py` exists)
4. Tools > External Plugins > Refresh Plugins, or just restart the PCB editor

Either way, restart the PCB editor if the button doesn't show up.

### Schematic editor button (KiCad 10)

The schematic editor button uses KiCad's newer plugin API, so you have to turn that on once: Preferences > Plugins > check "Enable KiCad API", then restart KiCad. The Plugin Manager asks you about this when you install. The button shows up in the schematic editor's top toolbar. If it doesn't, try Tools > Refresh Plugins. KiCad 9 only has the PCB editor button.

## Usage

1. Click the Part Importer button (the chip with the green arrow). In the PCB editor it's also under Tools > External Plugins > Part Importer. Save the board first, the plugin uses its folder as the project folder
2. Paste a link or a part number and hit Import
3. Watch the log. When it says Done, the part is in the `parts` library in the schematic editor and footprint browser

If KiCad doesn't show the `parts` library right after your first import, close and reopen the project. KiCad 10 has no way for a plugin to make it reload the library list.

Stuff that works:

```
C2040
https://www.lcsc.com/product-detail/C2040.html
https://jlcpcb.com/partdetail/RaspberryPi-RP2040/C2040
https://www.digikey.com/en/products/detail/stmicroelectronics/STM32F405RGT6/2754208
https://www.mouser.com/ProductDetail/Texas-Instruments/LM358DR?qs=...
https://www.mouser.com/ProductDetail/595-LM358DR
https://www.newark.com/stmicroelectronics/stm32f405rgt6/32-bit-microcontroller-advanced/dp/71T9854
505-LT1618EDD#TRPBFCT-ND     (DigiKey part number)
595-LM358DR                  (Mouser part number)
LT1618EDD#TRPBF              (manufacturer part number)
```

Other country sites (digikey.de, mouser.co.uk, uk.farnell.com, etc.) work too.

Some DigiKey part numbers, like Würth's `732-1025-1-ND`, don't have the manufacturer part number in them at all. Those end up in the browser flow below, so pasting the product link is better.

### When LCSC doesn't have it

If there's no exact match on LCSC, the pick list has an extra first option, "Get CAD files from the distributor in my browser instead". It also goes there by itself when LCSC has the part but EasyEDA has no CAD data for it.

1. The plugin opens the part's CAD models page in your browser. For a DigiKey link that's DigiKey's own models page (`digikey.com/en/models/<id>`), for others it's the product page where you click EDA/CAD Models
2. Pick a tab (Ultra Librarian, SnapMagic, Manufacturer Provided), choose KiCad (or STEP for just the 3D model) and hit Download
3. The plugin watches your Downloads folder for 10 minutes and imports each file as it lands. A manufacturer STEP on its own gets attached to the footprint you just imported

I didn't make it log in and scrape those sites for you. They don't have a public API, they block bots, and it would break every time they change their site. One click in your own browser is way more reliable.

A bare part number only gets you DigiKey's search page. To land on the right models page straight away, register a free app at [developer.digikey.com](https://developer.digikey.com) and put its client ID and secret in `~/.config/kicad-importer/digikey.json` as `{"client_id": "...", "client_secret": "..."}` (or set `DIGIKEY_CLIENT_ID` and `DIGIKEY_CLIENT_SECRET`). The plugin then asks DigiKey's official API for the product and opens its models page. It still opens the page for you, you pick the format and click Download. With the credentials set it also asks the API for the manufacturer's STEP file. If DigiKey lists a link for it, the plugin downloads it and attaches it to the footprint, so the only thing left for you to download is the Ultra Librarian or SnapMagic KiCad zip. If DigiKey doesn't list one, the log says so.

### DigiKey symbols instead of LCSC ones

LCSC's symbols are sometimes off. Tick "Use DigiKey symbols" in the import window (it remembers the setting) and every import goes like this:

1. The part imports from LCSC like normal, so it works right away
2. DigiKey's models page opens. Download the KiCad (V6 and later) format from SnapMagic or Ultra Librarian
3. The plugin swaps that symbol in and points it at LCSC's footprint and 3D model, which stay as they are

It checks that the symbol's pin numbers exist on the footprint. Diodes and transistors are the usual suspects (`A`/`K` vs `1`/`2`), so if you see a warning, check the pinout before using the part.

If you saved a download somewhere else, use the "Import file..." button and pick it.

Some downloads are a whole part family (Würth's zip for 744042100 has every WE-TPC inductor in it). The plugin only takes the part you asked for, plus the footprint and 3D model it uses.

If EasyEDA starts saying it's refusing requests, that's their rate limit. Wait a few minutes and try again.

## Using it without KiCad

You can also run it from a terminal with plain Python 3.9+, no KiCad and no extra packages. Clone the repo and run it from the repo folder:

```
python3 -m plugins.importer C2040 595-LM358DR ~/Downloads/ul_LT1618.zip --project ~/my-project
```

It does the same thing as the button: same library, same lib tables, same `parts.csv`. You can pass downloaded zips or files too, add `--mpn 744042100` to only take that part out of a family zip. If a distributor link has no exact LCSC match it prints the closest ones and you rerun it with the C number you want. Leave out `--project` and it uses the folder you're in. Running it with no arguments just runs the link parser self-check.

To throw parts out of the library again, use `--remove` with symbol names or patterns. It also deletes the footprints, 3D models and `parts.csv` rows nothing uses anymore:

```
python3 -m plugins.importer --remove '2828_*' '4818_*' --project ~/my-project
```

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

One row per part, with these columns:

| column | what it is |
| --- | --- |
| source | lcsc, digikey, mouser, newark, farnell, element14, or mpn for a bare part number |
| url | the link you pasted (empty for a bare LCSC number) |
| mpn | manufacturer part number |
| lcsc_id | LCSC number, empty for parts that came from a download. Reimporting replaces the row (matched on LCSC number, or the MPN when there isn't one) |
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
