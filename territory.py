"""Territory map (beta): works out each player's territory. mcdash's /realms page shows them as 3D markers
inside the live BlueMap (bluemap/territories.js), in the dashboard's colours, centred on world spawn.

Inputs (read-only):
  CO_DIR     CoreProtect's DuckDB (database.duckdb + .wal). Copied first, because the live server holds the lock.
  TILES_DIR  BlueMap low-res tiles for the overworld (level 2 = 5 blocks per pixel; top half colour, bottom half height).
  LEVEL_DAT  the world's level.dat, for the spawn point.
Output (OUT_DIR): realms.json, with each territory as a smoothed outline (block coords) and its surface height range.

Only surface activity counts: an event is kept when its y is no more than SURFACE_SLACK blocks below the lowest
surface point of its chunk (surface = BlueMap's top-block height). Mines, caves and deep basements are ignored.

Score per player per chunk:
  - significant blocks placed (storage/workstations/beds count most; dirt, stone, crops, fluids, leaves count nothing)
  - time spent there: 5-minute slots in which the player did anything logged there (blocks, chests, items, chat, commands)
  - both fade with age (half-life HALF_LIFE_DAYS).
Each player's score is blurred into an area of influence; every chunk goes to the strongest player above a threshold.
Run: python territory.py   (about a minute; mc-realms.timer runs it every 3 hours)
"""
import gzip, json, math, os, re, shutil, struct, tempfile, time, zlib

import duckdb
import numpy as np
from PIL import Image, ImageFilter

CO_DIR = os.environ.get("CO_DIR", "/co")
TILES_DIR = os.environ.get("TILES_DIR", "/tiles")
LEVEL_DAT = os.environ.get("LEVEL_DAT", "/level.dat")
OUT_DIR = os.environ.get("OUT_DIR", "/app/data/realms")
LOD, LOD_BLOCKS = 2, 2500          # level-2 tile covers 2500 blocks in 500 px -> 5 blocks per pixel
BPP = 5                            # blocks per pixel of the output map
CHUNK = 16
HALF_LIFE_DAYS = 21.0
SLOT = 300                         # seconds per presence slot
BLUR_CHUNKS = 8                    # spread of a player's influence around their works
THRESH = 0.32                      # density needed to hold land
SURFACE_SLACK = 8                  # blocks below the chunk's lowest surface that still count as "surface"
UNKNOWN_SURFACE_MIN_Y = 58         # chunks BlueMap hasn't rendered: keep events at or above this y
SPAWN_RADIUS = 10                  # blocks: spawn is its own neutral territory, nobody claims it
MARGIN = 600                       # blocks of map beyond the furthest realm

# ---------------------------------------------------------------- block weights
ZERO = re.compile(r"(water|lava|fire|vine|leaves|amethyst|bamboo|gravel|sand$|^dirt|grass|netherrack|^stone$|deepslate$|"
                  r"cobblestone$|cobbled_deepslate$|tuff$|granite$|diorite$|andesite$|wheat|carrots|potatoes|beetroots|stem|"
                  r"^pumpkin$|^melon$|sugar_cane|kelp|cactus|berry_bush|sapling|scaffolding|snow|farmland|dirt_path|"
                  r"magma_block|end_stone|fern|seagrass|torchflower_crop|pitcher_crop|cocoa|nether_wart|podzol|mud$|clay$|"
                  r"ice$|obsidian|bedrock|tnt|air|moss_carpet|^moss_block|dripleaf|azalea|mushroom$)")
LOW = re.compile(r"(torch|rail|redstone_wire|slime_block|honey_block|piston|observer|hopper|repeater|comparator|lever|button|"
                 r"pressure_plate|tripwire|dispenser|dropper|sculk)")
HIGH = re.compile(r"(chest|barrel|furnace|smoker|crafting_table|_bed$|enchanting_table|anvil|brewing_stand|lectern|"
                  r"bookshelf|smithing_table|beacon|respawn_anchor|jukebox|shulker_box|cartography_table|fletching_table|"
                  r"loom|stonecutter|grindstone|composter|bell$|decorated_pot|campfire|crafter|chiseled_bookshelf)")


def block_weight(material):
    m = material.split(":", 1)[-1]
    if HIGH.search(m):
        return 6.0
    if ZERO.search(m):
        return 0.0
    if LOW.search(m):
        return 0.25
    return 1.0


# ---------------------------------------------------------------- spawn
def read_spawn():
    """World spawn (x, y, z) from level.dat: compound 'spawn' holding int-array 'pos' [x, y, z]."""
    try:
        d = gzip.open(LEVEL_DAT).read()
        i = d.find(b"\x05spawn")
        j = d.find(b"\x0b\x00\x03pos", i)
        if i >= 0 and 0 <= j - i < 64:
            x, y, z = struct.unpack(">3i", d[j + 10:j + 22])
            return x, y, z
        sx = d.find(b"\x03\x00\x06SpawnX"); sz = d.find(b"\x03\x00\x06SpawnZ")
        if sx >= 0 and sz >= 0:
            return struct.unpack(">i", d[sx + 9:sx + 13])[0], 64, struct.unpack(">i", d[sz + 9:sz + 13])[0]
    except OSError as e:
        print(f"no level.dat ({e})")
    return int(os.environ.get("SPAWN_X", 0)), 64, int(os.environ.get("SPAWN_Z", 0))


# ---------------------------------------------------------------- data
def open_coreprotect(tmp):
    """Copy the DB pair and open the copy; retries if the copy caught a write half-way."""
    for attempt in range(4):
        for f in ("database.duckdb", "database.duckdb.wal"):
            src = os.path.join(CO_DIR, f)
            if os.path.exists(src):
                shutil.copyfile(src, os.path.join(tmp, f))
        try:
            con = duckdb.connect(os.path.join(tmp, "database.duckdb"))
            con.execute("select count(*) from co_user").fetchone()
            return con
        except Exception as e:
            print(f"copy unreadable ({e}); retrying", flush=True)
            time.sleep(5)
    raise SystemExit("could not read the CoreProtect database")


EVENT_TABLES = ("co_block", "co_container", "co_item", "co_chat", "co_command", "co_session", "co_sign", "co_entity_interaction")


def activity_bounds(con):
    """Block rectangle holding nearly all player activity in the overworld (ignores far-flung trips)."""
    src = " union all ".join(f"select user, x, z from {t} where wid = 1" for t in EVENT_TABLES)
    x0, x1, z0, z1 = con.execute(f"""
        with ev as ({src}) select quantile_cont(x, 0.003), quantile_cont(x, 0.997), quantile_cont(z, 0.003), quantile_cont(z, 0.997)
        from ev join co_user u on u.rowid = ev.user where u.user not like '#%'""").fetchone()
    pad = 1000
    return [int(v) // 500 * 500 for v in (x0 - pad, z0 - pad, x1 + pad + 500, z1 + pad + 500)]


def load_surface(con, tmp, col, hgt, bounds):
    """Table surf(cx, cz, h): lowest rendered surface height in each chunk."""
    bx0, bz0 = bounds[0], bounds[1]
    known = col[..., 3] > 0
    H, W = hgt.shape
    cz_idx = ((np.arange(H) * BPP + bz0) >> 4)[:, None].repeat(W, 1)
    cx_idx = ((np.arange(W) * BPP + bx0) >> 4)[None, :].repeat(H, 0)
    cx, cz, h = cx_idx[known], cz_idx[known], hgt[known]
    ox, oz = cx.min(), cz.min()
    grid = np.full((cz.max() - oz + 1, cx.max() - ox + 1), np.inf, np.float32)
    np.minimum.at(grid, (cz - oz, cx - ox), h)
    rr, cc = np.nonzero(np.isfinite(grid))
    path = os.path.join(tmp, "surf.csv")
    np.savetxt(path, np.column_stack([cc + ox, rr + oz, grid[rr, cc]]), fmt="%d", delimiter=",", header="cx,cz,h", comments="")
    con.execute(f"create temp table surf as select * from read_csv('{path}', header=true, columns={{'cx':'INTEGER','cz':'INTEGER','h':'INTEGER'}})")
    print(f"surface heights for {len(rr)} chunks", flush=True)


SURFACE_OK = f"(s.h is null and e.y >= {UNKNOWN_SURFACE_MIN_Y} or e.y >= s.h - {SURFACE_SLACK})"


# "Time spent" only counts things a person has to be at the keyboard to do. Left out: item pickups/drops (co_item),
# block clicks (co_block action 2: buttons, doors, levers) and kills (action 3), which AFK fishing, mob farms and
# autoclickers produce endlessly.
PRESENCE_SOURCES = (
    "select user, time, x, y, z from co_block where wid = 1 and action in (0, 1)",
    "select user, time, x, y, z from co_container where wid = 1",
    "select user, time, x, y, z from co_chat where wid = 1",
    "select user, time, x, y, z from co_command where wid = 1",
    "select user, time, x, y, z from co_session where wid = 1",
    "select user, time, x, y, z from co_sign where wid = 1",
    "select user, time, x, y, z from co_entity_interaction where wid = 1",
)


def load_activity(con, now, spawn):
    """Returns {player: {(cx, cz): [weighted_places, presence_slots_decayed, raw_slots, last_time]}}, surface only.
    Nothing inside the spawn circle counts for anyone."""
    con.execute("create temp table players as select rowid as uid, user as name from co_user where user not like '#%'")
    con.execute("create temp table weights (type integer, w double)")
    mats = con.execute("select id, material from co_material_map").fetchall()
    con.executemany("insert into weights values (?, ?)", [(i, block_weight(m)) for i, m in mats if block_weight(m) > 0])
    hl = HALF_LIFE_DAYS * 86400.0
    data = {}

    def cell(name, cx, cz):
        return data.setdefault(name, {}).setdefault((cx, cz), [0.0, 0.0, 0, 0])

    sx, sy, sz = spawn
    outside_spawn = f"((e.x::BIGINT - {sx}) * (e.x::BIGINT - {sx}) + (e.z::BIGINT - {sz}) * (e.z::BIGINT - {sz}) > {SPAWN_RADIUS * SPAWN_RADIUS})"
    con.execute("create temp table breaks as select x, y, z, max(time) as last_break from co_block where wid = 1 and action = 0 group by x, y, z")
    places = con.execute(f"""
        select p.name, e.x >> 4, e.z >> 4, sum(w.w * pow(0.5, ({now} - e.time) / {hl})), max(e.time)
        from co_block e join players p on p.uid = e.user join weights w on w.type = e.type
        left join surf s on s.cx = (e.x >> 4) and s.cz = (e.z >> 4)
        where e.wid = 1 and e.action = 1 and e.rolled_back = 0 and {SURFACE_OK} and {outside_spawn}
          -- only blocks still standing: nobody broke that spot after it was placed. Placing and breaking the same
          -- chest over and over therefore counts once, and blocks a griefer places then you clear don't count.
          and not exists (select 1 from breaks k where k.x = e.x and k.y = e.y and k.z = e.z and k.last_break > e.time)
        group by all""").fetchall()
    for name, cx, cz, s, last in places:
        c = cell(name, cx, cz); c[0] += s; c[3] = max(c[3], last)

    src = " union all ".join(PRESENCE_SOURCES)
    slots = con.execute(f"""
        with e as ({src}),
        s0 as (select distinct e.user, e.time // {SLOT} as slot, e.x >> 4 as cx, e.z >> 4 as cz
               from e left join surf s on s.cx = (e.x >> 4) and s.cz = (e.z >> 4) where {SURFACE_OK} and {outside_spawn})
        select p.name, cx, cz, sum(pow(0.5, ({now} - slot * {SLOT}) / {hl})), count(*), max(slot) * {SLOT}
        from s0 join players p on p.uid = s0.user group by all""").fetchall()
    for name, cx, cz, dec, raw, last in slots:
        c = cell(name, cx, cz); c[1] += dec; c[2] += raw; c[3] = max(c[3], last)
    return data


# ---------------------------------------------------------------- territory
def box_blur(a, r):
    """Three box blurs ~ a gaussian. Separable, via cumulative sums."""
    for _ in range(3):
        for axis in (0, 1):
            pad = [(0, 0), (0, 0)]; pad[axis] = (r + 1, r)
            c = np.cumsum(np.pad(a, pad, mode="constant"), axis=axis)
            hi = np.take(c, range(2 * r + 1, c.shape[axis]), axis=axis)
            lo = np.take(c, range(0, c.shape[axis] - 2 * r - 1), axis=axis)
            a = (hi - lo) / (2 * r + 1)
    return a


def chunk_score(v):
    places, presence = v[0], v[1]
    # sqrt keeps one giant wall or AFK farm from dwarfing everything else
    return np.sqrt(places) * 1.0 + np.sqrt(presence) * 2.0


def find_realms(data, area):
    scores = {}
    for name, cells in data.items():
        s = {k: chunk_score(v) for k, v in cells.items()}
        if sum(s.values()) > 40:
            scores[name] = s
    gx0, gz0 = area[0] >> 4, area[1] >> 4
    W, H = (area[2] >> 4) - gx0, (area[3] >> 4) - gz0
    names = sorted(scores)
    dens = np.zeros((len(names), H, W), dtype=np.float32)
    for i, n in enumerate(names):
        g = np.zeros((H, W), dtype=np.float32)
        for (cx, cz), v in scores[n].items():
            if 0 <= cx - gx0 < W and 0 <= cz - gz0 < H:
                g[cz - gz0, cx - gx0] += v
        dens[i] = box_blur(g, BLUR_CHUNKS // 2)
    best = dens.argmax(0); top = dens.max(0)
    owner = np.where(top > THRESH, best, -1)
    return names, owner, dens, (gx0, gz0, W, H)


def outlines(comp):
    """Boundary loops of a set of grid cells, as lists of (col, row) corner points.
    Edges run with the territory on their right; the loop with the largest area is the outer edge, the rest are holes."""
    cells = set(comp)
    nxt = {}
    for r, c in cells:
        if (r - 1, c) not in cells: nxt.setdefault((c, r), []).append((c + 1, r))          # top, going east
        if (r, c + 1) not in cells: nxt.setdefault((c + 1, r), []).append((c + 1, r + 1))  # right, going south
        if (r + 1, c) not in cells: nxt.setdefault((c + 1, r + 1), []).append((c, r + 1))  # bottom, going west
        if (r, c - 1) not in cells: nxt.setdefault((c, r + 1), []).append((c, r))          # left, going north
    loops = []
    while nxt:
        start = next(iter(nxt)); loop = [start]; cur = start; prev_dir = None
        while True:
            opts = nxt[cur]
            if len(opts) > 1 and prev_dir:  # pinch point: keep turning right so loops stay simple
                def turn(p):
                    d = (p[0] - cur[0], p[1] - cur[1])
                    return (prev_dir[0] * d[1] - prev_dir[1] * d[0])
                opts.sort(key=turn, reverse=True)
            n = opts.pop(0)
            if not opts:
                del nxt[cur]
            prev_dir = (n[0] - cur[0], n[1] - cur[1])
            cur = n
            if cur == start:
                break
            loop.append(cur)
        # drop points in the middle of straight runs
        pts = [p for i, p in enumerate(loop)
               if (loop[i - 1][0] - p[0]) * (loop[(i + 1) % len(loop)][1] - p[1]) != (loop[i - 1][1] - p[1]) * (loop[(i + 1) % len(loop)][0] - p[0])]
        if len(pts) >= 3:
            loops.append(pts)
    area = lambda l: 0.5 * sum(l[i - 1][0] * l[i][1] - l[i][0] * l[i - 1][1] for i in range(len(l)))
    loops.sort(key=lambda l: -abs(area(l)))
    return loops[0], loops[1:]


def chaikin(pts, rounds=3):
    """Round off the chunk staircase into smooth curves (closed loop)."""
    for _ in range(rounds):
        out = []
        for i in range(len(pts)):
            (x0, z0), (x1, z1) = pts[i], pts[(i + 1) % len(pts)]
            out += [(0.75 * x0 + 0.25 * x1, 0.75 * z0 + 0.25 * z1), (0.25 * x0 + 0.75 * x1, 0.25 * z0 + 0.75 * z1)]
        pts = out
    return pts


def simplify(pts, eps=2.5):
    """Ramer-Douglas-Peucker on a closed loop (block units)."""
    def rdp(seg):
        if len(seg) < 3:
            return seg
        (x0, z0), (x1, z1) = seg[0], seg[-1]
        dx, dz = x1 - x0, z1 - z0; n = (dx * dx + dz * dz) ** 0.5 or 1e-9
        dists = [abs(dz * (x - x0) - dx * (z - z0)) / n for x, z in seg[1:-1]]
        i = max(range(len(dists)), key=dists.__getitem__)
        if dists[i] <= eps:
            return [seg[0], seg[-1]]
        return rdp(seg[:i + 2])[:-1] + rdp(seg[i + 1:])
    far = max(range(len(pts)), key=lambda i: (pts[i][0] - pts[0][0]) ** 2 + (pts[i][1] - pts[0][1]) ** 2)
    return rdp(pts[:far + 1])[:-1] + rdp(pts[far:] + [pts[0]])[:-1]


def inside(pt, poly):
    """Point-in-polygon (ray casting); poly is [[x, z], ...]."""
    x, z = pt; hit = False
    for i in range(len(poly)):
        (x0, z0), (x1, z1) = poly[i - 1], poly[i]
        if (z0 > z) != (z1 > z) and x < x0 + (z - z0) * (x1 - x0) / (z1 - z0):
            hit = not hit
    return hit


def components(mask):
    """Connected components (4-neighbour) of a boolean grid, as lists of (row, col)."""
    seen = np.zeros_like(mask, dtype=bool); out = []
    for r0, c0 in zip(*np.nonzero(mask)):
        if seen[r0, c0]:
            continue
        stack = [(r0, c0)]; seen[r0, c0] = True; comp = []
        while stack:
            r, c = stack.pop(); comp.append((r, c))
            for rr, cc in ((r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)):
                if 0 <= rr < mask.shape[0] and 0 <= cc < mask.shape[1] and mask[rr, cc] and not seen[rr, cc]:
                    seen[rr, cc] = True; stack.append((rr, cc))
        out.append(comp)
    return out


# ---------------------------------------------------------------- base map
def load_tiles(bx0, bz0, bx1, bz1):
    """Colour and height arrays (5 blocks per pixel) for the block rectangle, stitched from level-2 tiles."""
    W, H = (bx1 - bx0) // BPP, (bz1 - bz0) // BPP
    col = np.zeros((H, W, 4), dtype=np.uint8); hgt = np.zeros((H, W), dtype=np.float32)
    root = os.path.join(TILES_DIR, str(LOD))
    for dirpath, _, files in os.walk(root):
        for f in files:
            m = re.fullmatch(r"x(-?\d+)z(-?\d+)\.png", (os.path.relpath(dirpath, root).replace(os.sep, "") + f))
            if not m:
                continue
            tx, tz = int(m[1]), int(m[2])
            ox, oz = tx * LOD_BLOCKS, tz * LOD_BLOCKS            # block origin of this tile
            if ox + LOD_BLOCKS < bx0 or ox > bx1 or oz + LOD_BLOCKS < bz0 or oz > bz1:
                continue
            im = np.asarray(Image.open(os.path.join(dirpath, f)).convert("RGBA"))[:, :500]
            c, meta = im[:500], im[501:1001]
            h = meta[..., 1].astype(np.float32) * 256 + meta[..., 2]
            h = np.where(h >= 32768, h - 65536, h)
            px, pz = (ox - bx0) // BPP, (oz - bz0) // BPP
            sx0, sz0 = max(0, -px), max(0, -pz)
            sx1, sz1 = min(500, W - px), min(500, H - pz)
            if sx1 <= sx0 or sz1 <= sz0:
                continue
            col[pz + sz0:pz + sz1, px + sx0:px + sx1] = c[sz0:sz1, sx0:sx1]
            hgt[pz + sz0:pz + sz1, px + sx0:px + sx1] = h[sz0:sz1, sx0:sx1]
    return col, hgt


def hexrgb(h):
    return np.array([int(h[i:i + 2], 16) for i in (1, 3, 5)], np.float32)


# The dashboard's player colours and rule (index.html playerColor / mcmon): CRC-32 of the name picks the colour,
# so a territory is always the same colour as its owner's name in the player list.
PALETTE = ["#78c8ff", "#ff96c8", "#a0e68c", "#ffc86e", "#bea0ff", "#78e6dc", "#ff8278"]


def player_color(name):
    return zlib.crc32(name.encode("utf-8")) % len(PALETTE)


# ---------------------------------------------------------------- main
def main():
    now = time.time()
    os.makedirs(OUT_DIR, exist_ok=True)
    spawn = read_spawn()
    with tempfile.TemporaryDirectory() as tmp:
        con = open_coreprotect(tmp)
        area = activity_bounds(con)
        col, hgt = load_tiles(*area)
        load_surface(con, tmp, col, hgt, area)
        data = load_activity(con, now, spawn)
        uuids = dict(con.execute("select user, uuid from co_user where uuid is not null").fetchall())
        con.close()
    names, owner, dens, grid = find_realms(data, area)
    gx0, gz0, W, H = grid
    realms, cells_of = [], {}
    for i, n in enumerate(names):
        for comp in components(owner == i):
            mass = float(sum(dens[i][r, c] for r, c in comp))
            if mass < 30 or len(comp) < 16:
                continue
            cells = {(gx0 + c, gz0 + r) for r, c in comp}
            raw = [v for k, v in data[n].items() if k in cells]
            places = sum(v[0] for v in raw); slots = sum(v[2] for v in raw); last = max((v[3] for v in raw), default=0)
            if slots * SLOT / 3600 < 4 and places < 150:
                continue  # passing through, not settled
            peak = max(comp, key=lambda rc: dens[i][rc])
            rl = {
                "player": n, "uuid": uuids.get(n), "mass": round(mass, 1),
                "seat": [int((gx0 + peak[1]) * CHUNK + 8), int((gz0 + peak[0]) * CHUNK + 8)],
                "chunks": len(comp), "hours": round(slots * SLOT / 3600, 1), "builds": int(round(places)),
                "last_active": int(last), "days_idle": round((now - last) / 86400, 1),
            }
            realms.append(rl); cells_of[id(rl)] = comp
    # each player's largest holding is their seat; the rest are outposts
    realms.sort(key=lambda r: -r["mass"])
    seen, colors, seats = set(), {}, {}
    for r in realms:
        r["capital"] = r["player"] not in seen; seen.add(r["player"])
        seats.setdefault(r["player"], r["seat"])
    for r in realms:
        r["color"] = player_color(r["player"])
    # the map is centred on spawn and reaches just past the furthest realm in each axis
    sx, sy, sz = spawn
    xs = [(gx0 + c) * CHUNK + d for r in realms for _, c in cells_of[id(r)] for d in (0, CHUNK)]
    zs = [(gz0 + rr) * CHUNK + d for r in realms for rr, _ in cells_of[id(r)] for d in (0, CHUNK)]
    hx = max(abs(v - sx) for v in xs) + MARGIN if xs else 2000
    hz = max(abs(v - sz) for v in zs) + MARGIN if zs else 2000
    hx, hz = -(-hx // 100) * 100, -(-hz // 100) * 100
    bounds = (sx - hx, sz - hz, sx + hx, sz + hz)
    # Spawn is its own neutral territory: a circle of SPAWN_RADIUS blocks.
    spawn_circle = [(sx + SPAWN_RADIUS * math.cos(t * math.pi / 24), sz + SPAWN_RADIUS * math.sin(t * math.pi / 24)) for t in range(48)]
    spawn_chunks = {(r_, c_) for r_ in range(((sz - SPAWN_RADIUS) >> 4) - gz0, ((sz + SPAWN_RADIUS) >> 4) - gz0 + 1)
                    for c_ in range(((sx - SPAWN_RADIUS) >> 4) - gx0, ((sx + SPAWN_RADIUS) >> 4) - gx0 + 1)}
    # outlines in block coordinates, and the surface height range under each territory (for 3D markers)
    conv = lambda loop: [[round((gx0 + c) * CHUNK, 1), round((gz0 + rr) * CHUNK, 1)] for c, rr in simplify(chaikin(loop), 2.5 / CHUNK)]
    for r in realms:
        outer, holes = outlines(cells_of[id(r)])
        r["shape"] = conv(outer)
        r["holes"] = [conv(h) for h in holes if len(h) >= 4]
        touching = [p for p in spawn_circle if inside(p, r["shape"]) and not any(inside(p, h) for h in r["holes"])]
        if len(touching) == len(spawn_circle):
            r["holes"].append([[round(x, 1), round(z, 1)] for x, z in spawn_circle])   # spawn sits inside: cut it out
        elif touching:
            # spawn straddles the border: give up the chunks under the circle so the territory stops short of it
            kept = [rc for rc in cells_of[id(r)] if rc not in spawn_chunks]
            if kept:
                outer, holes = outlines(kept)
                r["shape"] = conv(outer)
                r["holes"] = [conv(h) for h in holes if len(h) >= 4]
        hs = []
        for rr, c in cells_of[id(r)]:
            px = ((gx0 + c) * CHUNK + 8 - area[0]) // BPP; pz = ((gz0 + rr) * CHUNK + 8 - area[1]) // BPP
            if 0 <= pz < hgt.shape[0] and 0 <= px < hgt.shape[1] and col[pz, px, 3] > 0:
                hs.append(float(hgt[pz, px]))
        r["y"] = [int(np.percentile(hs, 10)) if hs else 60, int(np.percentile(hs, 90)) if hs else 80]
        sx_, sz_ = r["seat"]
        px = (sx_ - area[0]) // BPP; pz = (sz_ - area[1]) // BPP
        r["seat_y"] = int(hgt[pz, px]) if 0 <= pz < hgt.shape[0] and 0 <= px < hgt.shape[1] and col[pz, px, 3] > 0 else r["y"][1]
    del col, hgt
    tmpj = os.path.join(OUT_DIR, ".realms.json")
    json.dump({"generated": int(now), "bounds": [int(v) for v in bounds], "spawn": [int(sx), int(sz)],
               "spawn_zone": {"radius": SPAWN_RADIUS, "y": int(sy), "shape": [[round(x, 1), round(z, 1)] for x, z in spawn_circle]},
               "blocks_per_pixel": BPP, "palette": PALETTE, "realms": realms}, open(tmpj, "w"))
    os.replace(tmpj, os.path.join(OUT_DIR, "realms.json"))
    print(f"{len(realms)} holdings for {len(seen)} players; spawn {sx},{sz}; "
          f"{time.time() - now:.0f}s", flush=True)


if __name__ == "__main__":
    main()
