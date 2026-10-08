-- FaceTime auto-admit helper (Notification Center strategy).
--
-- BlueBubbles' own admitSelf is dead on Tahoe (it reads a notification DB that
-- moved and is WAL-locked). Instead we watch Notification Center for the
-- "FaceTime Link — Someone requested to join" notification and click its
-- approve control. That notification is a reliably-labeled element, unlike
-- FaceTime's opaque SwiftUI call window.
--
-- Structure observed on Tahoe 26.3:
--   notification container (AXGroup)
--     text group (AXGroup) -> "FaceTime Link" / "Someone requested to join" / "Nm ago"
--     AXButton   (actions: AXPress)   <- the approve/primary action
--     AXMenuButton (actions: AXShowMenu) <- options menu (approve/decline may live here)
--
-- Runs as a granted .app (Accessibility). Kept alive by launchd.

property logPath : missing value
property lastActed : "" -- de-dupe so we don't spam-click the same notification

on run
	set homePath to POSIX path of (path to home folder)
	set logPath to homePath & "imsg-relay/facetime-admit.log"
	logLine("=== FaceTime admit helper started (notification strategy) ===")
	repeat
		try
			admitPass()
		on error e
			logLine("pass error: " & e)
		end try
		delay 1
	end repeat
end run

on admitPass()
	tell application "System Events"
		if not (exists process "NotificationCenter") then return
		tell process "NotificationCenter"
			repeat with w in windows
				set joinText to missing value
				try
					set els to entire contents of w
					repeat with el in els
						try
							if (role of el) is "AXStaticText" and ((name of el as text) contains "requested to join") then
								set joinText to el
								exit repeat
							end if
						end try
					end repeat
				end try
				if joinText is not missing value then
					my handleJoinNotification(joinText)
					return
				end if
			end repeat
		end tell
	end tell
end admitPass

on handleJoinNotification(joinText)
	tell application "System Events"
		-- container = grandparent of the text (text -> textGroup -> container)
		set textGroup to (value of attribute "AXParent" of joinText)
		set container to (value of attribute "AXParent" of textGroup)

		-- a stable-ish key so we don't re-click the same live notification every second
		set noteKey to ""
		try
			set noteKey to (name of joinText as text) & "|" & (position of container as text)
		end try

		set kids to (UI elements of container)
		set theBtn to missing value
		set theMenu to missing value
		repeat with c in kids
			set r to (role of c as text)
			if r is "AXButton" then set theBtn to c
			if r is "AXMenuButton" then set theMenu to c
		end repeat

		-- Diagnostics: log the options-menu items once per notification.
		if noteKey is not lastActed and theMenu is not missing value then
			try
				perform action "AXShowMenu" of theMenu
				delay 0.5
				repeat with m in (menus of theMenu)
					repeat with mi in (menu items of m)
						my logLine("menu item: [" & (name of mi as text) & "]")
					end repeat
				end repeat
				key code 53 -- escape, close the menu without acting
			on error e
				my logLine("menu probe err: " & e)
			end try
		end if

		-- Primary action: click the approve button.
		if theBtn is not missing value then
			if noteKey is not lastActed then
				try
					click theBtn
					my logLine("*** clicked notification action button (approve?) key=" & noteKey)
					set lastActed to noteKey
				on error e
					my logLine("button click err: " & e)
				end try
			end if
		else
			my logLine("join notification present but NO AXButton child found")
		end if
	end tell
end handleJoinNotification

on logLine(m)
	try
		do shell script "printf '%s %s\\n' \"$(date '+%H:%M:%S')\" " & quoted form of m & " >> " & quoted form of logPath
	end try
end logLine
