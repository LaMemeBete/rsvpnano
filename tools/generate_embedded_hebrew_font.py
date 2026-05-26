#!/usr/bin/env python3
"""Render Hebrew bitmap glyphs from a TrueType font into an embedded header.

The on-device renderer indexes Latin glyphs by a single storage byte. Hebrew
words are stored as UTF-8 (tagged with a sentinel) and looked up by Unicode
codepoint, so this generator emits a separate header containing a flat array
indexed by `codepoint - first_codepoint`.

Default range covers the 22 Hebrew letters U+05D0..U+05EA, which includes the
five final-form letters (kaf/mem/nun/pe/tsadi). Niqqud / cantillation marks
are dropped at firmware ingest, so this generator does not need to emit them.
"""

from __future__ import annotations

import argparse
import math
import os
import pathlib
import subprocess
import tempfile


CANVAS_WIDTH = 112
CANVAS_HEIGHT = 128
ORIGIN_X = 10
BASELINE_Y = 76
ALPHA_THRESHOLD = 16
FONT_TOP_PADDING = 4
FONT_BOTTOM_PADDING = 2

DEFAULT_FIRST_CP = 0x05D0  # ALEF
DEFAULT_LAST_CP = 0x05EA   # TAV (range covers all 22 letters including finals)

DEFAULT_OUTPUT_PATH = pathlib.Path("src/display/EmbeddedSerifHebrew.h")
DEFAULT_SYMBOL_PREFIX = "EmbeddedSerifHebrew"
DEFAULT_FONT_NAME = "NotoSansHebrew-Regular"
DEFAULT_FONT_FILE = pathlib.Path("third_party/noto-sans-hebrew/NotoSansHebrew-Regular.ttf")
DEFAULT_POINT_SIZE = 52


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--point-size", type=int, default=DEFAULT_POINT_SIZE)
    parser.add_argument("--font-name", default=DEFAULT_FONT_NAME,
                        help="PostScript name of the font inside the TTF.")
    parser.add_argument("--font-file", type=pathlib.Path, default=DEFAULT_FONT_FILE)
    parser.add_argument("--first-codepoint", type=lambda v: int(v, 0), default=DEFAULT_FIRST_CP)
    parser.add_argument("--last-codepoint", type=lambda v: int(v, 0), default=DEFAULT_LAST_CP)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--symbol-prefix", default=DEFAULT_SYMBOL_PREFIX)
    return parser.parse_args()


def render_glyph(tmp_dir: pathlib.Path, codepoint: int, font_name: str, point_size: int,
                 font_dir: pathlib.Path) -> pathlib.Path:
    output = tmp_dir / f"u{codepoint:04X}.pgm"
    program = (
        "1 setgray clippath fill "
        "0 setgray "
        f"/{font_name} findfont {point_size} scalefont setfont "
        f"{ORIGIN_X} {BASELINE_Y} moveto "
        f"/uni{codepoint:04X} glyphshow showpage"
    )
    subprocess.run(
        [
            "gs",
            "-q",
            "-dNOPAUSE",
            "-dBATCH",
            "-dTextAlphaBits=4",
            "-dGraphicsAlphaBits=4",
            "-sDEVICE=pgmraw",
            "-r72",
            f"-g{CANVAS_WIDTH}x{CANVAS_HEIGHT}",
            f"-sFONTPATH={font_dir}",
            f"-sOutputFile={output}",
            "-c",
            program,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return output


def advance_width(codepoint: int, font_name: str, point_size: int,
                  font_dir: pathlib.Path) -> int:
    result = subprocess.run(
        [
            "gs",
            "-q",
            "-dNODISPLAY",
            f"-sFONTPATH={font_dir}",
            "-c",
            (
                f"/{font_name} findfont {point_size} scalefont setfont "
                "0 0 moveto "
                f"/uni{codepoint:04X} glyphshow "
                "currentpoint pop == quit"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"Failed to determine advance width for U+{codepoint:04X}")
    return max(1, int(math.floor(float(lines[-1]) + 0.5)))


def parse_pgm(path: pathlib.Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    if not data.startswith(b"P5\n"):
        raise ValueError(f"Unexpected PGM header in {path}")
    parts = data.split(b"\n")
    index = 1
    while parts[index].startswith(b"#"):
        index += 1
    width, height = map(int, parts[index].split())
    if int(parts[index + 1]) != 255:
        raise ValueError(f"Unexpected max value in {path}")
    raster = b"\n".join(parts[index + 2:])
    if len(raster) != width * height:
        raise ValueError(f"Unexpected raster length in {path}")
    return width, height, raster


def alpha_at(raster: bytes, width: int, x: int, y: int) -> int:
    return 255 - raster[y * width + x]


def main() -> None:
    args = parse_args()
    if not (0x0590 <= args.first_codepoint <= args.last_codepoint <= 0x05FF):
        raise ValueError("Codepoint range must lie within the Hebrew block U+0590..U+05FF.")
    font_dir = args.font_file.parent.resolve()
    if not args.font_file.is_file():
        raise FileNotFoundError(f"Font file not found: {args.font_file}")

    glyph_images: dict[int, tuple[int, int, bytes]] = {}
    global_top = CANVAS_HEIGHT
    global_bottom = -1

    with tempfile.TemporaryDirectory(prefix="hebrew_font_") as tmp:
        tmp_dir = pathlib.Path(tmp)
        for cp in range(args.first_codepoint, args.last_codepoint + 1):
            pgm = render_glyph(tmp_dir, cp, args.font_name, args.point_size, font_dir)
            w, h, raster = parse_pgm(pgm)
            glyph_images[cp] = (w, h, raster)
            for y in range(h):
                for x in range(w):
                    if alpha_at(raster, w, x, y) > ALPHA_THRESHOLD:
                        global_top = min(global_top, y)
                        global_bottom = max(global_bottom, y)
                        break

    if global_bottom < global_top:
        raise RuntimeError("Failed to detect any glyph pixels")

    crop_top = max(0, global_top - FONT_TOP_PADDING)
    crop_bottom = min(CANVAS_HEIGHT - 1, global_bottom + FONT_BOTTOM_PADDING)
    font_height = crop_bottom - crop_top + 1

    bitmap_bytes: list[int] = []
    glyph_entries: list[str] = []
    for cp in range(args.first_codepoint, args.last_codepoint + 1):
        w, _h, raster = glyph_images[cp]
        min_x, max_x = w, -1
        for y in range(crop_top, crop_bottom + 1):
            for x in range(w):
                if alpha_at(raster, w, x, y) > ALPHA_THRESHOLD:
                    min_x = min(min_x, x)
                    max_x = max(max_x, x)
        bitmap_offset = len(bitmap_bytes)
        if max_x >= min_x:
            gw = max_x - min_x + 1
            for y in range(crop_top, crop_bottom + 1):
                for x in range(min_x, max_x + 1):
                    a = alpha_at(raster, w, x, y)
                    bitmap_bytes.append(0 if a <= ALPHA_THRESHOLD else a)
            x_off = min_x - ORIGIN_X
        else:
            gw = 0
            x_off = 0
        xa = advance_width(cp, args.font_name, args.point_size, font_dir)
        glyph_entries.append(
            "    {" + f"{bitmap_offset}, {x_off}, {gw}, {xa}" + "},"
            f"  // U+{cp:04X}"
        )

    prefix = args.symbol_prefix
    lines = [
        "#pragma once",
        "",
        "#include <Arduino.h>",
        "",
        "// Generated from a Hebrew TrueType font and embedded as glyph data.",
        f"// Source font: {args.font_name} at {args.point_size} pt",
        "// Indexed by (codepoint - kFirstCodepoint). Niqqud/cantillation marks are",
        "// not included; they are dropped at firmware ingest.",
        "",
        f"struct {prefix}Glyph {{",
        "  uint32_t bitmapOffset;",
        "  int8_t xOffset;",
        "  uint8_t width;",
        "  uint8_t xAdvance;",
        "};",
        "",
        f"constexpr uint32_t k{prefix}FirstCodepoint = 0x{args.first_codepoint:04X};",
        f"constexpr uint32_t k{prefix}LastCodepoint = 0x{args.last_codepoint:04X};",
        f"constexpr uint8_t k{prefix}Height = {font_height};",
        "",
        f"static const uint8_t k{prefix}Bitmaps[] PROGMEM = {{",
    ]
    for offset in range(0, len(bitmap_bytes), 16):
        chunk = bitmap_bytes[offset:offset + 16]
        lines.append("    " + ", ".join(f"{v:3d}" for v in chunk) + ",")
    lines += [
        "};",
        "",
        f"static const {prefix}Glyph k{prefix}Glyphs[] PROGMEM = {{",
        *glyph_entries,
        "};",
        "",
    ]
    args.output.write_text("\n".join(lines) + "\n", encoding="ascii")


if __name__ == "__main__":
    main()
