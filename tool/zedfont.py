#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["fonttools>=4.40", "pillow>=10"]
# ///
"""zedfont: the emote font, rebuilt for Zed's terminal.

  zedfont.py                 detect Zed's terminal metrics, build + install the font if anything changed
  zedfont.py --force         rebuild + reinstall regardless
  zedfont.py --setup         same, and add the font fallback to Zed's settings.json if it is missing
  zedfont.py --scale 1.5 --font-size 15 --line-height 1.3   pin a detected value (remembered)

Why a second font: Zed's Windows text path only rasterizes COLR color glyphs
(gpui_windows/src/direct_write.rs: "todo: support formats other than COLR"), so the CBDT/PNG
font wezterm uses draws as blank space. Here every emote becomes a stack of solid-color COLRv0
layers made of pixel rectangles, one rectangle per device pixel. Three Zed facts shape it:
  - each layer is rasterized antialiased in its own pass, then composited: tiled layers crack
    along every shared edge, so layers are stacked (each covers everything painted after it)
  - emoji are painted at an integer origin with no subpixel variant (window.rs paint_emoji), so
    if one grid square is exactly one device pixel and the box edges are whole pixels, the result
    is the source image 1:1 with no resampling mush. Hence the font is built per font size + DPI
  - the line's ascent is the max over every font in it (DirectWrite GetLineMetrics), so this
    font's own ascent/descent stay tiny or every line with an emote would shift

Lands in <data>/zed/ and is installed per user (no admin); state is <data>/zed.json.
"""
import glob, hashlib, io, json, math, os, re, shutil, subprocess, sys
from pathlib import Path

FAMILY = "Twitch Emotes COLR"
FIRST_CP = 0x100000
UPM = 2048
GLYPH_BUDGET = 64000  # glyf indices are uint16; leave room under 65535
LINE_HEIGHTS = {"standard": 1.3, "comfortable": 1.618}  # settings_content/src/terminal.rs


def data_dir():
    return Path(os.environ.get("TWITCH_EMOTES_DIR") or Path.home() / ".twitch-emotes")


# ---------------------------------------------------------------- detection

def zed_settings_path():
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Zed" / "settings.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "zed" / "settings.json"


def parse_jsonc(text):
    """Zed's settings are JSON with comments and trailing commas. Strip both, strings untouched."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i):
            i = text.find("\n", i)
            i = n if i < 0 else i
        elif text.startswith("/*", i):
            i = text.find("*/", i + 2)
            i = n if i < 0 else i + 2
        else:
            out.append(c)
            i += 1
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))


def read_settings():
    p = zed_settings_path()
    try:
        return parse_jsonc(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"warning: could not read {p} ({e}); using Zed's defaults", file=sys.stderr)
        return {}


def display_scale():
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop\WindowMetrics") as k:
                return winreg.QueryValueEx(k, "AppliedDPI")[0] / 96
        except OSError:
            return 1.0
    if sys.platform == "darwin":
        return 2.0
    try:  # Hyprland; anything else assumes 1x, pin it with --scale
        mons = json.loads(subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True, timeout=5).stdout)
        return max(m.get("scale", 1.0) for m in mons) if mons else 1.0
    except Exception:
        return 1.0


def font_names(path):
    from fontTools.ttLib import TTFont
    try:
        name = TTFont(path, fontNumber=0, lazy=True)["name"]
    except Exception:
        return set(), ""
    fams = {str(n).lower() for n in name.names if n.nameID in (1, 16) and n.platformID in (1, 3)}
    style = next((str(n) for n in name.names if n.nameID in (17, 2)), "")
    return fams, style.lower()


def find_font_file(family):
    """Path to the Regular face of `family`, or None."""
    if sys.platform != "win32":
        try:
            out = subprocess.run(["fc-match", "-f", "%{file}", f"{family}:style=Regular"], capture_output=True, text=True, timeout=5).stdout
            return Path(out) if out and out_has(out, family) else None
        except Exception:
            return None
    # The registry keys fonts by full name ("Iosevka NFM"), not the family Zed is given
    # ("Iosevka Nerd Font Mono"), but file names follow the family. So: name match first,
    # confirmed by the name table, then every file as a last resort
    dirs = [Path(os.environ["LOCALAPPDATA"]) / "Microsoft/Windows/Fonts", Path(os.environ["WINDIR"]) / "Fonts"]
    files = [f for d in dirs if d.is_dir() for f in d.iterdir() if f.suffix.lower() in (".ttf", ".otf", ".ttc")]
    squeezed = family.lower().replace(" ", "")
    likely = sorted((f for f in files if f.name.lower().replace(" ", "").startswith(squeezed)), key=lambda f: "regular" not in f.name.lower())
    for f in likely + [f for f in files if f not in likely]:
        fams, style = font_names(f)
        if family.lower() in fams and style in ("regular", "book", "normal", ""):
            return f
    return None


def out_has(path, family):
    return family.split()[0].lower() in path.lower()


def font_metrics(family):
    """(ascent, descent+gap, 'm' advance) in em, the way DirectWrite reports them to Zed."""
    path = find_font_file(family) if family else None
    if path is None:
        print(f"warning: terminal font {family!r} not found, guessing its metrics", file=sys.stderr)
        return 0.95, 0.25, 0.6
    from fontTools.ttLib import TTFont
    f = TTFont(path, fontNumber=0, lazy=True)
    upm, os2, hhea = f["head"].unitsPerEm, f["OS/2"], f["hhea"]
    if os2.fsSelection & 0x80:  # USE_TYPO_METRICS
        a, d, gap = os2.sTypoAscender, -os2.sTypoDescender, os2.sTypoLineGap
    else:
        a, d, gap = os2.usWinAscent, os2.usWinDescent, max(0, hhea.lineGap)
    adv = f["hmtx"][f.getBestCmap().get(ord("m"), ".notdef")][0]
    return a / upm, (d + gap) / upm, adv / upm


def detect(pins):
    s = read_settings()
    t = s.get("terminal") or {}
    size = pins.get("font_size") or t.get("font_size") or s.get("buffer_font_size") or 15
    lh = pins.get("line_height") or t.get("line_height") or "standard"
    lh = LINE_HEIGHTS.get(lh, 1.3) if isinstance(lh, str) else max(float((lh or {}).get("custom", 1.3)), 1.0)
    family = t.get("font_family") or s.get("buffer_font_family")
    scale = pins.get("scale") or display_scale()
    has_fallback = FAMILY in (t.get("font_fallbacks") or [])
    return {"font_size": float(size), "line_height": float(lh), "family": family, "scale": float(scale)}, has_fallback, "terminal" in s


# ---------------------------------------------------------------- build

def rects(mask, w, h):
    """Opaque runs per row, glued down while identical. Greedy, fine for pixel art."""
    out, live = [], {}
    for y in range(h + 1):
        runs = set()
        if y < h:
            x = 0
            while x < w:
                if mask[y * w + x]:
                    x0 = x
                    while x < w and mask[y * w + x]:
                        x += 1
                    runs.add((x0, x))
                else:
                    x += 1
        for k in [k for k in live if k not in runs]:
            out.append((k[0], live.pop(k), k[1], y))
        for k in runs:
            live.setdefault(k, y)
    return out


def build(cbdt_path, out_path, m):
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import TTFont, newTable
    from PIL import Image

    em = m["em_px"]
    unit = UPM / em  # font units per device pixel
    side, left, top = m["side_px"], m["left_px"], m["top_px"]

    src = TTFont(cbdt_path)
    strike = src["CBDT"].strikeData[0]
    cmap = src.getBestCmap()
    names = [g for g in src.getGlyphOrder()[1:] if g in strike]
    ncol = max(6, min(32, GLYPH_BUDGET // max(1, len(names)) - 2))

    def outline(rs):
        pen = TTGlyphPen(None)
        for x0, y0, x1, y1 in rs:  # clockwise, y up
            L, R = round((left + x0) * unit), round((left + x1) * unit)
            T, B = round((top - y0) * unit), round((top - y1) * unit)
            pen.moveTo((L, T)); pen.lineTo((R, T)); pen.lineTo((R, B)); pen.lineTo((L, B)); pen.closePath()
        return pen.glyph()

    order, glyphs, hmtx, layers, palette, pal_ix = [".notdef"], {".notdef": TTGlyphPen(None).glyph()}, {".notdef": (UPM, 0)}, {}, [], {}
    for g in names:
        img = Image.open(io.BytesIO(strike[g].imageData)).convert("RGBA").resize((side, side), Image.LANCZOS)
        alpha = img.getchannel("A")
        opaque = [v >= 128 for v in alpha.tobytes()]
        rgb = Image.new("RGB", img.size)
        rgb.paste(img, mask=alpha.point(lambda v: 255 if v >= 128 else 0))
        q = rgb.quantize(ncol, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        qpal, qd = q.getpalette(), list(q.tobytes())
        # The base glyph's outline is what Zed sizes the sprite from (raster_bounds): the full silhouette
        glyphs[g] = outline(rects(opaque, side, side))
        hmtx[g] = (UPM, 0)
        order.append(g)
        count = {}
        for i, c in enumerate(qd):
            if opaque[i]:
                count[c] = count.get(c, 0) + 1
        ranked = sorted(count, key=count.get, reverse=True)
        rank = {c: k for k, c in enumerate(ranked)}
        lr = []
        for k, c in enumerate(ranked):
            # Stacked: layer k covers itself and every layer above it, so seams show color, not background
            ln = f"{g}.{k}"
            glyphs[ln] = outline(rects([opaque[i] and rank[qd[i]] >= k for i in range(len(qd))], side, side))
            hmtx[ln] = (0, 0)
            order.append(ln)
            rgbc = tuple(qpal[c * 3:c * 3 + 3])
            if rgbc not in pal_ix:
                pal_ix[rgbc] = len(palette)
                palette.append(rgbc)
            lr.append((ln, pal_ix[rgbc]))
        layers[g] = lr

    fb = FontBuilder(UPM, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({cp: g for cp, g in cmap.items() if g in layers})
    fb.setupGlyf(glyphs)
    # lsb must equal each outline's xMin: DirectWrite places a glyph by its hmtx lsb, so a stale 0
    # shoves every layer left by its own xMin and the layers stop lining up (the original scuff)
    glyf = fb.font["glyf"]
    for g in order:
        glyf[g].recalcBounds(glyf)
        hmtx[g] = (hmtx[g][0], getattr(glyf[g], "xMin", 0))
    fb.setupHorizontalMetrics(hmtx)
    # Tiny on purpose: DirectWrite takes the line's ascent from the tallest font in it
    asc = UPM // 2
    fb.setupHorizontalHeader(ascent=asc, descent=0)
    fb.setupOS2(sTypoAscender=asc, sTypoDescender=0, sTypoLineGap=0, usWinAscent=asc, usWinDescent=0, fsSelection=0x40 | 0x80)
    fb.font["OS/2"].version = 4  # USE_TYPO_METRICS needs v4
    fb.setupNameTable({"familyName": FAMILY, "styleName": "Regular"})
    fb.setupPost()
    gasp = newTable("gasp")
    gasp.version, gasp.gaspRange = 1, {0xFFFF: 0x000A}  # no gridfit: it would snap each layer on its own
    fb.font["gasp"] = gasp
    fb.setupCOLR(layers, version=0)
    fb.setupCPAL([[(r / 255, g / 255, b / 255, 1.0) for r, g, b in palette]])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fb.save(out_path)
    return len(layers)


def placement(p, cells):
    """Device-pixel box of an emote relative to its baseline origin, all whole pixels."""
    em = p["font_size"] * p["scale"]
    asc, desc, adv = p["metrics"]
    row = p["line_height"] * em
    baseline = (row - (asc + desc) * em) / 2 + asc * em  # gpui text_system/line.rs: padding_top + ascent
    side = min(math.floor(row), math.floor(cells * adv * em))  # one row tall, like wezterm
    top = round(baseline)
    left = max(0, round((cells * adv * em - side) / 2))
    return {"em_px": em, "side_px": side, "top_px": top, "left_px": left}


# ---------------------------------------------------------------- install

def font_dir():
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Fonts"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "fonts" / "twitch-emotes"


def install(built):
    """Per-user install under a fresh file name (font caches key on the name), old ones dropped."""
    d = font_dir()
    d.mkdir(parents=True, exist_ok=True)
    dest = d / built.name
    shutil.copyfile(built, dest)
    if sys.platform == "win32":
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows NT\CurrentVersion\Fonts") as k:
            winreg.SetValueEx(k, f"{FAMILY} (TrueType)", 0, winreg.REG_SZ, str(dest))
    for old in list(d.glob("TwitchEmotesZed-*.ttf")) + list(d.glob("TwitchEmotesCOLR*.ttf")):
        if old != dest:
            try:
                old.unlink()
            except OSError:
                pass  # Zed still has it mapped; the next build sweeps it
    if sys.platform.startswith("linux"):
        subprocess.run(["fc-cache", "-f", str(d)], capture_output=True)
    return dest


def patch_settings():
    """Add terminal.font_fallbacks to Zed's settings.json. Text surgery, so comments survive."""
    p = zed_settings_path()
    real = Path(os.path.realpath(p))  # dotfiles symlink stays a symlink
    text = real.read_text(encoding="utf-8") if real.exists() else "{\n}\n"
    s = parse_jsonc(text) if text.strip() else {}
    if FAMILY in ((s.get("terminal") or {}).get("font_fallbacks") or []):
        return f"{real} already has the fallback"
    manual = f'add  "font_fallbacks": ["{FAMILY}"]  to the "terminal" block of {real}'
    if "terminal" in s:
        return f"your settings already have a terminal block, so I am not touching it: {manual}"
    end = text.rstrip().rfind("}")
    if end < 0:
        return manual
    body = text[:end].rstrip()
    sep = "" if body.endswith(("{", ",")) else ","
    patched = f'{body}{sep}\n  // twitch-emotes: emote font for the terminal (github.com/translibrius/claude-twitch-mod)\n  "terminal": {{ "font_fallbacks": ["{FAMILY}"] }},\n}}\n'
    parse_jsonc(patched)  # never write something Zed can't read
    if real.exists():
        shutil.copyfile(real, str(real) + ".twitch-emotes.bak")
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text(patched, encoding="utf-8")
    return f"added the font fallback to {real} (backup beside it)"


# ---------------------------------------------------------------- glue

def current_cbdt(data):
    gen = (data / "generation").read_text().strip() if (data / "generation").exists() else ""
    p = data / f"font-{gen}" / "TwitchEmotes.ttf"
    return (p, gen) if p.exists() else (None, gen)


def run(force=False, pins=None, setup=False, quiet=False):
    data = data_dir()
    state_file = data / "zed.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    pins = {**state.get("pins", {}), **{k: v for k, v in (pins or {}).items() if v is not None}}
    cbdt, gen = current_cbdt(data)
    if cbdt is None:
        sys.exit("no emote font yet: start Claude Code once with the mod installed, it downloads one")
    p, has_fallback, _ = detect(pins)
    cells = json.loads((data / "emotes.json").read_text(encoding="utf-8")).get("cells", 3)
    # This file's size rides along so a mod update that changes the build also rebuilds
    key = hashlib.sha1(json.dumps([p, cells, gen, Path(__file__).stat().st_size], sort_keys=True).encode()).hexdigest()[:10]
    said = []
    installed = Path(state["installed"]) if state.get("installed") else None
    if force or state.get("key") != key or installed is None or not installed.exists():
        p["metrics"] = font_metrics(p["family"])
        m = placement(p, cells)
        built = data / "zed" / f"TwitchEmotesZed-{key}.ttf"
        n = build(cbdt, built, m)
        for old in (data / "zed").glob("TwitchEmotesZed-*.ttf"):
            if old != built:
                old.unlink(missing_ok=True)
        dest = install(built)
        state = {"key": key, "installed": str(dest), "params": p, "placement": m, "pins": pins}
        state_file.write_text(json.dumps(state, indent=1))
        said.append(f"built the Zed emote font: {n} emotes, {m['side_px']}px at {p['font_size']:g}pt x{p['scale']:g} "
                    f"(line height {p['line_height']:g}). Restart Zed to load it")
    elif not quiet:
        said.append("Zed emote font is up to date")
    if setup:
        said.append(patch_settings())
    elif not has_fallback:
        said.append(f'Zed is not using it yet: run /emotes setup, or add "font_fallbacks": ["{FAMILY}"] to "terminal" in {zed_settings_path()}')
    return "\n".join(said)


def rebuild_if_configured():
    """emotefont calls this after every build/add: keep the Zed font in step if one exists."""
    if (data_dir() / "zed.json").exists():
        print(run(quiet=True))


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Build + install the emote font for Zed's terminal")
    ap.add_argument("--force", action="store_true", help="rebuild even if nothing changed")
    ap.add_argument("--setup", action="store_true", help="also add the font fallback to Zed's settings.json")
    ap.add_argument("--quiet", action="store_true", help="say nothing when there is nothing to do")
    ap.add_argument("--scale", type=float, help="pin the display scale (detected: Windows DPI, Hyprland, 2 on macOS)")
    ap.add_argument("--font-size", type=float, help="pin the terminal font size")
    ap.add_argument("--line-height", type=float, help="pin the terminal line height")
    a = ap.parse_args()
    said = run(a.force, {"scale": a.scale, "font_size": a.font_size, "line_height": a.line_height}, a.setup, a.quiet)
    if said:
        print(said)


if __name__ == "__main__":
    try:
        import fontTools, PIL  # noqa: F401
    except ImportError as e:
        sys.exit(f"zedfont needs fonttools and pillow ({e}).\n  pip install fonttools pillow\nor run it with: uv run {Path(__file__).name}")
    main()
