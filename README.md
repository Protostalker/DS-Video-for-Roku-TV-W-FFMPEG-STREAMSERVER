# Synology DS Video for Roku, with a stream server

A Roku channel for browsing and playing your Synology Video Station library, plus an
optional helper that runs on the NAS and converts anything the Roku cannot play, such as
HEVC video with E-AC3 audio in an MKV.

This is a fork of [sapman1208/DS-Video-for-Roku-TV](https://github.com/sapman1208/DS-Video-for-Roku-TV).
The channel itself (library browsing, artwork, ratings, watch status, resume, the
Video Station restore kit) is that project's work. This fork adds the stream server,
format detection and a custom player.

## What was added

- **Stream server** (`tools/streamserver`): a small Python and ffmpeg service in Docker
  (Synology Container Manager). It repackages (remux) or re-encodes (transcode) a video and
  hands the Roku an HLS stream. It does not use Video Station's transcoder or any Synology
  codec license.
- **Format detection**: the channel asks the Roku what it can decode and picks the lightest
  way that works:
  1. direct play,
  2. remux (video copied untouched, audio converted),
  3. transcode (H.264 with AAC or AC3),
  4. Video Station's RokuVTE wrapper as a last resort.

  If a step does not start within a time limit, the app moves to the next one on its own.
  Without the server the app still works, using steps 1 and 4 only.
- **Custom player** for server streams, since the Roku's own controls cannot scrub a stream
  that is still being prepared: a progress bar with the real position and length, pause,
  skip, and a menu for subtitle and audio tracks.
- **Subtitles**: text tracks and subtitle files next to the video are drawn by the app and
  switched on automatically. Picture subtitles (PGS, DVD) are burned into the video.

## Setup guide

Two parts: put the stream server on the NAS (10 minutes), then put the channel on the Roku
(5 minutes). You need a Synology NAS with Video Station data, a Roku, and a Windows, Mac or
Linux computer on the same network.

### What you need first

- A Synology NAS running DSM 7 with **Container Manager** installed (Package Center; it is
  called "Docker" on DSM 7.1 and older). The NAS needs internet access once, to download
  ffmpeg while the server is built.
- A DSM account that is an **administrator** (needed for SSH and `sudo`).
- The Roku and the NAS on the same network, and a Roku with developer mode (see Part 2).
- This repository as a folder on your computer: on GitHub click **Code, Download ZIP**, then
  unzip it.

### Part 1: install the stream server on the NAS

1. **Turn on SSH.** DSM, Control Panel, Terminal & SNMP, tick **Enable SSH service**, Apply.
2. **Copy the server files to the NAS.** Open File Station. Inside the shared folder
   `docker` (Container Manager creates it; create it if it is missing) make a folder called
   `ds-video-stream`. Upload the **contents** of the repository's `tools/streamserver`
   folder into it, so that these files sit directly inside `ds-video-stream`:
   `Dockerfile`, `streamserver.py`, `planner.py`, `docker-compose.yml`,
   `docker-compose.vaapi.yml`, `install-on-nas.sh`. On the NAS that folder is
   `/volume1/docker/ds-video-stream`. Use a name without spaces.
3. **Connect over SSH.** On your computer open a terminal (Windows: PowerShell) and type,
   using your own DSM username and NAS address:

   ```sh
   ssh yourusername@192.168.1.50
   ```

   Answer `yes` if it asks about a fingerprint, then type your DSM password. Nothing shows
   while you type it, which is normal.
4. **Run the installer.**

   ```sh
   cd /volume1/docker/ds-video-stream
   sudo sh install-on-nas.sh
   ```

   Type your password again if `sudo` asks. The first run takes a few minutes while it
   builds. It finishes with a line saying the server is running and healthy. If something
   is wrong it says what in plain words (for example that Container Manager is missing).
5. **Check it.** In a browser on your computer open `http://<NAS address>:8899/api/health`.
   You should see `"ok": true` and, under `dsm`, `"reachable": true`.
6. **Firewall.** If the DSM firewall is on (Control Panel, Security, Firewall), allow TCP
   port `8899` from your home network.

Prefer no typing? In Container Manager go to **Project, Create**, pick the
`ds-video-stream` folder, keep "Use existing docker-compose.yml", click Next until Done. The
result is the same. Details and other options are in
[tools/streamserver/README.md](tools/streamserver/README.md).

The server starts again by itself after a NAS reboot.

### Part 2: install the channel on the Roku

1. **Turn on developer mode.** With the Roku remote press: Home three times, Up twice,
   Right, Left, Right, Left, Right. Note the IP address it shows, accept the agreement, set
   a developer password and let the Roku restart.
2. **Identify the Zip** Find the zip file in the repo, and remember where you saved it lol (should be `roku-ds-video-x.x.x`)
3. **Upload it.** In a browser go to `http://<ROKU_IP>/`, sign in as user `rokudev` with
   your developer password, click **Upload**, choose the zip, then **Install**. The channel
   starts on its own. Later, it is in your channel list as **Synology DS Video**.
4. **Sign in inside the channel.** Enter:
   - NAS address: hostname or IP only, such as `nas.example.com` or `192.168.1.50`.
   - Port: your DSM port (`5001` for HTTPS, `5000` for HTTP) and the matching protocol.
   - A DSM username and password that can use Video Station.
5. **Check the server link.** Open **Settings** in the channel, leave "Stream Server" blank
   (it finds `http://<NAS address>:8899` on its own) and press **Save**. It says "Stream
   server OK" or tells you what is wrong.

### Part 3: try it

Play a file that used to fail, such as an HEVC video with E-AC3 audio in an MKV. It may
sit on the loading screen for up to about a minute the first time while the server
prepares it. Subtitles come on by themselves if the video has any.

### If you watch away from home

Forward TCP port `8899` on your router to the NAS and use your DDNS name as the NAS address
in the channel. Read the note about encryption under "Known limits" first.

### Updating and removing

- **Update the server:** copy the new files over the old ones in `ds-video-stream` and run
  `sudo sh install-on-nas.sh` again. It is safe to repeat.
- **Update the channel:** build a new zip and upload it the same way; it replaces the old one.
- **Check on the server:** `sudo sh install-on-nas.sh --status` and `--logs`.
- **Remove the server:** `sudo sh install-on-nas.sh --uninstall`.

### When something goes wrong

| What you see | Likely cause and fix |
| --- | --- |
| Settings says the server is not reachable | Container not running, firewall blocking port 8899, or the port differs from Settings. Run `sudo sh install-on-nas.sh --status`. |
| Health page says `"reachable": false` under `dsm` | DSM is not on port 5000. Change `DSM_URL` in `docker-compose.yml` and run the installer again. |
| Installer says Docker was not found or not running | Install or open Container Manager in Package Center, then run it again. |
| Build fails | The NAS could not download packages. Check its internet access and DNS. |
| Black screen while playing | Run `sudo docker logs -f --tail 50 ds-video-stream` and press play; see the troubleshooting list in the server README. |
| Roku on the internet path cannot connect | Port 8899 is not forwarded on the router. |
| The app ignores the server for two minutes after a failure | Press Save in Settings to retry at once. |

## Player controls (server streams)

| Key | Action |
| --- | --- |
| OK or Play | Pause and resume |
| Left / Right | Back / forward 10 seconds |
| Rewind / Fast forward | Back / forward 60 seconds |
| Up | Show the progress bar |
| Down or `*` | Menu: subtitle track, audio track, automatic subtitles on or off |
| Back | Stop |

Skipping within what the server has already prepared is instant. Skipping further starts a
new stream at that point, which takes a few seconds.

## Known limits

- **The connection is not encrypted by default.** The stream server speaks plain HTTP and
  the app sends your Synology session to it. On a home network that is usually fine. If you
  expose the port to the internet, put it behind an HTTPS reverse proxy (DSM: Control Panel,
  Login Portal, Reverse Proxy) and enter the `https://` address in `Settings > Stream Server`.
- Picture-subtitle burn-in, Intel GPU encoding (`HWACCEL=vaapi`) and HDR tone-mapping are
  experimental and have had little or no testing on real hardware.
- A re-encoded 1080p stream needs a NAS CPU that can keep up. Files that only need remuxing
  are far lighter.
- Seeking far ahead in a re-encoded stream restarts it at that point.
- Tested on one Roku and one Synology NAS. The server logic has automated tests
  (`python3 -m unittest test_streamserver` in `tools/streamserver`, with ffmpeg installed).

## Restore kit for DSM 7.2.2 and newer

Synology removed Video Station from recent DSM builds. `tools/` also holds the original
project's restore kit that reinstalls Video Station and the patched RokuVTE wrapper. It is
documented in [tools/README.md](tools/README.md). It wraps the script
`tools/Video_Station_for_DSM_722-1.4.22` by Dave Russell (MIT, see its `UPSTREAM.md`).

## Repository layout

```text
manifest, source/, components/, images/   the Roku channel
tools/streamserver/                       stream server (Docker) and its tests
tools/rokuvte/                            RokuVTE wrapper for Video Station
tools/ds-video-restore-kit.sh, build-ds-video-restore-kit.py, install.sh   restore kit
```

## Credits

Original channel and restore kit: sapman1208. Video Station restore script: Dave Russell
(007revad). Stream server, format detection and custom player: added in this fork.
