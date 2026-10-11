# geonode-tdc-sync

Mirror datasets from a [GeoNode](https://geonode.org) data portal to the
[Transport Data Commons](https://transport-data.org) (TDC) CKAN catalogue,
so that publishing once on your own portal also publishes on TDC.

Built by [Transport for Cairo](https://transportforcairo.com) to keep its portal,
[data.transportforcairo.com](https://data.transportforcairo.com), in step with the
TfC organisation on [ckan.transport-data.org](https://ckan.transport-data.org/organization/tfc).
The first run (September 2026) mirrored 43 records — GTFS feeds, route networks, stops,
terminals and survey datasets across six cities — with their files and metadata.

TDC's CKAN has no harvester, so every member organisation pushes its records. This tool
is one way to do that from GeoNode without re-entering metadata by hand.

## What it does

- **Selects** GeoNode datasets and documents with a filter you set in the config —
  ideally a `tdc` keyword, so that publishing to TDC is just tagging a record on your portal.
- **Maps metadata** from GeoNode onto TDC's schema: title, abstract, purpose, supplemental
  information, data-quality statement, attribution, keywords, regions, dates, licence.
- **Fills TDC-only fields** (`topics`, `sectors`, `modes`, `services`) from keyword rules,
  so records don't all land with identical metadata.
- **Uploads the original file** (shapefile zip, GTFS zip, …) as a `data` resource, and adds
  a URL-only "Live WFS (GeoJSON)" resource for vector layers.
- **Is idempotent.** Each TDC record stores the GeoNode UUID in `related_datasets`
  (`geonode:<uuid>`) and GeoNode's `last_updated` in a `comments[]` entry with initials `SYNC`.
  Re-runs skip unchanged records, patch changed ones, and re-upload a file only when GeoNode
  changed. Fields people edit on TDC that the tool doesn't derive (indicators, units,
  dimensioning, introduction text, extra related datasets) are kept.
- **Never touches** TDC records without a `geonode:` marker, so it can share an organisation
  with records entered by hand.

## Quick start

Requires Python 3.9+, a GeoNode 4.x portal (it uses `/api/v2`), and an API token for a
TDC account that can edit your organisation.

```bash
pip install -r requirements.txt
cp config.example.yaml my_portal.yaml          # then edit it
export TDC_API_TOKEN=...                        # ckan.transport-data.org → your user page → API tokens

python geonode_tdc_sync.py --config my_portal.yaml --dry-run      # see what would be sent
python geonode_tdc_sync.py --config my_portal.yaml --only 188     # push one record first
python geonode_tdc_sync.py --config my_portal.yaml                # then everything
```

Options: `--dry-run`, `--only <pk,pk>`, `--force` (re-push even when unchanged), `-v`, `--version`.
The exit code is non-zero if any record failed, so it can run under cron or CI.

## Configuration

`config.example.yaml` is a commented template. `sync_config.yaml` is Transport for Cairo's
live configuration, kept here as a worked example.

| Key | Purpose |
|---|---|
| `geonode_url`, `tdc_url` | The two endpoints |
| `tdc_org` | Your organisation slug on TDC |
| `portal_name`, `name_prefix` | Used in resource names, the `sources` field and generated record names |
| `select` | GeoNode `/api/v2/resources` filters; results are unioned |
| `exclude` | GeoNode pks never to mirror |
| `require_published` | Skip records with `is_published=false` on GeoNode |
| `mirror_apps` | Also mirror geostories, dashboards and maps as URL-only records |
| `license_map` | GeoNode licence identifier → TDC `license_id` |
| `defaults` | Fallback values, including the licence for records with none on GeoNode |
| `keyword_rules` | GeoNode keyword slugs / title words → TDC `topics`, `sectors`, `modes`, `services` |
| `overrides` | Per-record values keyed by GeoNode pk |

## How GeoNode and TDC differ

Three mismatches are handled explicitly, and are worth knowing about if you adapt the tool:

- **Regions.** GeoNode stores a region *tree* (continent `AFR` → sub-region `NAF` → country `EGY`).
  TDC has two flat fields: `geographies` (lower-case ISO3 countries) and `regions`
  (`afr`, `eur`, `asi`, `ame_n`, `ame_s`, `oce`, `worldwide`). Because `geographies` is free text,
  a sub-region like `naf` would be accepted silently and make the record invisible to country
  filters. The tool reads `/api/v2/regions`, keeps country codes as `geographies`, maps
  continents to `regions`, drops sub-regions, and warns when a record has no country.
- **Controlled vocabularies.** `topics`, `sectors`, `modes` and `services` exist only on TDC and
  are validated by CKAN; unknown values are rejected. Valid values are listed at
  `/api/3/action/scheming_dataset_schema_show?type=dataset`.
- **Resource types.** GeoNode datasets and documents carry files; geostories, dashboards and maps
  do not, so they are skipped unless `mirror_apps: true`.

TDC's schema has no city field and no spatial extent, so city-level information travels only in
titles, descriptions and tags.

## Before your first real run

1. **Licence.** Records with a blank or unmapped licence on GeoNode get `defaults.license_id`.
   Decide that value deliberately, and set real licences on GeoNode where they differ.
2. **Approval.** New records may enter TDC as pending; check with the TDC team how approval
   works for your organisation.
3. **Start private.** `defaults.private: true` keeps records unlisted until you've reviewed them.
4. **Large files.** Uploads go through CKAN to object storage; for files above ~1 GB, consider a
   URL-only resource instead (see `sync_one`).

Run it by hand after each release, or nightly under cron on the portal server.

## Licence

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
Copyright 2026 Transport for Cairo.

This licence covers the code only. The datasets the tool moves keep their own licences.
