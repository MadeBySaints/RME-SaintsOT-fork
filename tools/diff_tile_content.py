#!/usr/bin/env python3
"""
Finds tiles whose *content* differs between two .otbm files at the same
(x, y, z), not just tiles that exist in one but not the other. Used to
find exactly what an edit touched when it was made on top of existing map
tiles instead of empty coordinates (see check_map_conflicts.py for the
positional-conflict check, which only catches new/removed tiles).

Hashes each tile node's raw byte span (crc32) instead of storing full
tile content, to keep memory bounded on an 18M-tile map.
"""

import argparse
import re
import sys
import time
import zlib
from pathlib import Path

NODE_START = 0xFE
NODE_END = 0xFF
ESCAPE = 0xFD

OTBM_TILE_AREA = 4
OTBM_TILE = 5
OTBM_HOUSETILE = 14

_SPECIAL_BYTE_RE = re.compile(b"[\xfd\xfe\xff]")


def _pack(x: int, y: int, z: int) -> int:
    return (x << 24) | (y << 8) | z


def unpack(key: int) -> tuple[int, int, int]:
    return (key >> 24) & 0xFFFF, (key >> 8) & 0xFFFF, key & 0xFF


def _read_escaped_byte(data: bytes, pos: int) -> tuple[int, int]:
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
    while True:
        m = _SPECIAL_BYTE_RE.search(data, pos, end)
        if m is None:
            raise ValueError("Unexpected end of file while skipping node data")
        i = m.start()
        if data[i] == ESCAPE:
            pos = i + 2
            continue
        return i


def extract_tile_hashes(path: Path) -> dict[int, int]:
    """Returns {packed (x,y,z): crc32 of that tile node's raw byte span}."""
    data = path.read_bytes()
    n = len(data)

    pos = 4
    if data[pos] != NODE_START:
        raise ValueError(f"{path}: expected root node start at offset {pos}")
    pos += 1
    pos += 1  # root type byte

    hashes: dict[int, int] = {}
    # stack entries: (kind, base_x, base_y, base_z, tile_key_or_None, span_start_or_None)
    stack: list[tuple[str, int, int, int, int | None, int | None]] = [
        ("generic", 0, 0, 0, None, None)
    ]

    while stack:
        kind, base_x, base_y, base_z, tile_key, span_start = stack[-1]
        marker_pos = _skip_to_marker(data, pos, n)
        marker = data[marker_pos]

        if marker == NODE_END:
            stack.pop()
            if tile_key is not None:
                hashes[tile_key] = zlib.crc32(data[span_start:marker_pos])
            pos = marker_pos + 1
            continue

        pos = marker_pos + 1
        child_type, pos = _read_u8(data, pos)

        if child_type == OTBM_TILE_AREA:
            bx, pos = _read_u16(data, pos)
            by, pos = _read_u16(data, pos)
            bz, pos = _read_u8(data, pos)
            stack.append(("tilearea", bx, by, bz, None, None))
        elif kind == "tilearea" and child_type in (OTBM_TILE, OTBM_HOUSETILE):
            tile_start = pos
            tx, pos = _read_u8(data, pos)
            ty, pos = _read_u8(data, pos)
            key = _pack(base_x + tx, base_y + ty, base_z)
            stack.append(("generic", 0, 0, 0, key, tile_start))
        else:
            stack.append(("generic", 0, 0, 0, None, None))

    return hashes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old", type=Path, help="Pristine/baseline .otbm")
    parser.add_argument("new", type=Path, help="Edited .otbm")
    parser.add_argument("--max-report", type=int, default=100)
    args = parser.parse_args()

    print(f"Hashing {args.old.name}...")
    t0 = time.monotonic()
    old_hashes = extract_tile_hashes(args.old)
    print(f"  -> {len(old_hashes):,} tiles in {time.monotonic() - t0:.1f}s")

    print(f"Hashing {args.new.name}...")
    t0 = time.monotonic()
    new_hashes = extract_tile_hashes(args.new)
    print(f"  -> {len(new_hashes):,} tiles in {time.monotonic() - t0:.1f}s")

    old_keys = old_hashes.keys()
    new_keys = new_hashes.keys()

    added = new_keys - old_keys
    removed = old_keys - new_keys
    common = old_keys & new_keys
    changed = {k for k in common if old_hashes[k] != new_hashes[k]}

    print()
    print(f"Added tiles:   {len(added):,}")
    print(f"Removed tiles: {len(removed):,}")
    print(f"Changed tiles: {len(changed):,}")

    touched = sorted(added | removed | changed)
    if not touched:
        print("\nNo differences found.")
        return 0

    coords = [unpack(k) for k in touched]
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    zs = [c[2] for c in coords]
    print(f"\nBounding box of all touched tiles: "
          f"x[{min(xs)}-{max(xs)}] y[{min(ys)}-{max(ys)}] z[{min(zs)}-{max(zs)}]")

    zcount: dict[int, int] = {}
    for z in zs:
        zcount[z] = zcount.get(z, 0) + 1
    print("Per-floor tile counts:", sorted(zcount.items()))

    out_file = args.new.with_name(args.new.stem + "_touched_tiles.txt")
    with out_file.open("w") as f:
        for k in touched:
            x, y, z = unpack(k)
            status = "ADDED" if k in added else "REMOVED" if k in removed else "CHANGED"
            f.write(f"{x}\t{y}\t{z}\t{status}\n")
    print(f"\nFull list written to {out_file}")

    print("\nFirst tiles:")
    for k in touched[: args.max_report]:
        x, y, z = unpack(k)
        status = "ADDED" if k in added else "REMOVED" if k in removed else "CHANGED"
        print(f"  ({x}, {y}, {z}) {status}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
