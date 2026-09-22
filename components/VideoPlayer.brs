sub init()
      m.top.observeField("videoData", "onVideoDataSet")
      m.videoNode = m.top.findNode("videoNode")
      m.videoNode.observeField("state", "onVideoStateChange")
      m.videoNode.observeField("errorCode", "onVideoErrorDetail")
      m.videoNode.observeField("errorMsg", "onVideoErrorDetail")
      m.videoNode.observeField("bufferingStatus", "onVideoBuffering")
      hasAvailableSubtitleTracks = m.videoNode.hasField("availableSubtitleTracks")
      hasCurrentSubtitleTrack = m.videoNode.hasField("currentSubtitleTrack")
      print "VIDEO_SUBTITLE_FIELDS available="; hasAvailableSubtitleTracks; " current="; hasCurrentSubtitleTrack
      if hasAvailableSubtitleTracks then m.videoNode.observeField("availableSubtitleTracks", "onAvailableSubtitleTracks")
      if hasCurrentSubtitleTrack then m.videoNode.observeField("currentSubtitleTrack", "onCurrentSubtitleTrack")
      m.top.findNode("progressTimer").observeField("fire", "onProgressTimer")
      m.top.findNode("overlayRefreshTimer").observeField("fire", "onOverlayRefreshTimer")
      m.top.findNode("overlayTimer").observeField("fire", "onOverlayTimer")
      m.top.findNode("resumeSeekTimer").observeField("fire", "onResumeSeekTimer")
      m.top.findNode("directStartupTimer").observeField("fire", "onDirectStartupTimer")
      m.top.findNode("subtitleTimer").observeField("fire", "onSubtitleTimer")
      m.top.findNode("scrubTimer").observeField("fire", "onScrubTimer")
      m.top.findNode("toastTimer").observeField("fire", "onToastTimer")
      m.top.findNode("trackList").observeField("itemSelected", "onTrackSelected")
      m.customUi = false
      m.menuOpen = false
      m.hasError = false
      m.hasPlayed = false
      m.reportedStart = false
      m.reportedDone = false
      m.userStopped = false
      m.progressDebugTicks = 0
      m.top.setFocus(true)
  end sub

  sub onVideoDataSet(event as object)
      videoData = event.getData()
      if videoData = invalid then return

      m.hasPlayed = false
      m.reportedDone = false
      m.userStopped = false
      m.seekApplied = false
      m.seekAttempts = 0
      m.lastSyncedPosition = -1
      m.watchStatusInFlight = false
      m.resumeSeekDoneAt = invalid
      m.resumePosition = resumePositionForVideo(videoData)
      if videoData.lookUp("forceStartOver") = true then m.resumePosition = 0
      stopServerSession()
      resetCustomUi()
      m.streamAttemptIndex = 0
      ' Up to four ways to play a file (direct, remux, transcode, Video Station wrapper),
      ' then one more request that reports "no more ways".
      m.maxStreamAttempts = 4
      m.lastPlayerError = ""
      m.serverBase = ""
      m.serverSession = ""
      m.totalDuration = 0
      m.startupTimeout = 45
      m.retryingStream = false
      m.progressDebugTicks = 0
      m.userSeekTarget = invalid
      printVideoDataDebug("VIDEO_DATA", videoData)
      if (m.resumePosition = invalid or m.resumePosition <= 0) and m.top.resumePosition <> invalid
          m.resumePosition = int(m.top.resumePosition)
      end if
      requestStreamUrl()
  end sub

  sub requestStreamUrl()
      videoData = m.top.videoData
      if videoData = invalid then return
      authData = videoData.authData
      if authData = invalid then return

      fileId = videoData.fileId
      filePath = videoData.filePath
      videoId = videoData.id
      if fileId = invalid and videoId = invalid and (filePath = invalid or filePath = "")
          showError("No file ID or file path for this video.")
          return
      end if

      m.top.findNode("loadingOverlay").visible = true
      m.top.findNode("loadingLabel").visible = false
      m.top.findNode("videoTitle").visible = false
      m.top.findNode("errorLabel").visible = false
      m.top.findNode("backgroundRect").visible = true
      m.hasError = false

      task = createObject("roSGNode", "APITask")
      task.request = {
          action: "getStreamUrl",
          baseUrl: authData.baseUrl,
          proxyBaseUrl: authData.proxyBaseUrl,
          sid: authData.sid,
          synoToken: authData.synoToken,
          username: authData.username,
          password: authData.password,
          fileId: fileId,
          mapperId: videoData.mapperId,
          filePath: videoData.filePath,
          id: videoData.id,
          title: videoData.title,
          resumePosition: m.resumePosition,
          originalAvailable: videoData.originalAvailable,
          mediaType: videoData.type,
          attemptIndex: m.streamAttemptIndex,
          audioIndex: m.audioIndex,
          burnIndex: m.burnIndex
      }
      print "STREAM_REQUEST attempt="; m.streamAttemptIndex; " title="; safeDynamicString(videoData.lookUp("title")); " type="; safeDynamicString(videoData.lookUp("type")); " fileId="; safeDynamicString(fileId); " videoId="; safeDynamicString(videoId); " path="; safeDynamicString(filePath)
      task.observeField("response", "onStreamUrlReady")
      task.control = "RUN"
      m.streamTask = task
  end sub

  sub onStreamUrlReady(event as object)
      response = event.getData()
      if response = invalid
          showError("No response from stream task.")
          return
      end if
      if response.success <> true
          if response.retryable = true
              ' This way of playing could not even be set up: move on to the next one.
              m.retryingStream = false
              if response.error <> invalid then m.lastPlayerError = response.error
              if response.attemptIndex <> invalid then m.streamAttemptIndex = int(response.attemptIndex)
              if retryNextStreamCandidate() then return
          end if
          errMsg = "Stream open failed."
          if response.error <> invalid then errMsg = response.error
          detail = ""
          if response.detail <> invalid then detail = chr(10) + left(response.detail, 1500)
          if m.lastPlayerError <> invalid and m.lastPlayerError <> "" then detail = detail + chr(10) + "Last player error: " + m.lastPlayerError
          showError(errMsg + detail)
          return
      end if
      streamUrl = response.streamUrl
      if streamUrl = invalid or streamUrl = ""
          showError("Empty stream URL.")
          return
      end if
      fmt = response.streamFormat
      if fmt = invalid or fmt = "" then fmt = "mp4"
      isLive = false
      if response.isLive = true then isLive = true
      m.isHlsStream = (fmt = "hls")
      m.nativeHlsResume = response.nativeHlsResume = true
      m.nativeHlsResumeBase = 0
      if m.nativeHlsResume = true and response.resumePosition <> invalid
          m.nativeHlsResumeBase = int(response.resumePosition)
      end if
      if m.isHlsStream = true and m.nativeHlsResume = true
          m.resumePosition = m.nativeHlsResumeBase
          m.seekApplied = true
          print "VIDEO_RESUME_NATIVE_HLS position="; m.resumePosition
      end if
      m.streamDebug = ""
      if response.debugInfo <> invalid then m.streamDebug = response.debugInfo
      if response.attemptIndex <> invalid then m.streamAttemptIndex = int(response.attemptIndex)
      m.subtitleUrl = ""
      if response.subtitleUrl <> invalid then m.subtitleUrl = response.subtitleUrl
      m.streamHttpHeaders = invalid
      if response.httpHeaders <> invalid then m.streamHttpHeaders = response.httpHeaders
      m.directVteAttempt = response.directVte = true
      m.serverBase = ""
      m.serverSession = ""
      m.totalDuration = 0
      if response.serverBase <> invalid and response.serverSession <> invalid
          m.serverBase = response.serverBase
          m.serverSession = response.serverSession
      end if
      if response.totalDuration <> invalid then m.totalDuration = response.totalDuration
      m.startupTimeout = 45
      if response.startupTimeout <> invalid then m.startupTimeout = response.startupTimeout
      m.customUi = response.customUi = true
      m.subtitleOptions = []
      m.audioOptions = []
      m.subtitleContext = invalid
      m.serverMode = ""
      m.introOverlayShown = false
      if m.customUi = true
          if response.subtitleOptions <> invalid then m.subtitleOptions = response.subtitleOptions
          if response.audioOptions <> invalid then m.audioOptions = response.audioOptions
          if response.subtitleContext <> invalid then m.subtitleContext = response.subtitleContext
          if response.serverMode <> invalid then m.serverMode = response.serverMode
          m.defaultSubtitleIndex = response.defaultSubtitle
          if m.audioIndex <> invalid
              m.activeAudio = m.audioIndex
          else
              m.activeAudio = response.activeAudio
          end if
      end if
      if m.subtitleUrl <> invalid and m.subtitleUrl <> "" then print "VIDEO_SUBTITLE url="; m.subtitleUrl
      print "STREAM_READY attempt="; m.streamAttemptIndex; " fmt="; fmt; " live="; isLive; " url="; debugUrlSummary(streamUrl); " debug="; oneLine(left(safeDynamicString(m.streamDebug), 500))
      startPlayback(streamUrl, fmt, isLive)
      setupCustomUi()
  end sub

  sub startPlayback(streamUrl as string, fmt as string, isLive as boolean)
      m.top.findNode("backgroundRect").visible = false
      m.top.findNode("loadingOverlay").visible = false
      m.top.findNode("loadingLabel").visible = false
      m.top.findNode("videoTitle").visible = false
      m.top.findNode("resumeOverlay").visible = false
      m.resumeHoldActive = false

      video = m.videoNode
      video.width = 1920
      video.height = 1080
      video.translation = [0, 0]
      video.visible = true
      applyVideoUi()

      content = createObject("roSGNode", "ContentNode")
      content.url = streamUrl
      content.streamFormat = fmt
      if fmt = "hls" and isLive
          content.Live = true
      end if
      content.addFields({
          HttpCertificatesFile: "common:/certs/ca-bundle.crt",
          HttpVerifyPeer: false,
          HttpVerifyHost: false,
          ForwardQueryStringParams: true
      })
      if m.streamHttpHeaders <> invalid
          content.addFields({ HttpHeaders: m.streamHttpHeaders })
          print "VIDEO_HTTP_HEADERS count="; m.streamHttpHeaders.count()
      end if

      videoData = m.top.videoData
      if videoData <> invalid and videoData.title <> invalid
          content.title = videoData.title
      end if
      if m.subtitleUrl <> invalid and m.subtitleUrl <> ""
          content.SubtitleTracks = [
              {
                  TrackName: m.subtitleUrl,
                  Language: "eng",
                  Description: "English"
              }
          ]
          print "VIDEO_SUBTITLE_METADATA count="; content.SubtitleTracks.count()
      end if
      if m.resumePosition <> invalid and m.resumePosition > 0 and fmt <> "hls"
          content.PlayStart = m.resumePosition
          content.playStart = m.resumePosition
      else if fmt = "hls"
          content.PlayStart = 0
          content.playStart = 0
          content.BookmarkPosition = 0
          content.bookmarkPosition = 0
      end if

      video.content = content
      if m.subtitleUrl <> invalid and m.subtitleUrl <> ""
          print "VIDEO_SUBTITLE_NATIVE_READY track="; m.subtitleUrl
      end if
      ensureVideoFocus()
      m.hasPlayed = false
      playStartText = ""
      if m.resumePosition <> invalid and m.resumePosition > 0 and fmt <> "hls" then playStartText = safeDynamicString(m.resumePosition)
      print "VIDEO_CONTENT fmt="; fmt; " streamFormat="; safeDynamicString(content.streamFormat); " title="; safeDynamicString(content.title); " url="; debugUrlSummary(content.url); " playStart="; playStartText
      print "VIDEO_PLAY fmt="; fmt
      if m.resumePosition <> invalid and m.resumePosition > 0 then print "VIDEO_RESUME position="; m.resumePosition
      if m.resumePosition <> invalid and m.resumePosition > 0 and m.nativeHlsResume <> true
          beginResumeHold()
      else
          setVideoMuted(false)
      end if
      if m.resumePosition <> invalid and m.resumePosition > 0 and fmt <> "hls"
          video.control = "prebuffer"
      else
          video.control = "play"
      end if
      m.streamUrl = streamUrl
      m.streamFmt = fmt
      m.retryingStream = false
      ' Watchdog: if nothing plays within the time limit, try the next way of playing.
      startupTimer = m.top.findNode("directStartupTimer")
      if startupTimer <> invalid
          startupTimer.control = "stop"
          startupTimer.duration = m.startupTimeout
          startupTimer.control = "start"
          print "VIDEO_STARTUP_TIMER start seconds="; m.startupTimeout
      end if
  end sub

  sub beginResumeHold()
      m.resumeHoldActive = true
      m.videoNode.visible = false
      setVideoMuted(true)
      overlay = m.top.findNode("resumeOverlay")
      if overlay <> invalid then overlay.visible = true
  end sub

  sub endResumeHold()
      m.resumeHoldActive = false
      m.videoNode.visible = true
      applyVideoUi()
      ensureVideoFocus()
      setVideoMuted(false)
      overlay = m.top.findNode("resumeOverlay")
      if overlay <> invalid then overlay.visible = false
  end sub

  sub setVideoMuted(enabled as boolean)
      video = m.videoNode
      if video = invalid then return
      if video.hasField("mute") then video.mute = enabled
      if video.hasField("muted") then video.muted = enabled
  end sub

  sub onAvailableSubtitleTracks(event as object)
      if event = invalid then return
      tracks = event.getData()
      count = 0
      if tracks <> invalid then count = tracks.count()
      print "VIDEO_SUBTITLE_AVAILABLE count="; count
  end sub

  sub onCurrentSubtitleTrack(event as object)
      if event = invalid then return
      print "VIDEO_SUBTITLE_CURRENT track="; event.getData()
  end sub

  sub onVideoStateChange(event as object)
      state = event.getData()
      print "VIDEO_STATE state="; state; " pos="; safeDynamicString(m.videoNode.position); " dur="; safeDynamicString(m.videoNode.duration); " buffer="; debugAny(m.videoNode.bufferingStatus); " errCode="; safeDynamicString(m.videoNode.errorCode); " errMsg="; safeDynamicString(m.videoNode.errorMsg)
      if state = "playing"
          stopDirectStartupTimer()
          ensureVideoFocus()
          m.hasPlayed = true
          m.paused = false
          if m.customUi = true and m.introOverlayShown <> true
              m.introOverlayShown = true
              showPlaybackOverlay()
          end if
          if m.reportedStart <> true
              m.reportedStart = true
              m.top.playbackStarted = true
          end if
          applyPendingSeek()
          startResumeSeekTimer()
          timer = m.top.findNode("progressTimer")
          if timer <> invalid then timer.control = "start"
      else if state = "paused"
          m.paused = true
          if m.customUi = true then showPlaybackOverlay()
      else if state = "buffering"
          ensureVideoFocus()
          if m.streamFmt <> "hls" and m.resumePosition <> invalid and m.resumePosition > 0 and m.hasPlayed <> true
              m.videoNode.control = "play"
          end if
      else if state = "finished" or state = "stopped"
          stopDirectStartupTimer()
          if m.retryingStream = true then return
          timer = m.top.findNode("progressTimer")
          if timer <> invalid then timer.control = "stop"
          seekTimer = m.top.findNode("resumeSeekTimer")
          if seekTimer <> invalid then seekTimer.control = "stop"
          endResumeHold()
          if state = "finished" and not m.userStopped
              finishPlaybackPosition()
          else
              saveResumePosition()
          end if
          ' Only exit if no error was shown — otherwise user reads the error and presses Back.
          if not m.hasError
              reason = "finished"
              if state = "stopped" or m.userStopped then reason = "back"
              reportPlaybackDone(reason)
          end if
      else if state = "error"
          stopDirectStartupTimer()
          if m.customUi = true and m.hasPlayed = true and m.serverSession <> "" and m.sessionRestarts < 2
              ' A stream that was playing broke (for example the server dropped it after a long
              ' pause): start it again from where it was, the same way as before.
              m.sessionRestarts = m.sessionRestarts + 1
              print "VIDEO_SERVER_STREAM_RESTART count="; m.sessionRestarts
              restartStreamAt(effectivePlaybackPosition())
              return
          end if
          if retryNextStreamCandidate() then return
          m.hasError = true
          endResumeHold()
          dbg = ""
          if m.streamDebug <> invalid and m.streamDebug <> "" then dbg = chr(10) + m.streamDebug
          print "VIDEO_ERROR_FINAL fmt="; safeDynamicString(m.streamFmt); " attempt="; safeDynamicString(m.streamAttemptIndex); " url="; debugUrlSummary(m.streamUrl); " code="; safeDynamicString(m.videoNode.errorCode); " msg="; safeDynamicString(m.videoNode.errorMsg)
          errText = playerErrorText()
          if errText <> "" then errText = " " + errText
          showError("Playback error (fmt=" + m.streamFmt + ")." + errText + dbg)
      end if
  end sub

  sub stopDirectStartupTimer()
      timer = m.top.findNode("directStartupTimer")
      if timer <> invalid then timer.control = "stop"
  end sub

  sub stopStartupWatchdog()
      stopDirectStartupTimer()
  end sub

  function playerErrorText() as string
      text = ""
      code = safeDynamicString(m.videoNode.errorCode)
      msg = safeDynamicString(m.videoNode.errorMsg)
      if code <> "" and code <> "0" then text = "code " + code
      if msg <> "" then
          if text <> "" then text = text + " "
          text = text + msg
      end if
      if text = "" then return safeDynamicString(m.lastPlayerError)
      return text
  end function

  ' Tell the stream server the viewer is done so it stops encoding right away.
  ' The request is handed to MainScene so it survives this screen being closed.
  sub stopServerSession()
      if m.serverSession = invalid or m.serverSession = "" then return
      if m.serverBase = invalid or m.serverBase = "" then return
      print "VIDEO_STOP_SERVER_SESSION"
      m.top.stopServerStream = { serverBase: m.serverBase, serverSession: m.serverSession }
      m.serverSession = ""
  end sub

  sub onDirectStartupTimer(event as object)
      if event = invalid then return
      if m.hasPlayed = true then return
      if m.hasError = true then return
      print "VIDEO_STARTUP_TIMEOUT attempt="; safeDynamicString(m.streamAttemptIndex); " state="; safeDynamicString(m.videoNode.state)
      m.lastPlayerError = "nothing played within " + safeDynamicString(m.startupTimeout) + " seconds"
      if retryNextStreamCandidate() then return
      m.hasError = true
      endResumeHold()
      m.videoNode.control = "stop"
      showError("This video did not start playing (" + m.lastPlayerError + ").")
  end sub

  function retryNextStreamCandidate() as boolean
      if m.retryingStream = true then return true
      if m.streamAttemptIndex = invalid then m.streamAttemptIndex = 0
      if m.maxStreamAttempts = invalid then m.maxStreamAttempts = 12
      if m.streamAttemptIndex >= m.maxStreamAttempts then return false

      fresh = playerErrorText()
      if fresh <> "" then m.lastPlayerError = fresh
      if m.hasPlayed = true
          ' It was playing and then failed: carry on from where it got to.
          m.resumePosition = effectivePlaybackPosition()
          m.seekApplied = false
          m.seekAttempts = 0
          m.resumeSeekDoneAt = invalid
          m.hasPlayed = false
      end if
      stopStartupWatchdog()
      stopServerSession()
      m.streamAttemptIndex = m.streamAttemptIndex + 1
      m.retryingStream = true
      m.hasError = false
      endResumeHold()
      print "VIDEO_RETRY_STREAM nextCandidate="; m.streamAttemptIndex; " prevFmt="; safeDynamicString(m.streamFmt); " prevUrl="; debugUrlSummary(m.streamUrl); " errCode="; safeDynamicString(m.videoNode.errorCode); " errMsg="; safeDynamicString(m.videoNode.errorMsg)
      m.videoNode.control = "stop"
      requestStreamUrl()
      return true
  end function

  sub reportPlaybackDone(reason as string)
      stopCustomUiTimers()
      stopStartupWatchdog()
      stopServerSession()
      if m.reportedDone then return
      m.reportedDone = true
      m.top.playbackResult = {
          reason: reason,
          videoData: m.top.videoData
      }
      m.top.playbackDone = true
  end sub

  sub onProgressTimer(event as object)
      if event = invalid then return
      applyPendingSeek()
      saveResumePosition()
      clearSettledUserSeekTarget()
      m.progressDebugTicks = m.progressDebugTicks + 1
      if m.progressDebugTicks >= 6
          m.progressDebugTicks = 0
          print "VIDEO_PROGRESS pos="; safeDynamicString(effectivePlaybackPosition()); " rawPos="; safeDynamicString(m.videoNode.position); " dur="; safeDynamicString(m.videoNode.duration); " state="; safeDynamicString(m.videoNode.state); " fmt="; safeDynamicString(m.streamFmt); " buffer="; debugAny(m.videoNode.bufferingStatus)
      end if
      if m.top.findNode("controlOverlay").visible then updatePlaybackOverlay()
  end sub

  sub startResumeSeekTimer()
      if m.seekApplied = true then return
      if m.resumePosition = invalid or m.resumePosition <= 0 then return
      timer = m.top.findNode("resumeSeekTimer")
      if timer = invalid then return
      timer.control = "start"
  end sub

  sub onResumeSeekTimer(event as object)
      if event = invalid then return
      applyPendingSeek()
  end sub

  sub applyPendingSeek()
      if m.seekApplied = true then return
      if m.resumePosition = invalid or m.resumePosition <= 0 then return
      currentPos = int(m.videoNode.position)
      if currentPos >= m.resumePosition - 5
          m.seekApplied = true
          m.resumeSeekDoneAt = createObject("roTimespan")
          m.resumeSeekDoneAt.mark()
          timer = m.top.findNode("resumeSeekTimer")
          if timer <> invalid then timer.control = "stop"
          endResumeHold()
          ensureVideoFocus()
          print "VIDEO_SEEK_DONE position="; currentPos; " target="; m.resumePosition
          return
      end if
      maxSeekAttempts = 24
      if m.isHlsStream = true then maxSeekAttempts = 480
      if m.seekAttempts >= maxSeekAttempts
          m.seekApplied = true
          m.resumeSeekDoneAt = invalid
          timer = m.top.findNode("resumeSeekTimer")
          if timer <> invalid then timer.control = "stop"
          endResumeHold()
          ensureVideoFocus()
          print "VIDEO_SEEK_GIVEUP position="; currentPos; " target="; m.resumePosition
          return
      end if
      m.seekAttempts = m.seekAttempts + 1
      m.videoNode.seek = m.resumePosition
      ensureVideoFocus()
      print "VIDEO_SEEK attempt="; m.seekAttempts; " target="; m.resumePosition; " current="; currentPos
  end sub

  sub showPlaybackOverlay()
      updatePlaybackOverlay()
      overlay = m.top.findNode("controlOverlay")
      overlay.visible = true
      positionSubtitleGroup()
      refreshTimer = m.top.findNode("overlayRefreshTimer")
      if refreshTimer <> invalid then refreshTimer.control = "start"
      timer = m.top.findNode("overlayTimer")
      timer.control = "stop"
      timer.control = "start"
  end sub

  function seekServerStreamBy(deltaSeconds as integer) as boolean
      if m.serverSession = invalid or m.serverSession = "" then return false
      if m.isHlsStream <> true then return false
      baseOffset = int(m.nativeHlsResumeBase)
      encoded = int(m.videoNode.duration)
      relBase = int(m.videoNode.position)
      if m.userSeekTarget <> invalid then relBase = int(m.userSeekTarget)
      absTarget = baseOffset + relBase + deltaSeconds
      total = int(m.totalDuration)
      if total > 0 and absTarget > total - 10 then absTarget = total - 10
      if absTarget < 0 then absTarget = 0
      relTarget = absTarget - baseOffset
      if relTarget >= 0 and encoded > 0 and relTarget <= encoded - 10
          m.userSeekTarget = relTarget
          m.videoNode.seek = relTarget
          print "VIDEO_USER_SEEK server in-range abs="; absTarget; " rel="; relTarget
          showPlaybackOverlay()
      else
          print "VIDEO_USER_SEEK server restart abs="; absTarget
          restartStreamAt(absTarget)
      end if
      return true
  end function

  sub restartStreamAt(absSeconds as integer)
      m.retryingStream = true
      m.hasPlayed = false
      m.paused = false
      m.resumePosition = absSeconds
      m.userSeekTarget = invalid
      stopStartupWatchdog()
      stopServerSession()
      m.videoNode.control = "stop"
      requestStreamUrl()
  end sub

  sub seekBy(deltaSeconds as integer)
      if m.videoNode = invalid then return
      if seekServerStreamBy(deltaSeconds) then return
      duration = int(m.videoNode.duration)
      current = int(m.videoNode.position)
      base = current
      if m.userSeekTarget <> invalid
          base = int(m.userSeekTarget)
      end if
      target = base + deltaSeconds
      if target < 0 then target = 0
      if duration > 0 and target > duration - 5 then target = duration - 5
      if target < 0 then target = 0
      m.userSeekTarget = target
      m.videoNode.seek = target
      if m.resumePosition <> invalid and m.resumePosition > 0
          m.seekApplied = true
          m.resumeSeekDoneAt = createObject("roTimespan")
          m.resumeSeekDoneAt.mark()
      end if
      print "VIDEO_USER_SEEK from="; current; " base="; base; " to="; target; " delta="; deltaSeconds; " duration="; duration
      showPlaybackOverlay()
  end sub

  sub clearSettledUserSeekTarget()
      if m.userSeekTarget = invalid then return
      current = int(m.videoNode.position)
      target = int(m.userSeekTarget)
      if abs(current - target) <= 5 then m.userSeekTarget = invalid
  end sub

  sub togglePlayPause()
      if m.videoNode = invalid then return
      state = safeDynamicString(m.videoNode.state)
      if state = "playing" or state = "buffering"
          m.videoNode.control = "pause"
          print "VIDEO_USER_PAUSE"
      else
          m.videoNode.control = "resume"
          print "VIDEO_USER_RESUME state="; state
      end if
      showPlaybackOverlay()
  end sub

  sub onOverlayRefreshTimer(event as object)
      if event = invalid then return
      if m.top.findNode("controlOverlay").visible
          updatePlaybackOverlay()
      else
          timer = m.top.findNode("overlayRefreshTimer")
          if timer <> invalid then timer.control = "stop"
      end if
  end sub

  function totalDurationSeconds() as integer
      duration = int(m.videoNode.duration)
      if m.isHlsStream = true and m.nativeHlsResume = true
          ' The stream may begin part way into the video: add that on, or use the real length.
          duration = duration + int(m.nativeHlsResumeBase)
          if m.totalDuration <> invalid and int(m.totalDuration) > duration then duration = int(m.totalDuration)
      end if
      return duration
  end function

  sub updatePlaybackOverlay()
      title = ""
      videoData = m.top.videoData
      if videoData <> invalid and videoData.title <> invalid then title = videoData.title
      m.top.findNode("overlayTitle").text = title

      playbackPos = effectivePlaybackPosition()
      duration = totalDurationSeconds()
      shownPos = playbackPos
      scrubbing = (m.scrubTarget <> invalid)
      if scrubbing then shownPos = int(m.scrubTarget)
      timeText = formatPlaybackTime(shownPos)
      if duration > 0 then timeText = timeText + " / " + formatPlaybackTime(duration)
      if scrubbing then timeText = "Skip to " + timeText
      m.top.findNode("overlayTime").text = timeText

      progressWidth = 1
      if duration > 0
          progressWidth = int((shownPos / duration) * 1720)
          if progressWidth < 1 then progressWidth = 1
          if progressWidth > 1720 then progressWidth = 1720
      end if
      m.top.findNode("overlayProgress").width = progressWidth

      buffered = m.top.findNode("overlayBuffered")
      marker = m.top.findNode("overlayScrubMarker")
      stateLabel = m.top.findNode("overlayState")
      hint = m.top.findNode("overlayHint")
      if m.customUi = true
          ' Lighter bar: how much of the video has been prepared so far.
          buffered.visible = false
          if duration > 0
              encodedEnd = int(m.nativeHlsResumeBase) + int(m.videoNode.duration)
              bufferedWidth = int((encodedEnd / duration) * 1720)
              if bufferedWidth > 1720 then bufferedWidth = 1720
              if bufferedWidth > progressWidth
                  buffered.width = bufferedWidth
                  buffered.visible = true
              end if
          end if
          marker.visible = scrubbing
          if scrubbing then marker.translation = [100 + progressWidth - 3, 139]
          if m.paused = true
              stateLabel.text = "PAUSED"
          else
              stateLabel.text = ""
          end if
          hint.text = "OK pause / play      Left / Right skip 10 s      Rewind / Fast forward skip 60 s      Down subtitles and audio"
      else
          buffered.visible = false
          marker.visible = false
          stateLabel.text = ""
          hint.text = ""
      end if
  end sub

  sub onOverlayTimer(event as object)
      if event = invalid then return
      ' Stay on screen while paused, skipping, or when a menu is open.
      if m.customUi = true
          if m.paused = true or m.scrubTarget <> invalid or m.menuOpen = true then return
      end if
      m.top.findNode("controlOverlay").visible = false
      positionSubtitleGroup()
      refreshTimer = m.top.findNode("overlayRefreshTimer")
      if refreshTimer <> invalid then refreshTimer.control = "stop"
  end sub

  function formatPlaybackTime(seconds as integer) as string
      total = int(seconds)
      hours = int(total / 3600)
      minutes = int((total - (hours * 3600)) / 60)
      secs = total - (hours * 3600) - (minutes * 60)
      if hours > 0
          return stri(hours).trim() + ":" + twoDigit(minutes) + ":" + twoDigit(secs)
      end if
      return stri(minutes).trim() + ":" + twoDigit(secs)
  end function

  function twoDigit(value as integer) as string
      text = stri(value).trim()
      if value < 10 then return "0" + text
      return text
  end function

  function resumePositionForVideo(videoData as object) as integer
      if videoData = invalid then return 0
      value = videoData.lookUp("resumePosition")
      if value = invalid then return 0
      t = type(value)
      if t = "roInteger" or t = "Integer" then return value
      if t = "roFloat" or t = "Float" then return int(value)
      if t = "roString" or t = "String" then return val(value)
      return 0
  end function

  function resumeKeyForVideo(videoData as object) as string
      if videoData = invalid then return ""
      if videoData.filePath <> invalid and videoData.filePath <> "" then return "path:" + videoData.filePath
      if videoData.fileId <> invalid and videoData.fileId <> "" then return "file:" + safeDynamicString(videoData.fileId)
      if videoData.id <> invalid and videoData.id <> "" then return "id:" + safeDynamicString(videoData.id)
      return ""
  end function

  function safeDynamicString(value as dynamic) as string
      if value = invalid then return ""
      t = type(value)
      if t = "roString" or t = "String" then return value
      if t = "roInteger" or t = "Integer" then return stri(value).trim()
      if t = "roFloat" or t = "Float" then return stri(int(value)).trim()
      if t = "roBoolean" or t = "Boolean" then
          if value then return "true"
          return "false"
      end if
      return ""
  end function

  function oneLine(value as string) as string
      text = ""
      for idx = 1 to len(value)
          ch = mid(value, idx, 1)
          if ch = chr(10) or ch = chr(13)
              text = text + " "
          else
              text = text + ch
          end if
      end for
      return text
  end function

  function debugAny(value as dynamic) as string
      if value = invalid then return ""
      text = safeDynamicString(value)
      if text <> "" then return text
      return bslib_toString(value)
  end function

  function debugUrlSummary(url as dynamic) as string
      text = safeDynamicString(url)
      if text = "" then return ""
      q = instr(1, text, "?")
      if q > 0 then text = left(text, q - 1) + "?..."
      if len(text) > 260 then text = left(text, 260) + "..."
      return text
  end function

  sub printVideoDataDebug(prefix as string, videoData as object)
      if videoData = invalid
          print prefix; " invalid"
          return
      end if
      print prefix; " title="; safeDynamicString(videoData.lookUp("title")); " type="; safeDynamicString(videoData.lookUp("type")); " id="; safeDynamicString(videoData.lookUp("id")); " fileId="; safeDynamicString(videoData.lookUp("fileId")); " mapper="; safeDynamicString(videoData.lookUp("mapperId")); " path="; safeDynamicString(videoData.lookUp("filePath")); " resume="; safeDynamicString(videoData.lookUp("resumePosition")); " originalAvailable="; safeDynamicString(videoData.lookUp("originalAvailable"))
  end sub

  sub saveResumePosition()
      if not m.hasPlayed then return
      if m.resumePosition <> invalid and m.resumePosition > 0 and m.seekApplied <> true then return
      key = resumeKeyForVideo(m.top.videoData)
      if key = "" then return
      playbackPos = effectivePlaybackPosition()
      if playbackPos < 30 then return
      ' Compare against the whole video's length, not the Roku node's own "duration" field:
      ' for a stream-server HLS stream that field is the length of just the growing playlist
      ' (or, after a resume, of what is left from the resume point), not the full video. Using
      ' it directly here made a resumed episode look "almost finished" within moments of
      ' resuming, clearing the resume point and marking it watched.
      duration = totalDurationSeconds()
      if duration > 0 and playbackPos > duration - 90
          finishPlaybackPosition()
          return
      end if
      reg = createObject("roRegistrySection", "DSVideoResume")
      reg.write(key, stri(playbackPos).trim())
      reg.flush()
      syncWatchStatus(playbackPos)
  end sub

  function effectivePlaybackPosition() as integer
      playbackPos = int(m.videoNode.position)
      if m.isHlsStream = true and m.nativeHlsResume = true and m.nativeHlsResumeBase > 0
          return int(m.nativeHlsResumeBase) + playbackPos
      end if
      if m.isHlsStream = true and m.resumePosition <> invalid and m.resumePosition > 0 and m.seekApplied = true and m.resumeSeekDoneAt <> invalid
          elapsed = int(m.resumeSeekDoneAt.totalMilliseconds() / 1000)
          resumeBasedPos = int(m.resumePosition) + elapsed
          if resumeBasedPos > playbackPos then playbackPos = resumeBasedPos
      end if
      return playbackPos
  end function

  sub clearResumePosition()
      key = resumeKeyForVideo(m.top.videoData)
      if key = "" then return
      reg = createObject("roRegistrySection", "DSVideoResume")
      if reg.exists(key)
          reg.delete(key)
          reg.flush()
      end if
      syncWatchStatus(0)
  end sub

  sub finishPlaybackPosition()
      key = resumeKeyForVideo(m.top.videoData)
      if key <> ""
          reg = createObject("roRegistrySection", "DSVideoResume")
          if reg.exists(key)
              reg.delete(key)
              reg.flush()
          end if
      end if
      duration = totalDurationSeconds()
      position = effectivePlaybackPosition()
      if duration > 0 then position = duration
      if position < 1 then position = 1
      m.watchStatusInFlight = false
      print "WATCH_STATUS_FINISHED position="; position; " duration="; duration
      syncWatchStatus(position)
  end sub

  sub syncWatchStatus(position as integer)
      if m.watchStatusInFlight = true then return
      videoData = m.top.videoData
      if videoData = invalid then return
      authData = videoData.authData
      if authData = invalid then authData = m.top.authData
      if authData = invalid then return

      if position > 0 and m.lastSyncedPosition <> invalid and m.lastSyncedPosition >= 0
          delta = position - m.lastSyncedPosition
          if delta < 0 then delta = -delta
          if delta < 10 then return
      end if
      m.lastSyncedPosition = position
      m.watchStatusInFlight = true

      task = createObject("roSGNode", "APITask")
      task.request = {
          action: "updateWatchStatus",
          baseUrl: authData.baseUrl,
          proxyBaseUrl: authData.proxyBaseUrl,
          sid: authData.sid,
          synoToken: authData.synoToken,
          videoId: videoData.id,
          videoType: videoData.type,
          fileId: videoData.fileId,
          mapperId: videoData.mapperId,
          filePath: videoData.filePath,
          position: position
      }
      task.observeField("response", "onWatchStatusSynced")
      task.control = "RUN"
      m.watchStatusTask = task
  end sub

  sub onWatchStatusSynced(event as object)
      if event = invalid then return
      m.watchStatusInFlight = false
      response = event.getData()
      if response = invalid then return
      if response.success = true
          print "WATCH_STATUS_SYNC ok"
      else if response.error <> invalid
          print "WATCH_STATUS_SYNC error="; response.error
          if response.detail <> invalid then print "WATCH_STATUS_SYNC detail="; response.detail
      end if
  end sub

  sub onVideoErrorDetail(event as object)
      if event = invalid then return
      print "VIDEO_ERROR_DETAIL code="; m.videoNode.errorCode; " msg="; m.videoNode.errorMsg
  end sub

  sub onVideoBuffering(event as object)
      print "VIDEO_BUFFER "; event.getData()
  end sub

  sub ensureVideoFocus()
      if m.videoNode = invalid then return
      applyVideoUi()
      if m.customUi = true and m.hasError <> true
          ' The player handles every key itself. If the Video node kept focus it would also
          ' pause/resume on OK and Play by itself, and cancel out our own toggle.
          if m.menuOpen <> true then m.top.setFocus(true)
          return
      end if
      if m.videoNode.visible = true then m.videoNode.setFocus(true)
  end sub

  sub showError(msg as string)
      m.hasError = true
      m.top.findNode("backgroundRect").visible = true
      m.top.findNode("loadingOverlay").visible = false
      m.top.findNode("loadingLabel").visible = false
      m.top.findNode("videoTitle").visible = false
      errLabel = m.top.findNode("errorLabel")
      errLabel.text = msg + chr(10) + chr(10) + "Press Back to return."
      errLabel.visible = true
      m.top.setFocus(true)
  end sub

  ' ══ Custom player controls for streams made by the stream server ═════════════════
  ' The Roku's own controls cannot scrub a stream that is still being prepared, so the
  ' player draws its own progress bar, handles pause / skip, and draws subtitles itself
  ' (which also lets the viewer pick any subtitle or audio track from a menu).

  sub resetCustomUi()
      m.customUi = false
      m.paused = false
      m.scrubTarget = invalid
      m.scrubStreak = 0
      m.scrubDir = 0
      m.cues = invalid
      m.shownSubtitle = invalid
      m.activeSubId = ""
      m.pendingSubOption = invalid
      m.pendingSubLabel = ""
      m.subtitleAutoDone = false
      m.autoQueue = []
      m.autoLoading = false
      m.subtitleSeq = 0
      m.subtitleOptions = []
      m.audioOptions = []
      m.subtitleContext = invalid
      m.defaultSubtitleIndex = invalid
      m.activeAudio = invalid
      m.audioIndex = invalid
      m.burnIndex = invalid
      m.menuActions = []
      m.menuOpen = false
      m.sessionRestarts = 0
      m.introOverlayShown = false
      m.subtitlesAlways = true
      reg = createObject("roRegistrySection", "DSVideo")
      if reg.exists("subtitlesAlways") then m.subtitlesAlways = (reg.read("subtitlesAlways") <> "false")
      stopCustomUiTimers()
      hideSubtitle()
      menu = m.top.findNode("trackMenu")
      if menu <> invalid then menu.visible = false
  end sub

  sub applyVideoUi()
      if m.videoNode = invalid then return
      m.videoNode.enableUI = (m.customUi <> true)
  end sub

  sub stopCustomUiTimers()
      for each id in ["subtitleTimer", "scrubTimer", "toastTimer"]
          t = m.top.findNode(id)
          if t <> invalid then t.control = "stop"
      end for
  end sub

  sub setupCustomUi()
      applyVideoUi()
      timer = m.top.findNode("subtitleTimer")
      if m.customUi = true
          if timer <> invalid then timer.control = "start"
          if m.pendingSubOption <> invalid
              opt = m.pendingSubOption
              m.pendingSubOption = invalid
              loadSubtitleOption(opt)
          else
              autoLoadSubtitle()
          end if
      else
          if timer <> invalid then timer.control = "stop"
          hideSubtitle()
      end if
  end sub

  ' ── skipping ─────────────────────────────────────────────────────────────────
  sub scrubBy(delta as integer)
      if m.videoNode = invalid then return
      base = effectivePlaybackPosition()
      if m.scrubTarget <> invalid then base = int(m.scrubTarget)
      direction = 1
      if delta < 0 then direction = -1
      if m.scrubTarget <> invalid and direction = m.scrubDir
          m.scrubStreak = m.scrubStreak + 1
      else
          m.scrubStreak = 0
      end if
      m.scrubDir = direction
      stepSize = delta
      if m.scrubStreak >= 10
          stepSize = delta * 6
      else if m.scrubStreak >= 5
          stepSize = delta * 3
      end if
      target = base + stepSize
      total = totalDurationSeconds()
      if total > 0 and target > total - 10 then target = total - 10
      if target < 0 then target = 0
      m.scrubTarget = target
      showPlaybackOverlay()
      timer = m.top.findNode("scrubTimer")
      timer.control = "stop"
      timer.control = "start"
  end sub

  sub onScrubTimer(event as object)
      if event = invalid then return
      target = m.scrubTarget
      m.scrubTarget = invalid
      m.scrubStreak = 0
      if target = invalid then return
      seekServerTo(int(target))
      updatePlaybackOverlay()
  end sub

  ' Jump to a position in the whole video. Anything already prepared is instant; anything
  ' else starts the stream again from there (a few seconds).
  sub seekServerTo(absTarget as integer)
      baseOffset = int(m.nativeHlsResumeBase)
      encoded = int(m.videoNode.duration)
      relTarget = absTarget - baseOffset
      if relTarget >= 0 and encoded > 0 and relTarget <= encoded - 10
          m.userSeekTarget = relTarget
          m.videoNode.seek = relTarget
          if m.paused = true then m.videoNode.control = "resume"
          print "VIDEO_USER_SEEK in-range abs="; absTarget; " rel="; relTarget
      else
          print "VIDEO_USER_SEEK restart abs="; absTarget
          restartStreamAt(absTarget)
      end if
  end sub

  ' ── subtitles drawn by the player ───────────────────────────────────────────
  sub positionSubtitleGroup()
      group = m.top.findNode("subtitleGroup")
      if group = invalid then return
      if m.top.findNode("controlOverlay").visible = true
          group.translation = [0, 630]
      else
          group.translation = [0, 880]
      end if
  end sub

  sub hideSubtitle()
      m.shownSubtitle = invalid
      group = m.top.findNode("subtitleGroup")
      if group <> invalid then group.visible = false
  end sub

  sub onSubtitleTimer(event as object)
      if event = invalid then return
      if m.customUi <> true or m.cues = invalid
          if m.shownSubtitle <> invalid then hideSubtitle()
          return
      end if
      videoPos = m.videoNode.position
      if videoPos = invalid then return
      videoPos = videoPos + int(m.nativeHlsResumeBase)
      text = cueTextAt(videoPos)
      if m.shownSubtitle <> invalid and text = m.shownSubtitle then return
      if text = "" and m.shownSubtitle = invalid then return
      m.shownSubtitle = text
      setSubtitleText(text)
  end sub

  function cueTextAt(at as float) as string
      cues = m.cues
      n = cues.count()
      if n = 0 then return ""
      lo = 0
      hi = n - 1
      found = -1
      while lo <= hi
          midIdx = int((lo + hi) / 2)
          if cues[midIdx].s <= at
              found = midIdx
              lo = midIdx + 1
          else
              hi = midIdx - 1
          end if
      end while
      i = found
      while i >= 0 and i >= found - 3
          if cues[i].e >= at then return cues[i].t
          i = i - 1
      end while
      return ""
  end function

  sub setSubtitleText(text as string)
      group = m.top.findNode("subtitleGroup")
      if text = ""
          group.visible = false
          return
      end if
      label = m.top.findNode("subtitleLabel")
      boxRect = m.top.findNode("subtitleBox")
      label.text = text
      positionSubtitleGroup()
      group.visible = true
      bb = label.boundingRect()
      if bb <> invalid and bb.width > 0
          lt = label.translation
          boxRect.width = bb.width + 48
          boxRect.height = bb.height + 20
          boxRect.translation = [lt[0] + bb.x - 24, lt[1] + bb.y - 10]
          boxRect.visible = true
      else
          boxRect.visible = false
      end if
  end sub

  sub showToast(msg as string)
      label = m.top.findNode("toastLabel")
      label.text = msg
      label.visible = true
      timer = m.top.findNode("toastTimer")
      timer.control = "stop"
      timer.control = "start"
  end sub

  sub onToastTimer(event as object)
      m.top.findNode("toastLabel").visible = false
  end sub

  sub loadSubtitleOption(opt as object, auto = false as boolean)
      ctx = m.subtitleContext
      if ctx = invalid then return
      m.autoLoading = auto
      if auto <> true then m.autoQueue = []
      print "SUBTITLE_LOAD auto="; auto; " id="; opt.id; " kind="; opt.kind
      m.subtitleSeq = m.subtitleSeq + 1
      req = { action: "loadSubtitle", serverBase: ctx.serverBase, path: ctx.path, sid: ctx.sid, token: ctx.token, optionId: opt.id, seq: m.subtitleSeq }
      if opt.kind = "file"
          req.sidecar = opt.path
      else
          req.index = opt.index
      end if
      task = createObject("roSGNode", "APITask")
      task.request = req
      task.observeField("response", "onSubtitleLoaded")
      task.control = "RUN"
      m.subtitleTask = task
      m.activeSubId = opt.id
      m.pendingSubLabel = opt.label
      showToast("Loading subtitles: " + opt.label)
  end sub

  sub onSubtitleLoaded(event as object)
      response = event.getData()
      if response = invalid then return
      if response.seq <> m.subtitleSeq then return
      if response.success = true and response.cues <> invalid and response.cues.count() > 0
          m.cues = response.cues
          m.shownSubtitle = invalid
          m.autoQueue = []
          m.autoLoading = false
          print "SUBTITLE_ON cues="; response.cues.count(); " label="; m.pendingSubLabel
          showToast("Subtitles: " + m.pendingSubLabel)
      else
          m.activeSubId = ""
          detail = ""
          if response.error <> invalid then detail = " (" + response.error + ")"
          print "SUBTITLE_FAIL auto="; m.autoLoading; " detail="; detail
          if m.autoLoading = true and m.autoQueue.count() > 0
              autoTryNext()
              return
          end if
          m.autoLoading = false
          if response.success = true
              showToast("That subtitle track has no text")
          else
              showToast("Subtitles could not be loaded" + detail)
          end if
      end if
  end sub

  ' The video only has picture subtitles (PGS, DVD). They cannot be drawn as text, so when the
  ' stream is being re-encoded anyway, restart it once with the best picture track burned in.
  sub autoBurnSubtitle()
      if m.serverMode <> "transcode" or m.burnIndex <> invalid then return
      pick = invalid
      for each opt in m.subtitleOptions
          if opt.kind = "burn" and opt.forced <> true
              if pick = invalid then pick = opt
              if instr(1, lcase(opt.label), "english") > 0
                  pick = opt
                  exit for
              end if
          end if
      end for
      if pick = invalid then return
      print "SUBTITLE_AUTO burn id="; pick.id; " label="; pick.label
      m.burnIndex = pick.index
      m.activeSubId = pick.id
      m.cues = invalid
      hideSubtitle()
      showToast("Burning in subtitles: " + pick.label)
      restartStreamAt(effectivePlaybackPosition())
  end sub

  ' Take the next subtitle candidate when automatic loading failed on the previous one.
  sub autoTryNext()
      if m.autoQueue = invalid or m.autoQueue.count() = 0 then return
      opt = m.autoQueue.shift()
      loadSubtitleOption(opt, true)
  end sub

  ' Turn subtitles on by themselves when the setting allows: a file next to the video first,
  ' then the track the server prefers (English), then the first text track there is.
  sub autoLoadSubtitle()
      if m.subtitleAutoDone = true then return
      m.subtitleAutoDone = true
      if m.subtitlesAlways <> true then return
      ' Ordered candidates: files next to the video, the server's preferred track, then any
      ' other text track. If one fails or turns out empty the next one is tried.
      queue = []
      for each opt in m.subtitleOptions
          if opt.kind = "file" then queue.push(opt)
      end for
      if m.defaultSubtitleIndex <> invalid
          for each opt in m.subtitleOptions
              if opt.kind = "embedded" and opt.index = m.defaultSubtitleIndex then queue.push(opt)
          end for
      end if
      for each opt in m.subtitleOptions
          if opt.kind = "embedded"
              already = false
              for each q in queue
                  if q.id = opt.id then already = true
              end for
              if not already then queue.push(opt)
          end if
      end for
      print "SUBTITLE_AUTO candidates="; queue.count(); " options="; m.subtitleOptions.count()
      if queue.count() = 0
          autoBurnSubtitle()
          return
      end if
      m.autoQueue = queue
      autoTryNext()
  end sub

  ' ── subtitle and audio menu ─────────────────────────────────────────────────
  function menuMark(active as boolean) as string
      if active then return "[x]  "
      return "[  ]  "
  end function

  sub buildTrackMenu()
      content = createObject("roSGNode", "ContentNode")
      m.menuActions = []
      addMenuRow(content, menuMark(m.activeSubId = "" and m.burnIndex = invalid) + "Subtitles off", { kind: "subOff" })
      for each opt in m.subtitleOptions
          active = (m.activeSubId = opt.id)
          if opt.kind = "burn" then active = (m.burnIndex <> invalid and m.burnIndex = opt.index)
          addMenuRow(content, menuMark(active) + opt.label, { kind: "sub", opt: opt })
      end for
      if m.subtitleOptions.count() = 0
          addMenuRow(content, "      (no subtitles found for this video)", { kind: "none" })
      end if
      if m.audioOptions.count() > 1
          for each a in m.audioOptions
              active = (m.activeAudio <> invalid and m.activeAudio = a.index)
              addMenuRow(content, menuMark(active) + "Audio: " + a.label, { kind: "audio", opt: a })
          end for
      end if
      autoText = "Off"
      if m.subtitlesAlways = true then autoText = "On"
      addMenuRow(content, "Show subtitles automatically: " + autoText, { kind: "always" })
      m.top.findNode("trackList").content = content
  end sub

  sub addMenuRow(content as object, title as string, action as object)
      row = content.createChild("ContentNode")
      row.title = title
      m.menuActions.push(action)
  end sub

  sub openTrackMenu()
      if m.menuOpen = true then return
      buildTrackMenu()
      m.top.findNode("trackMenu").visible = true
      m.menuOpen = true
      list = m.top.findNode("trackList")
      list.jumpToItem = 0
      list.setFocus(true)
  end sub

  sub closeTrackMenu()
      m.top.findNode("trackMenu").visible = false
      m.menuOpen = false
      ' Explicitly let go of the list's focus first: a hidden node can otherwise keep it,
      ' which would let stray OK presses on this remote node be replayed as a fresh pick.
      m.top.findNode("trackList").setFocus(false)
      m.top.setFocus(true)
      ensureVideoFocus()
      showPlaybackOverlay()
  end sub

  sub onTrackSelected(event as object)
      ' Ignore a selection event that arrives after the menu is already closed (a stray
      ' repeat, or one queued right as we closed it), so it can't be replayed as a new pick.
      if m.menuOpen <> true then return
      idx = event.getData()
      if idx = invalid or idx < 0 or idx >= m.menuActions.count() then return
      action = m.menuActions[idx]
      kind = action.kind
      if kind = "subOff"
          m.autoQueue = []
          m.autoLoading = false
          m.activeSubId = ""
          m.cues = invalid
          hideSubtitle()
          m.subtitleAutoDone = true
          if m.burnIndex <> invalid
              m.burnIndex = invalid
              closeTrackMenu()
              restartStreamAt(effectivePlaybackPosition())
          else
              closeTrackMenu()
          end if
      else if kind = "sub"
          opt = action.opt
          m.subtitleAutoDone = true
          if opt.kind = "burn"
              m.burnIndex = opt.index
              m.activeSubId = opt.id
              m.cues = invalid
              hideSubtitle()
              closeTrackMenu()
              showToast("Burning in subtitles, restarting the video")
              restartStreamAt(effectivePlaybackPosition())
          else if m.burnIndex <> invalid
              ' Picture subtitles are part of the video; switching away means a new stream.
              m.burnIndex = invalid
              m.pendingSubOption = opt
              closeTrackMenu()
              restartStreamAt(effectivePlaybackPosition())
          else
              loadSubtitleOption(opt)
              closeTrackMenu()
          end if
      else if kind = "audio"
          m.audioIndex = action.opt.index
          m.activeAudio = action.opt.index
          closeTrackMenu()
          showToast("Changing audio, restarting the video")
          restartStreamAt(effectivePlaybackPosition())
      else if kind = "always"
          m.subtitlesAlways = not (m.subtitlesAlways = true)
          reg = createObject("roRegistrySection", "DSVideo")
          if m.subtitlesAlways
              reg.write("subtitlesAlways", "true")
          else
              reg.write("subtitlesAlways", "false")
          end if
          reg.flush()
          buildTrackMenu()
          m.top.findNode("trackList").jumpToItem = idx
          if m.subtitlesAlways = true and m.activeSubId = "" and m.burnIndex = invalid
              m.subtitleAutoDone = false
              autoLoadSubtitle()
          end if
      end if
  end sub

  function customKeyEvent(key as string) as boolean
      print "VIDEO_KEY(custom) key="; key; " state="; safeDynamicString(m.videoNode.state); " menu="; m.menuOpen
      if m.menuOpen = true
          if key = "back" or key = "options" then closeTrackMenu()
          return true
      end if
      if key = "back"
          m.userStopped = true
          stopStartupWatchdog()
          m.videoNode.control = "stop"
          reportPlaybackDone("back")
      else if key = "OK" or key = "play"
          togglePlayPause()
      else if key = "left" or key = "replay"
          scrubBy(-10)
      else if key = "right"
          scrubBy(10)
      else if key = "rewind"
          scrubBy(-60)
      else if key = "fastforward"
          scrubBy(60)
      else if key = "up"
          showPlaybackOverlay()
      else if key = "down" or key = "options"
          openTrackMenu()
      end if
      return true
  end function

  function onKeyEvent(key as string, press as boolean) as boolean
      if not press then return false
      if m.customUi = true and m.hasError <> true then return customKeyEvent(key)
      print "VIDEO_KEY key="; key; " state="; safeDynamicString(m.videoNode.state); " focus=player"
      if key = "back"
          m.userStopped = true
          stopStartupWatchdog()
          m.videoNode.control = "stop"
          reportPlaybackDone("back")
          return true
      else if key = "left" or key = "rewind"
          ensureVideoFocus()
          if key = "left" and m.top.findNode("controlOverlay").visible = true
              seekBy(-90)
              return true
          end if
          return false
      else if key = "right" or key = "fastforward"
          ensureVideoFocus()
          if key = "right" and m.top.findNode("controlOverlay").visible = true
              seekBy(90)
              return true
          end if
          return false
      else if key = "play"
          ensureVideoFocus()
          return false
      else if key = "up" or key = "OK" or key = "down"
          showPlaybackOverlay()
          ensureVideoFocus()
          return true
      end if
      return false
  end function
  
