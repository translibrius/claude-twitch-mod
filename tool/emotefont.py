#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["fonttools>=4.40", "pillow>=10"]
# ///
"""emotefont: builds a color bitmap font out of Twitch emotes, plus the name -> glyph map.

  emotefont.py build [--top N] [--ffz N]   fetch core.txt + the most used 7TV / FFZ emotes, build
  emotefont.py add NAME [URL] [@FRAME]     add one emote (looked up on 7TV by name unless URL given)
  emotefont.py list                        names the font has

A terminal can't inline images everywhere (ConPTY eats the escapes), but every terminal can
draw a font. So each emote is a glyph in Supplementary PUA-B (U+100000 + registry index).
Plane 15 is out: Nerd Fonts park Material Design icons at U+F0001 and up.

Font layout copies NotoColorEmoji: CBDT/CBLC with PNG strikes and NO glyf table. On purpose:
FreeType only loads a bitmap strike from a *scalable* font at an exact ppem match, while a
bitmap-only font makes the terminal pick the strike and scale it to the cell itself.

Everything lands in ~/.twitch-emotes (or $TWITCH_EMOTES_DIR):
  font-<N>/TwitchEmotes.ttf   the font; a new folder per build, see publish_font
  generation              the current N. wezterm.lua (written by the mod) watches it
  emotes.json             what the Claude Code mod reads
  registry.json           index -> emote. Append-only, so codepoints never move
  block.txt               names you never want swapped, one per line
"""
import argparse, hashlib, io, json, math, os, re, sys, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
FIRST_CP = 0x100000
PX = 64  # strike size. Drawn ~22px tall at 13pt, so 64 still covers a 2x HiDPI row
# Baseline split of the PX square, same proportion as Noto (950/-250)
ASC = round(PX * 0.79)
DESC = PX - ASC
UPM = 1024
UA = {"User-Agent": "Mozilla/5.0 emotefont (+https://github.com/translibrius/claude-twitch-mod)"}

# Names that pass the dictionary check but still show up in caps in normal dev talk
STOP = set("""OK NO YES NOT MUST TODO NOTE FIXME WARNING ERROR DONE FAIL PASS TRUE FALSE NULL NONE INFO
DEBUG JSON HTTP HTTPS API URL README WHY HOW WHO WHAT HELP STOP WAIT ONLY NEVER ALWAYS IMPORTANT
CPU GPU RAM SSD USB SQL HTML CSS XML YAML TOML UTF ASCII SDK CLI GUI IDE EOF LGTM WIP TLDR FYI
ASAP AKA IIRC IMO IMHO BTW LOL GG WTF OMG HMM HMMM ADHD""".split())

WORDLISTS = {  # filename -> url. Fetched once; only used to keep plain English out of the font
    "words-common.txt": "https://raw.githubusercontent.com/first20hours/google-10000-english/master/google-10000-english.txt",
    "words-all.txt": "https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt",
}


def http(url, data=None):
    headers = dict(UA)
    if data is not None:
        headers["content-type"] = "application/json"
        data = json.dumps(data).encode()
    return urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=30).read()


def seventv(query, variables):
    out = json.loads(http("https://7tv.io/v3/gql", {"query": query, "variables": variables}))
    if out.get("errors"):
        raise RuntimeError(out["errors"][0]["message"])
    return out["data"]["emotes"]["items"]


def seventv_url(item):
    # Animated ones only come as webp/gif/avif; Pillow reads the webp and we pick a frame
    return f"https:{item['host']['url']}/2x.{'webp' if item['animated'] else 'png'}"


def top_7tv(n):
    q = "query($p:Int!){emotes(query:\"\",limit:100,page:$p,filter:{category:TOP}){items{name animated host{url}}}}"
    out = []
    for page in range(1, math.ceil(n / 100) + 1):
        out += [(i["name"], seventv_url(i)) for i in seventv(q, {"p": page})]
    return out[:n]


def top_ffz(n):
    out = []
    for page in range(1, math.ceil(n / 200) + 1):
        data = json.loads(http(f"https://api.frankerfacez.com/v1/emotes?sort=count-desc&per_page=200&page={page}"))
        out += [(e["name"], e["urls"].get("2") or e["urls"]["1"]) for e in data["emoticons"]]
    return out[:n]


def find_7tv(name):
    q = ("query($q:String!){emotes(query:$q,limit:20,page:1,sort:{value:\"popularity\",order:DESCENDING},"
         "filter:{exact_match:true,case_sensitive:true}){items{name animated host{url}}}}")
    for item in seventv(q, {"q": name}):
        if item["name"] == name:
            return seventv_url(item)
    return None


def read_list(path):
    """Lines of `Name URL-or-path [@frame]`."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name, src, *frame = line.split()
        if not src.startswith(("http://", "https://")):
            src = str((path.parent / src).resolve())
        out.append((name, src, int(frame[0].lstrip("@")) if frame else None))
    return out


class Words:
    """Is this emote name also just a word? Then swapping it would eat normal prose."""

    def __init__(self, data):
        self.sets = {}
        for fname, url in WORDLISTS.items():
            path = data / fname
            if not path.exists():
                try:
                    path.write_bytes(http(url))
                except Exception as e:
                    print(f"warning: no {fname} ({e}); plain-word filter is weaker", file=sys.stderr)
                    path = None
            self.sets[fname] = set(path.read_text(encoding="utf-8").split()) if path else set()

    def looks_like_emote(self, name):
        if not re.fullmatch(r"[A-Za-z0-9]{3,25}", name) or name.isdigit() or name.upper() in STOP:
            return False
        low = name.lower()
        if name == low:
            # All lowercase reads as a word whether or not a dictionary knows it ("bitrate", "hmm", "mybad")
            return False
        if name == low.capitalize():
            # Reads as a word that starts a sentence, so any dictionary hit kills it ("Classic", "Sure")
            return low not in self.sets["words-all.txt"] and low not in self.sets["words-common.txt"]
        if name.isupper():
            # Shouting. Only common words are a real risk ("WHAT", "NICE"), "KEKW" is not
            return low not in self.sets["words-common.txt"]
        return True  # monkaS, catJAM: nobody types that by accident


def strike_key(src, frame):
    return hashlib.sha1(f"{PX}|{src}|{frame}".encode()).hexdigest()[:20]


def make_strike(src, frame):
    """One frame, margins cropped, fit into a transparent PX*PX square."""
    raw = http(src) if src.startswith(("http://", "https://")) else Path(src).read_bytes()
    img = Image.open(io.BytesIO(raw))
    # Plenty of animated emotes fade in from nothing, so default to the first frame with something on it
    for k in [frame] if frame is not None else range(min(getattr(img, "n_frames", 1), 60)):
        img.seek(k)
        im = img.convert("RGBA")
        if sum(im.getchannel("A").histogram()[128:]) > 0.05 * im.width * im.height:
            break
    # Emotes drawn in a corner of a big empty canvas are ants at 22px otherwise
    im = im.crop(im.getchannel("A").getbbox() or (0, 0, im.width, im.height))
    scale = min(PX / im.width, PX / im.height)
    w, h = max(1, round(im.width * scale)), max(1, round(im.height * scale))
    im = im.resize((w, h), Image.LANCZOS)
    canvas = Image.new("RGBA", (PX, PX), (0, 0, 0, 0))
    canvas.paste(im, ((PX - w) // 2, (PX - h) // 2))
    out = io.BytesIO()
    canvas.save(out, "PNG", optimize=True)
    return out.getvalue()


def load_strikes(data, registry):
    """PNG bytes per registry entry, None where the source is dead. Cached by source."""
    cache = data / "strikes"
    cache.mkdir(exist_ok=True)

    def one(entry):
        path = cache / (strike_key(entry["src"], entry.get("frame")) + ".png")
        if path.exists():
            return path.read_bytes()
        try:
            png = make_strike(entry["src"], entry.get("frame"))
        except Exception as e:
            return f"{type(e).__name__}: {e}"
        path.write_bytes(png)
        return png

    with ThreadPoolExecutor(16) as pool:
        results = list(pool.map(one, registry))
    for entry, r in zip(registry, results):
        if isinstance(r, str):
            print(f"  skipped {entry['name']}: {r}", file=sys.stderr)
    return [r if isinstance(r, bytes) else None for r in results]


def build_font(pngs, out_path, bearing):
    glyph_names = [f"e{i:05d}" for i in range(len(pngs))]
    order = [".notdef"] + glyph_names

    fb = FontBuilder(unitsPerEm=UPM, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap({FIRST_CP + i: g for i, g in enumerate(glyph_names)})
    fb.setupHorizontalMetrics({g: (UPM, 0) for g in order})
    asc, desc = round(UPM * ASC / PX), -round(UPM * DESC / PX)
    fb.setupHorizontalHeader(ascent=asc, descent=desc)
    fb.setupNameTable({"familyName": "Twitch Emotes", "styleName": "Regular"})
    fb.setupOS2(sTypoAscender=asc, sTypoDescender=desc, usWinAscent=asc, usWinDescent=-desc)
    fb.setupPost()
    font = fb.font

    blank = io.BytesIO()
    Image.new("RGBA", (PX, PX), (0, 0, 0, 0)).save(blank, "PNG")

    cbdt = font["CBDT"] = newTable("CBDT")
    cbdt.version = 3.0
    strike_data = {}
    for g, png in zip(glyph_names, pngs):
        glyph = cbdt_bitmap_format_17(None, font)
        m = glyph.metrics = SmallGlyphMetrics()
        m.height, m.width = PX, PX
        m.BearingX, m.BearingY, m.Advance = bearing, ASC, PX
        # The index subtable wants one unbroken glyph range, so a dead emote keeps its slot, empty
        glyph.imageData = png or blank.getvalue()
        strike_data[g] = glyph
    cbdt.strikeData = [strike_data]

    cblc = font["CBLC"] = newTable("CBLC")
    cblc.version = 3.0
    size = BitmapSizeTable()
    size.colorRef = 0
    size.startGlyphIndex, size.endGlyphIndex = 1, len(glyph_names)
    size.ppemX = size.ppemY = PX
    size.bitDepth = 32
    size.flags = 1  # horizontal metrics
    for attr in ("hori", "vert"):
        lm = SbitLineMetrics()
        lm.ascender, lm.descender, lm.widthMax = ASC, -DESC, PX
        lm.caretSlopeNumerator = lm.caretSlopeDenominator = lm.caretOffset = 0
        lm.minOriginSB = lm.minAdvanceSB = lm.maxBeforeBL = lm.minAfterBL = 0
        lm.pad1 = lm.pad2 = 0
        setattr(size, attr, lm)
    sub = eblc_index_sub_table_1(None, font)
    sub.indexFormat, sub.imageFormat = 1, 17
    sub.firstGlyphIndex, sub.lastGlyphIndex = 1, len(glyph_names)
    sub.names = glyph_names
    strike = Strike()
    strike.bitmapSizeTable = size
    strike.indexSubTables = [sub]
    cblc.strikes = [strike]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    font.save(out_path)


def publish_font(data, pngs, bearing):
    """Every build gets a fresh font-<N> folder and `generation` is bumped to N.

    Two birds: a running terminal keeps the old file mapped (Windows won't let us replace it),
    and wezterm only re-reads fonts when its config really changes. The shim in wezterm.lua
    watches `generation` and points font_dirs at font-<N>, so a bump is a live reload.
    """
    gen_file = data / "generation"
    old = int(gen_file.read_text().strip() or 0) if gen_file.exists() else 0
    gen = old + 1
    out = data / f"font-{gen}" / "TwitchEmotes.ttf"
    build_font(pngs, out, bearing)
    gen_file.write_text(str(gen))
    for stale in data.glob("font-*"):
        if stale.name != f"font-{gen}":
            try:
                for f in stale.iterdir():
                    f.unlink()
                stale.rmdir()
            except OSError:
                pass  # still mapped by a terminal that has not reloaded; next build gets it
    return out


def write_outputs(data, registry, cfg):
    cells = max(3, math.ceil(1 / cfg["cell_aspect"]))
    # The emote is 1 row tall and as wide; center it in the cells the mod pads it to
    bearing = round(PX * (cells * cfg["cell_aspect"] - 1) / 2)
    pngs = load_strikes(data, registry)
    out = publish_font(data, pngs, bearing)

    block_file = data / "block.txt"
    if not block_file.exists():
        block_file.write_text("# Emote names to never swap, one per line\n", encoding="utf-8")
    blocked = {l.strip() for l in block_file.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")}
    glyphs = {e["name"]: chr(FIRST_CP + i) for i, (e, png) in enumerate(zip(registry, pngs)) if png and e["name"] not in blocked}
    # Registry order is core first, then by popularity: the mod tells the model about the head of it
    (data / "emotes.json").write_text(json.dumps({"cells": cells, "glyphs": glyphs}, ensure_ascii=False), encoding="utf-8")
    (data / "registry.json").write_text(json.dumps(registry, indent=0), encoding="utf-8")
    print(f"{len(glyphs)} emotes in the font ({out.stat().st_size / 1048576:.1f} MiB)")
    import zedfont  # sibling file; Zed can't draw this font, so it gets its own rebuilt copy
    zedfont.rebuild_if_configured()


def upsert(registry, name, src, frame, override):
    for entry in registry:
        if entry["name"] == name:
            if override and (entry["src"], entry.get("frame")) != (src, frame):
                entry["src"], entry["frame"] = src, frame
            return False
    registry.append({"name": name, "src": src, "frame": frame})
    return True


def main():
    ap = argparse.ArgumentParser(description="Build the Twitch emote font")
    ap.add_argument("--dir", type=Path, default=Path(os.environ.get("TWITCH_EMOTES_DIR") or Path.home() / ".twitch-emotes"),
                    help="data directory (default ~/.twitch-emotes)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="fetch the emote lists and build the font")
    b.add_argument("--top", type=int, default=1000, help="how many of 7TV's most used emotes (default 1000)")
    b.add_argument("--ffz", type=int, default=200, help="how many of FFZ's most used emotes (default 200)")
    b.add_argument("--cell-aspect", type=float, help="your terminal cell's width / height (default 0.46; only moves the emote a pixel or two)")
    b.add_argument("--offline", action="store_true", help="rebuild from the registry, fetch nothing new")
    a = sub.add_parser("add", help="add one emote")
    a.add_argument("name")
    a.add_argument("rest", nargs="*", metavar="URL|@FRAME")
    sub.add_parser("list", help="print the emote names")
    args = ap.parse_args()

    data = args.dir
    data.mkdir(parents=True, exist_ok=True)
    reg_file, cfg_file = data / "registry.json", data / "config.json"
    registry = json.loads(reg_file.read_text(encoding="utf-8")) if reg_file.exists() else []
    cfg = {"cell_aspect": 0.46, **(json.loads(cfg_file.read_text()) if cfg_file.exists() else {})}

    if args.cmd == "list":
        print(" ".join(e["name"] for e in registry))
        return

    if args.cmd == "add":
        src = next((r for r in args.rest if not r.startswith("@")), None)
        frame = next((int(r[1:]) for r in args.rest if r.startswith("@")), None)
        src = src or find_7tv(args.name)
        if not src:
            sys.exit(f"no emote called exactly {args.name!r} on 7TV (names are case sensitive); pass a URL")
        if not upsert(registry, args.name, src, frame, override=True):
            print(f"{args.name} was already in, source updated")
        else:
            print(f"added {args.name}")
        write_outputs(data, registry, cfg)
        return

    if args.cell_aspect:
        cfg["cell_aspect"] = args.cell_aspect
    cfg_file.write_text(json.dumps(cfg))
    core = read_list(HERE / "core.txt")
    for name, src, frame in core:
        upsert(registry, name, src, frame, override=True)
    if not args.offline:
        words = Words(data)
        fetched = []
        for label, fn, n in (("7TV", top_7tv, args.top), ("FFZ", top_ffz, args.ffz)):
            try:
                got = fn(n) if n > 0 else []
            except Exception as e:
                print(f"warning: {label} top list failed: {type(e).__name__}: {e}", file=sys.stderr)
                got = []
            fetched += got
        added = sum(upsert(registry, name, src, None, override=False) for name, src in fetched if words.looks_like_emote(name))
        print(f"{len(core)} core + {added} new from the top lists")
    write_outputs(data, registry, cfg)


if __name__ == "__main__":
    try:
        from PIL import Image
        from fontTools.fontBuilder import FontBuilder
        from fontTools.ttLib import newTable
        from fontTools.ttLib.tables.BitmapGlyphMetrics import SmallGlyphMetrics
        from fontTools.ttLib.tables.C_B_D_T_ import cbdt_bitmap_format_17
        from fontTools.ttLib.tables.E_B_L_C_ import BitmapSizeTable, SbitLineMetrics, Strike, eblc_index_sub_table_1
    except ImportError as e:
        sys.exit(f"emotefont needs fonttools and pillow ({e}).\n  pip install fonttools pillow\nor run it with: uv run {Path(__file__).name}")
    main()
