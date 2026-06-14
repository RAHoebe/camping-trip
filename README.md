# Camping Trip Planner

![Version](https://img.shields.io/badge/Version-v0.1.0-informational.svg)

A small Flask app for planning camping trips with dated campsites, manual POIs, automatically synced GpxFeed campground data, and a Leaflet route map.

## Local Run

```bash
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:8034`.

On first run, an admin account is created:

- Username: `admin`
- Password: `change-me-please`

Override this with `DEFAULT_ADMIN_USERNAME`, `DEFAULT_ADMIN_PASSWORD`, and `DEFAULT_ADMIN_EMAIL`.

## Routing

Default routing is GraphHopper-compatible.

```text
ROUTE_PROVIDER=graphhopper
```

Create `graphhopper.key` in the project folder and paste only the API key in that file. It is ignored by git and Docker builds.

You can also set the key directly in PowerShell before starting:

```powershell
$env:ROUTE_PROVIDER="graphhopper"
$env:GRAPHHOPPER_API_KEY="your-key-here"
python app.py
```

`run_local.cmd`, `run_waitress.cmd`, and `run_docker.cmd` read `graphhopper.key` automatically. For Docker, `run_docker.cmd` passes the key to the container as the `GRAPHHOPPER_API_KEY` environment variable.

For OSRM:

```text
ROUTE_PROVIDER=osrm
OSRM_BASE_URL=https://router.project-osrm.org
```

Routes are calculated between campsites in arrival-date order. Manual POIs are displayed on the map but do not affect the route.

The app caches each route leg separately. When you refresh a larger trip, unchanged legs are reused and new GraphHopper calls are spaced out to avoid the minutely API limit. You can tune this if needed:

```text
GRAPHHOPPER_LEG_DELAY_SECONDS=8
GRAPHHOPPER_429_RETRY_SECONDS=65
GRAPHHOPPER_429_RETRIES=1
ROUTE_LEG_CACHE_HOURS=24
```

## Campground Data

The app automatically checks `GpxFeed/campgrounds` and imports only campsite/caravan-site GPX files from `gpx-stripped`. It stores the GitHub commit SHA and refreshes when a newer commit is available.

Control this with:

```text
GPXFEED_AUTO_UPDATE=true
GPXFEED_UPDATE_INTERVAL_HOURS=24
```

Admins can also force an update from `Admin -> Campground Data`.

## Release Version

The current app version is stored in `version.txt` and shown in the footer. Admins can enable a Docker Hub update check in `Admin -> Options`.

To create a GitHub release for the current version:

```cmd
release_github.cmd
```

Or pass a tag explicitly:

```cmd
release_github.cmd v0.1.1
```

See `DeploySynologyNAS.md` for NAS deployment.
