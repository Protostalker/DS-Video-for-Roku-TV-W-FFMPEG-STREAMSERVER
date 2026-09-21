# DS Video stream server

Optional helper for the DS Video Roku channel. It runs on your NAS in Docker
(Synology Container Manager) and converts videos the Roku cannot play into ones
it can: it either repackages the file (remux) or re-encodes it (transcode) and
hands the Roku an HLS stream. Direct play is still used whenever the Roku can
handle the file as it is.

It does not need Video Station's transcoder, Advanced Media Extensions or any
Synology codec license. It uses ffmpeg inside the container.

Your Roku login is never stored here. The server asks DSM whether the Roku's
session may read the file, and refuses if DSM says no.

## What you need

- A Synology NAS with Container Manager (called "Docker" on DSM 7.1 and older).
- The NAS needs internet access once, to download ffmpeg while building.
- Nothing else. The Roku app finds the server at `http://<NAS address>:8899`.

## Install, way 1: Container Manager (no typing)

1. Open File Station. Inside the shared folder `docker` (Container Manager
   creates it) make a folder called `ds-video-stream`.
2. Upload the contents of this `streamserver` folder into it: `Dockerfile`,
   `streamserver.py`, `planner.py`, `docker-compose.yml`.
3. Open Container Manager, go to **Project**, click **Create**.
4. Project name `ds-video-stream`. Path: choose the folder from step 1. It
   will say it found a `docker-compose.yml`; keep "Use existing docker-compose.yml".
5. Click Next until it asks about Web Station, skip that, then **Done**. The
   first build takes a few minutes.
6. When the project shows **Running**, you are finished. Nothing to set on the Roku.

If DSM on your NAS does not use port 5000 (for example you changed it), open
`docker-compose.yml` before step 3 and change the `DSM_URL` line. This is the
address DSM has on the NAS itself, for example `http://127.0.0.1:5000` or
`https://127.0.0.1:5001`.

## Install, way 2: one command over SSH

1. Copy this whole folder to the NAS the same way (File Station).
2. Turn on SSH: DSM Control Panel > Terminal & SNMP > Enable SSH service.
3. Connect (`ssh youruser@nas-ip`), go into the folder and run:

```sh
cd /volume1/docker/ds-video-stream
sudo sh install-on-nas.sh
```

The script finds Docker, finds DSM's address, uses the Intel GPU if there is
one, builds the image, starts the container and checks that it answers. It says
in plain words what is wrong if something is. It is safe to run again to update.

Other commands:

```sh
sudo sh install-on-nas.sh --status      # is it running?
sudo sh install-on-nas.sh --logs        # last log lines
sudo sh install-on-nas.sh --port 8898   # use another port
sudo sh install-on-nas.sh --uninstall   # remove it
```

The container is set to restart by itself after a NAS reboot.

## Check that it works

Open `http://<NAS address>:8899/api/health` in a browser. You should see
`"ok": true` and `"reachable": true` under `dsm`. In the Roku app, open
**Settings** and press **Save**: it reports "Stream server OK" or what is wrong.

## Firewall

If the DSM firewall is on, allow TCP port `8899` (or your custom port) from
your home network: Control Panel > Security > Firewall.

## Changing the port or address

The Roku assumes `http://<NAS address>:8899`. If you use another port, or run
the server on another machine, open **Settings > Stream Server** on the Roku and
type `nas-ip:8898` or a full address such as `http://10.0.1.74:8898`. Type `off`
to turn the feature off completely. Leave it blank for automatic.

The port itself is set by `PORT` in `docker-compose.yml` (way 1) or `--port`
(way 2). The two must match.

## Intel hardware encoding (optional)

If your NAS has an Intel CPU with a GPU, the installer enables it
automatically (`/dev/dri`). For the Container Manager route, add
`docker-compose.vaapi.yml` as a second file or copy its two lines into the main
file. If the GPU does not work, the server falls back to software encoding by
itself. Untested on real hardware, so treat it as a bonus.

## Settings you may want

Set these under `environment:` in `docker-compose.yml`.

| Name | Default | What it does |
| --- | --- | --- |
| `DSM_URL` | `http://127.0.0.1:5000` | Where DSM listens on the NAS itself |
| `PORT` | `8899` | Port the Roku connects to |
| `MAX_SESSIONS` | `3` | Videos being converted at once |
| `MAX_VIDEO_KBPS` | `8000` | Bitrate cap when video is re-encoded |
| `IDLE_TIMEOUT_SEC` | `1200` | Stop an abandoned stream after this long |
| `HWACCEL` | `none` | `vaapi` for Intel GPU encoding |
| `X264_PRESET` | `veryfast` | Slower preset = better quality, more CPU |

## Troubleshooting

- **Roku says the server is not reachable:** the container is not running, the
  port is blocked by the firewall, or the port does not match Settings.
  Run `sudo sh install-on-nas.sh --status`.
- **Health page says DSM is not reachable:** fix `DSM_URL`.
- **Build fails:** the NAS could not download packages. Check its DNS and internet.
- **Port already in use:** pick another with `--port`, and put the same
  address in the Roku's Settings.
- **Playback is choppy:** the NAS CPU is too slow to re-encode. Files that only
  need remuxing (for example HEVC with E-AC3 on a 4K Roku) do not have this problem.
- **Black screen when playing:** watch what the server is doing. Run
  `sudo docker logs -f --tail 50 ds-video-stream` and press play. Each stream
  has a folder under `cache/` next to the install with `ffmpeg.log` (ffmpeg's
  own errors), `index.m3u8` and the `seg_*.ts` pieces; new pieces appearing
  means ffmpeg is working. Install with `--debug` to log the exact ffmpeg command.
- **The app skips the server for two minutes** after it failed to reach it. Press
  Save in Settings to retry at once.

## What it cannot do yet

- Seeking far ahead in a re-encoded stream starts a new stream at that point
  (takes a few seconds). Short jumps within what is already encoded are instant.
- Picture-based subtitles (PGS, DVD) cannot be shown as text. The subtitle menu offers
  them as "picture, burned in", which re-encodes the video (heavier on the NAS, and
  untested with real PGS files).
- Tested end to end on one Roku and one Synology NAS (HEVC and E-AC3 MKV, remux and
  transcode). Picture-subtitle burn-in, Intel GPU encoding and HDR tone-mapping are
  experimental. The server logic is covered by automated tests
  (`python3 -m unittest test_streamserver` with ffmpeg installed).
- The connection is plain HTTP. Use an HTTPS reverse proxy if the port is reachable from
  the internet.
