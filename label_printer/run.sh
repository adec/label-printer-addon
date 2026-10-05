#!/usr/bin/with-contenv bashio
# ---------------------------------------------------------------------------
# Label Printer add-on
# Starts CUPS, auto-detects every supported USB label printer, registers each
# one as its own CUPS queue with the right driver, then runs the HTTP service.
#
# Plugged in + recognized = available: there are no per-printer toggles.
# The add-on options only declare which label roll is LOADED per printer
# (by DYMO part number / Zebra size) — that mirrors the physical roll.
#
# Supported today:
#   * DYMO LabelWriter (300/400/450/550/4XL/5XL)  — queue "dymo"
#   * Zebra ZPL/EPL desktop printers (ZD220, GK/GX420, …) — queue "zebra"
# ---------------------------------------------------------------------------

# Dev convenience: if a live copy exists in /share, use it so the service and
# CUPS config can be iterated by just restarting the add-on (no rebuild).
LIVE_DIR="/share/label-printer-addon/label_printer"
if bashio::fs.file_exists "${LIVE_DIR}/cupsd.conf"; then
    cp "${LIVE_DIR}/cupsd.conf" /etc/cups/cupsd.conf
    bashio::log.info "Using live cupsd.conf from /share"
fi
if bashio::fs.file_exists "${LIVE_DIR}/server.py"; then
    cp "${LIVE_DIR}/server.py" /server.py
    if bashio::fs.file_exists "${LIVE_DIR}/brother_network.py"; then
        cp "${LIVE_DIR}/brother_network.py" /brother_network.py
    fi
    bashio::log.info "Using live server.py from /share"
fi

DYMO_PRINTER="dymo"
ZEBRA_PRINTER="zebra"
export PRINTER_NAME="${DYMO_PRINTER}"   # default queue; keeps old callers working

# Map a detected USB device URI (e.g. usb://DYMO/LabelWriter%20450?serial=...)
# to the matching DYMO CUPS driver / PPD name.
detect_model() {
    local u
    # Match only the model name, not the ?serial=... suffix, so a serial number
    # that happens to contain 450/550/etc. can't cause a false match.
    u="$(printf '%s' "${1%%\?*}" | tr '[:upper:]' '[:lower:]')"
    case "$u" in
        *5xl*)       echo "lw5xl" ;;
        *550*turbo*) echo "lw550t" ;;
        *550*)       echo "lw550" ;;
        *4xl*)       echo "lw4xl" ;;
        *450*twin*)  echo "lw450tt" ;;
        *450*turbo*) echo "lw450t" ;;
        *450*duo*)   echo "lw450dl" ;;
        *450*)       echo "lw450" ;;
        *400*turbo*) echo "lw400t" ;;
        *400*)       echo "lw400" ;;
        *330*)       echo "lw330" ;;
        *)           echo "" ;;
    esac
}

# "104x159" (mm) -> "Custom.295x451" (PostScript points), which is how CUPS
# names a custom page size. Anything that isn't a plain WxH pair in millimetres
# is passed through, so a CUPS media name (w154h286) still works.
# points = mm * 72 / 25.4, done in integer maths as mm * 360 / 127 (rounded).
mm_to_media() {
    local spec w h
    spec="$(printf '%s' "$1" | tr -d ' ')"
    case "$spec" in
        [0-9]*x[0-9]*)
            w="${spec%%x*}"; h="${spec##*x}"
            case "$w$h" in
                *[!0-9]*) echo "$spec"; return ;;
            esac
            echo "Custom.$(( (w * 360 + 63) / 127 ))x$(( (h * 360 + 63) / 127 ))"
            ;;
        *) echo "$spec" ;;
    esac
}

# The option values are human-friendly ("99014 (54 x 101 mm)"); the first
# token is the part number / size the machine side works with.
MODEL="$(bashio::config 'printer_model')"
DYMO_CHOICE="$(bashio::config 'dymo_label')"; DYMO_CHOICE="${DYMO_CHOICE%% *}"
ZEBRA_CHOICE="$(bashio::config 'zebra_label')"; ZEBRA_CHOICE="${ZEBRA_CHOICE%% *}"

# DYMO part number -> the driver's native CUPS media name (native beats a
# Custom.* equivalent: exact margins from the PPD). "auto" lets the LW550
# family detect its own roll; 904980 is the wide 4XL/5XL roll (custom size,
# the narrow-carriage PPDs don't know it natively).
dymo_part_to_media() {
    case "$1" in
        auto)   echo "auto" ;;
        99010)  echo "w79h252" ;;
        99012)  echo "w102h252" ;;
        99014)  echo "w154h286" ;;
        99015)  echo "w154h198" ;;
        11352)  echo "w72h154" ;;
        11354)  echo "w162h90" ;;
        99019)  echo "w167h288" ;;
        904980) mm_to_media "104x159" ;;
        custom) mm_to_media "$(bashio::config 'dymo_custom_media' '54x101')" ;;
        *)      echo "$1" ;;
    esac
}
DEFAULT_MEDIA="$(dymo_part_to_media "${DYMO_CHOICE}")"
export DEFAULT_MEDIA

case "${ZEBRA_CHOICE}" in
    custom) ZEBRA_SIZE="$(bashio::config 'zebra_custom_size' '104x159')" ;;
    *)      ZEBRA_SIZE="${ZEBRA_CHOICE}" ;;
esac

bashio::log.info "Loaded labels — DYMO: ${DYMO_CHOICE} (${DEFAULT_MEDIA}), Zebra: ${ZEBRA_SIZE} mm"

# Register one CUPS queue. register_queue <name> <device-uri> <ppd-or-model> [media]
register_queue() {
    local name="$1" uri="$2" ppd="$3" media="$4"
    lpadmin -x "${name}" 2>/dev/null || true
    if [ -f "${ppd}" ]; then
        lpadmin -p "${name}" -v "${uri}" -E -P "${ppd}" || return 1
    else
        # Not a file on disk -> a CUPS model name such as drv:///sample.drv/zebra.ppd
        lpadmin -p "${name}" -v "${uri}" -E -m "${ppd}" || return 1
    fi
    cupsenable "${name}" || true
    cupsaccept "${name}" || true
    if [ -n "${media}" ] && [ "${media}" != "auto" ]; then
        lpoptions -p "${name}" -o "PageSize=${media}" || true
    fi
    return 0
}

bashio::log.info "Starting CUPS..."
mkdir -p /run/cups
chmod 755 /run/cups
cupsd

bashio::log.info "Waiting for CUPS socket..."
for _ in $(seq 1 30); do
    [ -e /run/cups/cups.sock ] && break
    sleep 1
done
sleep 1

PRINTERS_JSON="[]"
PRINTERS_JSON_PATH="/data/printers.json"

add_printer_json() {
    # add_printer_json <name> <kind> <model> <media> <raster:true|false>
    PRINTERS_JSON="$(printf '%s' "${PRINTERS_JSON}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
data.append({'name': '$1', 'kind': '$2', 'model': '$3',
             'media': '$4', 'raster': '$5' == 'true'})
print(json.dumps(data))
")"
}

write_printers_json() {
    printf '%s' "${PRINTERS_JSON}" > "${PRINTERS_JSON_PATH}"
}

# scan_dymo/scan_zebra register a printer the first time its USB device is
# seen; once a queue exists they no-op (so re-running them on a timer never
# tears down or reprints on an already-working printer). This is what lets a
# printer that powers up slower than the others after e.g. a mains outage —
# or one that drops off USB for a moment and comes back — get picked up
# without the add-on itself needing to restart. quiet=1 suppresses the
# "nothing found" log line for the periodic reruns, so a genuinely single-
# printer setup does not spam the log forever.
scan_dymo() {
    local quiet="${1:-0}"
    if lpstat -p "${DYMO_PRINTER}" >/dev/null 2>&1; then
        return 0
    fi
    local uri
    uri="$(lpinfo -v 2>/dev/null | grep -i 'dymo' | head -n 1 | awk '{print $2}')"
    if [ -z "${uri}" ]; then
        if [ "${quiet}" = "0" ]; then
            bashio::log.info "No DYMO LabelWriter found on USB."
        fi
        return 0
    fi

    local model="${MODEL}"
    if [ -z "${model}" ] || [ "${model}" = "auto" ]; then
        local detected
        detected="$(detect_model "${uri}")"
        if [ -n "${detected}" ]; then
            model="${detected}"
            bashio::log.info "Auto-detected DYMO model: ${model}"
        else
            model="lw550"
            bashio::log.warning "Could not auto-detect model from '${uri}'; using lw550."
        fi
    else
        bashio::log.info "Using configured DYMO model: ${model}"
    fi

    local ppd="/usr/share/cups/model/${model}.ppd"
    if ! bashio::fs.file_exists "${ppd}"; then
        bashio::log.warning "PPD ${ppd} not found, falling back to lw550.ppd"
        ppd="/usr/share/cups/model/lw550.ppd"
        model="lw550"
    fi
    export PRINTER_MODEL="${model}"

    bashio::log.info "Found DYMO at ${uri} — registering with ${ppd}"
    if register_queue "${DYMO_PRINTER}" "${uri}" "${ppd}" "${DEFAULT_MEDIA}"; then
        lpadmin -d "${DYMO_PRINTER}" || true   # default queue for callers that omit one
        add_printer_json "${DYMO_PRINTER}" "dymo" "${model}" "${DEFAULT_MEDIA}" "true"
        write_printers_json
        bashio::log.info "Printer '${DYMO_PRINTER}' ready (model=${model}, label=${DYMO_CHOICE}, media=${DEFAULT_MEDIA})."
    else
        bashio::log.error "Registering the DYMO queue failed."
    fi
}

scan_zebra() {
    local quiet="${1:-0}"
    if lpstat -p "${ZEBRA_PRINTER}" >/dev/null 2>&1; then
        return 0
    fi
    local uri
    uri="$(lpinfo -v 2>/dev/null | grep -iE 'zebra|ztc' | head -n 1 | awk '{print $2}')"
    if [ -z "${uri}" ]; then
        if [ "${quiet}" = "0" ]; then
            bashio::log.info "No Zebra printer found on USB."
        fi
        return 0
    fi

    # The device string states the active language (…ZD220-203dpi ZPL).
    # A queue with the matching driver accepts PNG/PDF (CUPS rasterises,
    # rastertolabel emits printer language); raw ZPL/EPL passes through
    # untouched either way.
    local ppd
    case "$(printf '%s' "${uri}" | tr '[:upper:]' '[:lower:]')" in
        *epl*)  ppd="drv:///sample.drv/zebraep2.ppd" ;;
        *cpcl*) ppd="drv:///sample.drv/zebracpl.ppd" ;;
        *)      ppd="drv:///sample.drv/zebra.ppd" ;;
    esac
    local media
    media="$(mm_to_media "${ZEBRA_SIZE}")"
    export ZEBRA_MEDIA="${media}"
    bashio::log.info "Found Zebra at ${uri} — registering with ${ppd}"
    if register_queue "${ZEBRA_PRINTER}" "${uri}" "${ppd}" "${media}"; then
        add_printer_json "${ZEBRA_PRINTER}" "zebra" "${ppd##*/}" "${media}" "true"
        write_printers_json
        bashio::log.info "Printer '${ZEBRA_PRINTER}' ready (label=${ZEBRA_SIZE} mm, media=${media})."
    else
        bashio::log.error "Registering the Zebra queue failed."
    fi
}

# USB/CUPS probing (lsusb, lpinfo, lpadmin) can take a good few seconds, and
# far longer than that when a printer's USB connection is flaky (e.g. still
# browning out after a power outage) — lpinfo/lpadmin then retry and stall.
# That must never delay the HTTP server itself: Supervisor's ingress check
# (and watchdog, if enabled) expects port 8000 to answer soon after the
# container starts, and kills+restarts the whole add-on if it does not — which
# only repeats the same slow/flaky USB probe again, forever. So the server
# starts first, and every bit of printer detection — the very first scan
# included — runs afterwards, in the background, never blocking it.
(
    # Detection is best-effort and must never take the add-on down with it.
    # bashio runs this whole script under `set -e -o pipefail -o errtrace`,
    # and inside this subshell that is a liability, not a safety net: a plain
    # `grep` with no match — no printer of that kind on USB yet — exits
    # non-zero, and under errexit+pipefail that alone killed this entire
    # subshell, taking the 5s rescan loop below with it. That is exactly why a
    # printer hot-plugged after boot was never picked up: only the boot-time
    # scan ever ran. Turn both off for this scope and handle failure
    # explicitly (scan_* already return 0 on "nothing found").
    set +o errexit
    set +o pipefail

    bashio::log.info "USB devices:"
    lsusb || true
    bashio::log.info "CUPS backends:"
    lpinfo -v || true

    scan_dymo 0
    scan_zebra 0
    export PRINTERS_JSON
    write_printers_json
    bashio::log.info "Configured printers: ${PRINTERS_JSON}"
    lpstat -t || true

    # Keep looking for printers that were not there yet at boot. server.py
    # notices printers.json change and geometry-fixes/warms just the printer
    # that is new — no add-on restart needed either way.
    while true; do
        sleep 5
        scan_dymo 1 || true
        scan_zebra 1 || true
    done
) &

# Forward a stop signal to every background child — relevant now that the
# print service is a plain background job rather than exec'd in as PID 1.
trap 'kill -TERM $(jobs -p) 2>/dev/null' TERM INT

bashio::log.info "Starting print service on :8000..."
python3 /server.py &
PRINT_SERVICE_PID=$!

# Block on the print service specifically, not the detection loop (which
# never exits on its own): if it crashes, run.sh exits with its code just
# like `exec` used to, so Supervisor still reacts to a real crash. `exec`
# itself was dropped because a background job of a process `exec` replaces
# is not reliably kept alive — the detection loop went silent after the
# very first scan when this used `exec python3 /server.py` here instead.
wait "${PRINT_SERVICE_PID}"
