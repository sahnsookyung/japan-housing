/** Normal interactive tile viewing only; no prefetching or geocoding requests. */
const destinationSymbols = {
  office: {
    label: "Office destination",
    html: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect x="3" y="7" width="18" height="14" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12h18M10 12v3h4v-3"/></svg>',
  },
  relocation: { label: "Relocation destination", html: "↗" },
  unknown: { label: "Office location unconfirmed", html: "?" },
};

function legendItem(className, html, label) {
  const item = document.createElement("span"),
    symbol = document.createElement("span");
  symbol.className = `${className} map-key-marker`;
  symbol.setAttribute("aria-hidden", "true");
  symbol.innerHTML = html;
  item.append(symbol, document.createTextNode(label));
  return item;
}

export function createHousingMap({ onRoom, onDrop, onMove, onEdit, onError }) {
  if (!globalThis.L) {
    onError(
      "Map library unavailable. Coordinates remain editable in the location form.",
    );
    return { update() {}, resize() {}, drop() {}, cancel() {}, focus() {} };
  }
  const L = globalThis.L;
  const map = L.map("housing-map", { scrollWheelZoom: false }).setView(
    [35.68, 139.7],
    11,
  );
  const tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution:
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  let tileFailure = false;
  tiles.on("tileerror", () => {
    if (!tileFailure) {
      tileFailure = true;
      onError(
        "Map tiles are unavailable. Pins and coordinate editing still work.",
      );
    }
  });
  const homes = L.layerGroup().addTo(map),
    destinations = L.layerGroup().addTo(map);
  let dropping = false,
    temporary = null,
    fitted = false;
  const roomMarkers = new Map();
  const icon = (className, text) =>
    L.divIcon({
      className,
      html: String(text),
      iconSize: [28, 28],
      iconAnchor: [14, 14],
    });
  function cancel() {
    dropping = false;
    map.getContainer().style.cursor = "";
    if (temporary) {
      temporary.remove();
      temporary = null;
    }
  }
  map.on("click", (e) => {
    if (!dropping) return;
    cancel();
    temporary = L.marker(e.latlng, {
      icon: icon("destination-pin", "+"),
      title: "New destination — not yet saved",
      keyboard: false,
    }).addTo(map);
    onDrop(e.latlng.lat, e.latlng.lng);
  });
  function update(data) {
    homes.clearLayers();
    destinations.clearLayers();
    roomMarkers.clear();
    for (const group of data.map_points) {
      const frontier = group.rooms.some((r) => r.frontier);
      const countLabel = `${group.count} ${group.count === 1 ? "room" : "rooms"} at this location`;
      const names =
        group.rooms
          .slice(0, 3)
          .map((room) => room.title)
          .join(" · ") +
        (group.count > 3 ? ` · ${group.count - 3} more rooms` : "");
      const label = `${countLabel}: ${names}${frontier ? " · Contains a frontier room" : ""}`;
      const marker = L.marker([group.latitude, group.longitude], {
        icon: icon(
          frontier ? "room-pin frontier-pin" : "room-pin",
          group.count,
        ),
        title: label,
        keyboard: true,
      }).addTo(homes);
      marker
        .getElement()
        ?.setAttribute("aria-label", `${label}. Open room choices.`);
      const hover = document.createElement("div");
      hover.textContent = label;
      hover.style.maxWidth = "260px";
      hover.style.whiteSpace = "normal";
      marker.bindTooltip(hover);
      const popup = document.createElement("div");
      for (const room of group.rooms) {
        const button = document.createElement("button");
        button.className = "map-room-choice";
        button.textContent = `${room.title} · ${room.price == null ? "Cost incomplete" : `${room.price_is_floor ? "≥ " : ""}¥${room.price.toLocaleString()}/mo${room.price_is_floor ? " · fees missing" : ""}`}`;
        button.addEventListener("click", () => onRoom(room.id));
        popup.append(button);
        roomMarkers.set(room.id, marker);
      }
      if (group.count > group.rooms.length) {
        const note = document.createElement("p");
        note.textContent = `Showing ${group.rooms.length} of ${group.count} rooms here. Search the property in the table for all rooms.`;
        popup.append(note);
      }
      marker.bindPopup(popup, { maxWidth: 310 });
    }
    const bounds = [];
    const visibleKinds = new Set();
    for (const destination of data.preferences.destinations) {
      if (
        !destination.enabled ||
        destination.latitude == null ||
        destination.kind === "remote"
      )
        continue;
      visibleKinds.add(destination.kind);
      const symbol = destinationSymbols[destination.kind];
      const label = `${symbol.label}: ${destination.label} · ${destination.verified ? "confirmed" : "approximate"}`;
      const description = `${label}. Select to edit, or drag to preview a move.`;
      const marker = L.marker([destination.latitude, destination.longitude], {
        icon: icon("destination-pin", symbol.html),
        draggable: true,
        title: description,
      }).addTo(destinations);
      marker.getElement()?.setAttribute("aria-label", description);
      const tip = document.createElement("span");
      tip.textContent = label;
      marker.bindTooltip(tip);
      let reviewingMove = false;
      marker.on("dragstart", () => {
        reviewingMove = true;
      });
      marker.on("click", () => {
        if (!dropping && !reviewingMove) onEdit(destination);
      });
      marker.on("dragend", async () => {
        const p = marker.getLatLng();
        marker.dragging.disable();
        try {
          const applied = await onMove(destination.id, p.lat, p.lng);
          if (!applied)
            marker.setLatLng([destination.latitude, destination.longitude]);
        } catch (error) {
          marker.setLatLng([destination.latitude, destination.longitude]);
          onError(error.message);
        } finally {
          marker.dragging.enable();
          reviewingMove = false;
        }
      });
      if (destination.kind === "office")
        bounds.push([destination.latitude, destination.longitude]);
    }
    document.getElementById("map-legend").replaceChildren(
      legendItem("room-pin", "1", "Rooms at this location"),
      ...Object.entries(destinationSymbols)
        .filter(([kind]) => kind === "office" || visibleKinds.has(kind))
        .map(([, symbol]) =>
          legendItem("destination-pin", symbol.html, symbol.label),
        ),
      legendItem("room-pin frontier-pin", "1", "Contains a frontier room"),
    );
    if (!fitted && bounds.length) {
      map.fitBounds(bounds, { padding: [35, 35], maxZoom: 12 });
      fitted = true;
    }
  }
  return {
    update,
    cancel,
    resize: () => map.invalidateSize(),
    drop() {
      dropping = true;
      map.getContainer().style.cursor = "crosshair";
    },
    focus(id) {
      const marker = roomMarkers.get(id);
      if (marker) {
        map.panTo(marker.getLatLng());
        marker.openPopup();
      }
    },
  };
}
