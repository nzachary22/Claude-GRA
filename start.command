#!/bin/bash
# Grant Reporting Assistant V.1.2 for Mac - starts the local app and opens the browser.
# Double-click this file in Finder. Setup runs on first use, and again whenever
# this folder moves or a new version changes the requirements. The Python
# environment lives outside this folder, in ~/Library/Application Support.
# Setup also adds a "Grant Reporting Assistant" app to the Applications folder
# in your home folder and shows it in Finder, ready to drag to the Dock, so later
# the app starts from there.

cd "$(dirname "$0")" || exit 1
GRA_DIR="$(pwd -P)"
SUPPORT="$HOME/Library/Application Support/GrantReportingAssistant"
VENV="$SUPPORT/venv"
STAMP="$VENV/gra-setup.txt"
APP_NAME="Grant Reporting Assistant"
printf '\033]0;%s\007' "$APP_NAME"

stop() {
  echo
  echo "$1"
  echo
  read -r -p "Press Return to close this window. " _
  exit 1
}

case "$GRA_DIR" in
  */AppTranslocation/*|/private/var/folders/*|/var/folders/*)
    stop "This is running from a temporary copy. Move the Grant Reporting Assistant
folder to your Documents folder, then double-click start.command there." ;;
esac

stamp_text() { pwd -P; cat pyproject.toml; }

find_python() {
  # The newest Python 3.11+ from python.org or Homebrew. /usr/bin/python3 is only
  # tried when Apple's developer tools are installed, because otherwise it opens
  # an install prompt instead of running.
  local c p
  for c in python3.14 python3.13 python3.12 python3.11 \
           /Library/Frameworks/Python.framework/Versions/3.1[1-9]/bin/python3 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    if [ "$p" = /usr/bin/python3 ]; then
      xcode-select -p >/dev/null 2>&1 || continue
    fi
    if "$p" -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >/dev/null 2>&1; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

make_app() {
  # A small app that opens this file, rewritten on every setup so it follows the
  # folder if it moves. Made on this computer, so the Mac does not block it.
  # A failure here is not fatal.
  local apps="$HOME/Applications" app tmp
  app="$apps/$APP_NAME.app"
  tmp="$(mktemp -d)" || return 1
  mkdir -p "$apps" || return 1
  local target
  target="$(printf '%s' "$GRA_DIR/start.command" | sed 's/\\/\\\\/g; s/"/\\"/g')"
  osacompile -o "$tmp/$APP_NAME.app" \
    -e "do shell script \"open -a Terminal \" & quoted form of \"$target\"" >/dev/null 2>&1 || return 1
  cp "$GRA_DIR/gra/static/icon.icns" "$tmp/$APP_NAME.app/Contents/Resources/applet.icns" 2>/dev/null
  codesign --force --sign - "$tmp/$APP_NAME.app" >/dev/null 2>&1
  rm -rf "$app" && mv "$tmp/$APP_NAME.app" "$app" || return 1
  rmdir "$tmp" 2>/dev/null
  touch "$app"
  return 0
}

needs_setup() {
  [ -x "$VENV/bin/python" ] || return 0
  [ -f "$STAMP" ] || return 0
  stamp_text | cmp -s - "$STAMP" || return 0
  return 1
}

if needs_setup; then
  PY="$(find_python)" || {
    open "https://www.python.org/downloads/macos/" 2>/dev/null
    stop "Python 3.11 or newer is required. The download page is now open in your
browser: download the latest macOS installer, run it, then double-click
start.command again."
  }
  echo "Setting up. This takes a minute..."
  # Files unzipped from a download are marked as coming from the internet. This
  # folder has now been opened on purpose, so clear the mark for the files in it.
  xattr -dr com.apple.quarantine "$GRA_DIR" 2>/dev/null
  mkdir -p "$SUPPORT"
  [ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
  [ -x "$VENV/bin/python" ] || stop "Setup failed: could not create the Python environment."
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet -e . || stop "Setup failed. Check the internet connection and try again."
  stamp_text > "$STAMP"
  if make_app; then
    echo "Added a \"$APP_NAME\" app to the Applications folder in your home folder."
    echo "Drag it from the Finder window to the Dock to start the app from there."
    open -R "$HOME/Applications/$APP_NAME.app" 2>/dev/null
  else
    echo "Could not add the $APP_NAME app. Start the app with start.command in this folder instead."
  fi
fi

echo "Grant Reporting Assistant is running at http://127.0.0.1:8765/"
echo "Close this window to stop it."
"$VENV/bin/python" -m gra.server || stop "The app stopped with an error (shown above)."
