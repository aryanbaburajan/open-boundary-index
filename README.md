# Open Boundary Index

Release-ready, web-simplified administrative boundaries derived from
[geoBoundaries](https://www.geoboundaries.org/)' gbOpen data.

The repository contains the reproducible generator and small source records.
Generated geometry belongs in versioned GitHub Release assets rather than Git,
so app deployments never need to bundle global ADM2 boundaries.

## Generate a release

```bash
python3 generate.py --output . --version 2026-08-24 --countries IND
```

Omit `--countries` for a global release. The output is written beneath
`dist/<version>/` and contains:

- `chunks/<ADM level>/<ISO3>.geojson.gz`: independently fetched geometry.
- `manifests/<ADM level>/<ISO3>.json.gz`: searchable name, aliases, bounds,
  centroid, and chunk references.
- `sources.json`, `SHA256SUMS`, and `ATTRIBUTION.txt`.

The generator rejects compressed assets larger than 100 MiB, the GitHub
regular-Git file limit. Upload the contents of `dist/<version>/` as release
assets and keep the version unchanged forever so shared maps remain
reproducible.

## Attribution

Boundary data is sourced from geoBoundaries, gbOpen. Each generated release
includes the upstream source records and required attribution.
