#!/usr/bin/env python3
"""
sentinel_change.py - muutostunnistus Sentinel-2-satelliittikuvista.

Annat pisteen (lat/lon) ja kaksi päivämäärää. Skripti:
  1. Hakee kummankin päivän ympäriltä Sentinel-2 L2A -kuvat
     Element84 Earth Search -STAC-katalogista (ilmainen, ei tiliä).
  2. Valitsee kuvan, jossa tutkittava alue on vähiten pilvessä
     (pilvet tunnistetaan kuvan oman SCL-luokittelukerroksen avulla).
  3. Lukee vain tarvittavan alueen pilvestä (COG = Cloud Optimized GeoTIFF),
     joten koko 100 x 100 km kuvaa ei ladata.
  4. Laskee indeksin (NBR paloille, NDVI kasvillisuudelle) ja niiden erotuksen.
  5. Tallentaa vertailukuvan (PNG) ja erotuskartan (GeoTIFF, avautuu QGIS:ssä).

Esimerkki (Palisades-palo, Los Angeles, tammikuu 2025):
    python3 sentinel_change.py --lat 34.07 --lon -118.55 \\
        --before 2024-12-28 --after 2025-01-20 --size-km 12 --index nbr
"""

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # ei näyttöä tarvita, piirretään suoraan tiedostoon
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from pystac_client import Client
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform as transform_coords

STAC_URL = "https://earth-search.aws.element84.com/v1"
COLLECTION = "sentinel-2-l2a"
RESOLUTION = 10  # metriä per pikseli (Sentinel-2:n tarkimmat kanavat)

# GDAL-asetukset: julkinen S3, ei turhia hakemistolistauksia -> nopeampi luku
os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("GDAL_HTTP_MULTIRANGE", "YES")
os.environ.setdefault("GDAL_HTTP_MERGE_CONSECUTIVE_RANGES", "YES")

# SCL (Scene Classification Layer) -luokat, jotka tulkitaan "ei käyttökelpoiseksi":
# 0 ei dataa, 1 viallinen, 3 pilven varjo, 8-9 pilvi, 10 ohut yläpilvi
SCL_BAD = [0, 1, 3, 8, 9, 10]
# Vesi (6) ei ole "pilvinen", mutta NDVI/NBR on vedellä pelkkää kohinaa,
# joten se jätetään indeksivertailusta pois.
SCL_WATER = 6

# Indeksit: (kanava A, kanava B) -> (A - B) / (A + B)
INDICES = {
    "ndvi": ("nir", "red"),     # kasvillisuus: korkea = vihreää
    "nbr": ("nir", "swir22"),   # Normalized Burn Ratio: matala = palanutta
}


# ---------------------------------------------------------------------------
# Kohdealue: neliö pisteen ympärillä UTM-koordinaateissa (metreinä)
# ---------------------------------------------------------------------------

@dataclass
class Grid:
    crs: str
    transform: object
    width: int
    height: int


def make_grid(lat: float, lon: float, size_km: float) -> Grid:
    """Neliö pisteen ympärille sen UTM-vyöhykkeellä, 10 m pikseleinä."""
    zone = int((lon + 180) // 6) + 1
    epsg = (32600 if lat >= 0 else 32700) + zone
    crs = f"EPSG:{epsg}"
    xs, ys = transform_coords("EPSG:4326", crs, [lon], [lat])
    half = size_km * 1000 / 2
    n = int(size_km * 1000 / RESOLUTION)
    tr = from_origin(xs[0] - half, ys[0] + half, RESOLUTION, RESOLUTION)
    return Grid(crs, tr, n, n)


def bbox_wgs84(lat: float, lon: float, size_km: float) -> list[float]:
    """Karkea lat/lon-laatikko STAC-hakua varten."""
    dlat = size_km / 2 / 111.0
    dlon = size_km / 2 / (111.0 * max(np.cos(np.radians(lat)), 0.01))
    return [lon - dlon, lat - dlat, lon + dlon, lat + dlat]


# ---------------------------------------------------------------------------
# Datan luku
# ---------------------------------------------------------------------------

def read_band(item, asset: str, grid: Grid, resampling=Resampling.bilinear) -> np.ndarray:
    """
    Lukee yhden kanavan suoraan pilvestä ja projisoi sen kohdealueen ruudukkoon.
    WarpedVRT hoitaa koordinaatistomuunnoksen ja eri resoluutiot (10 m / 20 m),
    joten kaikki kanavat ja molemmat päivät osuvat pikselilleen samaan kohtaan.
    """
    a = item.assets[asset]
    with rasterio.open(a.href) as src, WarpedVRT(
        src, crs=grid.crs, transform=grid.transform,
        width=grid.width, height=grid.height, resampling=resampling,
    ) as vrt:
        data = vrt.read(1).astype("float32")
    if asset == "scl":
        return data
    # Muunnos heijastusarvoksi (0..1). Earth Search kertoo skaalan metadatassa.
    bands = a.extra_fields.get("raster:bands", [{}])
    scale = bands[0].get("scale", 0.0001)
    offset = bands[0].get("offset", 0.0)
    nodata = bands[0].get("nodata", 0)
    # HUOM: Earth Search on jo lisännyt -0.1 BOA-offsetin pikseliarvoihin
    # (processing baseline >= 04.00), mutta metadata listaa sen silti.
    # Jos se lisättäisiin uudestaan, tummat pinnat menisivät negatiivisiksi
    # ja NDVI/NBR vääristyisivät.
    if item.properties.get("earthsearch:boa_offset_applied"):
        offset = 0.0
    out = data * scale + offset
    out[data == nodata] = np.nan
    return out


def cloud_fraction(item, grid: Grid) -> float:
    """Kuinka suuri osa kohdealueesta on pilveä/varjoa/ei-dataa (0..1)."""
    scl = read_band(item, "scl", grid, Resampling.nearest)
    return float(np.isin(scl, SCL_BAD).mean())


def pick_best_item(client, bbox, target: date, window: int, grid: Grid, max_candidates: int):
    """Hakee kuvat päivämäärän ±window ympäriltä ja valitsee vähiten pilvisen."""
    start = (target - timedelta(days=window)).isoformat()
    end = (target + timedelta(days=window)).isoformat()
    search = client.search(
        collections=[COLLECTION], bbox=bbox, datetime=f"{start}/{end}",
        query={"eo:cloud_cover": {"lt": 80}},
    )
    items = list(search.items())
    if not items:
        sys.exit(f"Ei kuvia välillä {start} – {end}. Kasvata --window-arvoa.")

    # Ensin koko tiilen pilvisyyden mukaan, sitten tarkistetaan oma alue SCL:stä
    items.sort(key=lambda it: it.properties.get("eo:cloud_cover", 100))
    best, best_cf = None, 2.0
    for it in items[:max_candidates]:
        cf = cloud_fraction(it, grid)
        d = it.datetime.date()
        print(f"    {d}  {it.id:<32} pilveä alueella {cf:5.1%}")
        days_off = abs((d - target).days)
        # Vähiten pilveä voittaa; tasatilanteessa lähempi päivä
        if (cf, days_off) < (best_cf, abs((best.datetime.date() - target).days) if best else 999):
            best, best_cf = it, cf
        if cf < 0.01 and days_off <= 3:
            break  # käytännössä pilvetön ja lähellä -> ei tarvitse etsiä enempää
    return best, best_cf


def load_scene(item, grid: Grid, index: str) -> dict:
    """Lukee RGB:n, indeksin kanavat ja pilvimaskin yhdelle kuvalle."""
    a_name, b_name = INDICES[index]
    bands = {}
    for name in {"red", "green", "blue", a_name, b_name}:
        bands[name] = read_band(item, name, grid)
    scl = read_band(item, "scl", grid, Resampling.nearest)
    a, b = bands[a_name], bands[b_name]
    with np.errstate(divide="ignore", invalid="ignore"):
        idx = (a - b) / (a + b)
    bad = np.isin(scl, SCL_BAD + [SCL_WATER])
    idx[bad] = np.nan
    return {"rgb": np.dstack([bands["red"], bands["green"], bands["blue"]]),
            "index": idx, "bad": bad, "date": item.datetime.date(), "id": item.id}


# ---------------------------------------------------------------------------
# Tulokset
# ---------------------------------------------------------------------------

def to_display(rgb: np.ndarray) -> np.ndarray:
    """Heijastusarvot näytettäväksi kuvaksi: venytys 2.–98. persentiilin välille."""
    valid = rgb[np.isfinite(rgb)]
    lo, hi = np.percentile(valid, (2, 98)) if valid.size else (0, 0.3)
    return np.clip((np.nan_to_num(rgb) - lo) / (hi - lo + 1e-6), 0, 1)


def change_stats(diff: np.ndarray, index: str) -> list[str]:
    """Tulkinta erotukselle. NBR-rajat: USGS:n dNBR-luokitus (Key & Benson 2006)."""
    valid = diff[np.isfinite(diff)]
    if valid.size == 0:
        return ["Ei vertailukelpoisia pikseleitä (pilvet?)."]
    px_km2 = (RESOLUTION * RESOLUTION) / 1e6
    lines = [f"Vertailukelpoista aluetta: {valid.size * px_km2:.1f} km²"]
    if index == "nbr":
        classes = [("Matala palovakavuus", 0.10, 0.27), ("Kohtalainen–matala", 0.27, 0.44),
                   ("Kohtalainen–korkea", 0.44, 0.66), ("Korkea palovakavuus", 0.66, 9)]
        for name, lo, hi in classes:
            n = int(((valid >= lo) & (valid < hi)).sum())
            lines.append(f"  {name:<22} dNBR {lo:.2f}–{hi if hi < 9 else '':<4}: "
                         f"{n * px_km2:7.2f} km² ({n / valid.size:5.1%})")
        burned = (valid >= 0.27).sum()
        lines.append(f"Arvioitu palanut ala (dNBR ≥ 0.27): {burned * px_km2:.2f} km²")
    else:
        loss = (valid > 0.2).sum()
        gain = (valid < -0.2).sum()
        lines.append(f"Kasvillisuus vähentynyt (dNDVI > 0.2): {loss * px_km2:.2f} km²")
        lines.append(f"Kasvillisuus lisääntynyt (dNDVI < -0.2): {gain * px_km2:.2f} km²")
    return lines


def save_outputs(before, after, diff, grid: Grid, args, stats: list[str]) -> Path:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{args.lat:.4f}_{args.lon:.4f}_{before['date']}_{after['date']}_{args.index}"

    # 1) GeoTIFF erotuksesta -> avautuu QGIS:ssä oikeaan paikkaan kartalle
    tif = out_dir / f"{stem}_diff.tif"
    with rasterio.open(tif, "w", driver="GTiff", width=grid.width, height=grid.height,
                       count=1, dtype="float32", crs=grid.crs, transform=grid.transform,
                       nodata=np.nan, compress="deflate") as dst:
        dst.write(diff.astype("float32"), 1)

    # 2) Vertailukuva: ennen | jälkeen | muutos
    label = "dNBR (ennen − jälkeen)" if args.index == "nbr" else "dNDVI (ennen − jälkeen)"
    fig, axes = plt.subplots(1, 3, figsize=(18, 6.6), constrained_layout=True)
    axes[0].imshow(to_display(before["rgb"]))
    axes[0].set_title(f"Ennen: {before['date']}")
    axes[1].imshow(to_display(after["rgb"]))
    axes[1].set_title(f"Jälkeen: {after['date']}")
    lim = 0.8 if args.index == "nbr" else 0.5
    im = axes[2].imshow(diff, cmap="RdYlGn_r", vmin=-lim, vmax=lim)
    axes[2].set_title(f"Muutos: {label}\npunainen = menetystä, vihreä = kasvua")
    fig.colorbar(im, ax=axes[2], shrink=0.8)
    for ax in axes:
        ax.set_xticks([]); ax.set_yticks([])
    fig.suptitle(f"Sentinel-2 muutostunnistus  {args.lat:.4f}, {args.lon:.4f}  "
                 f"({args.size_km} × {args.size_km} km)", fontsize=14)
    fig.text(0.01, 0.005, "Data: Copernicus Sentinel-2 (ESA), Element84 Earth Search",
             fontsize=8, color="gray")
    png = out_dir / f"{stem}.png"
    fig.savefig(png, dpi=110)
    plt.close(fig)

    # 3) Tekstiyhteenveto
    txt = out_dir / f"{stem}.txt"
    txt.write_text("\n".join([
        f"Piste: {args.lat}, {args.lon}  Alue: {args.size_km} km",
        f"Ennen:  {before['date']}  {before['id']}",
        f"Jälkeen: {after['date']}  {after['id']}",
        f"Indeksi: {args.index.upper()}", "", *stats, ""]), encoding="utf-8")
    return png


# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Sentinel-2 muutostunnistus kahden päivän välillä.")
    p.add_argument("--lat", type=float, required=True, help="leveysaste, esim. 34.07")
    p.add_argument("--lon", type=float, required=True, help="pituusaste, esim. -118.55")
    p.add_argument("--before", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    p.add_argument("--after", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    p.add_argument("--size-km", type=float, default=5, help="alueen sivun pituus km (oletus 5)")
    p.add_argument("--index", choices=INDICES, default="nbr", help="nbr (palot) tai ndvi (kasvillisuus)")
    p.add_argument("--window", type=int, default=10, help="hakuikkuna ± päivää (oletus 10)")
    p.add_argument("--candidates", type=int, default=6, help="montako kuvaa tarkistetaan per päivä")
    p.add_argument("--out", default="tulokset", help="tuloskansio (oletus tulokset)")
    args = p.parse_args()

    if args.before >= args.after:
        sys.exit("--before pitää olla ennen --after-päivää.")
    if not 0.5 <= args.size_km <= 50:
        sys.exit("--size-km pitää olla välillä 0.5–50.")

    grid = make_grid(args.lat, args.lon, args.size_km)
    bbox = bbox_wgs84(args.lat, args.lon, args.size_km)
    client = Client.open(STAC_URL)

    scenes = []
    for label, target in (("ENNEN", args.before), ("JÄLKEEN", args.after)):
        print(f"[{label}] haetaan kuvia {target} ±{args.window} pv ...")
        item, cf = pick_best_item(client, bbox, target, args.window, grid, args.candidates)
        print(f"  -> valittu {item.datetime.date()} (pilveä {cf:.1%})")
        if cf > 0.5:
            print("  ! yli puolet alueesta pilvessä, tulos voi olla puutteellinen")
        print("  luetaan kanavat ...")
        scenes.append(load_scene(item, grid, args.index))
    before, after = scenes

    # Erotus: ennen - jälkeen -> positiivinen = indeksi laski (palo / kasvillisuuden menetys)
    diff = before["index"] - after["index"]
    stats = change_stats(diff, args.index)
    png = save_outputs(before, after, diff, grid, args, stats)

    print()
    print("\n".join(stats))
    print(f"\nTallennettu: {png}  (+ _diff.tif ja .txt)")


if __name__ == "__main__":
    main()
