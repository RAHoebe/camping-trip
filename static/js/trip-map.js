(function () {
  const data = window.CAMPING_TRIP || { stops: [], pois: [] };
  const mapEl = document.getElementById("tripMap");
  const statusEl = document.getElementById("routeStatus");
  const toolbarEl = document.getElementById("mapToolbar");
  if (!mapEl || typeof L === "undefined") return;

  const map = L.map(mapEl, { zoomControl: true }).setView([52.1326, 5.2913], 7);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors"
  }).addTo(map);

  const bounds = [];
  let routeLine = null;
  let campgroundLayer = null;
  let campgroundVisible = true;
  let campgroundRadiusKm = 20;
  let lastSearchMode = "bounds";

  function updateMarkerScale() {
    const zoom = map.getZoom();
    let scale = 1;
    if (zoom <= 10) scale = 0.5;
    mapEl.style.setProperty("--map-icon-scale", scale);
  }

  map.on("zoomend", updateMarkerScale);
  updateMarkerScale();

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, function (char) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#039;" }[char];
    });
  }

  function parseCampgroundDescription(description) {
    const tags = {};
    String(description || "")
      .split(";")
      .map(function (part) { return part.trim(); })
      .filter(Boolean)
      .forEach(function (part) {
        const index = part.indexOf("=");
        if (index <= 0) return;
        const key = part.slice(0, index).trim();
        const value = part.slice(index + 1).trim();
        if (key && value) tags[key] = value;
      });
    return tags;
  }

  function campgroundText(record) {
    return [
      record && record.name,
      record && record.description,
      record && record.notes,
      record && record.address,
      record && record.raw_data
    ].filter(Boolean).join(" ").toLowerCase();
  }

  function campgroundKind(record) {
    const text = campgroundText(record);
    const farmWords = ["boerderij", "boerencamping", "farm", "farmcamping", "farm camping", "agricamping", "agriturismo"];
    const swimWords = ["swimming_pool=yes", "swimming pool", "swimmingpool", "zwembad", "swimming=yes", "swimming lake", "zwemmeer", "recreatieplas", "lake", "lagune", "strandbad"];
    if (swimWords.some(function (word) { return text.indexOf(word) !== -1; })) return "swim";
    if (farmWords.some(function (word) { return text.indexOf(word) !== -1; })) return "farm";
    return "standard";
  }

  function campgroundKindIcon(kind) {
    if (kind === "farm") return "house-heart";
    if (kind === "swim") return "water";
    return "tree";
  }

  function joinPresent(parts, separator) {
    return parts.filter(Boolean).join(separator || ", ");
  }

  function campgroundPopupHtml(campground, addUrl) {
    const tags = parseCampgroundDescription(campground.description);
    const title = tags.name || campground.name || "Campground";
    const street = joinPresent([tags["addr:street"], tags["addr:housenumber"]], " ");
    const place = joinPresent([tags["addr:postcode"], tags["addr:city"]]);
    const address = joinPresent([street, place], "<br>");
    const email = tags.email || campground.email;
    const phone = tags.phone || campground.phone;
    const website = tags.website || campground.website;
    const operator = tags.operator;
    const details = [];

    if (address) details.push(`<div><i class="bi bi-geo-alt"></i><span>${address}</span></div>`);
    if (operator) details.push(`<div><i class="bi bi-person-badge"></i><span>${escapeHtml(operator)}</span></div>`);
    if (phone) details.push(`<div><i class="bi bi-telephone"></i><span>${escapeHtml(phone)}</span></div>`);
    if (email) details.push(`<div><i class="bi bi-envelope"></i><span>${escapeHtml(email)}</span></div>`);
    if (website) {
      const safeWebsite = escapeHtml(website);
      details.push(`<div><i class="bi bi-globe"></i><a href="${safeWebsite}" target="_blank" rel="noopener">Website</a></div>`);
    }
    if (campground.distance_km) {
      details.unshift(`<div><i class="bi bi-crosshair"></i><span>${campground.distance_km} km from map center</span></div>`);
    }

    return `
      <div class="campground-popup-card">
        <div class="campground-popup-title">${escapeHtml(title)}</div>
        ${details.length ? `<div class="campground-popup-details">${details.join("")}</div>` : '<div class="text-muted small">No extra details available.</div>'}
        <a class="btn btn-sm btn-success" href="${addUrl}">Add campsite to trip</a>
      </div>
    `;
  }

  function formatDistance(meters) {
    if (!meters) return "";
    return `${Math.round(meters / 1000)} km`;
  }

  function formatTravelTime(seconds) {
    if (!seconds) return "";
    const totalMinutes = Math.round(Number(seconds) / 60);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    if (hours && minutes) return `${hours}h ${minutes}m`;
    if (hours) return `${hours}h`;
    return `${minutes}m`;
  }

  function formatGraphhopperCredits(credits, provider) {
    if ((!credits || credits.remaining === undefined || credits.remaining === null || credits.remaining === "") && provider === "graphhopper") {
      return "GraphHopper credits: refresh route to update";
    }
    if (!credits || credits.remaining === undefined || credits.remaining === null || credits.remaining === "") return "";
    const remainingNumber = Number(credits.remaining);
    const remaining = Number.isFinite(remainingNumber) ? remainingNumber.toLocaleString() : String(credits.remaining);
    const limitNumber = Number(credits.limit);
    const limit = Number.isFinite(limitNumber) ? ` / ${limitNumber.toLocaleString()}` : "";
    return `GraphHopper credits left: ${remaining}${limit}`;
  }

  function dateText(start, end) {
    if (start && end) return `${escapeHtml(start)} to ${escapeHtml(end)}`;
    return escapeHtml(start || end || "");
  }

  function stopNights(stop) {
    if (!stop.arrival_date || !stop.departure_date) return "";
    const arrival = new Date(stop.arrival_date + "T00:00:00");
    const departure = new Date(stop.departure_date + "T00:00:00");
    const nights = Math.round((departure - arrival) / 86400000);
    if (!Number.isFinite(nights) || nights < 1) return "";
    return `${nights} night${nights === 1 ? "" : "s"}`;
  }

  function stopPopupHtml(stop, index) {
    const metric = (data.stopMetrics || {})[String(stop.stop_id)] || (data.stopMetrics || {})[stop.stop_id];
    const rows = [];
    const nights = stopNights(stop);
    const stayText = dateText(stop.arrival_date, stop.departure_date) + (nights ? ` - ${nights}` : "");
    rows.push(`<div><i class="bi bi-calendar3"></i><span>${stayText}</span></div>`);
    if (metric && metric.distance_m) {
      const duration = metric.duration_s ? ` / ${formatTravelTime(metric.duration_s)}` : "";
      rows.push(`<div><i class="bi bi-arrow-right"></i><span>${formatDistance(metric.distance_m)}${duration} from previous</span></div>`);
    }
    if (metric && metric.cumulative_distance_m) {
      const duration = metric.cumulative_duration_s ? ` / ${formatTravelTime(metric.cumulative_duration_s)}` : "";
      rows.push(`<div><i class="bi bi-plus-circle"></i><span>${formatDistance(metric.cumulative_distance_m)}${duration} total</span></div>`);
    }
    if (stop.address) rows.push(`<div><i class="bi bi-geo-alt"></i><span>${escapeHtml(stop.address)}</span></div>`);
    const edit = data.admin && data.admin.editStopBaseUrl
      ? `<a class="btn btn-sm btn-outline-success" href="${data.admin.editStopBaseUrl}/${stop.stop_id}/edit">Edit campsite</a>`
      : "";
    return `
      <div class="campground-popup-card">
        <div class="campground-popup-title">${index}. ${escapeHtml(stop.name)}</div>
        <div class="campground-popup-details">${rows.join("")}</div>
        ${edit}
      </div>
    `;
  }

  function stopIcon(index, stop) {
    const kind = campgroundKind(stop || {});
    const badge = kind === "standard" ? "" : `<i class="bi bi-${campgroundKindIcon(kind)} marker-badge"></i>`;
    return L.divIcon({
      className: `stop-marker stop-${kind}`,
      html: `<span><strong>${index}</strong>${badge}</span>`,
      iconSize: [32, 32],
      iconAnchor: [16, 16]
    });
  }

  function homeIcon() {
    return L.divIcon({
      className: "home-marker",
      html: '<span><i class="bi bi-house-fill"></i></span><em>Home</em>',
      iconSize: [58, 44],
      iconAnchor: [29, 34]
    });
  }

  function poiIcon(category) {
    const icons = {
      viewpoint: "binoculars",
      restaurant: "cup-hot",
      hike: "person-walking",
      fuel: "fuel-pump",
      supermarket: "basket",
      parking: "p-square",
      custom: "pin-map"
    };
    return L.divIcon({
      className: `poi-marker poi-${category || "custom"}`,
      html: `<i class="bi bi-${icons[category] || icons.custom}"></i>`,
      iconSize: [28, 28],
      iconAnchor: [14, 14]
    });
  }

  function campgroundIcon(campground) {
    const kind = campgroundKind(campground || {});
    return L.divIcon({
      className: `campground-marker campground-${kind}`,
      html: `<i class="bi bi-${campgroundKindIcon(kind)}"></i>`,
      iconSize: [26, 26],
      iconAnchor: [13, 13]
    });
  }

  function withMapPoint(url, latlng, name) {
    const separator = url.indexOf("?") === -1 ? "?" : "&";
    const params = new URLSearchParams({
      lat: latlng.lat.toFixed(6),
      lon: latlng.lng.toFixed(6)
    });
    if (name) params.set("name", name);
    return `${url}${separator}${params.toString()}`;
  }

  let homeMarker = null;
  let homeSaving = false;

  function saveHomeLocation(latlng, marker) {
    if (!data.admin || !data.admin.homeLocationUrl || homeSaving) return;
    homeSaving = true;
    fetch(data.admin.homeLocationUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRFToken": data.admin.csrfToken || ""
      },
      body: JSON.stringify({
        name: (data.home && data.home.name) || "Home",
        latitude: latlng.lat,
        longitude: latlng.lng
      })
    })
      .then(function (response) {
        if (!response.ok) throw new Error("Could not save home location");
        return response.json();
      })
      .then(function (payload) {
        data.home = payload.home;
        if (marker && marker.bindPopup) {
          marker.bindPopup(`<strong>${escapeHtml(data.home.name || "Home")}</strong><br>Route starts here<br><span class="text-muted">Drag to move home</span>`);
        }
        setStatus("Home location saved. Refresh the route to recalculate from home.", "ok");
      })
      .catch(function (error) {
        setStatus(error.message || "Could not save home location.", "warning");
        if (marker && data.home) {
          marker.setLatLng([data.home.latitude, data.home.longitude]);
        }
      })
      .finally(function () {
        homeSaving = false;
      });
  }

  function addHomeMarker(home) {
    const homeLatLng = [Number(home.latitude), Number(home.longitude)];
    if (!Number.isFinite(homeLatLng[0]) || !Number.isFinite(homeLatLng[1])) return null;
    bounds.push(homeLatLng);
    const marker = L.marker(homeLatLng, {
      icon: homeIcon(),
      draggable: Boolean(data.admin),
      zIndexOffset: 10000
    })
      .bindPopup(`<strong>${escapeHtml(home.name || "Home")}</strong><br>Route starts here${data.admin ? '<br><span class="text-muted">Drag to move home</span>' : ""}`)
      .addTo(map);
    if (data.admin) {
      marker.on("dragend", function () {
        saveHomeLocation(marker.getLatLng(), marker);
      });
    }
    return marker;
  }

  if (data.home) {
    homeMarker = addHomeMarker(data.home);
  }

  data.stops.forEach(function (stop, index) {
    const latlng = [Number(stop.latitude), Number(stop.longitude)];
    if (!Number.isFinite(latlng[0]) || !Number.isFinite(latlng[1])) return;
    bounds.push(latlng);
    L.marker(latlng, { icon: stopIcon(index + 1, stop) })
      .bindPopup(stopPopupHtml(stop, index + 1), { maxWidth: 320 })
      .addTo(map);
  });

  data.pois.forEach(function (poi) {
    const latlng = [Number(poi.latitude), Number(poi.longitude)];
    if (!Number.isFinite(latlng[0]) || !Number.isFinite(latlng[1])) return;
    bounds.push(latlng);
    L.marker(latlng, { icon: poiIcon(poi.category) })
      .bindPopup(`<strong>${escapeHtml(poi.name)}</strong><br>${escapeHtml(poi.category || "POI")}`)
      .addTo(map);
  });

  if (bounds.length) {
    map.fitBounds(bounds, { padding: [32, 32], maxZoom: 12 });
  }

  if (data.admin) {
    campgroundLayer = L.layerGroup().addTo(map);
    let campgroundTimer = null;

    map.on("click", function (event) {
      const stopUrl = withMapPoint(data.admin.addStopUrl, event.latlng, "New campsite");
      const poiUrl = withMapPoint(data.admin.addPoiUrl, event.latlng);
      const setHomeButton = data.admin.homeLocationUrl ? '<button class="btn btn-sm btn-outline-secondary" type="button" data-map-action="set-home">Set home here</button>' : "";
      L.popup()
        .setLatLng(event.latlng)
        .setContent(`
          <div class="map-popup-actions">
            <strong>${event.latlng.lat.toFixed(5)}, ${event.latlng.lng.toFixed(5)}</strong>
            ${setHomeButton}
            <a class="btn btn-sm btn-success" href="${stopUrl}">Add campsite here</a>
            <a class="btn btn-sm btn-outline-success" href="${poiUrl}">Add POI here</a>
          </div>
        `)
        .openOn(map);
    });

    map.on("popupopen", function (event) {
      const button = event.popup.getElement().querySelector('[data-map-action="set-home"]');
      if (!button) return;
      button.addEventListener("click", function () {
        const latlng = event.popup.getLatLng();
        if (!homeMarker) {
          homeMarker = L.marker(latlng, { icon: homeIcon(), draggable: true, zIndexOffset: 10000 }).addTo(map);
          homeMarker.on("dragend", function () {
            saveHomeLocation(homeMarker.getLatLng(), homeMarker);
          });
        } else {
          homeMarker.setLatLng(latlng);
        }
        saveHomeLocation(latlng, homeMarker);
        map.closePopup();
      });
    });

    function setCampgroundMarkers(payload) {
      campgroundLayer.clearLayers();
      (payload.campgrounds || []).forEach(function (campground) {
        const latlng = [Number(campground.latitude), Number(campground.longitude)];
        if (!Number.isFinite(latlng[0]) || !Number.isFinite(latlng[1])) return;
        const addUrl = `${data.admin.addCampgroundBaseUrl}/${campground.campground_id}/add`;
        L.marker(latlng, { icon: campgroundIcon(campground) })
          .bindPopup(campgroundPopupHtml(campground, addUrl), { maxWidth: 320 })
          .addTo(campgroundLayer);
      });
    }

    function loadVisibleCampgrounds() {
      if (!campgroundVisible || !data.admin.campgroundsSearchUrl || map.getZoom() < 8) {
        campgroundLayer.clearLayers();
        return;
      }
      lastSearchMode = "bounds";
      const b = map.getBounds();
      const params = new URLSearchParams({
        north: b.getNorth().toFixed(6),
        south: b.getSouth().toFixed(6),
        east: b.getEast().toFixed(6),
        west: b.getWest().toFixed(6)
      });
      fetch(`${data.admin.campgroundsSearchUrl}?${params.toString()}`)
        .then(function (response) {
          if (!response.ok) throw new Error("Campground search failed");
          return response.json();
        })
        .then(setCampgroundMarkers)
        .catch(function () {
          campgroundLayer.clearLayers();
        });
    }

    function searchCampgroundsAroundCenter() {
      if (!campgroundVisible || !data.admin.campgroundsSearchUrl) return;
      lastSearchMode = "radius";
      const center = map.getCenter();
      const params = new URLSearchParams({
        lat: center.lat.toFixed(6),
        lon: center.lng.toFixed(6),
        radius_km: String(campgroundRadiusKm)
      });
      fetch(`${data.admin.campgroundsSearchUrl}?${params.toString()}`)
        .then(function (response) {
          if (!response.ok) throw new Error("Campground search failed");
          return response.json();
        })
        .then(function (payload) {
          setCampgroundMarkers(payload);
          L.circle(center, {
            radius: campgroundRadiusKm * 1000,
            color: "#166534",
            weight: 1,
            fillOpacity: 0.03
          }).addTo(campgroundLayer);
        })
        .catch(function () {
          campgroundLayer.clearLayers();
        });
    }

    function queueCampgroundLoad() {
      if (lastSearchMode === "radius") return;
      window.clearTimeout(campgroundTimer);
      campgroundTimer = window.setTimeout(loadVisibleCampgrounds, 250);
    }

    map.on("moveend zoomend", queueCampgroundLoad);
    queueCampgroundLoad();

    if (toolbarEl) {
      toolbarEl.innerHTML = `
        <button class="btn btn-sm btn-light" type="button" data-map-tool="refresh"><i class="bi bi-arrow-clockwise"></i> Refresh route</button>
        <button class="btn btn-sm btn-light" type="button" data-map-tool="fit"><i class="bi bi-bounding-box"></i> Fit trip</button>
        <button class="btn btn-sm btn-light" type="button" data-map-tool="toggle-campgrounds"><i class="bi bi-tree"></i> Hide campgrounds</button>
        <select class="form-select form-select-sm" data-map-tool="radius" aria-label="Campground search radius">
          <option value="10">10 km</option>
          <option value="20" selected>20 km</option>
          <option value="50">50 km</option>
        </select>
        <button class="btn btn-sm btn-light" type="button" data-map-tool="search"><i class="bi bi-search"></i> Search center</button>
      `;
      toolbarEl.addEventListener("click", function (event) {
        const tool = event.target.closest("[data-map-tool]");
        if (!tool) return;
        const action = tool.getAttribute("data-map-tool");
        if (action === "refresh") loadRoute(true);
        if (action === "fit") fitTrip();
        if (action === "search") {
          campgroundVisible = true;
          if (!map.hasLayer(campgroundLayer)) campgroundLayer.addTo(map);
          searchCampgroundsAroundCenter();
        }
        if (action === "toggle-campgrounds") {
          campgroundVisible = !campgroundVisible;
          if (campgroundVisible) {
            campgroundLayer.addTo(map);
            tool.innerHTML = '<i class="bi bi-tree"></i> Hide campgrounds';
            lastSearchMode = "bounds";
            loadVisibleCampgrounds();
          } else {
            campgroundLayer.clearLayers();
            map.removeLayer(campgroundLayer);
            tool.innerHTML = '<i class="bi bi-tree"></i> Show campgrounds';
          }
        }
      });
      const radiusSelect = toolbarEl.querySelector('[data-map-tool="radius"]');
      radiusSelect && radiusSelect.addEventListener("change", function () {
        campgroundRadiusKm = Number(radiusSelect.value) || 20;
      });
    }
  }

  function setStatus(text, kind) {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.dataset.kind = kind || "info";
  }

  function fitTrip() {
    if (bounds.length) map.fitBounds(bounds, { padding: [32, 32], maxZoom: 12 });
  }

  function loadRoute(refresh) {
    const params = new URLSearchParams();
    if (refresh) params.set("refresh", "1");
    const url = `/api/trips/${data.tripId}/route${params.toString() ? "?" + params.toString() : ""}`;
    setStatus(refresh ? "Refreshing route... large trips may take a minute." : "Loading route...", "info");
    fetch(url)
    .then(function (response) {
      if (!response.ok) throw new Error("Route request failed");
      return response.json();
    })
    .then(function (routeData) {
      if (routeLine) {
        map.removeLayer(routeLine);
        routeLine = null;
      }
      if (routeData.route && routeData.route.coordinates) {
        const latlngs = routeData.route.coordinates.map(function (coord) {
          return [coord[1], coord[0]];
        });
        routeLine = L.polyline(latlngs, {
          color: "#2563eb",
          weight: 5,
          opacity: 0.9,
          dashArray: "2 10",
          lineCap: "round"
        }).addTo(map);
        let routeMessage = `Route by ${routeData.provider}${routeData.distance_m ? " - " + formatDistance(routeData.distance_m) : ""}`;
        const credits = formatGraphhopperCredits(routeData.graphhopper_credits, routeData.provider);
        if (credits) routeMessage += ` - ${credits}`;
        setStatus(routeMessage, "ok");
      } else if (routeData.status === "not_enough_stops") {
        const credits = formatGraphhopperCredits(routeData.graphhopper_credits, routeData.provider);
        setStatus(`${routeData.message || "Add at least two campsites to calculate a route."}${credits ? " - " + credits : ""}`, "muted");
      } else if (routeData.status === "manual_refresh_required") {
        const credits = formatGraphhopperCredits(routeData.graphhopper_credits, routeData.provider);
        setStatus(`${routeData.message || "Use Refresh route when you are ready."}${credits ? " - " + credits : ""}`, "muted");
      } else {
        const credits = formatGraphhopperCredits(routeData.graphhopper_credits, routeData.provider);
        setStatus(`${routeData.message ? `Route unavailable: ${routeData.message}` : "Route unavailable."}${credits ? " - " + credits : ""}`, "warning");
      }
    })
    .catch(function (error) {
      setStatus(error.message || "Route unavailable.", "warning");
    });
  }

  loadRoute(false);
})();
