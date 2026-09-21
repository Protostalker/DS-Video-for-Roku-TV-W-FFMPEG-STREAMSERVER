"""Pure decision logic for the DS Video stream server.

Nothing in here touches the network or spawns processes, so it can be unit
tested in isolation:

  parse_caps()      turns the Roku's capability string into a Caps object
  parse_probe()     turns ffprobe JSON into a MediaInfo object
  decide()          picks direct play / remux / transcode and the audio action
  build_hls_command() builds the ffmpeg command line for a decision

Terminology used everywhere:

  direct     the Roku plays the original file untouched (FileStation download)
  remux      video stream is copied bit for bit into HLS, audio is copied or
             converted to something the Roku can play (cheap on the CPU)
  transcode  video is re-encoded to H.264 (expensive), audio as above
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Set, Tuple

BITMAP_SUBS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub", "dvb_teletext"}
ENGLISH = {"en", "eng", "english"}

# Containers the Roku is allowed to play natively, mapped to a container class.
DIRECT_CONTAINERS = {"mkv": "mkv", "mp4": "mp4", "m4v": "mp4", "mov": "mp4", "webm": "webm"}
# What each container class may carry when played natively.
DIRECT_VIDEO = {"mkv": {"h264", "hevc", "vp9"}, "mp4": {"h264", "hevc"}, "webm": {"vp9"}}
DIRECT_AUDIO = {
    "mkv": {"aac", "ac3", "eac3", "mp3", "flac", "dts", "opus", "vorbis", "alac"},
    "mp4": {"aac", "ac3", "eac3", "mp3", "alac"},
    "webm": {"opus", "vorbis"},
}
# Audio codecs HLS on a Roku can carry.
HLS_AUDIO = {"aac", "ac3", "eac3"}
# Video codecs we can place into HLS MPEG-TS without re-encoding.
HLS_COPY_VIDEO = {"h264", "hevc"}


# --------------------------------------------------------------------------
# Capabilities reported by the Roku
# --------------------------------------------------------------------------
@dataclass
class Caps:
    video: Set[str] = field(default_factory=lambda: {"h264"})
    audio: Set[str] = field(default_factory=lambda: {"aac", "ac3", "eac3", "mp3"})
    containers: Set[str] = field(default_factory=lambda: {"mp4", "mkv"})
    hdr: Set[str] = field(default_factory=set)
    max_height: int = 1080
    h264_level: int = 41          # x10, 41 == level 4.1
    aac_multichannel: bool = False
    known: bool = False           # False when the client sent no capabilities

    def to_json(self) -> dict:
        d = asdict(self)
        for k in ("video", "audio", "containers", "hdr"):
            d[k] = sorted(d[k])
        return d


def _csv(value: str) -> Set[str]:
    return {p.strip().lower() for p in value.split(",") if p.strip()}


def parse_caps(raw: Optional[str]) -> Caps:
    """Parse ``v=h264,hevc;a=aac,ac3;c=mp4,mkv;h=1080;l=42;aac6=0;hdr=hdr10``.

    Unknown keys are ignored, so newer clients can add fields safely.
    """
    caps = Caps()
    if not raw:
        return caps
    caps.known = True
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "v":
            caps.video = _csv(value)
        elif key == "a":
            caps.audio = _csv(value)
        elif key == "c":
            caps.containers = _csv(value)
        elif key == "hdr":
            caps.hdr = _csv(value)
        elif key == "h":
            caps.max_height = _to_int(value, caps.max_height)
        elif key == "l":
            caps.h264_level = _to_int(value, caps.h264_level)
        elif key == "aac6":
            caps.aac_multichannel = value in ("1", "true", "yes")
    if "hevc10" in caps.video:
        caps.video.add("hevc")
    return caps


# --------------------------------------------------------------------------
# ffprobe -> MediaInfo
# --------------------------------------------------------------------------
@dataclass
class VideoInfo:
    index: int
    codec: str
    profile: str = ""
    level: int = 0                # x10
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pix_fmt: str = ""
    bit_depth: int = 8
    interlaced: bool = False
    hdr: str = "sdr"              # sdr | hdr10 | hlg | dv
    dv_profile: int = 0
    bitrate: int = 0


@dataclass
class AudioInfo:
    index: int
    codec: str
    channels: int = 2
    profile: str = ""
    lang: str = ""
    title: str = ""
    default: bool = False
    bitrate: int = 0


@dataclass
class SubInfo:
    index: int
    codec: str
    lang: str = ""
    title: str = ""
    default: bool = False
    forced: bool = False

    @property
    def text(self) -> bool:
        return self.codec not in BITMAP_SUBS


@dataclass
class MediaInfo:
    path: str
    ext: str = ""
    container: str = ""
    duration: float = 0.0
    size: int = 0
    bitrate: int = 0
    video: Optional[VideoInfo] = None
    audios: List[AudioInfo] = field(default_factory=list)
    subs: List[SubInfo] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "container": self.container,
            "ext": self.ext,
            "duration": round(self.duration, 3),
            "size": self.size,
            "bitrate": self.bitrate,
            "video": asdict(self.video) if self.video else None,
            "audio": [asdict(a) for a in self.audios],
            "subtitles": [dict(asdict(s), text=s.text) for s in self.subs],
        }


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fps(value: str) -> float:
    if not value or value == "0/0":
        return 0.0
    if "/" in value:
        num, _, den = value.partition("/")
        den_f = _to_float(den, 1.0) or 1.0
        return _to_float(num) / den_f
    return _to_float(value)


def _bit_depth(pix_fmt: str, bits_per_raw) -> int:
    depth = _to_int(bits_per_raw, 0)
    if depth:
        return depth
    match = re.search(r"p(9|10|12|14|16)(?:le|be)?$", pix_fmt or "")
    if match:
        return int(match.group(1))
    if (pix_fmt or "").startswith("p010"):
        return 10
    return 8


def _norm_lang(lang: str) -> str:
    return (lang or "").strip().lower()


def parse_probe(data: dict, path: str) -> MediaInfo:
    fmt = data.get("format") or {}
    ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    media = MediaInfo(
        path=path,
        ext=ext,
        container=fmt.get("format_name", ""),
        duration=_to_float(fmt.get("duration")),
        size=_to_int(fmt.get("size")),
        bitrate=_to_int(fmt.get("bit_rate")),
    )
    for st in data.get("streams") or []:
        kind = st.get("codec_type")
        disp = st.get("disposition") or {}
        tags = {str(k).lower(): v for k, v in (st.get("tags") or {}).items()}
        idx = _to_int(st.get("index"))
        codec = (st.get("codec_name") or "").lower()
        if kind == "video":
            if disp.get("attached_pic") or media.video is not None:
                continue
            level = _to_int(st.get("level"), 0)
            if codec == "hevc" and level > 0:
                level = round(level / 3)
            transfer = st.get("color_transfer", "")
            hdr = "sdr"
            if transfer == "smpte2084":
                hdr = "hdr10"
            elif transfer == "arib-std-b67":
                hdr = "hlg"
            dv_profile = 0
            for sd in st.get("side_data_list") or []:
                if "DOVI" in str(sd.get("side_data_type", "")):
                    hdr = "dv"
                    dv_profile = _to_int(sd.get("dv_profile"))
            pix_fmt = st.get("pix_fmt", "")
            media.video = VideoInfo(
                index=idx,
                codec=codec,
                profile=st.get("profile", "") or "",
                level=level,
                width=_to_int(st.get("width")),
                height=_to_int(st.get("height")),
                fps=_fps(st.get("avg_frame_rate") or st.get("r_frame_rate") or ""),
                pix_fmt=pix_fmt,
                bit_depth=_bit_depth(pix_fmt, st.get("bits_per_raw_sample")),
                interlaced=st.get("field_order", "progressive") in ("tt", "bb", "tb", "bt"),
                hdr=hdr,
                dv_profile=dv_profile,
                bitrate=_to_int(st.get("bit_rate")),
            )
        elif kind == "audio":
            media.audios.append(AudioInfo(
                index=idx,
                codec=codec,
                channels=_to_int(st.get("channels"), 2) or 2,
                profile=st.get("profile", "") or "",
                lang=_norm_lang(tags.get("language", "")),
                title=str(tags.get("title", "")),
                default=bool(disp.get("default")),
                bitrate=_to_int(st.get("bit_rate")),
            ))
        elif kind == "subtitle":
            media.subs.append(SubInfo(
                index=idx,
                codec=codec,
                lang=_norm_lang(tags.get("language", "")),
                title=str(tags.get("title", "")),
                default=bool(disp.get("default")),
                forced=bool(disp.get("forced")),
            ))
    return media


# --------------------------------------------------------------------------
# Decision
# --------------------------------------------------------------------------
@dataclass
class PlanOptions:
    max_video_kbps: int = 8000
    prefer_surround: bool = True
    have_zscale: bool = False


@dataclass
class Plan:
    mode: str                      # direct | remux | transcode
    reasons: List[str] = field(default_factory=list)
    video_action: str = "copy"     # copy | encode
    audio_action: str = "copy"     # copy | aac | ac3 | none
    audio_index: Optional[int] = None
    audio_channels: int = 2        # output channels when audio is encoded
    scale_height: int = 0          # 0 = keep
    deinterlace: bool = False
    tonemap: bool = False
    burn_index: Optional[int] = None
    max_kbps: int = 0

    def to_json(self) -> dict:
        return asdict(self)


def choose_audio(audios: List[AudioInfo], want: Optional[int] = None) -> Optional[AudioInfo]:
    """Pick the audio track. ``want`` is an absolute ffprobe stream index."""
    if not audios:
        return None
    if want is not None:
        for a in audios:
            if a.index == want:
                return a
    for a in audios:
        if a.default:
            return a
    return audios[0]


def video_decodable(v: VideoInfo, caps: Caps) -> Tuple[bool, str]:
    """Can the device decode this video stream natively?"""
    if v.codec == "h264":
        if v.pix_fmt not in ("yuv420p", "yuvj420p", ""):
            return False, "H.264 %s (%d-bit) is not decodable on Roku" % (v.pix_fmt or "?", v.bit_depth)
        if v.bit_depth > 8:
            return False, "10-bit H.264 is not decodable on Roku"
        if "h264" not in caps.video:
            return False, "device does not report H.264 support"
        if v.level > 0 and v.level > caps.h264_level:
            return False, "H.264 level %.1f is above device limit %.1f" % (v.level / 10.0, caps.h264_level / 10.0)
    elif v.codec == "hevc":
        if "hevc" not in caps.video:
            return False, "device does not decode HEVC (non-4K Roku)"
        if v.bit_depth > 8 and "hevc10" not in caps.video:
            return False, "device does not decode 10-bit HEVC"
        if v.pix_fmt and not v.pix_fmt.startswith(("yuv420p", "p010")):
            return False, "HEVC chroma format %s is not supported" % v.pix_fmt
        if v.hdr == "dv" and v.dv_profile == 5 and "dv" not in caps.hdr:
            return False, "Dolby Vision profile 5 needs a Dolby Vision device"
    elif v.codec == "vp9":
        if "vp9" not in caps.video:
            return False, "device does not report VP9 support"
    else:
        return False, "video codec %s is not decodable on Roku" % (v.codec or "unknown")
    if v.height > caps.max_height:
        return False, "video height %d exceeds device limit %d" % (v.height, caps.max_height)
    if v.fps > 60.5:
        return False, "frame rate %.1f is above 60" % v.fps
    return True, ""


def _direct_audio_ok(a: AudioInfo, container: str, caps: Caps) -> Tuple[bool, str]:
    codec = a.codec
    if codec not in DIRECT_AUDIO.get(container, set()):
        return False, "audio %s cannot be played from a %s file" % (codec, container)
    if codec not in caps.audio:
        return False, "device does not report %s support" % codec
    if codec == "aac" and a.channels > 2 and not caps.aac_multichannel:
        return False, "multichannel AAC is not supported on this Roku"
    if codec == "dts" and a.profile and a.profile.upper() != "DTS" and not a.profile.upper().startswith("DTS-ES"):
        return False, "%s cannot be passed through" % a.profile
    return True, ""


def _plan_audio(a: Optional[AudioInfo], caps: Caps, opts: PlanOptions, plan: Plan) -> None:
    """Fill in the audio part of a remux/transcode plan (HLS output)."""
    if a is None:
        plan.audio_action = "none"
        return
    plan.audio_index = a.index
    codec, ch = a.codec, a.channels
    if codec == "aac" and (ch <= 2 or caps.aac_multichannel):
        plan.audio_action = "copy"
        return
    if codec == "ac3" and "ac3" in caps.audio:
        plan.audio_action = "copy"
        return
    if codec == "eac3" and "eac3" in caps.audio:
        plan.audio_action = "copy"
        return
    if ch > 2 and "ac3" in caps.audio and opts.prefer_surround:
        plan.audio_action = "ac3"
        plan.audio_channels = min(ch, 6)
        plan.reasons.append("audio %s %dch -> AC3 %dch" % (codec, ch, plan.audio_channels))
    else:
        plan.audio_action = "aac"
        plan.audio_channels = 2
        plan.reasons.append("audio %s %dch -> AAC stereo" % (codec, ch))


def decide(media: MediaInfo, caps: Caps, opts: Optional[PlanOptions] = None,
           force: Optional[str] = None, audio_index: Optional[int] = None,
           burn_index: Optional[int] = None) -> Plan:
    """Decide how to serve ``media`` to a device with ``caps``.

    ``force`` may be ``"remux"`` or ``"transcode"`` to skip cheaper options
    (the app uses this when a cheaper method already failed on the device).
    """
    opts = opts or PlanOptions()
    v = media.video
    audio = choose_audio(media.audios, audio_index)
    auto_audio = choose_audio(media.audios, None)
    plan = Plan(mode="transcode", audio_index=audio.index if audio else None)

    if v is None:
        # Audio only file: nothing to copy, let HLS carry converted audio.
        plan.mode = "remux"
        plan.video_action = "copy"
        _plan_audio(audio, caps, opts, plan)
        plan.reasons.append("no video stream")
        return plan

    decodable, why_not = video_decodable(v, caps)
    if not decodable:
        plan.reasons.append(why_not)
    can_copy = decodable and v.codec in HLS_COPY_VIDEO
    if decodable and not can_copy:
        plan.reasons.append("%s cannot be copied into HLS" % v.codec)

    # ---- direct play -------------------------------------------------
    container = DIRECT_CONTAINERS.get(media.ext)
    direct_ok = False
    direct_why = ""
    if not decodable:
        direct_why = why_not
    elif container is None:
        direct_why = "container .%s is not natively playable" % (media.ext or "?")
    elif container not in caps.containers:
        direct_why = "device does not report %s container support" % container
    elif v.codec not in DIRECT_VIDEO.get(container, set()):
        direct_why = "%s video cannot be played from a %s file" % (v.codec, container)
    elif audio is not None and audio is not auto_audio:
        direct_why = "a non-default audio track was requested"
    elif audio is not None:
        ok, direct_why = _direct_audio_ok(audio, container, caps)
        direct_ok = ok
    else:
        direct_ok = True

    if force is None and burn_index is None and direct_ok:
        plan.mode = "direct"
        plan.video_action = "copy"
        plan.audio_action = "copy"
        plan.reasons = ["compatible with the device, playing the original file"]
        return plan
    if direct_why:
        plan.reasons.append(direct_why)

    # ---- remux ---------------------------------------------------------
    if force != "transcode" and burn_index is None and can_copy:
        plan.mode = "remux"
        plan.video_action = "copy"
        _plan_audio(audio, caps, opts, plan)
        return plan

    # ---- transcode -----------------------------------------------------
    plan.mode = "transcode"
    plan.video_action = "encode"
    if burn_index is not None:
        plan.burn_index = burn_index
        plan.reasons.append("burning in subtitle stream %d" % burn_index)
    if force == "transcode" and can_copy:
        plan.reasons.append("re-encoding because a cheaper method failed on the device")
    _plan_audio(audio, caps, opts, plan)
    plan.deinterlace = v.interlaced
    plan.tonemap = v.hdr in ("hdr10", "hlg", "dv") and opts.have_zscale
    if v.height > 1080:
        plan.scale_height = 1080
        plan.reasons.append("scaling %dp -> 1080p" % v.height)
    if v.height and v.height <= 480:
        plan.max_kbps = min(opts.max_video_kbps, 2500)
    elif v.height and v.height <= 720:
        plan.max_kbps = min(opts.max_video_kbps, 5000)
    else:
        plan.max_kbps = opts.max_video_kbps
    return plan


def pick_embedded_subtitle(media: MediaInfo) -> Optional[SubInfo]:
    """Pick one embedded *text* subtitle to expose to the Roku, or None."""
    text = [s for s in media.subs if s.text]
    if not text:
        return None
    english = [s for s in text if s.lang in ENGLISH]
    pool = english or (text if len(text) == 1 else [])
    if not pool:
        return None
    for s in pool:
        if not s.forced and "sdh" not in s.title.lower() and "commentary" not in s.title.lower():
            return s
    return pool[0]


# --------------------------------------------------------------------------
# ffmpeg command line
# --------------------------------------------------------------------------
@dataclass
class HlsOptions:
    ffmpeg: str = "ffmpeg"
    preset: str = "veryfast"
    crf: int = 21
    segment_seconds: int = 6
    use_vaapi: bool = False
    vaapi_device: str = "/dev/dri/renderD128"
    verify_tls: bool = False


def http_input_options(url: str, opts: HlsOptions) -> List[str]:
    if not url.lower().startswith(("http://", "https://")):
        return []
    # NB: do not add -reconnect_at_eof: a normal end of file would be retried forever.
    out = ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
    if url.lower().startswith("https://") and not opts.verify_tls:
        out += ["-tls_verify", "0"]
    return out


def _even(n: float) -> int:
    n = int(round(n))
    return n if n % 2 == 0 else n + 1


def build_hls_command(opts: HlsOptions, url: str, media: MediaInfo, plan: Plan,
                      start: int, outdir: str, use_hw: bool = False,
                      burn_stream_pos: Optional[int] = None) -> List[str]:
    """Build the ffmpeg command that writes an HLS event playlist to ``outdir``.

    ``burn_stream_pos`` is the position of the bitmap subtitle among the
    subtitle streams (for the overlay filter), only used with plan.burn_index.
    """
    v = media.video
    seg = opts.segment_seconds
    hw = bool(use_hw and opts.use_vaapi and plan.video_action == "encode"
              and not plan.tonemap and not plan.deinterlace and plan.burn_index is None)

    cmd = [opts.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "warning"]
    if hw:
        cmd += ["-hwaccel", "vaapi", "-hwaccel_device", opts.vaapi_device,
                "-hwaccel_output_format", "vaapi"]
    cmd += ["-fflags", "+genpts"]
    cmd += http_input_options(url, opts)
    if start > 0:
        cmd += ["-ss", str(int(start))]
    cmd += ["-i", url]

    # ---- stream selection -------------------------------------------------
    burn = plan.burn_index is not None and v is not None
    if v is not None and not burn:
        cmd += ["-map", "0:%d" % v.index]
    if plan.audio_index is not None and plan.audio_action != "none":
        cmd += ["-map", "0:%d" % plan.audio_index]
    cmd += ["-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1"]

    # ---- video ---------------------------------------------------------
    if v is not None:
        if plan.video_action == "copy":
            cmd += ["-c:v", "copy", "-avoid_negative_ts", "make_zero"]
        else:
            fps = v.fps if 1 < v.fps <= 60.5 else 24.0
            gop = max(12, int(round(fps * seg)))
            max_k = plan.max_kbps or 8000
            new_h = plan.scale_height
            new_w = _even(v.width * new_h / float(v.height)) if (new_h and v.height) else 0
            if hw:
                if new_h:
                    vf = "scale_vaapi=w=%d:h=%d:format=nv12" % (new_w, new_h)
                else:
                    vf = "scale_vaapi=format=nv12"
                cmd += ["-vf", vf, "-c:v", "h264_vaapi", "-profile:v", "high",
                        "-b:v", "%dk" % int(max_k * 0.8), "-maxrate", "%dk" % max_k,
                        "-bufsize", "%dk" % (max_k * 2)]
            else:
                chain = []
                if plan.deinterlace:
                    chain.append("yadif=0:-1:0")
                if plan.tonemap:
                    chain.append("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                                 "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv")
                if new_h:
                    chain.append("scale=%d:%d" % (new_w, new_h))
                chain.append("format=yuv420p")
                if burn:
                    # Overlay the bitmap subtitle on the unscaled source first,
                    # then run the normal chain (deinterlace/tonemap/scale/format).
                    sub_ref = "[0:s:%d]" % (burn_stream_pos or 0)
                    fc = "[0:%d]%soverlay[ov];[ov]%s[v]" % (v.index, sub_ref, ",".join(chain))
                    cmd += ["-filter_complex", fc, "-map", "[v]"]
                else:
                    cmd += ["-vf", ",".join(chain)]
                cmd += ["-c:v", "libx264", "-preset", opts.preset, "-crf", str(opts.crf),
                        "-maxrate", "%dk" % max_k, "-bufsize", "%dk" % (max_k * 2),
                        "-profile:v", "high", "-level:v", "4.1", "-pix_fmt", "yuv420p",
                        "-sc_threshold", "0"]
            cmd += ["-g", str(gop), "-keyint_min", str(gop),
                    "-force_key_frames", "expr:gte(t,n_forced*%d)" % seg,
                    "-max_muxing_queue_size", "1024"]

    # ---- audio ---------------------------------------------------------
    if plan.audio_action == "copy":
        cmd += ["-c:a", "copy"]
    elif plan.audio_action == "aac":
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2", "-af", "aresample=async=1:first_pts=0"]
    elif plan.audio_action == "ac3":
        cmd += ["-c:a", "ac3", "-b:a", "640k", "-ac", str(plan.audio_channels),
                "-af", "aresample=async=1:first_pts=0"]

    # ---- HLS output ----------------------------------------------------
    cmd += ["-f", "hls", "-hls_time", str(seg), "-hls_list_size", "0",
            "-hls_playlist_type", "event", "-start_number", "0",
            "-hls_flags", "independent_segments+temp_file",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", "%s/seg_%%05d.ts" % outdir,
            "%s/index.m3u8" % outdir]
    return cmd
