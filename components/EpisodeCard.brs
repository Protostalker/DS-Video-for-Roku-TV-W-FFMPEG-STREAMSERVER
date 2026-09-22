sub init()
    m.top.observeField("itemContent", "onContentSet")
    m.top.observeField("focusPercent", "onFocusChange")
    m.top.findNode("poster").observeField("loadStatus", "onPosterLoadStatus")
    m.content = invalid
end sub

sub onContentSet(event as object)
    content = event.getData()
    if content = invalid then return
    m.content = content
    m.top.findNode("titleLabel").text = content.title
    if content.description <> invalid
        m.top.findNode("episodeLabel").text = content.description
    else
        m.top.findNode("episodeLabel").text = ""
    end if
    dateLabel = m.top.findNode("dateLabel")
    dateLabel.text = ""
    dateLabel.visible = false
    if content.episodeDate <> invalid and content.episodeDate <> ""
        dateLabel.text = content.episodeDate
        dateLabel.visible = true
    end if

    poster = m.top.findNode("poster")
    if content.HDPosterUrl <> invalid and content.HDPosterUrl <> ""
        poster.uri = content.HDPosterUrl
        poster.visible = true
    else if content.SDPosterUrl <> invalid and content.SDPosterUrl <> ""
        poster.uri = content.SDPosterUrl
        poster.visible = true
    else
        poster.visible = false
    end if

    watched = content.watched = true
    m.top.findNode("watchedBadge").visible = watched
    m.top.findNode("watchedCheck").visible = watched

    ' In-progress bar along the bottom edge of the poster: only for a partially-watched
    ' episode (some resume position saved, but not enough to count as watched yet). A
    ' fully-watched episode gets the checkmark badge above instead, not both.
    ratio = 0
    if content.watchRatio <> invalid then ratio = content.watchRatio
    track = m.top.findNode("progressTrack")
    fill = m.top.findNode("progressFill")
    if not watched and ratio > 0
        track.visible = true
        fill.visible = true
        fill.width = int(520 * ratio / 100)
    else
        track.visible = false
        fill.visible = false
    end if
end sub

sub onPosterLoadStatus(event as object)
    if m.content = invalid then return
    status = event.getData()
    if status = invalid then return
    if status = "ready"
        setContentArtworkState("true")
    else if status = "failed" or status = "failure"
        setContentArtworkState("false")
    end if
end sub

sub setContentArtworkState(value as string)
    if m.content = invalid then return
    if m.content.hasField("artworkLoaded")
        m.content.artworkLoaded = value
    else
        m.content.addFields({ artworkLoaded: value })
    end if
end sub

sub onFocusChange(event as object)
    if event = invalid then return
end sub

function onKeyEvent(key as string, press as boolean) as boolean
    if not press then return false
    return false
end function
