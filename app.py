#!/usr/bin/env python3
"""Pi Streamer — Line-in to Icecast streaming server.

Streams audio from a USB sound card via ALSA to an Icecast MP3 stream.
Designed for always-on headless operation on Raspberry Pi.

Pipeline: arecord (ALSA) → sox (EQ/gate) → ffmpeg (MP3) → Icecast

Environment variables (from pi-streamer.conf):
    ALSA_DEVICE              ALSA capture device (default: hw:1,0)
    ICECAST_HOST             Icecast hostname (default: localhost)
    ICECAST_PORT             Icecast port (default: 8000)
    ICECAST_SOURCE_PASSWORD  Icecast source password (default: hackme)
    WEB_UI_PORT              Web UI port (default: 5080)
"""

import json as jsonlib
import os
import random
import subprocess
import threading
import time
from flask import Flask, render_template, jsonify, request
from urllib.request import urlopen

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ALSA_DEVICE = os.environ.get("ALSA_DEVICE", "hw:1,0")
ICECAST_HOST = os.environ.get("ICECAST_HOST", "localhost")
ICECAST_PORT = int(os.environ.get("ICECAST_PORT", "8000"))
ICECAST_SOURCE_PASSWORD = os.environ.get("ICECAST_SOURCE_PASSWORD", "hackme")
WEB_UI_PORT = int(os.environ.get("WEB_UI_PORT", "5080"))
INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(INSTALL_DIR, "tuning_state.json")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
pipeline_lock = threading.Lock()
state = {
    "running": False,
    "proc": None,
    "monitor_thread": None,
    "signal_level": 0.0,
    "peak_level": 0.0,
    "error": None,
    "last_cmd": "",
}

tuning = {
    "bitrate": 128,
    "gate_threshold": 2,     # 0=off, 1-10 noise gate aggressiveness
    "eq_low_cut": 200,       # Hz — highpass, kill below this
    "eq_high_cut": 4000,     # Hz — lowpass, kill above this
    "eq_speech_boost": 0,    # dB — boost speech band (1-2kHz), 0=off
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def save_tuning():
    """Persist current tuning to disk."""
    try:
        with open(STATE_FILE, "w") as f:
            jsonlib.dump(tuning, f, indent=2)
        print(f"[STREAM] Saved tuning to {STATE_FILE}", flush=True)
    except Exception as e:
        print(f"[STREAM] Failed to save tuning: {e}", flush=True)


def load_tuning():
    """Load saved tuning from disk if available."""
    try:
        with open(STATE_FILE) as f:
            saved = jsonlib.load(f)
        for k, v in saved.items():
            if k in tuning:
                tuning[k] = v
        print(f"[STREAM] Loaded tuning: bitrate={tuning['bitrate']}, "
              f"low={tuning['eq_low_cut']}, high={tuning['eq_high_cut']}", flush=True)
    except FileNotFoundError:
        print("[STREAM] No saved tuning found, using defaults", flush=True)
    except Exception as e:
        print(f"[STREAM] Failed to load tuning: {e}", flush=True)


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------
def build_arecord_args():
    """Build arecord command for ALSA capture."""
    return [
        "arecord",
        "-D", ALSA_DEVICE,
        "-f", "S16_LE",
        "-r", "44100",
        "-c", "1",
        "-t", "raw",
    ]


def build_sox_filter_args():
    """Build sox filter chain: highpass → lowpass → speech boost → noise gate."""
    thresh = int(tuning.get("gate_threshold", 2))
    low_cut = int(tuning.get("eq_low_cut", 200))
    high_cut = int(tuning.get("eq_high_cut", 4000))
    speech_boost = int(tuning.get("eq_speech_boost", 0))

    effects = []

    if low_cut > 0:
        effects += ["highpass", str(low_cut)]

    if 0 < high_cut < 20000:
        effects += ["lowpass", str(high_cut)]

    if speech_boost > 0:
        effects += ["equalizer", "1500", "1.5q", f"+{speech_boost}"]

    if thresh > 0:
        knee = int(-70 + (thresh - 1) * 5.5)
        above = min(knee + 15, -5)
        tf = f"6:-inf,-inf,{knee},-inf,{above},{above},0,0"
        effects += ["compand", "0.01,0.3", tf, "0"]

    if not effects:
        effects = ["vol", "1.0"]

    return [
        "sox",
        "-t", "raw", "-r", "44100", "-e", "signed-integer", "-b", "16", "-c", "1", "-",
        "-t", "raw", "-r", "44100", "-e", "signed-integer", "-b", "16", "-c", "1", "-",
        *effects,
    ]


def build_ffmpeg_args():
    """Build ffmpeg MP3 encoder → Icecast."""
    icecast_url = (f"icecast://source:{ICECAST_SOURCE_PASSWORD}"
                   f"@{ICECAST_HOST}:{ICECAST_PORT}/scanner")
    return [
        "ffmpeg",
        "-hide_banner",
        "-f", "s16le",
        "-ar", "44100",
        "-ac", "1",
        "-i", "pipe:0",
        "-codec:a", "libmp3lame",
        "-b:a", f"{int(tuning['bitrate'])}k",
        "-f", "mp3",
        "-content_type", "audio/mpeg",
        icecast_url,
    ]


def build_shell_command():
    """Build full pipeline: arecord | sox | ffmpeg."""
    arecord = " ".join(build_arecord_args())
    sox = " ".join(build_sox_filter_args())
    ffmpeg = " ".join(build_ffmpeg_args())
    kill = "pkill -9 arecord; pkill -9 sox; pkill -9 ffmpeg; sleep 1"
    return f"{kill}; {arecord} | {sox} | {ffmpeg}"


# ---------------------------------------------------------------------------
# Icecast health check
# ---------------------------------------------------------------------------
def poll_icecast_stats():
    """Check if /scanner mount is active on Icecast."""
    try:
        url = f"http://{ICECAST_HOST}:{ICECAST_PORT}/status-json.xsl"
        with urlopen(url, timeout=2) as resp:
            data = jsonlib.loads(resp.read().decode())
        source = data.get("icestats", {}).get("source")
        if source is None:
            return 0.0
        sources = [source] if isinstance(source, dict) else source
        for s in sources:
            if "/scanner" in s.get("listenurl", ""):
                return 65.0
        return 0.0
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Monitor / Watchdog
# ---------------------------------------------------------------------------
def monitor_loop():
    """Background watchdog: monitors process health AND Icecast mount.

    Restarts pipeline if:
      - Pipeline process exits
      - Icecast /scanner mount disappears for 20+ seconds
    """
    decay = 0.9
    restart_count = 0
    max_restarts = 50
    mount_missing_count = 0
    MOUNT_MISSING_THRESHOLD = 20
    heartbeat_counter = 0

    while state["running"]:
        time.sleep(1)
        heartbeat_counter += 1

        if heartbeat_counter % 300 == 0:
            print(f"[STREAM] Heartbeat: running, restarts={restart_count}",
                  flush=True)

        # Check 1: Process died
        proc_dead = state["proc"] and state["proc"].poll() is not None

        # Check 2: Icecast mount missing
        level = poll_icecast_stats()
        if level > 0:
            mount_missing_count = 0
            level += random.uniform(-15, 15)
            level = max(10.0, min(95.0, level))
        else:
            mount_missing_count += 1

        state["signal_level"] = round(level, 1)
        state["peak_level"] = max(level, state["peak_level"] * decay)

        # Decide if restart needed
        needs_restart = False
        reason = ""

        if proc_dead:
            needs_restart = True
            err = ""
            try:
                err = (state["proc"].stderr.read().decode(errors="replace")[:200]
                       if state["proc"].stderr else "")
            except:
                pass
            reason = f"Process exited: {err}" if err else "Process exited"

        elif mount_missing_count >= MOUNT_MISSING_THRESHOLD:
            needs_restart = True
            reason = f"Icecast mount missing for {mount_missing_count}s"

        if not needs_restart:
            continue

        # Restart
        restart_count += 1
        state["signal_level"] = 0
        state["peak_level"] = 0
        print(f"[STREAM] {reason}", flush=True)

        if restart_count > max_restarts:
            state["running"] = False
            state["error"] = f"Gave up after {max_restarts} restarts"
            print("[STREAM] Max restarts reached, giving up", flush=True)
            return

        print(f"[STREAM] Auto-restart {restart_count}/{max_restarts} in 3s...",
              flush=True)
        state["error"] = f"Restarting ({restart_count})..."
        state["running"] = False
        time.sleep(3)

        result = start_pipeline()
        if result.get("ok"):
            print("[STREAM] Auto-restart successful", flush=True)
            mount_missing_count = 0
            return
        else:
            print(f"[STREAM] Auto-restart failed: {result.get('error')}",
                  flush=True)
            state["error"] = result.get("error")
            state["running"] = True
            mount_missing_count = 0
            time.sleep(5)
            continue

    state["signal_level"] = 0
    state["peak_level"] = 0


# ---------------------------------------------------------------------------
# Kill / Start / Stop
# ---------------------------------------------------------------------------
def kill_existing():
    """Kill all pipeline processes."""
    subprocess.run(["pkill", "-9", "arecord"], capture_output=True)
    subprocess.run(["pkill", "-9", "sox"], capture_output=True)
    subprocess.run(["pkill", "-9", "ffmpeg"], capture_output=True)
    time.sleep(1)


def start_pipeline():
    """Start the audio pipeline."""
    with pipeline_lock:
        if state["running"]:
            icecast_ok = poll_icecast_stats() > 0
            proc_alive = state["proc"] and state["proc"].poll() is None
            if icecast_ok and proc_alive:
                return {"ok": False, "error": "Already running"}
            print("[STREAM] Stale state detected, forcing cleanup...",
                  flush=True)
            state["running"] = False
            if state["proc"]:
                try:
                    state["proc"].kill()
                    state["proc"].wait(timeout=3)
                except:
                    pass

        state["error"] = None
        state["signal_level"] = 0
        state["peak_level"] = 0
        kill_existing()

        shell_cmd = build_shell_command()
        state["last_cmd"] = shell_cmd
        print(f"[STREAM] Command: {shell_cmd}", flush=True)

        try:
            proc = subprocess.Popen(
                shell_cmd,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            time.sleep(2)

            if proc.poll() is not None:
                err = proc.stderr.read().decode(errors="replace")
                return {"ok": False, "error": f"Pipeline exited: {err}"}

            state["proc"] = proc
            state["running"] = True

            t = threading.Thread(target=monitor_loop, daemon=True)
            t.start()
            state["monitor_thread"] = t

            return {"ok": True, "cmd": shell_cmd}

        except Exception as e:
            kill_existing()
            return {"ok": False, "error": str(e)}


def stop_pipeline():
    """Force-stop everything regardless of current state."""
    with pipeline_lock:
        state["running"] = False
        if state["proc"]:
            try:
                state["proc"].kill()
                state["proc"].wait(timeout=3)
            except:
                pass
        kill_existing()
        state["proc"] = None
        state["monitor_thread"] = None
        state["signal_level"] = 0
        state["peak_level"] = 0
        state["error"] = None
        return {"ok": True}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    for key in ("bitrate", "gate_threshold", "eq_low_cut", "eq_high_cut",
                "eq_speech_boost"):
        if key in data:
            tuning[key] = int(data[key])
    result = start_pipeline()
    if result.get("ok"):
        save_tuning()
    return jsonify(result)


@app.route("/api/stop", methods=["POST"])
def api_stop():
    return jsonify(stop_pipeline())


@app.route("/api/status")
def api_status():
    return jsonify({
        "running": state["running"],
        "signal_level": state["signal_level"],
        "peak_level": state["peak_level"],
        "error": state["error"],
        "tuning": tuning,
        "last_cmd": state["last_cmd"],
        "alsa_device": ALSA_DEVICE,
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"[STREAM] Pi Streamer starting", flush=True)
    print(f"[STREAM] ALSA device: {ALSA_DEVICE}", flush=True)
    print(f"[STREAM] Icecast: {ICECAST_HOST}:{ICECAST_PORT}", flush=True)
    print(f"[STREAM] Web UI: 0.0.0.0:{WEB_UI_PORT}", flush=True)

    load_tuning()

    def auto_start():
        """Wait for Icecast, then auto-start pipeline."""
        for attempt in range(15):
            try:
                with urlopen(f"http://{ICECAST_HOST}:{ICECAST_PORT}/",
                             timeout=2):
                    break
            except Exception:
                print(f"[STREAM] Waiting for Icecast... ({attempt+1}/15)",
                      flush=True)
                time.sleep(2)
        else:
            print("[STREAM] WARNING: Icecast not reachable, starting anyway",
                  flush=True)

        time.sleep(1)
        print(f"[STREAM] Auto-starting pipeline...", flush=True)
        result = start_pipeline()
        if result.get("ok"):
            print("[STREAM] Auto-start successful", flush=True)
        else:
            print(f"[STREAM] Auto-start failed: {result.get('error')}",
                  flush=True)

    threading.Thread(target=auto_start, daemon=True).start()

    app.run(host="0.0.0.0", port=WEB_UI_PORT, debug=False)
