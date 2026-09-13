#!/usr/bin/env python3
"""
CHIP — native Linux desktop app.

Wraps the CHIP holographic agent (chip_web.py + chip_ui.html) in a
WebKitGTK window. No Electron: just Python + GTK.

Requires (installed by install.sh):
    apt install python3-gi gir1.2-webkit2-4.1
    pip3 install --user openai
"""

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.join(HERE, "server")
SERVER_SCRIPT = os.path.join(SERVER_DIR, "chip_web.py")

APP_ID = "io.github.chip.app"
APP_NAME = "CHIP"
PORT = 8765


# ---------------------------------------------------------------------------
# Embedded server (subprocess — keeps chip_web's argparse untouched)
# ---------------------------------------------------------------------------

def find_free_port(start=PORT):
    for port in range(start, start + 64):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return PORT


class ServerHandle:
    def __init__(self):
        self.port = find_free_port()
        self.proc = None

    def start(self):
        env = dict(os.environ)
        cmd = [sys.executable, SERVER_SCRIPT, "--host", "127.0.0.1",
               "--port", str(self.port)]
        self.proc = subprocess.Popen(cmd, cwd=SERVER_DIR, env=env,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/api/stats", timeout=1) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
            if self.proc.poll() is not None:
                return False
            time.sleep(0.15)
        return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# WebKitGTK fallbacks (4.1 then 4.0, GTK3)
# ---------------------------------------------------------------------------

def _import_webkit():
    import gi
    gi.require_version("Gtk", "3.0")
    try:
        gi.require_version("WebKit2", "4.1")
    except ValueError:
        gi.require_version("WebKit2", "4.0")
    from gi.repository import Gtk, WebKit2  # noqa: F401
    return Gtk, WebKit2


# ---------------------------------------------------------------------------
# GTK application
# ---------------------------------------------------------------------------

def run_gui(server):
    Gtk, WebKit2 = _import_webkit()

    app = Gtk.Application.new(APP_ID, 0)
    win = None

    def on_activate(a):
        nonlocal win
        if win is not None:
            win.present()
            return
        win = Gtk.ApplicationWindow(application=a, title=APP_NAME)
        win.set_default_size(1180, 780)
        win.set_position(Gtk.WindowPosition.CENTER)

        try:
            provider = Gtk.CssProvider()
            provider.load_from_data(b"""
                window { background:#02060c; }
                webview { background:#02060c; }
            """)
            Gtk.StyleContext.add_provider_for_screen(
                Gtk.Screen.get_default(), provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        except Exception:
            pass

        web = WebKit2.WebView()
        settings = web.get_settings()
        settings.set_enable_developer_extras(True)
        settings.set_enable_media_stream(True)   # mic for voice-in

        if hasattr(settings, "set_media_content_types_requiring_hardware_acceleration"):
            try:
                settings.set_media_content_types_requiring_hardware_acceleration(b"")
            except Exception:
                pass

        win.add(web)
        win.show_all()

        def load():
            web.load_uri(f"http://127.0.0.1:{server.port}/")

        threading.Thread(target=_wait_then, args=(server, GLib.idle_add, load),
                         daemon=True).start()

    app.connect("activate", on_activate)
    try:
        exit_status = app.run([])
    finally:
        server.stop()
    return exit_status


def _wait_then(server, when_ready, fn):
    server.start()
    GLib.idle_add(fn)


# ---------------------------------------------------------------------------
# CLI / launcher
# ---------------------------------------------------------------------------

def main():
    # Native cores hint in bundled server dir
    if not os.path.exists(SERVER_SCRIPT):
        print(f"ERROR: expected {SERVER_SCRIPT} — keep chip_app.py and server/ together.")
        sys.exit(1)

    missing = shutil.which("python3")
    if not missing:
        print("python3 not found.")
        sys.exit(1)

    server = ServerHandle()

    try:
        run_gui(server)
    except (ImportError, ValueError) as e:
        print("CHIP needs the WebKitGTK bridge. Install with:  "
              "sudo apt install python3-gi gir1.2-webkit2-4.1")
        print(f"({e})")
        server.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()