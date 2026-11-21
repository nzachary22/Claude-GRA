#!/bin/sh
# Installs Grant Reporting Assistant for the ChatGPT desktop app (Mac).
# Finds Python 3.11 or newer (python.org or Homebrew) and runs the installer next to this file.
HERE="$(cd "$(dirname "$0")" && pwd)"
for c in /Library/Frameworks/Python.framework/Versions/3.1[1-9]/bin/python3 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3 \
         python3.14 python3.13 python3.12 python3.11 python3; do
  p="$(command -v "$c" 2>/dev/null)" || continue
  if [ "$p" = /usr/bin/python3 ]; then
    xcode-select -p >/dev/null 2>&1 || continue
  fi
  if "$p" -c "import sys; raise SystemExit(sys.version_info < (3, 11))" >/dev/null 2>&1; then
    "$p" "$HERE/files/install.py" "$@"
    status=$?
    echo
    echo "You can close this window now."
    exit $status
  fi
done
echo
echo "Grant Reporting Assistant needs Python 3.11 or newer, which was not found on this Mac."
echo "The Python download page will open now. Download Python, open its installer, then run this file again."
open "https://www.python.org/downloads/macos/"
exit 1
