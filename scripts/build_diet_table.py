#!/usr/bin/env python3
"""Derive Aviary's bundled diet lookup from the AVONET dataset.

Aviary ships a compact, gzipped subset of AVONET rather than the 18 MB original: just the
few ecological columns the species pages display, keyed on eBird scientific names (which
is what BirdNET-Go emits, so lookups match without synonym handling).

Run this only to regenerate the table — the add-on itself needs neither this script nor
openpyxl at runtime.

    pip install openpyxl
    python scripts/build_diet_table.py            # downloads AVONET, writes the table
    python scripts/build_diet_table.py --zip ELEData.zip   # reuse a local download

Source: Tobias et al. (2022) AVONET: morphological, ecological and geographical data for
all birds. Ecology Letters. https://doi.org/10.6084/m9.figshare.16586228 — CC BY 4.0.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import os
import sys
import urllib.request
import zipfile

# figshare download for ELEData.zip (item 16586228). Contains AVONET2_eBird.xlsx.
ELEDATA_URL = "https://ndownloader.figshare.com/files/38429873"
SHEET_PATH = "ELEData/TraitData/AVONET2_eBird.xlsx"
SHEET_NAME = "AVONET2_eBird"

# Columns kept, in output order. AVONET's own values are written through verbatim; the
# mapping to human-readable food descriptions lives in app/traits.py so the data file
# stays a faithful subset.
FIELDS = ("species", "niche", "level", "lifestyle", "habitat")
SOURCE_COLUMNS = ("Species2", "Trophic.Niche", "Trophic.Level", "Primary.Lifestyle", "Habitat")

OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "aviary", "app", "data", "avonet_diet.csv.gz",
)


def load_rows(zip_bytes: bytes) -> list[tuple[str, ...]]:
    try:
        import openpyxl
    except ImportError:
        sys.exit("openpyxl is required to regenerate the table: pip install openpyxl")

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        xlsx = io.BytesIO(z.read(SHEET_PATH))
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    ws = wb[SHEET_NAME]
    rows = ws.iter_rows(values_only=True)
    header = list(next(rows))
    idx = {name: header.index(name) for name in SOURCE_COLUMNS}

    out: list[tuple[str, ...]] = []
    for row in rows:
        if not row:
            continue
        values = []
        for name in SOURCE_COLUMNS:
            v = row[idx[name]]
            v = "" if v is None or str(v).strip() in ("", "NA") else str(v).strip()
            values.append(v)
        # Species name is the key; skip rows with nothing worth looking up.
        if not values[0] or not any(values[1:]):
            continue
        out.append(tuple(values))
    out.sort()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", help="path to a previously downloaded ELEData.zip")
    args = ap.parse_args()

    if args.zip:
        with open(args.zip, "rb") as f:
            blob = f.read()
    else:
        print(f"Downloading AVONET ELEData.zip from figshare ({ELEDATA_URL}) …")
        with urllib.request.urlopen(ELEDATA_URL) as resp:  # noqa: S310 - fixed URL
            blob = resp.read()

    rows = load_rows(blob)
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)

    # Serialize fully in memory, then compress in one shot. Writing csv through a
    # TextIOWrapper around a GzipFile silently truncates the tail, because closing the
    # GzipFile does not flush the wrapper's buffer.
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(FIELDS)
    writer.writerows(rows)
    payload = buf.getvalue().encode("utf-8")
    # mtime=0 so regenerating identical data produces a byte-identical file (no
    # gratuitous diffs in version control).
    with open(OUT_PATH, "wb") as f:
        f.write(gzip.compress(payload, compresslevel=9, mtime=0))

    # Read the file back and assert the round trip, so a truncated table can't ship.
    with gzip.open(OUT_PATH, "rt", encoding="utf-8", newline="") as f:
        back = list(csv.DictReader(f))
    if len(back) != len(rows):
        sys.exit(f"round-trip mismatch: wrote {len(rows)} rows, read back {len(back)}")

    print(f"Wrote {len(rows):,} species -> {OUT_PATH} "
          f"({os.path.getsize(OUT_PATH):,} bytes gzipped; round trip verified)")


if __name__ == "__main__":
    main()
