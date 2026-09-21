#!/usr/bin/env python3
"""DS Video stream server.

A small ffmpeg based helper for the DS Video Roku channel. The Roku asks it
"can I play this file?" (/api/plan). If not, it asks for an HLS stream that the
Roku can play (/api/start): video is copied when possible (remux), otherwise
re-encoded to H.264, and audio is converted to AAC/AC3 when needed.

Authentication is delegated to DSM: every request carries the DSM session id
(sid) the Roku already has, and the server proves the user may read the file by
requesting the first byte of it through the FileStation download API. The same
URL is then handed to ffmpeg, so the server needs no access to the volumes and
no path mapping.

Pure standard library, Python 3.8+. Requires ffmpeg and ffprobe.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import logging
import os
import re
import shutil
import signal
import socketserver
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import planner  # noqa: E402

VERSION = "1.0.0"
log = logging.getLogger("dsv-stream")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Config:
    """All settings come from environment variables (see README)."""

    def __init__(self, env=None):
        e = os.environ if env is None else env
        self.host = e.get("HOST", "0.0.0.0")
        self.port = _int(e.get("PORT"), 8899)
        self.dsm_url = e.get("DSM_URL", "http://127.0.0.1:5000").rstrip("/")
        self.verify_tls = _truthy(e.get("DSM_VERIFY_TLS", "0"))
        self.work_dir = e.get("WORK_DIR", "/tmp/dsv-stream")
        self.ffmpeg = e.get("FFMPEG_BIN", "ffmpeg")
        self.ffprobe = e.get("FFPROBE_BIN", "ffprobe")
        self.hwaccel = e.get("HWACCEL", "none").strip().lower()
        self.vaapi_device = e.get("VAAPI_DEVICE", "/dev/dri/renderD128")
        self.max_sessions = max(1, _int(e.get("MAX_SESSIONS"), 3))
        self.idle_timeout = max(30, _int(e.get("IDLE_TIMEOUT_SEC"), 1200))
        self.segment_seconds = max(2, _int(e.get("SEGMENT_SECONDS"), 6))
        self.lookahead_seconds = max(30, _int(e.get("LOOKAHEAD_SECONDS"), 180))
        self.preroll_segments = max(1, _int(e.get("PREROLL_SEGMENTS"), 2))
        self.playlist_wait = max(5, _int(e.get("PLAYLIST_WAIT_SEC"), 45))
        self.preset = e.get("X264_PRESET", "veryfast")
        self.crf = _int(e.get("X264_CRF"), 21)
        self.max_kbps = max(1000, _int(e.get("MAX_VIDEO_KBPS"), 8000))
        self.prefer_surround = _truthy(e.get("PREFER_SURROUND", "1"))
        self.log_level = e.get("LOG_LEVEL", "INFO").upper()


# --------------------------------------------------------------------------
# DSM access
# --------------------------------------------------------------------------
class Dsm:
    """Talks to DSM only to check that a session may read a file."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._ok: Dict[tuple, float] = {}
        self._lock = threading.Lock()
        self._ctx = None
        if cfg.dsm_url.startswith("https") and not cfg.verify_tls:
            self._ctx = ssl._create_unverified_context()

    def file_url(self, path: str, sid: str, token: str) -> str:
        q = [("api", "SYNO.FileStation.Download"), ("version", "2"), ("method", "download"),
             ("path", path), ("mode", "open"), ("_sid", sid)]
        if token:
            q.append(("SynoToken", token))
        return "%s/webapi/entry.cgi?%s" % (self.cfg.dsm_url, urllib.parse.urlencode(q, quote_via=urllib.parse.quote))

    def check_access(self, path: str, sid: str, token: str) -> None:
        key = (hashlib.sha256(sid.encode()).hexdigest(), path)
        now = time.time()
        with self._lock:
            if self._ok.get(key, 0) > now:
                return
        req = urllib.request.Request(self.file_url(path, sid, token), headers={"Range": "bytes=0-0"})
        try:
            resp = urllib.request.urlopen(req, timeout=20, context=self._ctx)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ApiError(403, "DSM refused access to the file")
            if exc.code == 404:
                raise ApiError(404, "file not found")
            raise ApiError(502, "DSM returned HTTP %d" % exc.code)
        except (urllib.error.URLError, OSError) as exc:
            raise ApiError(502, "cannot reach DSM at %s: %s" % (self.cfg.dsm_url, exc))
        with resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "json" in ctype:
                try:
                    body = json.loads(resp.read(4096).decode("utf-8", "replace"))
                    code = int((body.get("error") or {}).get("code", 0))
                except Exception:
                    code = 0
                if code in (105, 106, 107, 119):
                    raise ApiError(401, "DSM session is invalid or expired (error %d)" % code)
                if code == 408:
                    raise ApiError(404, "file not found")
                raise ApiError(403, "DSM refused the request (error %d)" % code)
            resp.read(1)
        with self._lock:
            self._ok[key] = now + 120
            if len(self._ok) > 500:
                self._ok = {k: v for k, v in self._ok.items() if v > now}


def clean_path(raw: str) -> str:
    """Validate a FileStation path such as /video/Movies/x.mkv."""
    if not raw or not raw.startswith("/") or "\x00" in raw or len(raw) > 1024:
        raise ApiError(400, "invalid path")
    if any(part == ".." for part in raw.split("/")):
        raise ApiError(400, "invalid path")
    return raw


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------
class Prober:
    def __init__(self, cfg: Config, dsm: Dsm):
        self.cfg = cfg
        self.dsm = dsm
        self._cache: Dict[str, tuple] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._glock = threading.Lock()
        self.opts = planner.HlsOptions(ffmpeg=cfg.ffmpeg, verify_tls=cfg.verify_tls)

    def probe(self, path: str, sid: str, token: str) -> planner.MediaInfo:
        self.dsm.check_access(path, sid, token)
        with self._glock:
            lock = self._locks.setdefault(path, threading.Lock())
            hit = self._cache.get(path)
            if hit and hit[0] > time.time():
                return hit[1]
        with lock:
            hit = self._cache.get(path)
            if hit and hit[0] > time.time():
                return hit[1]
            url = self.dsm.file_url(path, sid, token)
            cmd = [self.cfg.ffprobe, "-v", "error", "-print_format", "json", "-show_format",
                   "-show_streams", "-probesize", "10000000", "-analyzeduration", "10000000",
                   "-rw_timeout", "30000000"] + planner.http_input_options(url, self.opts) + [url]
            try:
                out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
            except subprocess.TimeoutExpired:
                raise ApiError(504, "ffprobe timed out")
            except FileNotFoundError:
                raise ApiError(500, "ffprobe is not installed")
            if out.returncode != 0:
                err = out.stderr.decode("utf-8", "replace").strip()[-300:]
                raise ApiError(502, "ffprobe failed: %s" % _scrub(err))
            try:
                media = planner.parse_probe(json.loads(out.stdout.decode("utf-8", "replace")), path)
            except ValueError:
                raise ApiError(502, "ffprobe returned invalid JSON")
            with self._glock:
                self._cache[path] = (time.time() + 600, media)
                if len(self._cache) > 300:
                    now = time.time()
                    self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            return media


def _scrub(text: str) -> str:
    """Remove session ids / tokens from anything that might be logged or returned."""
    return re.sub(r"(?<![A-Za-z])(_sid|sid|SynoToken|token)=[^&\s\"']+", r"\1=***", text)


# --------------------------------------------------------------------------
# Subtitle extraction (text subtitles -> SRT, shifted to the stream start)
# --------------------------------------------------------------------------
class SubtitleJobs:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = os.path.join(cfg.work_dir, "subs")
        os.makedirs(self.dir, exist_ok=True)
        self._jobs: Dict[str, dict] = {}
        self._by_key: Dict[tuple, str] = {}
        self._lock = threading.Lock()
        self.opts = planner.HlsOptions(ffmpeg=cfg.ffmpeg, verify_tls=cfg.verify_tls)

    def start_cached(self, key, url: str, stream_index: Optional[int]) -> str:
        """Extract a whole subtitle track (no time shift) once, and reuse the result."""
        with self._lock:
            hit = self._by_key.get(key)
            if hit and hit in self._jobs and self._jobs[hit]["state"] != "error":
                return hit
        job_id = self.start(url, stream_index, 0)
        with self._lock:
            self._by_key[key] = job_id
        return job_id

    def start(self, url: str, stream_index: Optional[int], offset: int) -> str:
        job_id = uuid.uuid4().hex
        out = os.path.join(self.dir, job_id + ".srt")
        job = {"state": "pending", "file": out, "created": time.time(), "event": threading.Event()}
        with self._lock:
            self._jobs[job_id] = job
        threading.Thread(target=self._run, args=(job, url, stream_index, offset), daemon=True).start()
        return job_id

    def _run(self, job: dict, url: str, stream_index: Optional[int], offset: int) -> None:
        cmd = [self.cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
        cmd += planner.http_input_options(url, self.opts)
        if offset > 0:
            cmd += ["-ss", str(int(offset))]
        cmd += ["-i", url]
        if stream_index is not None:
            cmd += ["-map", "0:%d" % stream_index]
        cmd += ["-vn", "-an", "-c:s", "srt", "-f", "srt", "-y", job["file"]]
        try:
            res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900)
            if res.returncode == 0 and os.path.exists(job["file"]):
                job["state"] = "ready"
            else:
                log.warning("subtitle extraction failed: %s", _scrub(res.stderr.decode("utf-8", "replace")[-200:]))
                job["state"] = "error"
        except Exception as exc:  # noqa: BLE001
            log.warning("subtitle extraction error: %s", exc)
            job["state"] = "error"
        finally:
            job["event"].set()

    def wait(self, job_id: str, timeout: float) -> Optional[str]:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job:
            return None
        job["event"].wait(timeout)
        return job["file"] if job["state"] == "ready" else None

    def sweep(self, max_age: int = 6 * 3600) -> None:
        now = time.time()
        with self._lock:
            old = [k for k, j in self._jobs.items() if now - j["created"] > max_age]
            for k in old:
                self._by_key = {key: v for key, v in self._by_key.items() if v != k}
                job = self._jobs.pop(k)
                try:
                    os.remove(job["file"])
                except OSError:
                    pass


# --------------------------------------------------------------------------
# HLS sessions (one ffmpeg process each)
# --------------------------------------------------------------------------
class Session:
    def __init__(self, mgr: "SessionManager", owner: str, path: str, media: planner.MediaInfo,
                 plan: planner.Plan, start: int, url: str, burn_pos: Optional[int]):
        self.mgr = mgr
        self.cfg = mgr.cfg
        self.id = uuid.uuid4().hex
        self.owner = owner
        self.path = path
        self.media = media
        self.plan = plan
        self.start = start
        self.url = url
        self.burn_pos = burn_pos
        self.dir = os.path.join(self.cfg.work_dir, self.id)
        self.created = time.time()
        self.last_access = self.created
        self.lock = threading.RLock()
        self.proc: Optional[subprocess.Popen] = None
        self.logf = None
        self.exited = False
        self.rc: Optional[int] = None
        self.paused = False
        self.stopped = False
        self.max_served = -1
        self.used_hw = False
        self.hw_failed = False
        self.lookahead_segments = max(6, self.cfg.lookahead_seconds // self.cfg.segment_seconds)

    # -- process control ---------------------------------------------------
    def launch(self, use_hw: bool) -> None:
        with self.lock:
            os.makedirs(self.dir, exist_ok=True)
            for name in os.listdir(self.dir):
                if name.startswith("seg_") or name.startswith("index"):
                    try:
                        os.remove(os.path.join(self.dir, name))
                    except OSError:
                        pass
            cmd = planner.build_hls_command(self.mgr.hls_opts, self.url, self.media, self.plan,
                                            self.start, self.dir, use_hw=use_hw,
                                            burn_stream_pos=self.burn_pos)
            self.used_hw = "h264_vaapi" in cmd
            log.info("session %s: %s (video=%s audio=%s%s) start=%ds", self.id[:8], self.plan.mode,
                     self.plan.video_action, self.plan.audio_action,
                     ", vaapi" if self.used_hw else "", self.start)
            log.debug("session %s command: %s", self.id[:8], " ".join(_scrub(c) for c in cmd))
            if self.logf:
                self.logf.close()
            self.logf = open(os.path.join(self.dir, "ffmpeg.log"), "wb")
            self.exited = False
            self.rc = None
            self.paused = False
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=self.logf)

    def stop(self) -> None:
        with self.lock:
            if self.stopped:
                return
            self.stopped = True
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.kill()
                except OSError:
                    pass
                try:
                    self.proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
            if self.logf:
                try:
                    self.logf.close()
                except OSError:
                    pass
            shutil.rmtree(self.dir, ignore_errors=True)

    def touch(self) -> None:
        self.last_access = time.time()

    def note_served(self, number: int) -> None:
        if number > self.max_served:
            self.max_served = number

    def count_segments(self) -> int:
        try:
            return sum(1 for n in os.listdir(self.dir) if n.startswith("seg_") and n.endswith(".ts"))
        except OSError:
            return 0

    def tick(self) -> None:
        """Called twice a second: pause ffmpeg when far ahead, handle exits."""
        with self.lock:
            p = self.proc
            if p is None or self.stopped:
                return
            rc = p.poll()
            if rc is not None:
                if not self.exited:
                    self.exited = True
                    self.rc = rc
                    if rc != 0 and self.used_hw and not self.hw_failed and self.count_segments() == 0:
                        self.hw_failed = True
                        log.warning("session %s: VAAPI encode failed, retrying in software", self.id[:8])
                        self.launch(use_hw=False)
                        return
                    log.info("session %s: ffmpeg exited rc=%s", self.id[:8], rc)
                return
            ahead = self.count_segments() - (self.max_served + 1)
            if not self.paused and ahead >= self.lookahead_segments:
                self.paused = True
                p.send_signal(signal.SIGSTOP)
            elif self.paused and ahead <= max(2, self.lookahead_segments - 6):
                self.paused = False
                p.send_signal(signal.SIGCONT)

    # -- playlist ----------------------------------------------------------
    def _read_playlist(self) -> str:
        try:
            with open(os.path.join(self.dir, "index.m3u8"), "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            return ""
        return text if text.startswith("#EXTM3U") else ""

    def playlist_ready(self) -> bool:
        text = self._read_playlist()
        if not text:
            return False
        count = text.count("#EXTINF")
        if "#EXT-X-ENDLIST" in text:
            return True
        return count >= self.cfg.preroll_segments or (self.exited and count > 0)

    def failure_detail(self) -> str:
        try:
            with open(os.path.join(self.dir, "ffmpeg.log"), "rb") as fh:
                return _scrub(fh.read()[-600:].decode("utf-8", "replace")).strip()
        except OSError:
            return ""

    def playlist_text(self) -> str:
        text = self._read_playlist()
        if self.exited and text and "#EXT-X-ENDLIST" not in text:
            text = text.rstrip("\n") + "\n#EXT-X-ENDLIST\n"
        return text

    def wait_playlist(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline and not self.stopped:
            if self.playlist_ready():
                return True
            if self.exited and self.rc not in (0, None) and self.count_segments() == 0 \
                    and (self.hw_failed or not self.used_hw):
                return False
            time.sleep(0.2)
        return False

    def status(self) -> dict:
        return {"id": self.id[:8], "mode": self.plan.mode, "age": int(time.time() - self.created),
                "paused": self.paused, "segments": self.count_segments(), "served": self.max_served,
                "exited": self.exited, "rc": self.rc, "vaapi": self.used_hw}


class SessionManager:
    def __init__(self, cfg: Config, hls_opts: planner.HlsOptions):
        self.cfg = cfg
        self.hls_opts = hls_opts
        self._sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, owner: str, path: str, media: planner.MediaInfo, plan: planner.Plan,
               start: int, url: str, burn_pos: Optional[int]) -> Session:
        with self._lock:
            # A client only ever plays one thing: drop its earlier session for the same file.
            for sess in [s for s in self._sessions.values() if s.owner == owner and s.path == path]:
                self._drop(sess)
            while len(self._sessions) >= self.cfg.max_sessions:
                oldest = min(self._sessions.values(), key=lambda s: s.last_access)
                log.info("session limit reached, stopping %s", oldest.id[:8])
                self._drop(oldest)
            sess = Session(self, owner, path, media, plan, start, url, burn_pos)
            self._sessions[sess.id] = sess
        try:
            sess.launch(use_hw=self.hls_opts.use_vaapi)
        except OSError as exc:
            with self._lock:
                self._drop(sess)
            raise ApiError(500, "cannot start ffmpeg: %s" % exc)
        return sess

    def _drop(self, sess: Session) -> None:
        self._sessions.pop(sess.id, None)
        threading.Thread(target=sess.stop, daemon=True).start()

    def get(self, sess_id: str) -> Optional[Session]:
        with self._lock:
            return self._sessions.get(sess_id)

    def stop(self, sess_id: str) -> bool:
        with self._lock:
            sess = self._sessions.get(sess_id)
            if not sess:
                return False
            self._drop(sess)
        return True

    def stop_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for sess in sessions:
            sess.stop()

    def sweep(self) -> None:
        now = time.time()
        with self._lock:
            sessions = list(self._sessions.values())
        for sess in sessions:
            sess.tick()
            if now - sess.last_access > self.cfg.idle_timeout:
                log.info("session %s idle, stopping", sess.id[:8])
                with self._lock:
                    self._drop(sess)

    def snapshot(self) -> List[dict]:
        with self._lock:
            return [s.status() for s in self._sessions.values()]


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------
class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        os.makedirs(cfg.work_dir, exist_ok=True)
        self.dsm = Dsm(cfg)
        self.prober = Prober(cfg, self.dsm)
        self.subs = SubtitleJobs(cfg)
        self.have_zscale = self._has_filters("zscale", "tonemap")
        use_vaapi = cfg.hwaccel == "vaapi"
        if use_vaapi and not os.path.exists(cfg.vaapi_device):
            log.warning("HWACCEL=vaapi but %s does not exist, using software encoding", cfg.vaapi_device)
            use_vaapi = False
        self.hls_opts = planner.HlsOptions(
            ffmpeg=cfg.ffmpeg, preset=cfg.preset, crf=cfg.crf, segment_seconds=cfg.segment_seconds,
            use_vaapi=use_vaapi, vaapi_device=cfg.vaapi_device, verify_tls=cfg.verify_tls)
        self.plan_opts = planner.PlanOptions(max_video_kbps=cfg.max_kbps,
                                             prefer_surround=cfg.prefer_surround,
                                             have_zscale=self.have_zscale)
        self.sessions = SessionManager(cfg, self.hls_opts)
        self._ffmpeg_version = None
        self._dsm_checked = (0.0, False)
        self._stop = threading.Event()
        self._sweeper = threading.Thread(target=self._sweep_loop, daemon=True)

    def _has_filters(self, *names: str) -> bool:
        try:
            out = subprocess.run([self.cfg.ffmpeg, "-hide_banner", "-filters"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=20).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            return False
        return all(re.search(r"\s%s\s" % re.escape(n), out) for n in names)

    def start_background(self) -> None:
        self._sweeper.start()

    def _sweep_loop(self) -> None:
        n = 0
        while not self._stop.wait(0.5):
            try:
                self.sessions.sweep()
                n += 1
                if n % 1200 == 0:
                    self.subs.sweep()
            except Exception:  # noqa: BLE001
                log.exception("sweeper error")

    def shutdown(self) -> None:
        self._stop.set()
        self.sessions.stop_all()

    # -- API ---------------------------------------------------------------
    def ffmpeg_version(self) -> str:
        if self._ffmpeg_version is None:
            try:
                out = subprocess.run([self.cfg.ffmpeg, "-version"], stdout=subprocess.PIPE, timeout=10)
                self._ffmpeg_version = out.stdout.decode("utf-8", "replace").splitlines()[0]
            except (OSError, subprocess.SubprocessError, IndexError):
                self._ffmpeg_version = "not found"
        return self._ffmpeg_version

    def dsm_reachable(self) -> bool:
        """Cheap check that DSM answers on DSM_URL (cached for 30 s)."""
        now = time.time()
        if now < self._dsm_checked[0]:
            return self._dsm_checked[1]
        url = self.cfg.dsm_url + "/webapi/query.cgi?api=SYNO.API.Info&version=1&method=query&query=SYNO.API.Auth"
        ok = False
        try:
            with urllib.request.urlopen(url, timeout=3, context=self.dsm._ctx) as resp:
                ok = b'"success"' in resp.read(2048)
        except (urllib.error.URLError, OSError, ValueError):
            ok = False
        self._dsm_checked = (now + 30, ok)
        return ok

    def api_health(self) -> dict:
        return {"ok": True, "name": "ds-video-stream", "version": VERSION, "ffmpeg": self.ffmpeg_version(),
                "vaapi": self.hls_opts.use_vaapi, "tonemap": self.have_zscale,
                "dsm": {"url": self.cfg.dsm_url, "reachable": self.dsm_reachable()},
                "sessions": self.sessions.snapshot()}

    def _auth(self, params: Dict[str, str]):
        sid = params.get("sid") or params.get("_sid") or ""
        token = params.get("token") or params.get("SynoToken") or ""
        if not sid:
            raise ApiError(401, "missing sid")
        path = clean_path(params.get("path", ""))
        return path, sid, token

    def api_plan(self, params: Dict[str, str]) -> dict:
        path, sid, token = self._auth(params)
        media = self.prober.probe(path, sid, token)
        caps = planner.parse_caps(params.get("caps"))
        force = params.get("mode") if params.get("mode") in ("remux", "transcode") else None
        plan = planner.decide(media, caps, self.plan_opts, force=force,
                              audio_index=_opt_int(params.get("audio")))
        # Can the file be remuxed (video copied into HLS) if direct play turns out not to work?
        can_remux = plan.mode == "remux" or planner.decide(
            media, caps, self.plan_opts, force="remux", audio_index=_opt_int(params.get("audio"))).mode == "remux"
        return {"ok": True, "version": VERSION, "mode": plan.mode, "can_remux": can_remux,
                "reasons": plan.reasons, "duration": media.duration, "plan": plan.to_json(),
                "media": media.to_json(), "caps": caps.to_json()}

    def api_start(self, params: Dict[str, str]) -> dict:
        path, sid, token = self._auth(params)
        media = self.prober.probe(path, sid, token)
        caps = planner.parse_caps(params.get("caps"))
        mode = params.get("mode", "auto")
        force = mode if mode in ("remux", "transcode") else None
        burn = _opt_int(params.get("burn"))
        burn_pos = None
        if burn is not None:
            for pos, sub in enumerate(media.subs):
                if sub.index == burn:
                    burn_pos = pos
            if burn_pos is None or media.subs[burn_pos].text:
                raise ApiError(400, "burn must be the index of a bitmap subtitle stream")
        plan = planner.decide(media, caps, self.plan_opts, force=force,
                              audio_index=_opt_int(params.get("audio")), burn_index=burn)
        if plan.mode == "direct":
            raise ApiError(409, "this file can be played directly, no stream needed")
        start = max(0, _int(params.get("start"), 0))
        if media.duration and start >= media.duration - 5:
            start = 0

        url = self.dsm.file_url(path, sid, token)
        owner = hashlib.sha256(sid.encode()).hexdigest()[:16]
        sess = self.sessions.create(owner, path, media, plan, start, url, burn_pos)

        subtitle_url = ""
        sidecar = params.get("sidecar", "")
        if params.get("nosub") == "1":
            # The app draws subtitles itself and asks for tracks through /api/sub.
            sidecar = ""
            emb = None
        else:
            emb = planner.pick_embedded_subtitle(media)
        if sidecar:
            # The app may guess a sidecar name that does not exist. That must never stop playback.
            try:
                sidecar = clean_path(sidecar)
                self.dsm.check_access(sidecar, sid, token)
            except ApiError as exc:
                log.info("ignoring sidecar subtitle: %s", exc.message)
                sidecar = ""
        if sidecar:
            job = self.subs.start(self.dsm.file_url(sidecar, sid, token), None, start)
            subtitle_url = "/sub/%s.srt" % job
        elif emb is not None and burn is None:
            job = self.subs.start(url, emb.index, start)
            subtitle_url = "/sub/%s.srt" % job
        default_sub = planner.pick_embedded_subtitle(media)
        info = media.to_json()
        return {"ok": True, "session": sess.id, "url": "/s/%s/index.m3u8" % sess.id, "mode": plan.mode,
                "offset": start, "duration": media.duration, "reasons": plan.reasons,
                "subtitle_url": subtitle_url,
                "audio": info["audio"], "audio_index": plan.audio_index,
                "subs": info["subtitles"], "default_subtitle": default_sub.index if default_sub else None}

    def api_sub(self, params: Dict[str, str]) -> dict:
        """Start extracting one whole subtitle track (embedded or a file next to the video) to SRT."""
        path, sid, token = self._auth(params)
        sidecar = params.get("sidecar", "")
        if sidecar:
            sidecar = clean_path(sidecar)
            self.dsm.check_access(sidecar, sid, token)
            job = self.subs.start_cached(("file", sidecar), self.dsm.file_url(sidecar, sid, token), None)
        else:
            index = _opt_int(params.get("index"))
            if index is None:
                raise ApiError(400, "give index or sidecar")
            media = self.prober.probe(path, sid, token)
            chosen = [s for s in media.subs if s.index == index and s.text]
            if not chosen:
                raise ApiError(400, "index is not a text subtitle stream of this file")
            job = self.subs.start_cached(("stream", path, index), self.dsm.file_url(path, sid, token), index)
        return {"ok": True, "url": "/sub/%s.srt" % job}

    def api_stop(self, params: Dict[str, str]) -> dict:
        sess_id = params.get("session", "")
        if not re.fullmatch(r"[0-9a-f]{32}", sess_id):
            raise ApiError(400, "invalid session")
        return {"ok": True, "stopped": self.sessions.stop(sess_id)}


def _opt_int(value) -> Optional[int]:
    if value in (None, "", "-1"):
        return None
    try:
        return int(value)
    except ValueError:
        return None


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "DSVStream/" + VERSION
    protocol_version = "HTTP/1.1"
    app: App = None  # set by make_server

    def log_message(self, fmt, *args):  # never log query strings (they contain the sid)
        text = fmt % args if args else fmt
        text = re.sub(r"\?\S*", "?...", text, count=1)
        line = "%s %s" % (self.address_string(), _scrub(text))
        # Requests for the API are worth seeing; the steady stream of segment downloads is not.
        if " /s/" in text or " /sub/" in text:
            log.debug("%s", line)
        else:
            log.info("%s", line)

    # -- helpers -----------------------------------------------------------
    def _send(self, status: int, ctype: str, body: bytes, head: bool = False, extra=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _json(self, status: int, payload: dict, head: bool = False) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"), head)

    def _error(self, exc: ApiError, head: bool = False) -> None:
        self._json(exc.status, {"ok": False, "error": exc.message}, head)

    def do_HEAD(self):  # noqa: N802
        self._dispatch(True)

    def do_GET(self):  # noqa: N802
        self._dispatch(False)

    def _dispatch(self, head: bool) -> None:
        try:
            parts = urllib.parse.urlsplit(self.path)
            params = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query, keep_blank_values=True).items()}
            path = parts.path
            app = self.app
            if path == "/":
                self._send(200, "text/plain; charset=utf-8",
                           ("DS Video stream server %s\n" % VERSION).encode(), head)
            elif path == "/api/health":
                self._json(200, app.api_health(), head)
            elif path == "/api/plan":
                self._json(200, app.api_plan(params), head)
            elif path == "/api/start":
                self._json(200, app.api_start(params), head)
            elif path == "/api/stop":
                self._json(200, app.api_stop(params), head)
            elif path == "/api/sub":
                self._json(200, app.api_sub(params), head)
            else:
                m = re.fullmatch(r"/s/([0-9a-f]{32})/([A-Za-z0-9_.\-]+)", path)
                s = re.fullmatch(r"/sub/([0-9a-f]{32})\.srt", path)
                if m:
                    self._serve_session_file(m.group(1), m.group(2), head)
                elif s:
                    self._serve_subtitle(s.group(1), head)
                else:
                    raise ApiError(404, "not found")
        except ApiError as exc:
            self._error(exc, head)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("unhandled error")
            try:
                self._json(500, {"ok": False, "error": "internal error"}, head)
            except OSError:
                pass

    # -- files ---------------------------------------------------------------
    def _serve_subtitle(self, job_id: str, head: bool) -> None:
        file = self.app.subs.wait(job_id, 120)
        if not file:
            raise ApiError(404, "subtitle not available")
        with open(file, "rb") as fh:
            body = fh.read()
        self._send(200, "application/x-subrip; charset=utf-8", body, head)

    def _serve_session_file(self, sess_id: str, name: str, head: bool) -> None:
        sess = self.app.sessions.get(sess_id)
        if sess is None:
            raise ApiError(404, "unknown or expired session")
        sess.touch()
        if name == "index.m3u8":
            if not sess.wait_playlist(self.app.cfg.playlist_wait):
                detail = sess.failure_detail()
                raise ApiError(502, "ffmpeg did not produce a playlist" + (": " + detail if detail else ""))
            self._send(200, "application/vnd.apple.mpegurl", sess.playlist_text().encode("utf-8"), head)
            return
        seg = re.fullmatch(r"seg_(\d{5})\.ts", name)
        if not seg:
            raise ApiError(404, "not found")
        number = int(seg.group(1))
        sess.note_served(number)
        full = os.path.join(sess.dir, name)
        deadline = time.time() + 40
        while not os.path.exists(full):
            if sess.stopped or time.time() > deadline or (sess.exited and not os.path.exists(full)):
                raise ApiError(404, "segment not available")
            sess.touch()
            time.sleep(0.15)
        self._send_file(full, "video/mp2t", head)

    def _send_file(self, full: str, ctype: str, head: bool) -> None:
        try:
            size = os.path.getsize(full)
            fh = open(full, "rb")
        except OSError:
            raise ApiError(404, "segment not available")
        with fh:
            start, end, status = 0, size - 1, 200
            rng = self.headers.get("Range", "")
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip()) if rng else None
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(size - 1, int(m.group(2)))
                else:
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            if status == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            if head:
                return
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def make_server(cfg: Config) -> "tuple[Server, App]":
    app = App(cfg)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = Server((cfg.host, cfg.port), handler)
    app.start_background()
    return server, app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="DS Video stream server")
    parser.add_argument("--check", action="store_true", help="print the detected configuration and exit")
    args = parser.parse_args(argv)
    cfg = Config()
    logging.basicConfig(level=getattr(logging, cfg.log_level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    if shutil.which(cfg.ffmpeg) is None or shutil.which(cfg.ffprobe) is None:
        log.error("ffmpeg/ffprobe not found (FFMPEG_BIN=%s FFPROBE_BIN=%s)", cfg.ffmpeg, cfg.ffprobe)
        return 2
    # Start clean: leftovers from a previous run are never reusable.
    shutil.rmtree(cfg.work_dir, ignore_errors=True)
    server, app = make_server(cfg)
    if args.check:
        print(json.dumps(app.api_health(), indent=2))
        server.server_close()
        app.shutdown()
        return 0
    log.info("DS Video stream server %s listening on %s:%d (DSM %s, vaapi=%s, tonemap=%s)",
             VERSION, cfg.host, cfg.port, cfg.dsm_url, app.hls_opts.use_vaapi, app.have_zscale)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
