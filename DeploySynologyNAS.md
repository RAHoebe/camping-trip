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

## Portainer Install

Portainer is optional, but it is the easiest way to install and update the app on a Synology NAS.

### Install Portainer

If Portainer is not installed yet:

1. Open **Container Manager** in DSM.
2. Go to **Registry** and download `portainer/portainer-ce:latest`.
3. Go to **Image**, select the Portainer image, and click **Launch**.
4. Use these settings:
   - Container name: `portainer`
   - Local port `9000` -> container port `9000`
   - Local port `8000` -> container port `8000`
   - Mount `/var/run/docker.sock` -> `/var/run/docker.sock`
   - Mount `/volume1/docker/portainer` -> `/data`
5. Open `http://your-nas-ip:9000`, create the first admin account, and select the local Docker environment.

### Deploy With A Portainer Stack

1. Open Portainer at `http://your-nas-ip:9000`.
2. Select your Docker environment.
3. Go to **Stacks**.
4. Click **Add stack**.
5. Name the stack `camping_trip`.
6. Paste this compose file into the web editor:

```yaml
version: "3.8"

services:
  camping_trip:
    image: your-dockerhub-username/camping_trip:latest
    container_name: camping_trip
    restart: unless-stopped
    ports:
      - "8034:8034"
    volumes:
      - /volume1/docker/camping_trip/data:/app/data
    environment:
      - SECRET_KEY=replace-with-a-random-secret
      - DEBUG=false
      - TZ=Europe/Amsterdam
      - GUNICORN_LOG_LEVEL=warning
      - DEFAULT_ADMIN_USERNAME=admin
      - DEFAULT_ADMIN_PASSWORD=change-this-before-public-use
      - DEFAULT_ADMIN_EMAIL=admin@example.local
      - ROUTE_PROVIDER=graphhopper
      - GRAPHHOPPER_API_KEY=your-graphhopper-key
      - GRAPHHOPPER_LEG_DELAY_SECONDS=8
      - GRAPHHOPPER_429_RETRY_SECONDS=65
      - GRAPHHOPPER_429_RETRIES=1
      - ROUTE_LEG_CACHE_HOURS=24
      - GPXFEED_AUTO_UPDATE=true
      - GPXFEED_UPDATE_INTERVAL_HOURS=24
```

Replace `your-dockerhub-username/camping_trip:latest`, `SECRET_KEY`, `DEFAULT_ADMIN_PASSWORD`, and `GRAPHHOPPER_API_KEY` before deploying. You can generate a secret key with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

7. Click **Deploy the stack**.
8. Open `http://your-nas-ip:8034`.

### Portainer With OSRM Instead Of GraphHopper

If you do not want to use a GraphHopper key, replace the route environment variables with:

```yaml
      - ROUTE_PROVIDER=osrm
      - OSRM_BASE_URL=https://router.project-osrm.org
```

GraphHopper is recommended for normal use because the public OSRM demo server can be slow or rate-limited.

### Update Through Portainer

When you publish a new Docker image:

1. Go to **Stacks**.
2. Open the `camping_trip` stack.
3. Click **Editor**.
4. Keep the same compose file, or change the image tag if you use versioned tags.
5. Enable **Re-pull image and redeploy** / **Pull latest image** if Portainer shows that option.
6. Click **Update the stack**.

The `/volume1/docker/camping_trip/data` volume is reused, so the SQLite database, settings, GpxFeed data, and route cache stay in place.

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
