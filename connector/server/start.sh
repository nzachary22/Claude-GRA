#!/bin/sh
# Starts the connector on a Mac with the newest Python 3.11+ from python.org or Homebrew.
# The Claude app starts programs with a short search path, so the usual places are tried by name.
# /usr/bin/python3 is only tried when Apple's developer tools are installed, because otherwise it
# opens an install prompt instead of running.
HERE="$(cd "$(dirname "$0")" && pwd)"
for c in /Library/Frameworks/Python.framework/Versions/3.1[1-9]/bin/python3 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3 \
         python3.14 python3.13 python3.12 python3.11 python3; do
  p="$(command -v "$c" 2>/dev/null)" || continue
  if [ "$p" = /usr/bin/python3 ]; then
    xcode-select -p >/dev/null 2>&1 || continue
  fi
  if "$p" -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >/dev/null 2>&1; then
    exec "$p" "$HERE/main.py" "$@"
  fi
done
# No suitable Python: start with any Python 3 so the chat can explain what to install.
for c in /usr/local/bin/python3 /opt/homebrew/bin/python3 python3; do
  p="$(command -v "$c" 2>/dev/null)" || continue
  if [ "$p" = /usr/bin/python3 ]; then xcode-select -p >/dev/null 2>&1 || continue; fi
  exec "$p" "$HERE/main.py" "$@"
done
echo "Grant Reporting Assistant needs Python 3.11 or newer: https://www.python.org/downloads/macos/" >&2
exit 1
