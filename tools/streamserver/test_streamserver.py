"""Tests for the DS Video stream server.

    python3 -m unittest -v test_streamserver

Needs ffmpeg/ffprobe (with libx264, libx265, libvpx-vp9, libopus, libmp3lame).
Test media is generated on the fly; a fake DSM serves it through the same
FileStation download endpoint the real server uses.
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

import planner
import streamserver

GOOD_SID = "GOODSID0123456789"
TOKEN = "TESTTOKEN"

# Capability strings the way the Roku app sends them.
CAPS_HD = "v=h264;a=aac,ac3,eac3,mp3;c=mp4,mkv;h=1080;l=41;aac6=0"
CAPS_4K = "v=h264,hevc,hevc10,vp9;a=aac,ac3,eac3,mp3,flac,opus;c=mp4,mkv,webm;h=2160;l=42;aac6=1"
CAPS_4K_MP4ONLY = "v=h264,hevc;a=aac,ac3,eac3,mp3;c=mp4;h=2160;l=42;aac6=0"


# --------------------------------------------------------------------------
# Test media
# --------------------------------------------------------------------------
def _ff(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def make_media(root: str) -> None:
    d = os.path.join(root, "video", "Movies")
    os.makedirs(d, exist_ok=True)
    srt = os.path.join(d, "hevc_eac3.en.srt")
    with open(srt, "w") as fh:
        fh.write("1\n00:00:01,000 --> 00:00:03,000\nHello one\n\n2\n00:00:12,000 --> 00:00:14,000\nHello twelve\n\n")
    six = "pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0"
    # H.264 + AAC stereo: plays directly
    _ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", "16", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2",
        os.path.join(d, "h264_aac.mp4"))
    shutil.copy(os.path.join(d, "hevc_eac3.en.srt"), os.path.join(d, "h264_aac.en.srt"))
    # HEVC + EAC3 + embedded text subtitle (the failing case from the bug report, minus PGS)
    _ff("-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=24000/1001", "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000",
        "-i", srt, "-t", "16", "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-tag:v", "hvc1",
        "-x265-params", "log-level=none:keyint=48:min-keyint=48", "-c:a", "eac3", "-b:a", "224k", "-ac", "2",
        "-c:s", "srt", "-metadata:s:s:0", "language=eng", os.path.join(d, "hevc_eac3.mkv"))
    # 10-bit H.264 + DTS 5.1: nothing about it is Roku friendly
    _ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24", "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000," + six,
        "-t", "10", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le", "-c:a", "dca", "-strict", "-2",
        "-b:a", "768k", os.path.join(d, "h264hi10_dts.mkv"))
    # MPEG-4 part 2 + MP3 mono in AVI
    _ff("-f", "lavfi", "-i", "testsrc2=size=640x480:rate=25", "-f", "lavfi", "-i", "sine=frequency=550:sample_rate=44100",
        "-t", "8", "-c:v", "mpeg4", "-c:a", "libmp3lame", os.path.join(d, "avi_mpeg4_mp3.avi"))
    # VP9 + Opus
    _ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24", "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=48000",
        "-t", "8", "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-c:a", "libopus",
        os.path.join(d, "vp9_opus.webm"))
    # HEVC + TrueHD 5.1 (default) + AC3 5.1
    _ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24", "-f", "lavfi", "-i", "sine=frequency=110:sample_rate=48000," + six,
        "-t", "10", "-map", "0:v", "-map", "1:a", "-map", "1:a", "-c:v", "libx265", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-x265-params", "log-level=none", "-c:a:0", "truehd", "-strict", "-2", "-c:a:1", "ac3",
        "-disposition:a:0", "default", "-disposition:a:1", "0", os.path.join(d, "hevc_truehd_ac3.mkv"))
    # AAC 5.1 in mkv
    _ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24", "-f", "lavfi", "-i", "sine=frequency=770:sample_rate=48000," + six,
        "-t", "8", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "384k",
        os.path.join(d, "h264_aac51.mkv"))
    # Longer file for the pause/resume test (transcoding it is slow enough to get ahead of the player)
    _ff("-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "90", "-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "2",
        os.path.join(d, "long.mkv"))


# --------------------------------------------------------------------------
# Fake DSM (FileStation download only)
# --------------------------------------------------------------------------
class FakeDsm(http.server.BaseHTTPRequestHandler):
    root = ""
    protocol_version = "HTTP/1.1"
    hits = 0

    def log_message(self, *a):
        pass

    def _json(self, code):
        body = json.dumps({"success": False, "error": {"code": code}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        FakeDsm.hits += 1
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
        if q.get("api") != "SYNO.FileStation.Download":
            return self._json(101)
        if q.get("_sid") != GOOD_SID:
            return self._json(119)
        full = os.path.normpath(os.path.join(self.root, q.get("path", "").lstrip("/")))
        if not full.startswith(self.root) or not os.path.isfile(full):
            return self._json(408)
        size = os.path.getsize(full)
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else 0
            end = min(int(b), size - 1) if b else size - 1
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with open(full, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = fh.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


# --------------------------------------------------------------------------
# Shared fixtures
# --------------------------------------------------------------------------
TMP = None
BASE = None
SERVERS = []
CAPTURE = Capture()


def setUpModule():
    global TMP, BASE
    TMP = tempfile.mkdtemp(prefix="dsv-test-")
    # Set DSV_TEST_MEDIA to a directory to keep the generated media between runs.
    media_root = os.environ.get("DSV_TEST_MEDIA") or os.path.join(TMP, "shares")
    if not os.path.exists(os.path.join(media_root, "video", "Movies", "long.mkv")):
        make_media(media_root)
    FakeDsm.root = media_root
    dsm = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeDsm)
    threading.Thread(target=dsm.serve_forever, daemon=True).start()
    env = {
        "HOST": "127.0.0.1", "PORT": "0", "DSM_URL": "http://127.0.0.1:%d" % dsm.server_address[1],
        "WORK_DIR": os.path.join(TMP, "work"), "X264_PRESET": "ultrafast", "SEGMENT_SECONDS": "2",
        "LOOKAHEAD_SECONDS": "30", "PLAYLIST_WAIT_SEC": "60", "LOG_LEVEL": "DEBUG",
    }
    logging.getLogger("dsv-stream").addHandler(CAPTURE)
    logging.getLogger("dsv-stream").setLevel(logging.DEBUG)
    cfg = streamserver.Config(env)
    server, app = streamserver.make_server(cfg)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    BASE = "http://127.0.0.1:%d" % server.server_address[1]
    SERVERS.extend([(dsm, None), (server, app)])


def tearDownModule():
    for srv, app in SERVERS:
        if app:
            app.shutdown()
        srv.shutdown()
        srv.server_close()
    shutil.rmtree(TMP, ignore_errors=True)



def get(path, params=None, raw=False, headers=None, method="GET"):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = resp.read()
            return resp.status, (body if raw else json.loads(body.decode())), resp.headers
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, (body if raw else json.loads(body.decode())), exc.headers
        except ValueError:
            return exc.code, body, exc.headers


def q(name, **extra):
    p = {"path": "/video/Movies/" + name, "sid": GOOD_SID, "token": TOKEN}
    p.update(extra)
    return p


def plan(name, caps, **extra):
    status, body, _ = get("/api/plan", q(name, caps=caps, **extra))
    assert status == 200, body
    return body


def download_hls(session_url, timeout=180):
    """Behave like a player: poll the playlist, fetch every segment, return (playlist, ts bytes)."""
    got, data, deadline = [], b"", time.time() + timeout
    text = ""
    while time.time() < deadline:
        status, text, _ = get(session_url, raw=True)
        assert status == 200, text
        text = text.decode()
        names = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        for name in names:
            if name not in got:
                s, body, _ = get(os.path.dirname(session_url) + "/" + name, raw=True)
                assert s == 200, (name, body[:200])
                data += body
                got.append(name)
        if "#EXT-X-ENDLIST" in text and len(got) == len(names):
            return text, data
        time.sleep(0.5)
    raise AssertionError("HLS stream did not finish in time")


def probe_bytes(data, suffix=".ts"):
    path = os.path.join(TMP, "probe" + suffix)
    with open(path, "wb") as fh:
        fh.write(data)
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                         check=True, stdout=subprocess.PIPE).stdout
    info = json.loads(out)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    return v, a, float(info["format"].get("duration", 0))


def decode_ok(data):
    """The whole stream must decode without errors."""
    path = os.path.join(TMP, "decode.ts")
    with open(path, "wb") as fh:
        fh.write(data)
    res = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"], stderr=subprocess.PIPE)
    return res.returncode == 0 and not res.stderr.strip(), res.stderr.decode()[-300:]


# --------------------------------------------------------------------------
# Unit tests: capabilities and planning
# --------------------------------------------------------------------------
class CapsTests(unittest.TestCase):
    def test_default_when_missing(self):
        caps = planner.parse_caps(None)
        self.assertFalse(caps.known)
        self.assertEqual(caps.video, {"h264"})

    def test_parse(self):
        caps = planner.parse_caps(CAPS_4K)
        self.assertTrue(caps.known)
        self.assertIn("hevc", caps.video)
        self.assertEqual(caps.max_height, 2160)
        self.assertTrue(caps.aac_multichannel)
        self.assertEqual(caps.h264_level, 42)

    def test_hevc10_implies_hevc(self):
        self.assertIn("hevc", planner.parse_caps("v=hevc10").video)

    def test_garbage_is_ignored(self):
        caps = planner.parse_caps("nonsense;v=;h=abc;x=y=z")
        self.assertEqual(caps.max_height, 1080)


def media(vcodec="h264", ext="mkv", pix="yuv420p", w=1920, h=1080, level=41, acodec="aac", ch=2, **kw):
    v = planner.VideoInfo(index=0, codec=vcodec, level=level, width=w, height=h, fps=23.976, pix_fmt=pix,
                          bit_depth=10 if "10" in pix else 8, **kw)
    audios = [planner.AudioInfo(index=1, codec=acodec, channels=ch, default=True)] if acodec else []
    return planner.MediaInfo(path="/v/x." + ext, ext=ext, video=v, audios=audios, duration=100)


class DecideTests(unittest.TestCase):
    def d(self, m, caps, **kw):
        return planner.decide(m, planner.parse_caps(caps), **kw)

    def test_hevc_on_non_4k_roku_transcodes(self):
        p = self.d(media("hevc", acodec="eac3"), CAPS_HD)
        self.assertEqual((p.mode, p.video_action, p.audio_action), ("transcode", "encode", "copy"))

    def test_hevc_on_4k_roku_plays_directly(self):
        self.assertEqual(self.d(media("hevc", acodec="eac3"), CAPS_4K).mode, "direct")

    def test_hevc_in_avi_container_is_remuxed(self):
        p = self.d(media("hevc", ext="avi", acodec="ac3"), CAPS_4K)
        self.assertEqual((p.mode, p.audio_action), ("remux", "copy"))

    def test_dts_audio_forces_remux_with_ac3(self):
        p = self.d(media("h264", acodec="dts", ch=6), CAPS_HD)
        self.assertEqual((p.mode, p.audio_action, p.audio_channels), ("remux", "ac3", 6))

    def test_stereo_flac_becomes_aac(self):
        p = self.d(media("h264", acodec="flac", ch=2), CAPS_HD)
        self.assertEqual((p.mode, p.audio_action), ("remux", "aac"))

    def test_multichannel_aac_on_old_roku(self):
        p = self.d(media("h264", acodec="aac", ch=6), CAPS_HD)
        self.assertEqual((p.mode, p.audio_action), ("remux", "ac3"))

    def test_multichannel_aac_where_supported(self):
        self.assertEqual(self.d(media("h264", acodec="aac", ch=6), CAPS_4K).mode, "direct")

    def test_10bit_h264_transcodes(self):
        p = self.d(media("h264", pix="yuv420p10le"), CAPS_4K)
        self.assertEqual(p.mode, "transcode")
        self.assertTrue(any("10-bit" in r for r in p.reasons))

    def test_h264_level_too_high(self):
        self.assertEqual(self.d(media("h264", level=51), CAPS_HD).mode, "transcode")

    def test_4k_h264_scales_down(self):
        p = self.d(media("h264", w=3840, h=2160, level=51), CAPS_4K)
        self.assertEqual((p.mode, p.scale_height), ("transcode", 1080))

    def test_mpeg4_transcodes(self):
        self.assertEqual(self.d(media("mpeg4", ext="avi", acodec="mp3"), CAPS_4K).mode, "transcode")

    def test_vp9_direct_only_if_supported(self):
        self.assertEqual(self.d(media("vp9", ext="webm", acodec="opus"), CAPS_4K).mode, "direct")
        self.assertEqual(self.d(media("vp9", ext="webm", acodec="opus"), CAPS_HD).mode, "transcode")

    def test_no_caps_is_conservative(self):
        p = self.d(media("hevc"), None)
        self.assertEqual(p.mode, "transcode")

    def test_force_remux_and_transcode(self):
        m = media("h264", acodec="aac")
        self.assertEqual(self.d(m, CAPS_HD).mode, "direct")
        self.assertEqual(self.d(m, CAPS_HD, force="remux").mode, "remux")
        self.assertEqual(self.d(m, CAPS_HD, force="transcode").mode, "transcode")

    def test_force_remux_cannot_copy_unsupported_video(self):
        self.assertEqual(self.d(media("mpeg4", ext="avi"), CAPS_HD, force="remux").mode, "transcode")

    def test_non_default_audio_track_needs_remux(self):
        m = media("h264", acodec="aac")
        m.audios.append(planner.AudioInfo(index=2, codec="ac3", channels=6))
        p = self.d(m, CAPS_HD, audio_index=2)
        self.assertEqual((p.mode, p.audio_index, p.audio_action), ("remux", 2, "copy"))

    def test_hdr_transcode_tonemaps_when_available(self):
        m = media("hevc", pix="yuv420p10le", hdr="hdr10")
        p = self.d(m, CAPS_HD, opts=planner.PlanOptions(have_zscale=True))
        self.assertTrue(p.tonemap)

    def test_dolby_vision_p5_needs_transcode(self):
        m = media("hevc", pix="yuv420p10le", hdr="dv", dv_profile=5)
        self.assertEqual(self.d(m, CAPS_4K).mode, "transcode")

    def test_embedded_subtitle_pick(self):
        m = media()
        m.subs = [planner.SubInfo(index=3, codec="hdmv_pgs_subtitle", lang="eng"),
                  planner.SubInfo(index=4, codec="subrip", lang="fre"),
                  planner.SubInfo(index=5, codec="subrip", lang="eng", forced=True),
                  planner.SubInfo(index=6, codec="subrip", lang="eng")]
        self.assertEqual(planner.pick_embedded_subtitle(m).index, 6)
        m.subs = [planner.SubInfo(index=3, codec="hdmv_pgs_subtitle", lang="eng")]
        self.assertIsNone(planner.pick_embedded_subtitle(m))


class CommandTests(unittest.TestCase):
    def build(self, m, caps, **kw):
        p = planner.decide(m, planner.parse_caps(caps), **{k: v for k, v in kw.items() if k in ("force",)})
        opts = planner.HlsOptions(use_vaapi=kw.get("vaapi", False))
        return p, planner.build_hls_command(opts, "http://dsm/x", m, p, kw.get("start", 0), "/w", use_hw=kw.get("vaapi", False))

    def test_remux_copies_video(self):
        p, cmd = self.build(media("hevc", ext="avi", acodec="ac3"), CAPS_4K)
        self.assertIn("copy", cmd)
        self.assertNotIn("libx264", cmd)
        self.assertEqual(cmd[cmd.index("-i") + 1], "http://dsm/x")

    def test_transcode_sets_keyframes_and_scaling(self):
        p, cmd = self.build(media("h264", w=3840, h=2160, level=51), CAPS_4K)
        self.assertIn("libx264", cmd)
        self.assertIn("scale=1920:1080,format=yuv420p", cmd[cmd.index("-vf") + 1])
        self.assertIn("expr:gte(t,n_forced*6)", cmd)

    def test_seek_is_an_input_option(self):
        p, cmd = self.build(media("hevc", ext="avi", acodec="ac3"), CAPS_4K, start=90)
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))
        self.assertEqual(cmd[cmd.index("-ss") + 1], "90")

    def test_vaapi_only_for_plain_transcodes(self):
        _, cmd = self.build(media("hevc"), CAPS_HD, vaapi=True)
        self.assertIn("h264_vaapi", cmd)
        m = media("hevc", interlaced=True)
        _, cmd = self.build(m, CAPS_HD, vaapi=True)
        self.assertNotIn("h264_vaapi", cmd)


# --------------------------------------------------------------------------
# Integration tests against the real ffmpeg + fake DSM
# --------------------------------------------------------------------------
class ApiTests(unittest.TestCase):
    def test_health(self):
        status, body, _ = get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("ffmpeg", body["ffmpeg"].lower())

    def test_bad_session_is_rejected(self):
        p = q("h264_aac.mp4")
        p["sid"] = "WRONG"
        status, body, _ = get("/api/plan", p)
        self.assertEqual(status, 401)

    def test_missing_sid(self):
        p = q("h264_aac.mp4")
        del p["sid"]
        self.assertEqual(get("/api/plan", p)[0], 401)

    def test_missing_file(self):
        self.assertEqual(get("/api/plan", q("nope.mkv"))[0], 404)

    def test_path_traversal(self):
        p = q("x")
        p["path"] = "/video/../../etc/passwd"
        self.assertEqual(get("/api/plan", p)[0], 400)
        p["path"] = "relative.mkv"
        self.assertEqual(get("/api/plan", p)[0], 400)

    def test_plan_direct_mp4(self):
        r = plan("h264_aac.mp4", CAPS_HD)
        self.assertEqual(r["mode"], "direct")
        self.assertEqual(r["media"]["video"]["codec"], "h264")
        self.assertGreater(r["duration"], 15)

    def test_plan_the_failing_file(self):
        """HEVC + EAC3 mkv: transcode on a non-4K Roku, direct on a 4K one."""
        r = plan("hevc_eac3.mkv", CAPS_HD)
        self.assertEqual((r["mode"], r["plan"]["audio_action"]), ("transcode", "copy"))
        self.assertEqual(plan("hevc_eac3.mkv", CAPS_4K)["mode"], "direct")
        r = plan("hevc_eac3.mkv", CAPS_4K_MP4ONLY)
        self.assertEqual((r["mode"], r["plan"]["audio_action"]), ("remux", "copy"))

    def test_start_refuses_direct_files(self):
        self.assertEqual(get("/api/start", q("h264_aac.mp4", caps=CAPS_HD))[0], 409)

    def test_unknown_session(self):
        self.assertEqual(get("/s/" + "0" * 32 + "/index.m3u8", raw=True)[0], 404)

    def test_sid_never_logged(self):
        get("/api/plan", q("h264_aac.mp4", caps=CAPS_HD))
        for line in CAPTURE.lines:
            self.assertNotIn(GOOD_SID, line)
            self.assertNotIn(TOKEN, line)


class StreamTests(unittest.TestCase):
    def start(self, name, caps, mode="auto", **extra):
        status, body, _ = get("/api/start", q(name, caps=caps, mode=mode, **extra))
        self.assertEqual(status, 200, body)
        self.addCleanup(get, "/api/stop", {"session": body["session"]})
        return body

    def test_remux_hevc_keeps_streams_and_is_valid(self):
        r = self.start("hevc_eac3.mkv", CAPS_4K_MP4ONLY)
        self.assertEqual(r["mode"], "remux")
        playlist, data = download_hls(r["url"])
        self.assertIn("#EXT-X-PLAYLIST-TYPE:EVENT", playlist)
        v, a, dur = probe_bytes(data)
        self.assertEqual((v["codec_name"], a["codec_name"]), ("hevc", "eac3"))
        self.assertAlmostEqual(dur, 16, delta=2.5)
        ok, err = decode_ok(data)
        self.assertTrue(ok, err)

    def test_transcode_hevc_to_h264_keeps_eac3(self):
        r = self.start("hevc_eac3.mkv", CAPS_HD)
        self.assertEqual(r["mode"], "transcode")
        _, data = download_hls(r["url"])
        v, a, dur = probe_bytes(data)
        self.assertEqual((v["codec_name"], v["pix_fmt"], a["codec_name"]), ("h264", "yuv420p", "eac3"))
        self.assertLessEqual(int(v["height"]), 1080)
        self.assertAlmostEqual(dur, 16, delta=2.5)
        ok, err = decode_ok(data)
        self.assertTrue(ok, err)

    def test_segments_are_regular_length_when_transcoding(self):
        r = self.start("hevc_eac3.mkv", CAPS_HD)
        playlist, _ = download_hls(r["url"])
        durations = [float(l.split(":")[1].rstrip(",")) for l in playlist.splitlines() if l.startswith("#EXTINF")]
        for d in durations[:-1]:
            self.assertAlmostEqual(d, 2.0, delta=0.2)

    def test_10bit_h264_and_dts_become_8bit_and_ac3(self):
        r = self.start("h264hi10_dts.mkv", CAPS_4K)
        self.assertEqual(r["mode"], "transcode")
        _, data = download_hls(r["url"])
        v, a, _ = probe_bytes(data)
        self.assertEqual((v["codec_name"], v["pix_fmt"]), ("h264", "yuv420p"))
        self.assertEqual((a["codec_name"], int(a["channels"])), ("ac3", 6))
        self.assertTrue(decode_ok(data)[0])

    def test_avi_mpeg4_mp3(self):
        r = self.start("avi_mpeg4_mp3.avi", CAPS_HD)
        _, data = download_hls(r["url"])
        v, a, dur = probe_bytes(data)
        self.assertEqual((v["codec_name"], a["codec_name"], int(a["channels"])), ("h264", "aac", 2))
        self.assertAlmostEqual(dur, 8, delta=2.5)

    def test_vp9_webm_on_device_without_vp9(self):
        r = self.start("vp9_opus.webm", CAPS_HD)
        _, data = download_hls(r["url"])
        v, a, _ = probe_bytes(data)
        self.assertEqual((v["codec_name"], a["codec_name"]), ("h264", "aac"))

    def test_truehd_default_track_is_converted_and_alt_track_can_be_chosen(self):
        r = self.start("hevc_truehd_ac3.mkv", CAPS_4K_MP4ONLY)
        self.assertEqual(r["mode"], "remux")
        _, data = download_hls(r["url"])
        v, a, _ = probe_bytes(data)
        self.assertEqual((v["codec_name"], a["codec_name"], int(a["channels"])), ("hevc", "ac3", 6))
        info = plan("hevc_truehd_ac3.mkv", CAPS_4K_MP4ONLY)["media"]["audio"]
        ac3_index = [t["index"] for t in info if t["codec"] == "ac3"][0]
        p2 = plan("hevc_truehd_ac3.mkv", CAPS_4K_MP4ONLY, audio=ac3_index)
        self.assertEqual(p2["plan"]["audio_action"], "copy")
        r2 = self.start("hevc_truehd_ac3.mkv", CAPS_4K_MP4ONLY, audio=ac3_index)
        _, data2 = download_hls(r2["url"])
        self.assertEqual(probe_bytes(data2)[1]["codec_name"], "ac3")

    def test_aac_51_to_ac3_on_old_roku(self):
        r = self.start("h264_aac51.mkv", CAPS_HD)
        self.assertEqual(r["mode"], "remux")
        _, data = download_hls(r["url"])
        v, a, _ = probe_bytes(data)
        self.assertEqual((v["codec_name"], a["codec_name"]), ("h264", "ac3"))

    def test_resume_offset(self):
        r = self.start("hevc_eac3.mkv", CAPS_4K_MP4ONLY, start=8)
        self.assertEqual(r["offset"], 8)
        _, data = download_hls(r["url"])
        _, _, dur = probe_bytes(data)
        self.assertAlmostEqual(dur, 8, delta=3)

    def test_embedded_subtitle_extracted_and_shifted(self):
        r = self.start("hevc_eac3.mkv", CAPS_HD)
        self.assertTrue(r["subtitle_url"].endswith(".srt"))
        status, body, _ = get(r["subtitle_url"], raw=True)
        self.assertEqual(status, 200)
        self.assertIn("Hello one", body.decode())
        self.assertRegex(body.decode(), r"00:00:01,00\d")
        r2 = self.start("hevc_eac3.mkv", CAPS_HD, start=10)
        text = get(r2["subtitle_url"], raw=True)[1].decode()
        self.assertNotIn("Hello one", text)
        self.assertIn("Hello twelve", text)
        self.assertRegex(text, r"00:00:02,00\d")  # 12 s minus the 10 s offset

    def test_sidecar_subtitle_is_shifted(self):
        r = self.start("h264_aac.mp4", CAPS_HD, mode="remux", start=10, sidecar="/video/Movies/h264_aac.en.srt")
        text = get(r["subtitle_url"], raw=True)[1].decode()
        self.assertIn("Hello twelve", text)
        self.assertRegex(text, r"00:00:02,00\d")

    def test_start_lists_tracks_and_can_skip_eager_subtitles(self):
        r = self.start("hevc_eac3.mkv", CAPS_HD, nosub="1")
        self.assertEqual(r["subtitle_url"], "")
        self.assertEqual([t["codec"] for t in r["subs"]], ["subrip"])
        self.assertTrue(r["subs"][0]["text"])
        self.assertEqual(r["default_subtitle"], r["subs"][0]["index"])
        self.assertEqual(r["audio"][0]["codec"], "eac3")

    def test_sub_endpoint_returns_whole_track_unshifted(self):
        r = self.start("hevc_eac3.mkv", CAPS_HD, nosub="1", start=10)
        idx = r["subs"][0]["index"]
        status, body, _ = get("/api/sub", q("hevc_eac3.mkv", index=idx))
        self.assertEqual(status, 200, body)
        status, text, _ = get(body["url"], raw=True)
        self.assertEqual(status, 200)
        text = text.decode()
        self.assertIn("Hello one", text)
        self.assertIn("Hello twelve", text)
        self.assertRegex(text, r"00:00:01,00\d")
        # asking again reuses the same extraction
        status, body2, _ = get("/api/sub", q("hevc_eac3.mkv", index=idx))
        self.assertEqual(body2["url"], body["url"])

    def test_sub_endpoint_sidecar_and_errors(self):
        status, body, _ = get("/api/sub", q("h264_aac.mp4", sidecar="/video/Movies/h264_aac.en.srt"))
        self.assertEqual(status, 200, body)
        self.assertIn("Hello one", get(body["url"], raw=True)[1].decode())
        self.assertEqual(get("/api/sub", q("h264_aac.mp4", index=99))[0], 400)
        self.assertEqual(get("/api/sub", q("h264_aac.mp4"))[0], 400)
        self.assertEqual(get("/api/sub", q("h264_aac.mp4", sidecar="/video/Movies/missing.srt"))[0], 404)

    def test_bad_sidecar_path_does_not_break_playback(self):
        r = self.start("h264_aac.mp4", CAPS_HD, mode="remux", sidecar="/video/Movies/missing.srt")
        self.assertEqual(r["subtitle_url"], "")
        self.assertEqual(get(r["url"], raw=True)[0], 200)

    def test_plan_reports_whether_remux_is_possible(self):
        self.assertTrue(plan("h264_aac.mp4", CAPS_HD)["can_remux"])
        self.assertFalse(plan("avi_mpeg4_mp3.avi", CAPS_HD)["can_remux"])

    def test_range_and_head_on_segments(self):
        r = self.start("h264_aac.mp4", CAPS_HD, mode="remux")
        get(r["url"], raw=True)
        base = os.path.dirname(r["url"])
        status, body, hdrs = get(base + "/seg_00000.ts", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(body[0], 0x47)  # MPEG-TS sync byte
        status, part, hdrs = get(base + "/seg_00000.ts", raw=True, headers={"Range": "bytes=188-375"})
        self.assertEqual(status, 206)
        self.assertEqual(len(part), 188)
        self.assertEqual(part, body[188:376])
        status, _, hdrs = get(base + "/seg_00000.ts", raw=True, method="HEAD")
        self.assertEqual(int(hdrs["Content-Length"]), len(body))

    def test_path_in_session_url_cannot_escape(self):
        r = self.start("h264_aac.mp4", CAPS_HD, mode="remux")
        base = os.path.dirname(r["url"])
        for bad in ("/..%2findex.m3u8", "/ffmpeg.log", "/seg_1.ts"):
            self.assertEqual(get(base + bad, raw=True)[0], 404, bad)

    def test_stop_removes_session_and_files(self):
        r = self.start("h264_aac.mp4", CAPS_HD, mode="remux")
        get(r["url"], raw=True)
        app = SERVERS[1][1]
        sess = app.sessions.get(r["session"])
        self.assertTrue(os.path.isdir(sess.dir))
        self.assertTrue(get("/api/stop", {"session": r["session"]})[1]["stopped"])
        time.sleep(1)
        self.assertFalse(os.path.exists(sess.dir))
        self.assertEqual(get(r["url"], raw=True)[0], 404)

    def test_same_client_replaces_its_own_session(self):
        a = self.start("h264_aac.mp4", CAPS_HD, mode="remux")
        b = self.start("h264_aac.mp4", CAPS_HD, mode="remux")
        time.sleep(1)
        self.assertEqual(get(a["url"], raw=True)[0], 404)
        self.assertEqual(get(b["url"], raw=True)[0], 200)

    def test_ffmpeg_pauses_when_far_ahead_and_resumes(self):
        r = self.start("long.mkv", CAPS_HD, mode="transcode")
        app = SERVERS[1][1]
        sess = app.sessions.get(r["session"])
        get(r["url"], raw=True)  # first playlist, nothing else requested
        deadline = time.time() + 60
        while time.time() < deadline and not sess.paused:
            time.sleep(0.5)
        self.assertTrue(sess.paused, sess.status())
        generated = sess.count_segments()
        self.assertLess(generated, 40)  # 90 s / 2 s = 45 segments in total
        time.sleep(3)
        self.assertEqual(sess.count_segments(), generated)  # really frozen
        base = os.path.dirname(r["url"])
        for n in range(generated - 8):  # a player consuming segments
            get(base + "/seg_%05d.ts" % n, raw=True)
        deadline = time.time() + 30
        while time.time() < deadline and sess.paused:
            time.sleep(0.5)
        self.assertFalse(sess.paused)
        _, data = download_hls(r["url"])
        _, _, dur = probe_bytes(data)
        self.assertAlmostEqual(dur, 90, delta=4)

    def test_session_limit(self):
        app = SERVERS[1][1]
        names = ["h264_aac.mp4", "h264_aac51.mkv", "h264hi10_dts.mkv", "avi_mpeg4_mp3.avi"]
        sessions = []
        for n in names:
            r = self.start(n, CAPS_HD, mode="remux")
            sessions.append(r)
        time.sleep(1)
        self.assertLessEqual(len(app.sessions.snapshot()), app.cfg.max_sessions)
        self.assertEqual(get(sessions[0]["url"], raw=True)[0], 404)  # the oldest one was evicted


if __name__ == "__main__":
    unittest.main(verbosity=2)
