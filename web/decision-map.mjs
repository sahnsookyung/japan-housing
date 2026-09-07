/** Normal interactive tile viewing only; no prefetching or geocoding requests. */
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
      keyboard: false,
    }).addTo(map);
    onDrop(e.latlng.lat, e.latlng.lng);
  });
  function update(data) {
    homes.clearLayers();
    destinations.clearLayers();
    roomMarkers.clear();
    for (const group of data.map_points) {
      const marker = L.marker([group.latitude, group.longitude], {
        icon: icon(
          group.rooms.some((r) => r.frontier)
            ? "room-pin frontier-pin"
            : "room-pin",
          group.count > 1 ? group.count : "•",
        ),
        title: `${group.count} room(s)`,
        keyboard: true,
      }).addTo(homes);
      const hover = document.createElement("div");
      hover.textContent =
        group.rooms
          .slice(0, 3)
          .map((room) => room.title)
          .join(" · ") +
        (group.count > 3 ? ` · ${group.count - 3} more rooms` : "");
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
    for (const destination of data.preferences.destinations) {
      if (
        !destination.enabled ||
        destination.latitude == null ||
        destination.kind === "remote"
      )
        continue;
      const marker = L.marker([destination.latitude, destination.longitude], {
        icon: icon(
          "destination-pin",
          destination.kind === "relocation" ? "R" : "W",
        ),
        draggable: true,
        title: `${destination.label}. Drag to preview a move, then confirm or cancel.`,
      }).addTo(destinations);
      const tip = document.createElement("span");
      tip.textContent = `${destination.label} · ${destination.verified ? "confirmed" : "approximate"}`;
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
