#!/usr/bin/env python3
"""
Checks whether any tile in the SaintsOT custom overlay map
(world/custom/otservbr-custom.otbm) occupies the same absolute (x, y, z)
coordinate as a tile already present in the base map (world/otservbr.otbm).

Why this matters: Canary loads the custom map with Map::loadMapCustom(),
which calls the exact same tile loader as the base map (IOMap::loadMap ->
Map::setBasicTile) using the coordinates stored *inside* the custom .otbm
file, with no positional offset. setBasicTile() fully replaces whatever
tile previously existed at that (x, y, z) -- it does not merge items onto
an existing tile. So if the custom map's editor places a tile on top of
a coordinate the base map already uses, the base map's tile (ground,
items, everything) is silently and completely overwritten the moment the
server boots with toggleMapCustom = true. There is no in-game warning for
this -- it just happens.

This script parses both .otbm files far enough to collect the set of
occupied (x, y, z) tile coordinates in each, then reports any overlap.
It does NOT understand item contents, houses, or attributes -- it only
needs tile positions, which keeps it fast and immune to bugs in item
attribute decoding.

Usage:
    python check_map_conflicts.py
    python check_map_conflicts.py --base PATH --custom PATH
    python check_map_conflicts.py --rebuild-cache

The base map rarely changes and is huge (~185MB), so its extracted
position set is cached next to it (.base_positions_cache.pkl) and only
rebuilt when the base file's size or mtime changes. The custom map is
small and is always parsed fresh.
"""

import argparse
import pickle
import re
import sys
import time
from pathlib import Path

# --- OTBM wire-format constants (see canary/src/io/io_definitions.hpp and
# canary/src/io/fileloader.hpp) ---
NODE_START = 0xFE
NODE_END = 0xFF
ESCAPE = 0xFD

OTBM_TILE_AREA = 4
OTBM_TILE = 5
OTBM_HOUSETILE = 14

_SPECIAL_BYTE_RE = re.compile(b"[\xfd\xfe\xff]")


def _pack(x: int, y: int, z: int) -> int:
    return (x << 24) | (y << 8) | z


def _read_escaped_byte(data: bytes, pos: int) -> tuple[int, int]:
    """Read one logical (unescaped) byte at pos. Returns (value, next_pos)."""
    b = data[pos]
    if b == ESCAPE:
        pos += 1
        return data[pos], pos + 1
    return b, pos + 1


def _read_u16(data: bytes, pos: int) -> tuple[int, int]:
    lo, pos = _read_escaped_byte(data, pos)
    hi, pos = _read_escaped_byte(data, pos)
    return lo | (hi << 8), pos


def _read_u8(data: bytes, pos: int) -> tuple[int, int]:
    return _read_escaped_byte(data, pos)


def _skip_to_marker(data: bytes, pos: int, end: int) -> int:
    """
    Advance pos past opaque (unknown) attribute/item data until the next
    *unescaped* NODE_START or NODE_END byte, which is left unconsumed.
    Safe because a real 0xFD is always immediately followed by exactly one
    literal byte that must never be re-interpreted as a marker -- so we
    jump straight to the next occurrence of any of the three special byte
    values and only stop for real (non-escaped) markers.
    """
    while True:
        m = _SPECIAL_BYTE_RE.search(data, pos, end)
        if m is None:
            raise ValueError("Unexpected end of file while skipping node data")
        i = m.start()
        b = data[i]
        if b == ESCAPE:
            pos = i + 2  # skip escape byte + its literal, keep scanning
            continue
        return i  # real NODE_START or NODE_END, unconsumed


def extract_tile_positions(path: Path) -> set[int]:
    """Returns a set of packed (x, y, z) keys for every tile node in the map."""
    data = path.read_bytes()
    n = len(data)

    pos = 4  # skip the 4-byte OTBM file identifier
    if data[pos] != NODE_START:
        raise ValueError(f"{path}: expected root node start at offset {pos}")
    pos += 1
    pos += 1  # root node type byte (always raw, unescaped, per OTBM convention)

    positions: set[int] = set()
    # Explicit stack instead of recursion: (kind, base_x, base_y, base_z)
    # kind is 'generic' or 'tilearea'
    stack: list[tuple[str, int, int, int]] = [("generic", 0, 0, 0)]

    while stack:
        kind, base_x, base_y, base_z = stack[-1]
        marker_pos = _skip_to_marker(data, pos, n)
        marker = data[marker_pos]

        if marker == NODE_END:
            stack.pop()
            pos = marker_pos + 1
            continue

        # marker == NODE_START: a child node begins here
        pos = marker_pos + 1
        child_type, pos = _read_u8(data, pos)

        if child_type == OTBM_TILE_AREA:
            bx, pos = _read_u16(data, pos)
            by, pos = _read_u16(data, pos)
            bz, pos = _read_u8(data, pos)
            stack.append(("tilearea", bx, by, bz))
        elif kind == "tilearea" and child_type in (OTBM_TILE, OTBM_HOUSETILE):
            tx, pos = _read_u8(data, pos)
            ty, pos = _read_u8(data, pos)
            positions.add(_pack(base_x + tx, base_y + ty, base_z))
            stack.append(("generic", 0, 0, 0))
        else:
            stack.append(("generic", 0, 0, 0))

    return positions


def _cache_path(base_path: Path) -> Path:
    return base_path.with_name(base_path.name + ".positions_cache.pkl")


def get_base_positions(base_path: Path, rebuild: bool = False) -> set[int]:
    cache_path = _cache_path(base_path)
    stat = base_path.stat()
    signature = (stat.st_size, stat.st_mtime_ns)

    if not rebuild and cache_path.exists():
        try:
            with cache_path.open("rb") as f:
                cached_signature, positions = pickle.load(f)
            if cached_signature == signature:
                return positions
        except (pickle.PickleError, EOFError, ValueError):
            pass  # fall through and rebuild

    print(f"Parsing base map {base_path.name} (no valid cache found)...")
    t0 = time.monotonic()
    positions = extract_tile_positions(base_path)
    print(f"  -> {len(positions):,} tiles in {time.monotonic() - t0:.1f}s")

    with cache_path.open("wb") as f:
        pickle.dump((signature, positions), f, protocol=pickle.HIGHEST_PROTOCOL)

    return positions


def unpack(key: int) -> tuple[int, int, int]:
    return (key >> 24) & 0xFFFF, (key >> 8) & 0xFFFF, key & 0xFF


def main() -> int:
    here = Path(__file__).resolve().parent.parent  # RME-SaintsOT/
    default_base = here / "maps" / "otservbr.otbm"
    default_custom = here / "maps" / "custom" / "otservbr-custom.otbm"

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, default=default_base)
    parser.add_argument("--custom", type=Path, default=default_custom)
    parser.add_argument(
        "--rebuild-cache",
        action="store_true",
        help="Force re-parsing the base map instead of using the cached position set",
    )
    parser.add_argument(
        "--max-report",
        type=int,
        default=50,
        help="Max conflicting coordinates to print directly (default 50)",
    )
    args = parser.parse_args()

    if not args.base.exists():
        print(f"Base map not found: {args.base}", file=sys.stderr)
        return 2
    if not args.custom.exists():
        print(f"Custom map not found: {args.custom}", file=sys.stderr)
        return 2

    base_positions = get_base_positions(args.base, rebuild=args.rebuild_cache)

    print(f"Parsing custom map {args.custom.name}...")
    t0 = time.monotonic()
    custom_positions = extract_tile_positions(args.custom)
    print(f"  -> {len(custom_positions):,} tiles in {time.monotonic() - t0:.1f}s")

    conflicts = sorted(custom_positions & base_positions)

    print()
    if not conflicts:
        print("No coordinate conflicts. Every tile in the custom map sits on "
              "coordinates the base map does not use.")
        return 0

    print(f"CONFLICT: {len(conflicts):,} tile(s) in the custom map share a "
          f"coordinate with the base map.")
    print("These base-map tiles will be silently overwritten the moment the "
          "server boots with the custom map loaded.\n")

    shown = conflicts[: args.max_report]
    for key in shown:
        x, y, z = unpack(key)
        print(f"  ({x}, {y}, {z})")

    if len(conflicts) > len(shown):
        out_file = args.custom.with_name("conflicts.txt")
        with out_file.open("w") as f:
            for key in conflicts:
                x, y, z = unpack(key)
                f.write(f"{x}\t{y}\t{z}\n")
        print(f"\n  ... and {len(conflicts) - len(shown):,} more. Full list "
              f"written to {out_file}")

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
