# The series.json sidecar

reader.mokuro.app keeps one index per series in a file called `series.json`, inside the series folder, beside the volume files. The bridge writes it on every finalize, so a library delivered by the bridge or by a script is visible in the reader without importing anything by hand.

This document covers the format, why the uploader has to write it, and the pin that keeps this project's writer honest about a format it does not own. A destination managed by mokuro-bunko is the exception: Bunko compiles `series.json` and the root `catalog.json` itself, and the bridge uploads only the volume files; see [bunko.md](bunko.md).

## What the file is

`<Series Title>/series.json` holds the series facts the reader can share between devices plus an index of the series' volumes. Compact JSON, one object, `version: 2`:

```json
{"version":2,"series_title":"Bakemonogatari","external_ids":{},"titles":{},"synonyms":[],"updated_at":"1970-01-01T00:00:00.000Z","volumes":[{"volume_uuid":"cfb5220c-57db-4008-9f44-e659d794e381","volume_title":"v01","page_count":187,"character_count":13247,"mokuro_version":"0.2.2","archive_size":1234}]}
```

| Field | Type | Notes |
| --- | --- | --- |
| `version` | number | `2`. The reader's parser accepts `1` and `2` and drops the file for anything else. |
| `series_title` | string | The folder name. Required and non-blank. |
| `external_ids` | object | `anilist` and `mal`, positive integers, either may be absent. |
| `titles` | object | `native`, `romaji`, `english`, each a non-empty string when present. |
| `synonyms` | array of strings | Non-empty strings only. |
| `tag` | string | Optional, trimmed. |
| `unit` | string | Optional, `volumes` or `chapters`. Absent means auto-detect. |
| `spine_offset` | number | Optional, clamped to +/-50. Shelf alignment, never a fact. |
| `updated_at` | string | Required, ISO 8601. The merge key for the facts. |
| `volumes` | array | The per-volume index. |

Every volume entry requires `volume_uuid`, `volume_title`, `page_count`, `character_count` and `mokuro_version`, in that order. `spine_width`, `archive_size`, `mokuro_size`, `mokuro_modified`, `mokuro_sha256`, `cover_size`, `cover_modified` and `offset` are optional, and `offset` is always serialized last. A field that fails validation is dropped, and a volume entry that fails validation is dropped individually rather than failing the file.

Two rules decide which entry wins when two describe the same volume: they are the same volume when the `volume_uuid` matches or when the folded `volume_title` matches, and a measured entry beats a zero-count placeholder. The bridge publishes only what it measured, and never writes a current timestamp into `updated_at`: the bridge has no series facts, so its files carry the epoch sentinel, which loses every comparison and therefore cannot outrank a real link on another device.

## Why the bridge has to write it

The reader creates this file itself, but only on an event it owns: an upload from the reader, a local fact edit while connected, or a rename. Its reconcile pass only queues a write for a folder the device has at least one non-placeholder local row for, so a cloud folder filled by an external tool has no path to an index. That is the reported breakage: `.cbz`, `.mokuro` and `.webp` alone are not enough for the reader to build the series.

The format was introduced by mokuro-reader v1.9.0 (commit `d1670bfe3d66`, first released 2026-08-29), not by v1.10.0. mokuro-bunko compiles the same file server-side from a local folder and carries the normative format document.

## The pin, and why nothing is vendored

The format belongs to Gnathonic's mokuro-reader, which is GPL-3.0, and mokuro-bunko is MPL-2.0. This project is MIT, so neither source tree is copied into it. Instead `mokuro_bridge/data/series_json_pin.json` pins every upstream file that participates in creating the file: the repository, the license, the exact commit, and the SHA-256 of that file's bytes at that commit. `mokuro_bridge/series_json.py` is a port of that contract, and it names the pinned files and functions in its module docstring.

That is what "evergreen" buys:

- **Nothing can go stale silently.** The pin is data, so a checked-in copy cannot drift out of date, and `--check-series-reference` names exactly which upstream file changed.
- **The port can be audited against upstream** by fetching the pinned commit, not by trusting this document.
- **There is no license contamination** in either direction.

What it cannot do is absorb a breaking upstream change. If the format moves, the port has to be updated, and the pin's job is to say so before users hit it: `mokuro-bridge --check-series-reference` exits 0 when the pinned bytes still match and neither repository has moved, 1 when either repository has newer commits, and 2 when the check could not run. Nothing checks on its own, and uploads never call GitHub.

To refresh after reviewing a change:

```
mokuro-bridge --check-series-reference
mokuro-bridge --refresh-series-reference
python -m pytest tests/test_series_json.py
```

`--refresh-series-reference` re-pins each repository at its default branch and prints the new digests. It cannot update the port for you.

## How the port is verified

Three layers, all offline:

1. **Parity against upstream behaviour.** `tests/fixtures/series_json/upstream_parity.json` was produced by probing the *pinned* upstream implementation in Node: every Unicode code point the reader counts as Japanese (compressed to ranges), its character counts for a set of strings containing halfwidth and compatibility kana, astral kanji and emoji, and its deterministic uuid for a set of inputs. The port must reproduce all of it, so `character_count` and volume identity cannot drift.
2. **A byte-exact golden case.** The example above is also the expected bytes of mokuro-bunko's own `spec_example_series_file` test, and `tests/test_series_json.py` asserts the port produces exactly those bytes.
3. **End-to-end.** `tests/test_finalize_series_json.py` drives the real finalize stream for a local folder and for a remote destination with the provider seam faked, and checks that the manifest is created, that a second volume is appended with the first entry intact, that an unchanged manifest is not rewritten, and that a `series.json` the bridge could not read is never overwritten.

If you refresh the pin, re-probe the fixture against the same commit: `tests/test_series_json.py` fails when the fixture and the pin name different reader commits.

## What the bridge does not do

- It does not write the root `catalog.json`. The reader and mokuro-bunko maintain that library-wide index themselves.
- It does not recover or rewrite volume entries for files that are already in the folder but were never uploaded by this bridge. It appends its own volume and leaves everything else alone, which is also why a partial index that another tool wrote survives a bridge upload.
- It does not download a `.mokuro` to measure a volume that is not part of the finalize it is running.
