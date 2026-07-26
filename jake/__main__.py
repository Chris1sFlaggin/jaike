"""Starts the Jake app + the IPC listener for the Copilot key / summon script."""

from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from .pet import JakePet  # noqa: E402

APP_ID = "sh.jake.familiar"


def socket_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / "jake.sock"


def _ipc_server(pet: JakePet, sock: socket.socket) -> None:
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        with conn:
            data = conn.recv(1024).decode(errors="ignore").strip()
            if data:
                GLib.idle_add(pet.handle_ipc, data)


def main() -> int:
    path = socket_path()

    # If an instance is already alive, summon it and quit (idempotent start):
    # a second launch must not steal or delete the first Jake's socket.
    if path.exists():
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(str(path))
            client.sendall(b"summon")
            client.close()
            return 0
        except OSError:
            # stale socket (Jake is dead): drop it and carry on
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    app = Gtk.Application(application_id=APP_ID)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(8)

    def on_activate(active_app: Gtk.Application) -> None:
        pet = JakePet(active_app)
        threading.Thread(
            target=_ipc_server, args=(pet, srv), daemon=True
        ).start()

    app.connect("activate", on_activate)
    try:
        return app.run(None)
    finally:
        srv.close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
