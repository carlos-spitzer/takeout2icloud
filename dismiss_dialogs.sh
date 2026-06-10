#!/bin/bash
# Standalone dialog dismisser for Photos.app
# Runs as a separate process to avoid Apple Events deadlock
# with the main import process.

LOGFILE="${HOME}/.takeout2icloud/dismiss.log"
COUNT=0
# Wait this many seconds after detecting a dialog before dismissing it.
# This gives import_photos() time to read the result before we close the sheet.
DISMISS_DELAY=${DISMISS_DELAY:-1}

while true; do
    RESULT=$(osascript -e '
    tell application "System Events"
        tell process "Photos"
            if exists sheet 1 of window 1 then
                return "dialog_found"
            else
                return "none"
            end if
        end tell
    end tell
    ' 2>/dev/null)

    if [[ "$RESULT" == "dialog_found" ]]; then
        # Wait before dismissing, so import_photos() can collect results
        sleep "$DISMISS_DELAY"

        # Now dismiss
        CLICK_RESULT=$(osascript -e '
        tell application "System Events"
            tell process "Photos"
                if exists sheet 1 of window 1 then
                    try
                        set btns to name of every button of sheet 1 of window 1
                        if btns contains "Aceptar" then
                            click button "Aceptar" of sheet 1 of window 1
                            return "dismissed:Aceptar"
                        else if btns contains "No importar" then
                            click button "No importar" of sheet 1 of window 1
                            return "dismissed:No importar"
                        else if btns contains "OK" then
                            click button "OK" of sheet 1 of window 1
                            return "dismissed:OK"
                        else
                            keystroke return
                            return "dismissed:Return"
                        end if
                    on error errMsg
                        return "error:" & errMsg
                    end try
                else
                    return "already_gone"
                end if
            end tell
        end tell
        ' 2>/dev/null)

        if [[ "$CLICK_RESULT" == dismissed:* ]]; then
            COUNT=$((COUNT + 1))
            echo "$(date '+%H:%M:%S') [$COUNT] $CLICK_RESULT" >> "$LOGFILE"
        fi
        # Brief pause after dismiss before checking again
        sleep 0.5
    else
        sleep 1.5
    fi
done
