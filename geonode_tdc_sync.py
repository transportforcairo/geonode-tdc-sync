#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Transport for Cairo
"""
geonode_tdc_sync.py — mirror datasets from a GeoNode portal to the
Transport Data Commons CKAN (ckan.transport-data.org).

Written for the Transport for Cairo portal (data.transportforcairo.com ->
TDC organisation "tfc"), but every portal-specific value is configurable:
see `geonode_url`, `tdc_org`, `portal_name` and `name_prefix` in the config.

Idempotent: each TDC record carries the GeoNode UUID in `related_datasets`
(as "geonode:<uuid>") and the GeoNode `last_updated` stamp in a comment,
so re-runs update-in-place and only re-upload files when GeoNode changed.

Usage
  python geonode_tdc_sync.py --config sync_config.yaml [--dry-run] [--only 188,189]
  TDC_API_TOKEN=... python geonode_tdc_sync.py --config sync_config.yaml

Requires:  pip install requests pyyaml
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from urllib.parse import urlparse

import requests
import yaml

__version__ = "1.0.0"

log = logging.getLogger("tdc-sync")

# Defaults (all overridable in the config file)
GEONODE = "https://data.transportforcairo.com"
TDC = "https://ckan.transport-data.org"
TDC_ORG = "tfc"
PORTAL_NAME = "TfC data portal"
MARK_PREFIX = "geonode:"          # stored in related_datasets
STAMP_INITIALS = "SYNC"           # stored in comments[]

# GeoNode regions are a tree: level 0 = Global, 1 = continent, 2 = sub-region
# (e.g. NAF "North Africa"), 3 = country (ISO-3166 alpha-3).
# TDC has TWO separate fields: `geographies` (lower-case ISO3 countries) and
# `regions` (its own continent slugs). Sub-regions like NAF exist in neither, so
# they must be dropped — `geographies` is free text on TDC and would silently
# accept "naf", producing a record that no country filter can find.
GEONODE_LEVEL_COUNTRY = 3
GEONODE_LEVEL_CONTINENT = 1

# GeoNode continent code -> TDC `regions` slug (TDC vocabulary:
# afr, eur, asi, ame_n, ame_s, oce, worldwide)
CONTINENT_TO_TDC_REGION = {
    "AFR": "afr", "EUR": "eur", "ASI": "asi", "OCE": "oce",
    "NAM": "ame_n", "SAM": "ame_s", "GLO": "worldwide",
}

# GeoNode resource types that carry downloadable data. Geostories, dashboards and
# maps are GeoNode app objects with no file behind them — they can only be
# mirrored as URL-only records (see `mirror_apps` in the config).
DATA_TYPES = ("dataset", "document")
APP_TYPES = ("geostory", "dashboard", "map")


# ----------------------------------------------------------------------------- GeoNode
class GeoNode:
    def __init__(self, base: str = GEONODE, auth: tuple[str, str] | None = None):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self._levels: dict[str, int] | None = None
        if auth:
            self.s.auth = auth

    def region_levels(self) -> dict[str, int]:
        """code -> level, so we can tell countries (3) from continents (1)/sub-regions (2)."""
        if getattr(self, "_levels", None) is None:
            r = self.s.get(f"{self.base}/api/v2/regions",
                           params={"page_size": 500, "format": "json"}, timeout=60)
            r.raise_for_status()
            j = r.json()
            self._levels = {x["code"]: x["level"] for x in (j.get("regions") or j.get("results", []))}
        return self._levels

    def resources(self, filters: dict[str, str]) -> list[dict]:
        """Iterate /api/v2/resources with django-rest-framework-filters syntax."""
        out, page = [], 1
        params = {"page_size": 50, "format": "json"}
        params.update({f"filter{{{k}}}": v for k, v in filters.items()})
        while True:
            params["page"] = page
            r = self.s.get(f"{self.base}/api/v2/resources", params=params, timeout=60)
            r.raise_for_status()
            j = r.json()
            out.extend(j["resources"])
            if not j["links"].get("next"):
                return out
            page += 1

    def detail(self, pk: str, resource_type: str) -> dict:
        ep = "datasets" if resource_type == "dataset" else "documents"
        r = self.s.get(f"{self.base}/api/v2/{ep}/{pk}", params={"format": "json"}, timeout=60)
        r.raise_for_status()
        return r.json()[ep[:-1]]  # "dataset" / "document"

    def download(self, res: dict, dest_dir: Path) -> Path:
        """Download the original uploaded asset (shapefile zip / GTFS zip / etc.)."""
        # Prefer the asset download (original upload); fall back to document download.
        urls = [d["url"] for d in res.get("download_urls", []) if "/assets/" in d["url"]] or \
               [d["url"] for d in res.get("download_urls", [])] or [res.get("download_url")]
        url = next(u for u in urls if u)
        with self.s.get(url, stream=True, timeout=600, allow_redirects=True) as r:
            r.raise_for_status()
            fname = _filename_from_response(r) or f"{res['alternate'].split(':')[-1]}.zip"
            path = dest_dir / fname
            with open(path, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        return path


def _filename_from_response(r: requests.Response) -> str | None:
    cd = r.headers.get("content-disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    if m:
        return m.group(1)
    tail = r.url.rstrip("/").split("/")[-1]
    return tail if "." in tail else None


# ----------------------------------------------------------------------------- CKAN
class CKAN:
    def __init__(self, base: str, token: str, org: str = TDC_ORG):
        self.base = base.rstrip("/")
        self.org = org
        self.s = requests.Session()
        self.s.headers["Authorization"] = token

    def action(self, name: str, data: dict | None = None, files: dict | None = None) -> Any:
        url = f"{self.base}/api/3/action/{name}"
        if files:
            r = self.s.post(url, data=data, files=files, timeout=900)
        else:
            r = self.s.post(url, json=data or {}, timeout=120)
        try:
            j = r.json()
        except ValueError:
            r.raise_for_status()
            raise
        if not j.get("success"):
            raise RuntimeError(f"{name} failed: {json.dumps(j.get('error'), indent=1)[:2000]}")
        return j["result"]

    def find_by_geonode_uuid(self, uuid: str) -> dict | None:
        res = self.action("package_search", {
            "fq": f'organization:{self.org} AND related_datasets:"{MARK_PREFIX}{uuid}"',
            "rows": 5, "include_private": True,
        })
        for p in res["results"]:
            if f"{MARK_PREFIX}{uuid}" in (p.get("related_datasets") or []):
                return p
        # Fallback: some CKAN builds don't index scheming multi-text fields; scan our org.
        res = self.action("package_search", {"fq": f"organization:{self.org}", "rows": 1000,
                                             "include_private": True})
        for p in res["results"]:
            if f"{MARK_PREFIX}{uuid}" in (p.get("related_datasets") or []):
                return p
        return None


# ----------------------------------------------------------------------------- mapping
def slugify(s: str, maxlen: int = 90) -> str:
    s = re.sub(r"[\[\]\(\)]", " ", s.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:maxlen].rstrip("-")


def infer_vocab(gn: dict, cfg: dict, field: str) -> list[str]:
    """Union of `keyword_rules` hits for this resource's GeoNode keywords + title."""
    slugs = {k["slug"] for k in gn.get("keywords", [])}
    hay = f"{gn.get('title','')} {gn.get('abstract','')}".lower()
    got: list[str] = []
    for rule in cfg.get("keyword_rules", []):
        match = set(rule.get("keywords", [])) & slugs or \
            any(w.lower() in hay for w in rule.get("title_contains", []))
        if match:
            for v in rule.get(field, []):
                if v not in got:
                    got.append(v)
    return got


def build_package(gn: dict, cfg: dict, existing: dict | None,
                  region_levels: dict[str, int] | None = None) -> dict:
    """Map a GeoNode resource (dataset or document) to a TDC CKAN package dict."""
    d = cfg["defaults"]
    o = (cfg.get("overrides") or {}).get(str(gn["pk"]), {})
    org = cfg.get("tdc_org", TDC_ORG)
    portal_name = cfg.get("portal_name", PORTAL_NAME)
    portal_host = urlparse(cfg.get("geonode_url", GEONODE)).netloc
    title = o.get("title") or gn["title"]
    prefix = cfg.get("name_prefix", f"{org}-")
    name = existing["name"] if existing else (o.get("name") or slugify(f"{prefix}{title}"))

    lv = region_levels or {}
    codes = [r["code"] for r in gn.get("regions", [])]
    # Countries only (level 3). Sub-regions like NAF and continents are dropped here.
    geographies = o.get("geographies") or \
        [c.lower() for c in codes if lv.get(c) == GEONODE_LEVEL_COUNTRY] or \
        d.get("geographies", [])
    tdc_regions = o.get("regions") or [
        s for s in dict.fromkeys(CONTINENT_TO_TDC_REGION.get(c) for c in codes
                                 if lv.get(c) == GEONODE_LEVEL_CONTINENT) if s
    ] or d.get("regions", [])
    if not geographies:
        log.warning("  %s (pk %s) has no country-level region on GeoNode -> no geographies on TDC",
                    gn["title"], gn["pk"])

    notes = gn.get("abstract") or title
    overview_parts = []
    if gn.get("raw_purpose") or gn.get("purpose"):
        overview_parts.append(f"**Purpose**\n\n{gn.get('raw_purpose') or gn.get('purpose')}")
    if gn.get("supplemental_information"):
        overview_parts.append(f"**Supplemental information**\n\n{gn['supplemental_information']}")
    if gn.get("data_quality_statement"):
        overview_parts.append(f"**Data quality**\n\n{gn['data_quality_statement']}")
    if gn.get("attribution"):
        overview_parts.append(f"**Attribution**\n\n{gn['attribution']}")
    overview_parts.append(f"Authoritative copy: {gn['detail_url']}")

    lic = (gn.get("license") or {}).get("identifier") or ""
    license_id = o.get("license_id") or cfg.get("license_map", {}).get(lic) or d.get("license_id")

    date = (gn.get("date") or "")[:10]
    t_start = (gn.get("temporal_extent_start") or o.get("temporal_coverage_start") or date or None)
    t_end = (gn.get("temporal_extent_end") or o.get("temporal_coverage_end") or date or None)

    pkg = {
        "name": name,
        "title": title,
        "notes": notes,
        "overview_text": "\n\n".join(overview_parts),
        "owner_org": org,
        "license_id": license_id,
        "tag_string": ",".join([k["name"] for k in gn.get("keywords", [])]
                               + [t for t in str(o.get("tag_string_extra", "")).split(",") if t]),
        "tdc_category": o.get("tdc_category", d.get("tdc_category", "community")),
        "geographies": geographies,
        "regions": tdc_regions,
        "topics": o.get("topics") or infer_vocab(gn, cfg, "topics") or d.get("topics", []),
        "sectors": o.get("sectors") or infer_vocab(gn, cfg, "sectors") or d.get("sectors", []),
        "modes": o.get("modes") or infer_vocab(gn, cfg, "modes") or d.get("modes", []),
        "services": o.get("services") or infer_vocab(gn, cfg, "services") or d.get("services", []),
        "frequency": o.get("frequency", d.get("frequency", "not_planned")),
        "language": {"eng": "en"}.get(gn.get("language", "eng"), gn.get("language", "en")),
        "data_provider": o.get("data_provider", d.get("data_provider", org)),
        "data_access": o.get("data_access", d.get("data_access", "Publicly available")),
        "url": gn["detail_url"],
        "sources": o.get("sources", d.get("sources", [{"title": portal_name, "url": gn["detail_url"]}])),
        "temporal_coverage_start": t_start[:10] if t_start else None,
        "temporal_coverage_end": t_end[:10] if t_end else None,
        "is_archived": False,
        "private": bool(o.get("private", d.get("private", False))),
        "related_datasets": [f"{MARK_PREFIX}{gn['uuid']}"],
        "comments": [{"initials": STAMP_INITIALS, "date": gn["last_updated"][:19],
                      "comment": f"auto-mirrored from {portal_host}"}],
    }
    if existing:
        # keep anything a human added on TDC that we don't own
        for keep in ("related_datasets",):
            extra = [x for x in (existing.get(keep) or []) if not x.startswith(MARK_PREFIX)]
            pkg[keep] = extra + pkg[keep]
        for keep in ("indicators", "units", "dimensioning", "introduction_text"):
            if existing.get(keep):
                pkg[keep] = existing[keep]
    return {k: v for k, v in pkg.items() if v is not None}


def geonode_stamp(pkg: dict) -> str | None:
    for c in pkg.get("comments") or []:
        if c.get("initials") == STAMP_INITIALS:
            return c.get("date")
    return None


def guess_format(path: Path, gn: dict) -> str:
    ext = path.suffix.lower().lstrip(".")
    if gn.get("resource_type") == "document" and ext == "zip" and "gtfs" in gn["title"].lower():
        return "GTFS"
    if gn.get("subtype") == "vector" and ext == "zip":
        return "SHP"
    return {"geojson": "GeoJSON", "gpkg": "GPKG", "csv": "CSV", "xlsx": "XLSX", "pdf": "PDF",
            "zip": "ZIP", "tif": "GeoTIFF", "tiff": "GeoTIFF"}.get(ext, ext.upper() or "ZIP")


# ----------------------------------------------------------------------------- sync
def sync_one(gn_res: dict, geonode: GeoNode, ckan: CKAN, cfg: dict, dry: bool) -> None:
    gn = geonode.detail(gn_res["pk"], gn_res["resource_type"])
    if not gn.get("is_approved", True):
        log.info("skip %s (not approved on GeoNode)", gn["title"])
        return
    if cfg.get("require_published", False) and not gn.get("is_published", True):
        log.warning("skip %s (pk %s): is_published=false on GeoNode. "
                    "Publish it there, or set require_published: false in the config.",
                    gn["title"], gn["pk"])
        return
    existing = ckan.find_by_geonode_uuid(gn["uuid"])
    pkg = build_package(gn, cfg, existing, geonode.region_levels())

    if existing and geonode_stamp(existing) == gn["last_updated"][:19] and not cfg.get("force"):
        log.info("up-to-date: %s -> %s", gn["title"], existing["name"])
        return

    if dry:
        log.info("[dry-run] %s %s\n%s", "UPDATE" if existing else "CREATE", pkg["name"],
                 json.dumps(pkg, indent=1)[:1500])
        return

    if existing:
        pkg["id"] = existing["id"]
        result = ckan.action("package_patch", pkg)
        log.info("patched %s", result["name"])
    else:
        result = ckan.action("package_create", pkg)
        log.info("created %s", result["name"])

    if gn.get("resource_type") in APP_TYPES:
        # Geostory / dashboard / map: URL-only record, nothing to upload.
        have = [r for r in result.get("resources", []) if r.get("url") == gn["detail_url"]]
        if not have:
            ckan.action("resource_create", {
                "package_id": result["id"],
                "name": f"View on {cfg.get('portal_name', PORTAL_NAME)} ({gn['resource_type']})",
                "url": gn["detail_url"], "format": "HTML", "resource_type": "documentation"})
        return

    # --- resources (files): replace our "data" resource, keep others humans added
    with tempfile.TemporaryDirectory() as tmp:
        fpath = geonode.download(gn, Path(tmp))
        fmt = guess_format(fpath, gn)
        ours = [r for r in result.get("resources", []) if r.get("name") == fpath.name
                or (r.get("description") or "").startswith(MARK_PREFIX)]
        meta = {"package_id": result["id"], "name": fpath.name, "format": fmt,
                "resource_type": "data", "description": f"{MARK_PREFIX}{gn['uuid']} {gn['last_updated']}"}
        with open(fpath, "rb") as fh:
            files = {"upload": (fpath.name, fh)}
            if ours:
                meta["id"] = ours[0]["id"]
                ckan.action("resource_patch", meta, files=files)
                log.info("  replaced file %s (%s)", fpath.name, fmt)
            else:
                ckan.action("resource_create", meta, files=files)
                log.info("  uploaded file %s (%s)", fpath.name, fmt)

    # Link to the WFS / portal page as a second, URL-only resource for vectors
    if gn.get("resource_type") == "dataset" and gn.get("subtype") == "vector":
        wfs = (f"{geonode.base}/geoserver/ows?service=WFS&version=1.0.0&request=GetFeature"
               f"&typeName={gn['alternate']}&outputFormat=application/json")
        have = [r for r in result.get("resources", []) if r.get("url") == wfs]
        if not have:
            ckan.action("resource_create", {"package_id": result["id"], "name": "Live WFS (GeoJSON)",
                                            "url": wfs, "format": "GeoJSON", "resource_type": "data",
                                            "hide_preview": True})


def main() -> int:
    ap = argparse.ArgumentParser(description="Mirror GeoNode datasets to the Transport Data Commons CKAN.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--config", default="sync_config.yaml")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="comma-separated GeoNode pks to sync")
    ap.add_argument("--force", action="store_true", help="re-push even if stamps match")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.v else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    cfg["force"] = a.force
    token = os.environ.get("TDC_API_TOKEN") or cfg.get("tdc_api_token")
    if not token and not a.dry_run:
        log.error("no TDC token: set TDC_API_TOKEN or tdc_api_token in config")
        return 2

    gn_auth = None
    if cfg.get("geonode_user") and cfg.get("geonode_password"):
        gn_auth = (cfg["geonode_user"], cfg["geonode_password"])
    geonode = GeoNode(cfg.get("geonode_url", GEONODE), gn_auth)
    ckan = CKAN(cfg.get("tdc_url", TDC), token or "dry", cfg.get("tdc_org", TDC_ORG))

    # Selection: everything matching the configured filters (e.g. a keyword), minus excludes
    wanted = DATA_TYPES + (APP_TYPES if cfg.get("mirror_apps") else ())
    selected: dict[str, dict] = {}
    skipped_apps = []
    for flt in cfg.get("select") or []:
        for r in geonode.resources(flt):
            if r["resource_type"] in wanted:
                selected[str(r["pk"])] = r
            elif r["resource_type"] in APP_TYPES:
                skipped_apps.append(f"{r['pk']} {r['resource_type']}: {r['title']}")
    if skipped_apps:
        log.info("skipping %d GeoNode app objects (no downloadable file): %s",
                 len(skipped_apps), "; ".join(skipped_apps[:5]))
    excl = {str(x) for x in cfg.get("exclude", [])}
    if a.only:
        keep = {x.strip() for x in a.only.split(",")}
        selected = {k: v for k, v in selected.items() if k in keep}
    selected = {k: v for k, v in selected.items() if k not in excl}
    log.info("%d GeoNode resources selected", len(selected))

    failures = 0
    for pk, r in selected.items():
        try:
            sync_one(r, geonode, ckan, cfg, a.dry_run)
        except Exception as e:  # noqa: BLE001
            failures += 1
            log.exception("FAILED %s (%s): %s", r.get("title"), pk, e)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
