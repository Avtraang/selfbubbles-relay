-- FaceTime auto-admit LOGIC (loaded + run each second by the stable loader).
-- Editing this file needs NO re-grant (loader bundle is unchanged).
--
-- DISCOVERY/CONTROL build. Trigger files in ~/imsg-relay drive it:
--   ft-shot        -> screencapture the screen to shots/latest.png (needs Screen Recording)
--   ft-dump        -> dump FaceTime window tree WITH positions to the log
--   ft-nc          -> dump NotificationCenter tree to the log
--   ft-click       -> click the FaceTime AXButton whose name/desc contains this file's text
--   ft-clickxy     -> click at screen coords "x,y" from this file's text
-- Triggers are consumed after running.

on admitPass()
	set home to POSIX path of (path to home folder)
	set base to home & "imsg-relay/"
	if triggered(base & "ft-shot") then my shoot()
	if triggered(base & "ft-dump") then my dumpProc("FaceTime")
	if triggered(base & "ft-nc") then my dumpProc("NotificationCenter")
	my consume(base & "ft-click", "clickBtn")
	my consume(base & "ft-clickxy", "clickXY")
	my consume(base & "ft-region", "region")
	my consume(base & "ft-snap", "snap")
	if triggered(base & "ft-closewins") then my closeWins()
	if triggered(base & "ft-frame") then my frameOf("FaceTime")
end admitPass

-- Close all FaceTime windows (clean slate) by pressing each window's close button.
on closeWins()
	tell application "System Events"
		if not (exists process "FaceTime") then return
		tell process "FaceTime"
			repeat 8 times
				if (count of windows) is 0 then exit repeat
				set didClose to false
				try
					repeat with b in (buttons of window 1)
						if (description of b as text) contains "close" then
							click b
							set didClose to true
							exit repeat
						end if
					end repeat
				end try
				if not didClose then
					try
						click button 1 of window 1
					on error
						exit repeat
					end try
				end if
				delay 0.3
			end repeat
		end tell
	end tell
	my logLine("closeWins done")
end closeWins

-- Move + resize FaceTime window 1 to a fixed rect "x,y,w,h" (screen points),
-- so every downstream coordinate is deterministic regardless of where the
-- user left/sized the window. Writes the resulting frame to ft-frame-out.txt.
on snapWindow(coords)
	set outFrame to ""
	tell application "System Events"
		if not (exists process "FaceTime") then return
		tell process "FaceTime"
			if (count of windows) < 1 then return
			try
				set frontmost to true   -- raise FaceTime so no browser intercepts clicks
			end try
			set AppleScript's text item delimiters to ","
			set xx to (text item 1 of coords) as integer
			set yy to (text item 2 of coords) as integer
			set ww to (text item 3 of coords) as integer
			set hh to (text item 4 of coords) as integer
			set AppleScript's text item delimiters to ""
			set w to window 1
			try
				set position of w to {xx, yy}
			end try
			try
				set size of w to {ww, hh}
			end try
			delay 0.2
			try
				set position of w to {xx, yy}
			end try
			set pz to (position of w)
			set sz to (size of w)
			set outFrame to ((item 1 of pz) as text) & "," & ((item 2 of pz) as text) & "," & ((item 1 of sz) as text) & "," & ((item 2 of sz) as text)
		end tell
	end tell
	if outFrame is not "" then
		set home to POSIX path of (path to home folder)
		do shell script "printf '%s' " & quoted form of outFrame & " > " & quoted form of (home & "imsg-relay/ft-frame-out.txt")
		my logLine("snapped -> " & outFrame)
	end if
end snapWindow

on triggered(p)
	try
		do shell script "test -f " & quoted form of p
		do shell script "rm -f " & quoted form of p
		return true
	on error
		return false
	end try
end triggered

-- If file exists, read its text, delete it, and dispatch to the named handler.
on consume(p, which)
	try
		do shell script "test -f " & quoted form of p
		set txt to (do shell script "cat " & quoted form of p)
		do shell script "rm -f " & quoted form of p
		if which is "clickBtn" then my clickFTButton(txt)
		if which is "clickXY" then my clickAt(txt)
		if which is "region" then my regionShot(txt)
		if which is "snap" then my snapWindow(txt)
	end try
end consume

-- Capture a screen region "x,y,w,h" (screen points) at full res, upscaled.
on regionShot(coords)
	try
		set home to POSIX path of (path to home folder)
		set out to home & "imsg-relay/shots/region.png"
		do shell script "/usr/sbin/screencapture -x -R" & coords & " " & quoted form of out & "; /usr/bin/sips --resampleWidth 1600 " & quoted form of out & " >/dev/null 2>&1 || true"
		my logLine("region shot " & coords)
	on error e
		my logLine("regionShot err: " & e)
	end try
end regionShot

on shoot()
	try
		set home to POSIX path of (path to home folder)
		set p to home & "imsg-relay/shots/latest.png"
		set fpath to home & "imsg-relay/shots/full.png"
		do shell script "mkdir -p " & quoted form of (home & "imsg-relay/shots") & "; /usr/sbin/screencapture -x " & quoted form of fpath & "; /bin/cp " & quoted form of fpath & " " & quoted form of p & "; /usr/bin/sips -Z 1600 " & quoted form of p & " >/dev/null 2>&1 || true"
		my logLine("screenshot -> " & p)
	on error e
		my logLine("shoot err: " & e)
	end try
end shoot

on clickFTButton(want)
	tell application "System Events"
		if not (exists process "FaceTime") then return
		tell process "FaceTime"
			repeat with w in windows
				try
					repeat with el in (entire contents of w)
						try
							if (role of el as text) is "AXButton" then
								set n to ""
								try
									set n to (name of el as text)
								end try
								set d to ""
								try
									set d to (description of el as text)
								end try
								if (n contains want) or (d contains want) then
									click el
									my logLine("clicked FaceTime button '" & want & "' (name=" & n & " desc=" & d & ")")
									return
								end if
							end if
						end try
					end repeat
				end try
			end repeat
		end tell
	end tell
	my logLine("clickFTButton: no match for '" & want & "'")
end clickFTButton

on clickAt(coords)
	try
		set AppleScript's text item delimiters to ","
		set xx to (text item 1 of coords) as integer
		set yy to (text item 2 of coords) as integer
		set AppleScript's text item delimiters to ""
		tell application "System Events" to click at {xx, yy}
		my logLine("clicked at " & xx & "," & yy)
	on error e
		my logLine("clickAt err: " & e)
	end try
end clickAt

-- Dump every element (role/name/desc/position/size) of a process's windows.
on dumpProc(procName)
	my logLine("---- DUMP " & procName & " ----")
	tell application "System Events"
		if not (exists process procName) then
			my logLine(procName & ": not running")
			return
		end if
		tell process procName
			set wc to (count of windows)
			my logLine(procName & " windows=" & wc)
			repeat with wi from 1 to wc
				my logLine("  == window " & wi & " ==")
				try
					repeat with el in (entire contents of window wi)
						try
							set r to (role of el as text)
							set n to ""
							try
								set n to (name of el as text)
							end try
							set d to ""
							try
								set d to (description of el as text)
							end try
							set ps to ""
							try
								set ps to (position of el as text) & " " & (size of el as text)
							end try
							if (r is "AXButton") or (r is "AXCheckBox") or (r is "AXImage") or (r is "AXStaticText") or (n is not "") then
								my logLine("    " & r & " [" & n & "] (" & d & ") @" & ps)
							end if
						end try
					end repeat
				end try
			end repeat
		end tell
	end tell
	my logLine("---- END DUMP " & procName & " ----")
end dumpProc

on frameOf(procName)
	set frameStr to ""
	tell application "System Events"
		if not (exists process procName) then return
		tell process procName
			if (count of windows) < 1 then return
			try
				set w to window 1
				set pz to (position of w)
				set sz to (size of w)
				set frameStr to ((item 1 of pz) as text) & "," & ((item 2 of pz) as text) & "," & ((item 1 of sz) as text) & "," & ((item 2 of sz) as text)
			end try
		end tell
	end tell
	if frameStr is not "" then
		set home to POSIX path of (path to home folder)
		do shell script "printf '%s' " & quoted form of frameStr & " > " & quoted form of (home & "imsg-relay/ft-frame-out.txt")
		my logLine("frame=" & frameStr)
	end if
end frameOf

on logLine(m)
	try
		set home to POSIX path of (path to home folder)
		do shell script "printf '%s %s\\n' \"$(date '+%H:%M:%S')\" " & quoted form of m & " >> " & quoted form of (home & "imsg-relay/facetime-admit.log")
	end try
end logLine
