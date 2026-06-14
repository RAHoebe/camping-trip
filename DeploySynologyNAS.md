# Deploying Camping Trip Planner to Synology NAS

This app follows the same Docker-oriented deployment style as `fam_video`.

## Build and Push

```bash
cd u:\Ron\Documents\Github\camping-trip
builddocker.cmd
docker tag camping_trip:latest your-dockerhub-username/camping_trip:latest
docker push your-dockerhub-username/camping_trip:latest
```

## Synology Folders

Create:

```text
/volume1/docker/camping_trip/
`-- data/
    `-- uploads/
```

Mount `/volume1/docker/camping_trip/data` to `/app/data`.

For Docker on Synology, add the GraphHopper key as an environment variable.

## Container Settings

- Container name: `camping_trip`
- Container port: `8034`
- Local port: `8034`
- Restart policy: always/unless-stopped

Environment variables:

```text
SECRET_KEY=replace-with-a-random-secret
DEBUG=false
TZ=Europe/Amsterdam
GUNICORN_LOG_LEVEL=warning
DEFAULT_ADMIN_USERNAME=admin
DEFAULT_ADMIN_PASSWORD=change-this-before-public-use
ROUTE_PROVIDER=graphhopper
GRAPHHOPPER_API_KEY=your-graphhopper-key
GRAPHHOPPER_LEG_DELAY_SECONDS=8
GRAPHHOPPER_429_RETRY_SECONDS=65
GRAPHHOPPER_429_RETRIES=1
ROUTE_LEG_CACHE_HOURS=24
GPXFEED_AUTO_UPDATE=true
GPXFEED_UPDATE_INTERVAL_HOURS=24
```

For a no-key OSRM-compatible route provider, set:

```text
ROUTE_PROVIDER=osrm
OSRM_BASE_URL=https://router.project-osrm.org
```

## First Login

If the database is empty, the app creates one admin account from `DEFAULT_ADMIN_USERNAME`,
`DEFAULT_ADMIN_PASSWORD`, and `DEFAULT_ADMIN_EMAIL`. Change that password after the first login.

## Reverse Proxy

Use DSM reverse proxy the same way as `fam_video`:

- Source: your chosen HTTPS hostname.
- Destination: `http://127.0.0.1:8034`.
- Enable WebSocket support if available.
- Set `HTTPS_ENABLED=true` when the app is only accessed through HTTPS.
