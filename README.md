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
GRAPHHOPPER_API_KEY_FILE=graphhopper.key
```

Create `graphhopper.key` in the project folder and paste only the API key in that file. It is ignored by git and Docker builds.

You can also set the key directly in PowerShell before starting:

```powershell
$env:ROUTE_PROVIDER="graphhopper"
$env:GRAPHHOPPER_API_KEY="your-key-here"
python app.py
```

`run_local.cmd`, `run_waitress.cmd`, and `run_docker.cmd` read `graphhopper.key` automatically. Docker mounts it into the container as a read-only secret file instead of baking it into the image.

For OSRM:

```text
ROUTE_PROVIDER=osrm
OSRM_BASE_URL=https://router.project-osrm.org
```

Routes are calculated between campsites in arrival-date order. Manual POIs are displayed on the map but do not affect the route.

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
