// Territory view for mcdash. BlueMap is embedded as
//   https://map.heyim.link/?territories        (dashboard live map: draws nothing until told to, camera left alone)
//   https://map.heyim.link/?territories=spawn  (the /realms page: also starts the camera at spawn)
// Does nothing on a normal BlueMap visit. In territory mode it:
//   - waits for mcdash to post the territory data (postMessage, so no CORS is needed),
//   - draws each territory as a translucent 3D "extrude" marker following the terrain height, with a name tag,
//   - dims the terrain with the dashboard's colour filter,
//   - follows focus / filter / visibility messages from the mcdash page.
(() => {
  const MODE = new URLSearchParams(location.search).get("territories");
  if (MODE === null) return;
  const PARENTS = ["https://mcdash.heyim.link", "http://127.0.0.1:8081", "http://localhost:8081"];
  const SET_ID = "heyim-territories";
  const app = window.bluemap;
  if (!app || window.parent === window) return;

  const css = document.createElement("style");
  css.textContent = `
    body.heyim-filter #map-container canvas { filter: saturate(.72) brightness(.62) contrast(1.1); }
    .heyim-tag { transform: translate(-50%, -50%); pointer-events: none; white-space: nowrap; color: var(--c);
      font: 700 13px/1.2 "JetBrains Mono", "Fira Code", ui-monospace, Menlo, Consolas, monospace; letter-spacing: .04em;
      text-shadow: 0 0 3px #0b0e14, 0 0 3px #0b0e14, 0 0 6px #0b0e14, 0 1px 1px #000; }
    .heyim-tag.out { font-weight: 400; font-size: 11px; opacity: .85; }
    /* head (from BlueMap's own player-head cache) over a coloured initial that shows if there's no head yet */
    .heyim-hd { position: relative; width: 26px; height: 26px; display: grid; place-items: center; border-radius: 4px;
      background: var(--c); color: #0b0e14; font-size: 14px; text-shadow: none; border: 2px solid var(--c);
      box-shadow: 0 0 0 1px #0b0e14, 0 2px 6px rgba(0,0,0,.6); }
    .heyim-hd span { position: absolute; inset: 0; border-radius: 2px; background-size: cover; image-rendering: pixelated; }
    .heyim-head { transform: translate(-50%, calc(-100% - 11px)); pointer-events: none; }
    .heyim-head.idle { opacity: .55; }
    .heyim-head.out .heyim-hd { width: 18px; height: 18px; font-size: 10px; border-width: 1px; }
    .heyim-tag.idle { opacity: .55; }
    .heyim-tag.sel { color: #dce1eb; text-decoration: underline var(--c) 2px; text-underline-offset: 3px; }`;
  document.head.appendChild(css);

  // BlueMap refreshes its own markers every few seconds and drops sets it doesn't know about; keep ours.
  const root = app.mapViewer.markers;
  const orig = root.updateMarkerSetsFromData.bind(root);
  root.updateMarkerSetsFromData = (data = {}, ignore = []) => orig(data, [...ignore, SET_ID]);

  let D = null, opts = { territories: true, outposts: true }, selected = null, centred = MODE !== "spawn";
  // label point: area-weighted centre of the outline, so the name sits inside the territory
  const centroid = pts => {
    let a = 0, cx = 0, cz = 0;
    for (let i = 0; i < pts.length; i++) {
      const [x0, z0] = pts[i], [x1, z1] = pts[(i + 1) % pts.length], f = x0 * z1 - x1 * z0;
      a += f; cx += (x0 + x1) * f; cz += (z0 + z1) * f;
    }
    return a ? [cx / (3 * a), cz / (3 * a)] : pts[0];
  };
  const rgb = (h, a) => ({ r: parseInt(h.slice(1, 3), 16), g: parseInt(h.slice(3, 5), 16), b: parseInt(h.slice(5, 7), 16), a });
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const NAME_DISTANCE = 5000;   // names fade out beyond this camera distance (outposts at half)
  const HEAD_DISTANCE = 1100;   // camera distance (blocks) under which heads appear; outposts at half that
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const ago = d => d < 1 ? "today" : d < 2 ? "yesterday" : `${Math.round(d)}d ago`;

  function draw() {
    const markers = {};
    if (D && opts.territories && app.mapViewer.map?.data.id === "world") {
      // spawn: its own neutral territory, a small circle nobody can claim
      if (D.spawn_zone) {
        const z = D.spawn_zone, [sx, sz] = D.spawn;
        markers["spawn-zone"] = {
          type: "extrude", label: "Spawn", listed: false,
          position: { x: sx, y: z.y, z: sz },
          shape: z.shape.map(([x, z2]) => ({ x, z: z2 })),
          shapeMinY: z.y - 4, shapeMaxY: z.y + 14,
          lineWidth: 2, lineColor: rgb("#dce1eb", .8), fillColor: rgb("#dce1eb", .1), depthTest: true,
          detail: `<b>Spawn</b><br>neutral ground, ${z.radius}-block radius &middot; nobody can claim it`,
        };
        markers["spawn-tag"] = {
          type: "html", label: "Spawn", listed: false, classes: [],
          position: { x: sx, y: z.y + 6, z: sz },
          html: `<div class="heyim-tag" style="--c:#dce1eb">Spawn</div>`,
          maxDistance: NAME_DISTANCE,
        };
      }
      D.realms.forEach((r, i) => {
        if (!r.capital && !opts.outposts) return;
        const c = D.palette[r.color], idle = r.days_idle >= 14;
        {
          markers[`area-${i}`] = {
            type: "extrude", label: r.player, listed: false,
            position: { x: r.seat[0], y: r.y[0], z: r.seat[1] },
            shape: r.shape.map(([x, z]) => ({ x, z })),
            holes: (r.holes || []).map(h => h.map(([x, z]) => ({ x, z }))),
            shapeMinY: r.y[0] - 4, shapeMaxY: r.y[1] + 14,
            lineWidth: 2, lineColor: rgb(c, idle ? .4 : .7), fillColor: rgb(c, selected === i ? .18 : .08),
            depthTest: true,
            detail: `<b>${esc(r.player)}</b> &middot; ${r.capital ? "main base" : "outpost"}<br>` +
              `${r.chunks.toLocaleString()} chunks &middot; ${Math.round(r.hours)} h here<br>` +
              `${r.builds.toLocaleString()} blocks built &middot; last active ${ago(r.days_idle)}`,
          };
        }
        const [lx, lz] = centroid(r.shape);
        markers[`tag-${i}`] = {
          type: "html", label: r.player, listed: false, classes: [],
          position: { x: lx, y: r.y[1] + 4, z: lz },
          html: `<div class="heyim-tag${r.capital ? "" : " out"}${idle ? " idle" : ""}${selected === i ? " sel" : ""}" style="--c:${c}">${esc(r.player)}</div>`,
          maxDistance: r.capital ? NAME_DISTANCE : NAME_DISTANCE / 2,
        };
        // the head only shows once the camera is close enough to be looking at this territory
        markers[`head-${i}`] = {
          type: "html", label: r.player, listed: false, classes: [],
          position: { x: lx, y: r.y[1] + 4, z: lz },
          html: `<div class="heyim-head${r.capital ? "" : " out"}${idle ? " idle" : ""}" style="--c:${c}"><div class="heyim-hd">${esc(r.player[0].toUpperCase())}` +
            `${UUID.test(r.uuid || "") ? `<span style="background-image:url(maps/world/assets/playerheads/${r.uuid}.png)"></span>` : ""}</div></div>`,
          maxDistance: r.capital ? HEAD_DISTANCE : HEAD_DISTANCE / 2,
        };
      });
    }
    root.updateMarkerSetFromData(SET_ID, { label: "Territories", toggleable: true, sorting: -10, markers });
  }

  async function focus(x, z, distance) {
    if (app.mapViewer.map?.data.id !== "world") await app.switchMap("world", false);
    const cm = app.mapViewer.controlsManager;
    cm.position.set(x, 70, z);
    cm.distance = distance;
  }

  app.events.addEventListener("bluemapMapChanged", draw);
  addEventListener("message", e => {
    if (!PARENTS.includes(e.origin) || !e.data || typeof e.data !== "object") return;
    const m = e.data;
    if (m.type === "heyim-territories") {
      D = m.data; draw();
      if (!centred) { centred = true; focus(D.spawn[0], D.spawn[1], 1800); }
    } else if (m.type === "heyim-focus") {
      selected = Number.isInteger(m.index) ? m.index : null; draw();
      focus(Number(m.x), Number(m.z), Number(m.distance) || 600);
    } else if (m.type === "heyim-filter") {
      document.body.classList.toggle("heyim-filter", !!m.on);
    } else if (m.type === "heyim-show") {
      opts = { territories: !!m.territories, outposts: !!m.outposts }; draw();
    }
  });
  window.parent.postMessage({ type: "heyim-ready" }, "*");
})();
