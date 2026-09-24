#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# askpass.sh - SUDO_ASKPASS helper for the GUI (see _sudo() in lib/common.sh).
#
# sudo runs its askpass program as a bare executable path with the prompt as
# the only argument, so this wrapper turns that into the GUI's own password
# dialog: `sro-gui --askpass` in the AppImage (this file sits next to the
# frozen binary there), `python3 main.py --askpass` from a source checkout.
# The password goes to stdout, which is exactly what sudo -A reads.
HERE="$(dirname "$(readlink -f "$0")")"
if [ -x "$HERE/sro-gui" ]; then
    exec "$HERE/sro-gui" --askpass "$@"
fi
exec "${SRO_GUI_PYTHON:-python3}" "$HERE/main.py" --askpass "$@"
