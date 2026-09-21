' StreamCaps: what can this Roku decode, and where is the DS Video stream server?
' Included by APITask.xml. Keep everything here free of scene graph calls so it
' can run on a Task thread.

' Build the capability string sent to the stream server, for example
'   v=h264,hevc;a=aac,ac3,eac3;c=mp4,mkv;h=2160;l=42;aac6=1;hdr=hdr10
' The server decides direct play / remux / transcode from it.
function dsvBuildCaps() as string
    di = createObject("roDeviceInfo")

    video = ["h264"]
    if dsvCanVideo(di, { Codec: "hevc", Profile: "main", Level: "4.1" }) then video.push("hevc")
    if dsvCanVideo(di, { Codec: "hevc", Profile: "main 10", Level: "4.1" }) then video.push("hevc10")
    if dsvCanVideo(di, { Codec: "vp9", Profile: "profile 0", Level: "4.1" }) then video.push("vp9")

    maxHeight = 1080
    if dsvContains(video, "hevc") then maxHeight = 2160

    h264Level = 41
    if dsvCanVideo(di, { Codec: "h264", Profile: "high", Level: "4.2" }) then h264Level = 42

    audio = []
    codecs = ["aac", "ac3", "eac3", "mp3", "flac", "opus", "vorbis", "alac", "dts"]
    for each codec in codecs
        if dsvCanAudio(di, { Codec: codec }) then audio.push(codec)
    end for
    if not dsvContains(audio, "aac") then audio.push("aac")

    aac6 = "0"
    if dsvCanAudio(di, { Codec: "aac", ChCnt: 6 }) then aac6 = "1"

    hdr = []
    dp = di.getDisplayProperties()
    if type(dp) = "roAssociativeArray"
        if dp.doesExist("Hdr10") and dp.Hdr10 = true then hdr.push("hdr10")
        if dp.doesExist("DolbyVision") and dp.DolbyVision = true then hdr.push("dv")
    end if

    caps = "v=" + dsvJoin(video, ",")
    caps = caps + ";a=" + dsvJoin(audio, ",")
    caps = caps + ";c=mp4,mkv"
    caps = caps + ";h=" + stri(maxHeight).trim()
    caps = caps + ";l=" + stri(h264Level).trim()
    caps = caps + ";aac6=" + aac6
    if hdr.count() > 0 then caps = caps + ";hdr=" + dsvJoin(hdr, ",")
    return caps
end function

function dsvCanVideo(di as object, fmt as object) as boolean
    r = di.canDecodeVideo(fmt)
    if type(r) <> "roAssociativeArray" then return false
    return r.result = true
end function

function dsvCanAudio(di as object, fmt as object) as boolean
    r = di.canDecodeAudio(fmt)
    if type(r) <> "roAssociativeArray" then return false
    return r.result = true
end function

function dsvContains(items as object, value as string) as boolean
    for each item in items
        if item = value then return true
    end for
    return false
end function

function dsvJoin(items as object, separator as string) as string
    out = ""
    for each item in items
        if out <> "" then out = out + separator
        out = out + item
    end for
    return out
end function

' Where is the stream server? The Settings screen stores an optional override in
' the registry (DSVideo / streamServer):
'   empty            -> automatic: http://<NAS address>:8899
'   "off" / "none"   -> feature disabled, the app behaves like before
'   "host"           -> http://host:8899
'   "host:port"      -> http://host:port
'   "http(s)://..."  -> used as typed
' Returns "" when disabled.
function dsvStreamServerBase(baseUrl as dynamic) as string
    raw = ""
    reg = createObject("roRegistrySection", "DSVideo")
    if reg.exists("streamServer") then raw = dsvTrim(reg.read("streamServer"))
    lowered = lcase(raw)
    if lowered = "off" or lowered = "none" or lowered = "disabled" then return ""

    if raw = ""
        raw = dsvTrim(baseUrl)
        if raw = "" then return ""
        raw = dsvStripScheme(raw)
        cut = instr(1, raw, "/")
        if cut > 0 then raw = left(raw, cut - 1)
        colon = instr(1, raw, ":")
        if colon > 0 then raw = left(raw, colon - 1)
        if raw = "" then return ""
        return "http://" + raw + ":8899"
    end if

    scheme = "http://"
    if left(lowered, 8) = "https://"
        scheme = "https://"
    end if
    hostPart = dsvStripScheme(raw)
    while right(hostPart, 1) = "/"
        hostPart = left(hostPart, len(hostPart) - 1)
    end while
    if hostPart = "" then return ""
    if instr(1, hostPart, ":") = 0 then hostPart = hostPart + ":8899"
    return scheme + hostPart
end function

function dsvStripScheme(url as string) as string
    lowered = lcase(url)
    if left(lowered, 8) = "https://" then return mid(url, 9)
    if left(lowered, 7) = "http://" then return mid(url, 8)
    return url
end function

function dsvTrim(value as dynamic) as string
    if value = invalid then return ""
    if type(value) <> "roString" and type(value) <> "String" then return ""
    s = value
    return s.trim()
end function
