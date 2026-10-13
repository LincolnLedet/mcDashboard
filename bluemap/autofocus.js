// Opened without a shared position link? Point the camera at whoever is online:
// one player -> follow them, several -> frame them all.
(async () => {
  const nav = performance.getEntriesByType("navigation")[0];
  if (nav && new URL(nav.name).hash.length > 1) return; // keep positions from shared links

  const app = window.bluemap;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const onlinePlayers = async mapId => {
    try {
      const res = await fetch(`maps/${mapId}/live/players.json`, { cache: "no-store" });
      return (await res.json()).players.filter(p => !p.foreign);
    } catch {
      return [];
    }
  };

  // Use the world (overworld, nether, end) with the most players in it.
  let best = null;
  for (const map of app.maps) {
    const players = await onlinePlayers(map.data.id);
    if (players.length && (!best || players.length > best.players.length)) best = { mapId: map.data.id, players };
  }
  if (!best) return;
  if (app.mapViewer.map?.data.id !== best.mapId) await app.switchMap(best.mapId, false);

  const cm = app.mapViewer.controlsManager;
  if (best.players.length === 1) {
    // Player markers load a moment after the map; wait for them.
    for (let i = 0; i < 20; i++) {
      const marker = app.playerMarkerManager?.getPlayerMarker(best.players[0].uuid);
      if (marker && cm.controls?.followPlayerMarker) {
        cm.controls.followPlayerMarker(marker);
        cm.distance = 200;
        return;
      }
      await sleep(500);
    }
  }

  const xs = best.players.map(p => p.position.x);
  const zs = best.players.map(p => p.position.z);
  const y = best.players.reduce((sum, p) => sum + p.position.y, 0) / best.players.length;
  const spread = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...zs) - Math.min(...zs));
  cm.position.set((Math.max(...xs) + Math.min(...xs)) / 2, y, (Math.max(...zs) + Math.min(...zs)) / 2);
  cm.distance = Math.min(Math.max(spread * 1.3, 300), 20000);
})();
