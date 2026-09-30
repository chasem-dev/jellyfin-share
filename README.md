# Jellyfin Share Portal

An expiring-link viewer for movies and TV from local media folders. The manager creates and revokes links for a movie, whole series, season, or episode. Viewers do not need a Jellyfin login. The portal reads media files; it does not modify Jellyfin, Sonarr, or Radarr.

## Ports and access

One container runs two separate HTTP listeners:

| Container port | Purpose | Recommended host access |
| --- | --- | --- |
| 8787 | Public viewer and share links | HTTPS reverse proxy, or direct LAN access for testing |
| 8788 | Private manager and catalog API | `127.0.0.1` or your server's LAN IP only |

The manager has no login. Keep port 8788 off the internet and do not proxy it through the public domain. Anyone with an active link can watch its selected media until it expires or is revoked.

## Basic Compose example

Save this as `compose.yml`. Replace the example server IP, the two media paths, and the `user` ID if your media/storage owner is different. Create writable `./data` and `./cache` folders before starting it.

```yaml
services:
  share-portal:
    image: ghcr.io/chasem-dev/jellyfin-share:0.1.0
    restart: unless-stopped
    user: "1000:1000"
    environment:
      PUBLIC_BASE_URL: "http://192.168.1.10:8787"
      PUBLIC_BIND: "0.0.0.0"
      ADMIN_BIND: "0.0.0.0"
      ADMIN_ALLOWED_HOSTS: "127.0.0.1,localhost"
      MEDIA_ROOTS: "/media/Movies:/media/TV"
      PORTAL_DATA: "/data"
      PORTAL_CACHE: "/cache"
    ports:
      - "8787:8787"             # Viewer, reachable on the network
      - "127.0.0.1:8788:8788"   # Manager, this computer only
    volumes:
      - "/path/to/Movies:/media/Movies:ro"
      - "/path/to/TV:/media/TV:ro"
      - "./data:/data"
      - "./cache:/cache"
```

Run `docker compose up -d`. Open the manager at `http://127.0.0.1:8788`; share links use `PUBLIC_BASE_URL`. To reach the manager from another device on your LAN, replace `127.0.0.1` in its port mapping with your server's LAN IP and add that IP to `ADMIN_ALLOWED_HOSTS`. Keep port 8788 private. For internet viewers, put port 8787 behind HTTPS and set `PUBLIC_BASE_URL` to the HTTPS address.

## Run the portable image

1. Copy `.env.example` to `.env`. It points to the published `ghcr.io/chasem-dev/jellyfin-share:0.1.0` image. To build from source instead, run `docker build -t jellyfin-share:0.1.0 .` and set `PORTAL_IMAGE=jellyfin-share:0.1.0`.
2. Edit `MOVIES_DIR` and `TV_DIR` to point to existing folders on the Docker host. Each folder should contain one subfolder per movie or series. Create the `DATA_DIR` and `CACHE_DIR` folders and make them writable by `PUID:PGID`. The default relative locations are `./instance/data` and `./instance/cache`.
3. Set `PUBLIC_URL` to the full address viewers will open. For a first test on the Docker host, `http://127.0.0.1:8787` works. For links sent to others, use a reachable address, preferably an HTTPS domain.
4. Start the container:

   ```sh
   docker compose --env-file .env -f compose.distribution.yml up -d
   ```

The manager is at `http://127.0.0.1:8788` with the sample settings. If you want to open it from other devices on your home network, set `MANAGER_HOST_BIND` to the server's LAN IP and add that IP to `MANAGER_ALLOWED_HOSTS`. For example, `MANAGER_ALLOWED_HOSTS=127.0.0.1,localhost,192.168.1.10`. You can change `MANAGER_HOST_PORT`; the container will accept the configured port in the browser's Host header. Keep the LAN firewall limited to trusted devices.

`PUBLIC_HOST_BIND` controls the viewer's direct host binding. It defaults to loopback for use behind a reverse proxy. Set it to `0.0.0.0` to expose port 8787 directly; then set `PUBLIC_URL` to that reachable IP and port. The generated link always uses `PUBLIC_URL`.

### Optional HTTPS with Caddy

On a host where ports 80 and 443 are free, point your domain's DNS record to the server, set `PUBLIC_URL=https://share.example.com` and `SHARE_DOMAIN=share.example.com` in `.env`, then run:

```sh
docker compose --env-file .env -f compose.distribution.yml -f compose.proxy.yml up -d
```

The included Caddy service proxies **only port 8787**. Port 8788 retains its private binding. If you already run a reverse proxy, use `compose.distribution.yml` alone and route your public hostname to the viewer on port 8787. Caddy running in another container needs a network route to this container or to the Docker host's published viewer port. HTTPS also requires your domain to resolve to the host and inbound 80/443 access.

## Media and posters

The portable Compose file mounts the movie and TV folders read-only as `/media/Movies` and `/media/TV`. The app catalogs folders under those roots. To add more roots, add read-only mounts and append their container paths to `MEDIA_ROOTS` in the Compose file. Roots named `Movies` are treated as movie libraries; other roots are treated as series libraries. The manager's **Refresh library** button rescans new files. Existing links keep their selected file lists from creation time.

Link previews contain Open Graph and Twitter card metadata plus a 1200×630 JPEG. A `poster.jpg`, `poster.png`, `poster.webp`, `folder.jpg`, or `cover.jpg` in a title folder is used automatically. Without artwork, the portal generates a branded title card.

For Jellyfin-managed artwork, `sync_posters.py` can create a private poster index from Jellyfin's database. Set `JELLYFIN_DB`, `JELLYFIN_METADATA_ROOT`, and `POSTER_INDEX_OUTPUT` to host paths. Set `MEDIA_PATH_MAP` to a JSON object mapping Jellyfin's media paths to the container's `/media/Movies` and `/media/TV` paths. For example:

```sh
JELLYFIN_DB=/path/to/jellyfin/data/jellyfin.db \
JELLYFIN_METADATA_ROOT=/path/to/jellyfin/metadata/library \
POSTER_INDEX_OUTPUT=./instance/data/poster-index.json \
MEDIA_PATH_MAP='{"/srv/media/Movies":"/media/Movies","/srv/media/TV":"/media/TV"}' \
python3 sync_posters.py
```

Set `JELLYFIN_METADATA_DIR` in `.env` to the host metadata library directory and add `-f compose.posters.yml` to the Compose command. The script reads Jellyfin's database on the host; the running viewer mounts only the artwork directory, read-only. Rerun the script when Jellyfin adds artwork.

## Storage and playback

`DATA_DIR/shares.db` contains live bearer tokens. Back it up privately and do not publish it. `CACHE_DIR` holds temporary HLS conversions. The container runs as `PUID:PGID`, uses a read-only filesystem, and needs write access only to those two directories. Completed playback caches older than six hours are cleaned as requests arrive.

MP4 files are served directly when a browser supports them. Other formats are converted to HLS with FFmpeg. Matching `.srt` and `.vtt` files beside a video appear as captions. Embedded subtitles are not exposed. CPU conversion can take a few seconds to start.

Links and preview images stop serving after expiration or revocation. A social app may retain a previously fetched preview in its own cache.

## This host

`compose.yml` is the existing, machine-specific deployment used on this server. The portable deployment is `compose.distribution.yml`; using it does not change the running local installation.
