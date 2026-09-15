"use strict";

/** Parse a response as JSON, turning an HTML error page into a readable message. */
async function readJson(res) {
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(
      res.status === 503
        ? "El servidor no tiene una base de datos utilizable todavía."
        : `El servidor respondió ${res.status} con una página, no JSON.`);
  }
}

const CLASS_NAME = { 0: "free", 1: "reserved", 2: "partial", 3: "bagged" };
// Why a journey is on the list, keyed as the planner reports it.
const STRENGTHS = {
  earliest: "llega antes",
  later_start: "sales más tarde",
  least_cycling: "menos bici",
  fewest_trains: "menos trenes",
  no_reservation: "sin reserva",
};
const LEG_COLOUR = { free: "#47c97e", reserved: "#e5a54b", partial: "#d2708a", bagged: "#8f9bab" };

// CARTO now wants an API key and stamps "API KEY REQUIRED" across every tile it
// serves without one, so both backgrounds come from Esri's Canvas services: the
// same muted grey that lets a coloured route stand out, and no key. Esri splits
// the canvas in two, ground and labels, and station names are the whole point of
// the labels, so both layers are laid down and both follow the switch.
// The Canvas services stop at zoom 16, so `native` says where each basemap runs
// out of tiles: past it Leaflet stretches the last real ones instead of asking
// for a level that answers with a pale "Map data not yet available" placeholder.
// World Topo is a full basemap rather than a canvas, so it keeps going to 19 and
// carries its own place names -- hence no separate labels service.
const BASEMAPS = {
  light: {
    base: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
    labels: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}",
    native: 16,
  },
  dark: {
    base: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
    labels: "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}",
    native: 16,
  },
  topo: {
    base: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
    labels: null,
    native: 19,
  },
};
// Light reads better for a route drawn in greens and ambers, but the panel is
// dark and some people want the map to match, so it is a choice that sticks.
const savedTheme = localStorage.getItem("bicitren.mapTheme");
let mapTheme = Object.hasOwn(BASEMAPS, savedTheme || "") ? savedTheme : "light";

const map = L.map("map", { zoomControl: true }).setView([40.2, -3.6], 6);
const TILE_OPTS = {
  attribution: '&copy; Esri, HERE, Garmin &copy; OpenStreetMap',
  maxZoom: 19,
};
const tiles = L.tileLayer(BASEMAPS[mapTheme].base,
                          { ...TILE_OPTS, maxNativeZoom: BASEMAPS[mapTheme].native }).addTo(map);

// Labels ride in their own pane between the ground and the route, so a place
// name never lands on top of the line the whole map exists to show.
map.createPane("labels");
map.getPane("labels").style.zIndex = 350;
map.getPane("labels").style.pointerEvents = "none";
const labels = L.tileLayer(BASEMAPS[mapTheme].labels || BASEMAPS.light.labels,
                           { ...TILE_OPTS, maxNativeZoom: 16,
                             attribution: "", pane: "labels" });
if (BASEMAPS[mapTheme].labels) labels.addTo(map);
const drawn = L.layerGroup().addTo(map);

const picked = { origin: null, destination: null };
let journeys = [];
let selected = -1;

/** Station dots are hollow, so their fill has to be the map's own background. */
const stationFill = () => (mapTheme === "dark" ? "#11151a" : "#ffffff");

function setMapTheme(next) {
  mapTheme = Object.hasOwn(BASEMAPS, next) ? next : "light";
  localStorage.setItem("bicitren.mapTheme", mapTheme);
  document.body.classList.toggle("map-dark", mapTheme === "dark");

  const spec = BASEMAPS[mapTheme];
  tiles.options.maxNativeZoom = spec.native;
  tiles.setUrl(spec.base);

  // A basemap that prints its own place names must not also get the overlay, or
  // every town is labelled twice, slightly offset.
  if (spec.labels) {
    labels.setUrl(spec.labels);
    if (!map.hasLayer(labels)) labels.addTo(map);
  } else if (map.hasLayer(labels)) {
    map.removeLayer(labels);
  }

  // The drawn layer carries the old background colour inside its markers, so
  // it has to be rebuilt rather than restyled by CSS.
  if (journeys[selected]) drawOnMap(journeys[selected], { keepView: true });
}

// Two labelled halves rather than one icon that toggles: a lone 🌙 on a white
// map is easy to miss and says nothing about what it does, and an emoji is at
// the mercy of whatever fonts the machine has. Words always render.
const ThemeSwitch = L.Control.extend({
  options: { position: "topright" },
  onAdd() {
    const box = L.DomUtil.create("div", "map-theme");
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", "Fondo del mapa");
    const choices = [["light", "Claro"], ["dark", "Oscuro"], ["topo", "Topo"]];
    const buttons = choices.map(([value, label]) => {
      const button = L.DomUtil.create("button", "", box);
      button.type = "button";
      button.textContent = label;
      button.title = `Fondo del mapa: ${label.toLowerCase()}`;
      L.DomEvent.on(button, "click", (event) => {
        L.DomEvent.stop(event);
        setMapTheme(value);
        sync();
      });
      return [value, button];
    });
    const sync = () => {
      for (const [value, button] of buttons) {
        button.classList.toggle("on", value === mapTheme);
        button.setAttribute("aria-pressed", String(value === mapTheme));
      }
    };
    L.DomEvent.disableClickPropagation(box);
    sync();
    return box;
  },
});
map.addControl(new ThemeSwitch());
document.body.classList.toggle("map-dark", mapTheme === "dark");

// ------------------------------------------------------------- autocomplete

function wireAutocomplete(id) {
  const input = document.getElementById(id);
  const list = document.getElementById(`${id}-suggest`);
  let timer = null;

  input.addEventListener("input", () => {
    picked[id] = null;
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { list.hidden = true; return; }
    // Debounced so typing does not hammer the geocoder.
    timer = setTimeout(async () => {
      let items;
      try {
        items = await readJson(await fetch(`/api/places?q=${encodeURIComponent(q)}`));
      } catch {
        list.hidden = true; return;   // suggestions are optional; stay quiet
      }
      if (items.error) { list.hidden = true; return; }
      list.innerHTML = "";
      if (!items.length) { list.hidden = true; return; }
      for (const item of items) {
        const li = document.createElement("li");
        const label = item.kind === "station" ? item.name : item.name.split(",").slice(0, 3).join(",");
        li.innerHTML = `<span class="tag">${item.kind === "station" ? "estación" : "lugar"}</span>${label}`;
        li.addEventListener("mousedown", (e) => {
          e.preventDefault();
          picked[id] = item;
          input.value = label;
          list.hidden = true;
        });
        list.appendChild(li);
      }
      list.hidden = false;
    }, 250);
  });

  input.addEventListener("blur", () => setTimeout(() => { list.hidden = true; }, 120));
}
// The time field means something different in whole-day mode; say so.
const wholeDay = document.getElementById("whole-day");
const timeLabel = document.getElementById("time-label");
const syncTimeLabel = () => {
  timeLabel.textContent = wholeDay.checked ? "No antes de" : "Hora";
  // Whole-day mode picks its own departures across the day, so the count of
  // successive departures no longer applies; grey it out rather than let the
  // server quietly ignore it.
  const dep = document.getElementById("departures");
  dep.disabled = wholeDay.checked;
  dep.closest("label").classList.toggle("muted", wholeDay.checked);
};
wholeDay.addEventListener("change", syncTimeLabel);
syncTimeLabel();

wireAutocomplete("origin");
wireAutocomplete("destination");

// The two bike-class boxes pull in opposite directions: one keeps only the
// trains that need no paperwork, the other adds the least certain ones. Ticking
// either should visibly rule the other out rather than leave the server to
// silently pick.
const freeOnly = document.getElementById("free-only");
const allowAvant = document.getElementById("allow-avant");
const syncClassBoxes = () => {
  allowAvant.disabled = freeOnly.checked;
  allowAvant.closest("label").classList.toggle("muted", freeOnly.checked);
};
freeOnly.addEventListener("change", syncClassBoxes);
syncClassBoxes();

for (const [slider, out, suffix] of [["departures", "departures-out", ""],
                                     ["access", "access-out", " km"],
                                     ["transfer", "transfer-out", " km"],
                                     ["days", "days-out", ""],
                                     ["trains", "trains-out", ""]]) {
  const el = document.getElementById(slider);
  const target = document.getElementById(out);
  const sync = () => { target.textContent = el.value + suffix; };
  el.addEventListener("input", sync); sync();
}

// -------------------------------------------------------------- formatting

const fmtTime = (iso) => new Date(iso).toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
const fmtDay = (iso) => new Date(iso).toLocaleDateString("es-ES", { weekday: "short", day: "numeric", month: "short" });

function fmtDur(s) {
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${h} h ${String(m).padStart(2, "0")}` : `${m} min`;
}

// ------------------------------------------------------------- share links

const SHARE_SLIDERS = [["departures", "dep"], ["access", "km"],
                       ["transfer", "tk"], ["days", "d"], ["trains", "tr"]];
const SHARE_FLAGS = [["free-only", "fo"], ["allow-avant", "av"],
                     ["other-operators", "op"], ["whole-day", "md"], ["night", "n"]];

/** The whole search as URL parameters, carrying the resolved coordinates so a
 *  shared link never re-geocodes and cannot land somewhere else. */
function shareParams(index) {
  const p = new URLSearchParams();
  if (!picked.origin || !picked.destination) return p;
  p.set("de", picked.origin.name);
  p.set("dc", `${picked.origin.lat.toFixed(5)},${picked.origin.lon.toFixed(5)}`);
  p.set("a", picked.destination.name);
  p.set("ac", `${picked.destination.lat.toFixed(5)},${picked.destination.lon.toFixed(5)}`);
  p.set("f", document.getElementById("date").value);
  p.set("h", document.getElementById("time").value);
  for (const [id, key] of SHARE_SLIDERS) p.set(key, document.getElementById(id).value);
  for (const [id, key] of SHARE_FLAGS) {
    if (document.getElementById(id).checked) p.set(key, "1");
  }
  if (index != null && index >= 0) p.set("j", String(index));
  return p;
}

const shareUrl = (index) =>
  `${location.origin}${location.pathname}?${shareParams(index)}`;

/** Copy text despite the app usually being served over plain HTTP on the LAN,
 *  where navigator.clipboard does not exist at all (it needs a secure context).
 *  Falls back to a hidden textarea, then reports failure so the caller can show
 *  the link instead of silently doing nothing. */
async function copyText(text) {
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* fall through to the legacy path */ }

  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  ta.remove();
  return ok;
}

function showLinkFallback(url) {
  const status = document.getElementById("status");
  const box = document.createElement("div");
  box.className = "warn";
  box.textContent = "Copia el enlace a mano:";
  const field = document.createElement("input");
  field.type = "text";
  field.value = url;
  field.readOnly = true;
  field.style.marginTop = "6px";
  box.appendChild(field);
  status.prepend(box);
  field.focus();
  field.select();
}

async function shareJourney(index, button) {
  const url = shareUrl(index);
  if (await copyText(url)) {
    button.textContent = "✓";
    button.classList.add("copied");
    setTimeout(() => { button.textContent = "🔗"; button.classList.remove("copied"); }, 1600);
  } else {
    showLinkFallback(url);
  }
}

/** Restore a shared search from the URL and run it. */
async function applyShareParams() {
  const p = new URLSearchParams(location.search);
  if (!p.has("dc") || !p.has("ac")) return;

  const point = (name, coords) => {
    const [lat, lon] = (coords || "").split(",").map(Number);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
    return { name: name || "", lat, lon, kind: "shared" };
  };
  picked.origin = point(p.get("de"), p.get("dc"));
  picked.destination = point(p.get("a"), p.get("ac"));
  if (!picked.origin || !picked.destination) return;

  document.getElementById("origin").value = picked.origin.name;
  document.getElementById("destination").value = picked.destination.name;
  if (p.get("f")) document.getElementById("date").value = p.get("f");
  if (p.get("h")) document.getElementById("time").value = p.get("h");
  for (const [id, key] of SHARE_SLIDERS) {
    if (p.has(key)) {
      const el = document.getElementById(id);
      el.value = p.get(key);
      el.dispatchEvent(new Event("input"));
    }
  }
  for (const [id, key] of SHARE_FLAGS) {
    document.getElementById(id).checked = p.has(key);
  }
  // Let the option panel's own rules re-apply (whole-day greys out departures,
  // free-only greys out Avant) rather than restoring an inconsistent state.
  syncTimeLabel();
  syncClassBoxes();
  if (SHARE_FLAGS.some(([, key]) => p.has(key))) {
    document.getElementById("advanced").open = true;
  }

  await runSearch(p.has("j") ? Number(p.get("j")) : 0);
}

// ------------------------------------------------------------------ render

function renderJourney(journey, index) {
  // A <details> so the list of departures reads as a timetable at a glance and
  // opens into the full leg-by-leg plan only for the one being considered.
  const card = document.createElement("details");
  card.className = "journey";
  card.dataset.index = index;
  card.open = index === 0;

  const trains = `${journey.n_trains} tren${journey.n_trains === 1 ? "" : "es"}`;
  const daysPill = journey.days > 1 ? `<span class="pill days">${journey.days} días</span>` : "";
  const kmPill = journey.bike_km > 0 ? `<span class="pill km">${journey.bike_km} km bici</span>` : "";

  // Alternatives for the same departure each win at something, and without
  // saying what, a slower one just looks like a worse one. A departure that is
  // simply the next train carries no tag, which is the right answer for it.
  const tags = (journey.strengths || []).filter((s) => s in STRENGTHS);
  const strengths = tags.length
    ? `<div class="j-why">${tags.map((s) => `<span class="why">${STRENGTHS[s]}</span>`).join("")}</div>`
    : "";

  card.innerHTML = `
    <summary>
      ${strengths}
      <div class="j-head">
        <div class="j-when">
          ${fmtTime(journey.depart)} → ${fmtTime(journey.arrive)}
          <small>${fmtDay(journey.depart)} · ${fmtDay(journey.arrive)}</small>
        </div>
        <div class="j-meta">${fmtDur(journey.duration_s)}<br>${trains} ${daysPill} ${kmPill}</div>
        <button class="share" type="button" title="Copiar enlace a esta combinación"
                aria-label="Copiar enlace">🔗</button>
        <span class="j-chev" aria-hidden="true">⌄</span>
      </div>
    </summary>
    <div class="legs"></div>`;

  const legs = card.querySelector(".legs");
  for (const leg of journey.legs) {
    const row = document.createElement("div");
    if (leg.kind === "ride") {
      row.className = `leg ${CLASS_NAME[leg.bike_class]}`;
      const stops = leg.stops_skipped ? ` · ${leg.stops_skipped} paradas` : " · directo";
      // Name the operator when it is not Renfe, so a journey that depends on
      // FGC or Euskotren says so rather than looking like an ordinary train.
      const op = leg.operator && !leg.operator.startsWith("Renfe")
        ? `<span class="op">${leg.operator}</span>` : "";
      row.innerHTML = `
        <div class="t">${fmtTime(leg.dep)}<br>${fmtTime(leg.arr)}</div>
        <div class="body">
          <span class="svc">${leg.service}</span>${op}${leg.from.name} → ${leg.to.name}
          <span class="note">${fmtDur(leg.seconds)}${stops} — ${leg.bike_note}</span>
        </div>`;
    } else if (leg.kind === "change") {
      row.className = "leg change";
      row.innerHTML = `
        <div class="t">${fmtTime(leg.dep)}<br>${fmtTime(leg.arr)}</div>
        <div class="body">↪ cambio a ${leg.to.name}
          <span class="note">${fmtDur(leg.seconds)} — misma estación o contigua</span></div>`;
    } else if (leg.kind === "bike") {
      row.className = "leg bike";
      row.innerHTML = `
        <div class="t">${fmtTime(leg.dep)}<br>${fmtTime(leg.arr)}</div>
        <div class="body">🚲 ${leg.km} km · ${leg.from.name} → ${leg.to.name}
          <span class="note">${fmtDur(leg.seconds)} pedaleando</span></div>`;
    } else {
      const overnight = leg.kind === "overnight";
      row.className = `leg gap ${overnight ? "overnight" : ""}`;
      const icon = overnight ? "🛏" : leg.kind === "layover" ? "⏸" : "↳";
      const what = overnight ? "noche en" : "espera en";
      row.innerHTML = `<div class="t"></div>
        <div class="body">${icon} ${what} ${leg.at} · ${fmtDur(leg.seconds)}</div>`;
    }
    legs.appendChild(row);
  }

  // Opening a departure is how you say "this is the one I am looking at", so it
  // draws; clicking anywhere inside an already-open card does the same without
  // closing it.
  card.querySelector(".share").addEventListener("click", (event) => {
    event.preventDefault();     // do not open/close the <details>
    event.stopPropagation();    // do not re-select through the card handler
    shareJourney(index, event.currentTarget);
  });
  card.addEventListener("toggle", () => { if (card.open) select(index); });
  card.addEventListener("click", () => select(index));
  return card;
}

function select(index) {
  // A card left over from a previous search can still fire a toggle; ignore it
  // rather than draw a journey that is no longer on the list.
  if (!journeys[index]) return;
  selected = index;
  if (picked.origin && picked.destination) {
    history.replaceState(null, "", `?${shareParams(index)}`);
  }
  journeys.forEach((_, i) => {
    document.querySelector(`.journey[data-index="${i}"]`)?.classList.toggle("active", i === index);
  });
  drawOnMap(journeys[index]);
}

function drawOnMap(journey, { keepView = false } = {}) {
  drawn.clearLayers();
  const bounds = [];

  for (const leg of journey.legs) {
    if (leg.kind === "ride") {
      const pts = leg.path.filter((p) => p.lat != null).map((p) => [p.lat, p.lon]);
      if (pts.length < 2) continue;
      const colour = LEG_COLOUR[CLASS_NAME[leg.bike_class]];
      L.polyline(pts, { color: colour, weight: 4, opacity: 0.9 }).addTo(drawn);
      pts.forEach((p) => bounds.push(p));
      for (const p of [leg.path[0], leg.path[leg.path.length - 1]]) {
        L.circleMarker([p.lat, p.lon], {
          radius: 5, color: colour, fillColor: stationFill(), fillOpacity: 1, weight: 2,
        }).bindPopup(`<strong>${p.name}</strong><br>${leg.service}`).addTo(drawn);
      }
    } else if (leg.kind === "bike" || leg.kind === "change") {
      const a = leg.from, b = leg.to;
      if (a.lat == null || b.lat == null) continue;
      L.polyline([[a.lat, a.lon], [b.lat, b.lon]], {
        color: LEG_COLOUR.free, weight: 3, opacity: 0.95, dashArray: "6 7",
      }).bindPopup(`🚲 ${leg.km} km · ${a.name} → ${b.name}`).addTo(drawn);
      bounds.push([a.lat, a.lon], [b.lat, b.lon]);
    }
  }
  // Redrawing after a basemap switch must not yank the view the user just panned.
  if (bounds.length && !keepView) map.fitBounds(bounds, { padding: [40, 40] });
}

// ------------------------------------------------------------------ submit

/** Offer the stations a rider would actually head for when a place has no line. */
function renderUncovered(data) {
  const status = document.getElementById("status");
  status.innerHTML = "";

  for (const block of data.uncovered) {
    const box = document.createElement("div");
    box.className = "uncovered";
    const role = block.role === "origin" ? "salir de" : "llegar a";
    box.innerHTML = `<p><strong>${block.place}</strong> no tiene estación con
      servicio a menos de ${data.access_km} km. Para ${role} allí, las estaciones
      más cercanas son:</p>`;

    if (block.ranked_by_effort) {
      const how = document.createElement("p");
      how.className = "rank-note";
      how.textContent = "Ordenadas por esfuerzo real (distancia + desnivel), no por distancia.";
      box.appendChild(how);
    }

    const list = document.createElement("div");
    list.className = "stations";
    for (const st of block.stations) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "station-chip";
      const climb = st.ascent_m != null
        ? `<span class="climb">↗ ${st.ascent_m} m</span>` : "";
      chip.innerHTML = `<span class="nm">${st.name}</span>
        <span class="dist">${st.km} km ${st.direction} ${climb}
          · ${fmtDur(st.ride_minutes * 60)} en bici</span>`;
      chip.addEventListener("click", () => {
        // Plan to the station itself rather than the unreachable village.
        picked[block.role] = { name: st.name, lat: st.lat, lon: st.lon, kind: "station" };
        document.getElementById(block.role).value = st.name;
        runSearch();
      });
      list.appendChild(chip);
    }
    box.appendChild(list);

    if (block.suggested_access_km && block.suggested_access_km <= 45) {
      const widen = document.createElement("button");
      widen.type = "button";
      widen.className = "widen";
      widen.textContent = `Ampliar el radio a ${block.suggested_access_km} km y buscar de nuevo`;
      widen.addEventListener("click", () => {
        const slider = document.getElementById("access");
        slider.value = block.suggested_access_km;
        slider.dispatchEvent(new Event("input"));
        runSearch();
      });
      box.appendChild(widen);
    } else if (block.stations.length) {
      const far = document.createElement("p");
      far.className = "far-note";
      far.textContent = `La más cercana está a ${block.stations[0].km} km, `
        + `más de lo que el radio permite. Elige una estación de arriba para `
        + `planificar hasta allí y pedalear el resto.`;
      box.appendChild(far);
    }
    status.appendChild(box);
  }
}

async function runSearch(selectIndex = 0) {
  const status = document.getElementById("status");
  const results = document.getElementById("results");
  const button = document.getElementById("go");
  for (const id of ["origin", "destination"]) {
    document.getElementById(`${id}-suggest`).hidden = true;
  }

  for (const id of ["origin", "destination"]) {
    if (!picked[id]) {
      const q = document.getElementById(id).value.trim();
      if (!q) return;
      // Fall back to the first match if the user typed without choosing.
      const res = await fetch(`/api/places?q=${encodeURIComponent(q)}`);
      const items = await readJson(res);
      if (items.error || !items.length) {
        status.innerHTML = `<div class="warn">No encuentro «${q}».</div>`;
        return;
      }
      picked[id] = items[0];
    }
  }

  button.disabled = true;
  status.innerHTML = "<p style='color:var(--muted)'>Buscando combinaciones…</p>";
  results.innerHTML = "";
  drawn.clearLayers();

  try {
    const res = await fetch("/api/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        origin: picked.origin,
        destination: picked.destination,
        date: document.getElementById("date").value,
        time: document.getElementById("time").value,
        days: +document.getElementById("days").value,
        max_trains: +document.getElementById("trains").value,
        access_km: +document.getElementById("access").value,
        transfer_km: +document.getElementById("transfer").value,
        free_only: freeOnly.checked,
        allow_avant: document.getElementById("allow-avant").checked,
        other_operators: document.getElementById("other-operators").checked,
        whole_day: document.getElementById("whole-day").checked,
        departures: +document.getElementById("departures").value,
        night_riding: document.getElementById("night").checked,
      }),
    });
    const data = await readJson(res);
    if (data.error) {
      status.innerHTML = `<div class="warn">${data.error}</div>`;
      return;
    }
    if (data.uncovered && data.uncovered.length) {
      journeys = [];
      selected = -1;
      renderUncovered(data);
      return;
    }
    journeys = data.journeys;
    selected = -1;
    status.innerHTML = (data.warnings || []).map((w) => `<div class="warn">${w}</div>`).join("");
    journeys.forEach((j, i) => results.appendChild(renderJourney(j, i)));
    if (journeys.length) {
      const index = Math.min(Math.max(selectIndex, 0), journeys.length - 1);
      // renderJourney opens the first card, and that toggle fires after this
      // runs, so a shared link to another departure would be overridden back to
      // the first. Move the open card before selecting.
      if (index !== 0) {
        const cards = results.querySelectorAll(".journey");
        cards[0].open = false;
        cards[index].open = true;
      }
      select(index);
    }
  } catch (err) {
    status.innerHTML = `<div class="warn">Error consultando el planificador: ${err.message}</div>`;
  } finally {
    button.disabled = false;
  }
}

document.getElementById("form").addEventListener("submit", (event) => {
  event.preventDefault();
  runSearch();
});

if (document.readyState === "complete") {
  applyShareParams();
} else {
  window.addEventListener("load", applyShareParams, { once: true });
}
