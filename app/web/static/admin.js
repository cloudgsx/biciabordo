"use strict";
// The corridor map: an arc per searched pair, thickness by how often, and red
// where nothing was ever found. Same Esri basemap as the app, no key needed.
const map = L.map("map", { zoomControl: true }).setView([40.2, -3.6], 5);
L.tileLayer(
  "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
  { attribution: "&copy; Esri", maxZoom: 19, maxNativeZoom: 16 }).addTo(map);

const busiest = Math.max(1, ...CORRIDORS.map((c) => c.n));
const bounds = [];

for (const c of CORRIDORS) {
  if (c.from_lat == null || c.to_lat == null) continue;
  const a = [c.from_lat, c.from_lon], b = [c.to_lat, c.to_lon];
  // A slight curve so opposite directions between the same pair stay distinct.
  const mid = [(a[0] + b[0]) / 2 + (b[1] - a[1]) * 0.12,
               (a[1] + b[1]) / 2 - (b[0] - a[0]) * 0.12];
  const unsolved = c.solved === 0;
  L.polyline([a, mid, b], {
    color: unsolved ? "#e5a54b" : "#2b7fd4",
    weight: 1 + (c.n / busiest) * 5,
    opacity: unsolved ? 0.95 : 0.6,
    smoothFactor: 1,
  }).bindPopup(`<strong>${c.from} → ${c.to}</strong><br>${c.n} búsquedas · ${c.solved} resueltas`)
    .addTo(map);
  for (const p of [a, b]) {
    L.circleMarker(p, { radius: 3, color: "#2b7fd4", fillOpacity: 1, weight: 1 }).addTo(map);
    bounds.push(p);
  }
}
if (bounds.length) map.fitBounds(bounds, { padding: [30, 30] });
