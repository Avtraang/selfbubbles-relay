-- Stable loader for the FaceTime auto-admit helper.
--
-- This bundle is what holds the Accessibility grant, so it must NEVER change
-- (any edit changes its ad-hoc code hash and revokes the grant). All real logic
-- lives in ft-admit-logic.scpt, which this loads and runs once per second — so
-- the logic can be edited freely without re-granting.

on run
	set homePath to POSIX path of (path to home folder)
	set logicPath to homePath & "imsg-relay/ft-admit-logic.scpt"
	set logPath to homePath & "imsg-relay/facetime-admit.log"
	logTo(logPath, "=== FaceTime admit loader started ===")
	repeat
		try
			set logic to (load script (POSIX file logicPath))
			tell logic to admitPass()
		on error e
			logTo(logPath, "loader err: " & e)
		end try
		delay 1
	end repeat
end run

on logTo(logPath, m)
	try
		do shell script "printf '%s %s\\n' \"$(date '+%H:%M:%S')\" " & quoted form of m & " >> " & quoted form of logPath
	end try
end logTo
