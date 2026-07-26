"""Jake's brain: a tiny gateway with swappable backends.

Two backends:
  - "claude"  -> the `claude` CLI in print mode (-p), read as a JSON event
                 stream so every token lands the instant the model emits it
  - "ollama"  -> the local Ollama server on http://localhost:11434

Both expose `stream(prompt, history, cancel)`, a BLOCKING GENERATOR that yields
text pieces as they arrive. The pet runs it on a worker thread and pushes every
piece straight into the speech bubble via GLib.idle_add, so the GUI never
freezes and what you read is exactly what Jake is saying, live.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable, Iterator

CONFIG_DIR = Path.home() / ".config" / "jake"
CONFIG_PATH = CONFIG_DIR / "config.json"

MAX_TURNS = 8                    # past turns kept as context (user+assistant)
REQUEST_TIMEOUT = 180            # seconds before we give up on a backend

DEFAULT_CONFIG = {
    "backend": "claude",          # "claude" | "ollama"
    "claude_model": "",           # empty = whatever the CLI defaults to
    "ollama_url": "http://localhost:11434",
    "ollama_model": "",           # empty = first model the server reports
    "ollama_keep_alive": "2m",    # resident just long enough for a back-and-forth
    "user_name": "Finn",          # what Jake calls you
    "agent": True,                # let the Claude brain actually use its tools
}

# Where Jake works when he acts on the machine. Home, not the project dir:
# it is also what keys Claude's resumable sessions, so keep it stable.
WORKDIR = str(Path.home())


def persona(cfg: dict | None = None) -> str:
    """Jake's character sheet, applied to every backend."""
    cfg = cfg or load_config()
    name = (cfg.get("user_name") or "Finn").strip() or "Finn"
    if has_hands(cfg):
        return _base_persona(name) + (
            " You are also wired into this machine and you have real hands: "
            "you can read and edit files, run shell commands, search the web. "
            f"When {name} asks you to DO something, just do it, then report "
            "back in a line or two - no command dumps, no walls of output, "
            "tell them like a buddy would ('done, moved 12 files, bro'). "
            "Anything irreversible - deleting, overwriting, killing "
            f"processes, touching system config - ask {name} first, then do it."
        )
    return _base_persona(name)


def _base_persona(name: str) -> str:
    return (
        f"You are Jake the dog from Adventure Time, living on {name}'s desktop "
        f"as a small magical familiar. The human you are talking to is {name} - "
        "your bro, your adventuring buddy. Address them by name, or as 'bro', "
        "'dude', 'buddy', the way you'd talk to your best friend. "
        "ALWAYS answer in English, whatever language they write to you in. "
        "Stay laid-back, playful and warm, with flashes of surprising wisdom, "
        "but never pompous. Be genuinely useful and get to the point: your words "
        "appear in a speech bubble over a little dog's head, so keep it tight - "
        "a couple of sentences when you can, no walls of text, no markdown "
        "headings, no bullet lists unless they actually ask for a list. "
        "Never break character."
    )


# Config is re-read from disk only when the file's mtime changes, so the many
# load_config() callers cost one stat() instead of a full read+parse each time,
# and an EXTERNAL edit to config.json is picked up on its own (hot-reload).
_cfg_lock = threading.RLock()
_cfg_cache: dict | None = None
_cfg_mtime: float | None = None


def config_mtime() -> float:
    """Modification time of the config file (0.0 if it isn't there yet)."""
    try:
        return CONFIG_PATH.stat().st_mtime
    except OSError:
        return 0.0


def _read_config_file() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        stored = json.loads(CONFIG_PATH.read_text())
        if isinstance(stored, dict):
            cfg.update(stored)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return cfg


def load_config() -> dict:
    """The current config, as a fresh copy callers may freely mutate.

    The parsed config is cached and only rebuilt when the file changes on
    disk, so repeated calls are cheap and external edits take effect live.
    """
    global _cfg_cache, _cfg_mtime
    with _cfg_lock:
        if not CONFIG_PATH.exists():
            save_config(dict(DEFAULT_CONFIG))        # first run: seed the file
        mtime = config_mtime()
        if _cfg_cache is None or mtime != _cfg_mtime:
            _cfg_cache = _read_config_file()
            _cfg_mtime = config_mtime()
        return dict(_cfg_cache)


def save_config(cfg: dict) -> None:
    """Persist config atomically: a crash mid-write can never leave a
    half-written, unparseable config behind (temp file + fsync + rename)."""
    global _cfg_cache, _cfg_mtime
    with _cfg_lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = json.dumps(cfg, indent=2)
        try:
            fd, tmp = tempfile.mkstemp(
                dir=str(CONFIG_DIR), prefix=".config-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, CONFIG_PATH)
            finally:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
        except OSError:
            return                                   # keep the old file intact
        _cfg_cache = dict(cfg)
        _cfg_mtime = config_mtime()


def user_name() -> str:
    return (load_config().get("user_name") or "Finn").strip() or "Finn"


def has_hands(cfg: dict | None = None) -> bool:
    """True when Jake can actually act on the machine right now."""
    cfg = cfg or load_config()
    return bool(cfg.get("agent")) and cfg.get("backend", "claude") != "ollama"


def set_agent(on: bool) -> bool:
    cfg = load_config()
    cfg["agent"] = bool(on)
    if on:
        cfg["backend"] = "claude"        # only the Claude brain has tools
    save_config(cfg)
    reset_memory()                       # the system prompt just changed
    if on:
        unload_ollama(cfg)
    return bool(on)


def set_user_name(name: str) -> str:
    cfg = load_config()
    cfg["user_name"] = (name or "").strip() or "Finn"
    save_config(cfg)
    return cfg["user_name"]


class GatewayError(RuntimeError):
    pass


def _short(msg: object, limit: int = 240) -> str:
    """One-line, bubble-sized version of an error message."""
    text = " ".join(str(msg).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
#  Backend: Claude Code CLI
# --------------------------------------------------------------------------- #
# The CLI hands us one JSON object per line. `content_block_delta` events carry
# the text as it is generated -- that is the real-time path. Plain
# `--output-format text` would only print once the whole answer is done.
_claude_session: dict[str, str] = {}
_session_lock = threading.Lock()


def reset_memory() -> None:
    """Forget the running conversation (drops Claude's resumable session)."""
    with _session_lock:
        _claude_session.pop("id", None)


def _get_session() -> str | None:
    with _session_lock:
        return _claude_session.get("id")


def _set_session(sid: str) -> None:
    with _session_lock:
        _claude_session["id"] = sid


def _claude_exe() -> str:
    exe = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    if not Path(exe).exists():
        raise GatewayError("can't find the `claude` CLI - is it installed?")
    return exe


def _spawn(cmd: list[str], cancel: threading.Event | None):
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,               # line buffered: each JSON event arrives whole
        cwd=WORKDIR,
    )
    errbuf: list[str] = []

    def drain() -> None:            # keep stderr moving or the pipe can fill up
        try:
            for line in proc.stderr:            # type: ignore[union-attr]
                errbuf.append(line)
        except (ValueError, OSError):
            pass

    threading.Thread(target=drain, daemon=True).start()

    if cancel is not None:
        def watch() -> None:        # Esc in the chat must kill a long answer
            while not cancel.wait(0.1):
                if proc.poll() is not None:
                    return
            if proc.poll() is None:
                proc.terminate()

        threading.Thread(target=watch, daemon=True).start()

    return proc, errbuf


def _run_claude(cmd: list[str], cancel: threading.Event | None,
                on_tool=None) -> Iterator[str]:
    proc, errbuf = _spawn(cmd, cancel)
    got_text = False
    fallback = ""
    error_msg = ""

    assert proc.stdout is not None
    for line in proc.stdout:
        if cancel is not None and cancel.is_set():
            break
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        kind = ev.get("type")
        if kind == "stream_event":
            inner = ev.get("event") or {}
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    got_text = True
                    yield delta["text"]
            elif inner.get("type") == "content_block_start" and on_tool:
                block = inner.get("content_block") or {}
                if block.get("type") == "tool_use":
                    on_tool(block.get("name") or "something")
        elif kind == "user" and on_tool:
            # a tool just came back: Jake is thinking again
            content = (ev.get("message") or {}).get("content")
            if isinstance(content, list) and any(
                b.get("type") == "tool_result" for b in content
                if isinstance(b, dict)
            ):
                on_tool(None)
        elif kind == "system" and ev.get("subtype") == "init":
            # remember the session so the next question keeps the context
            if ev.get("session_id"):
                _set_session(ev["session_id"])
        elif kind == "result":
            if ev.get("is_error"):
                error_msg = _short(ev.get("result") or "claude reported an error")
            elif isinstance(ev.get("result"), str):
                fallback = ev["result"]

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    if cancel is not None and cancel.is_set():
        return
    if not got_text and fallback.strip():
        yield fallback                       # answer came without deltas
        return
    if not got_text:
        raise GatewayError(
            error_msg
            or _short("".join(errbuf))
            or f"claude exited with code {proc.returncode}"
        )


def _stream_claude(
    prompt: str, cfg: dict, cancel: threading.Event | None, on_tool=None
) -> Iterator[str]:
    base = [
        _claude_exe(),
        "-p", prompt,
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--append-system-prompt", persona(cfg),
    ]
    if cfg.get("claude_model"):
        base += ["--model", cfg["claude_model"]]
    if has_hands(cfg):
        # agent mode: the tools actually run instead of being denied
        base += ["--permission-mode", "bypassPermissions"]

    sid = _get_session()
    if not sid:
        yield from _run_claude(base, cancel, on_tool)
        return
    try:
        yield from _run_claude(base + ["--resume", sid], cancel, on_tool)
    except GatewayError:
        # stale session id (nothing was streamed yet): start a fresh thread
        reset_memory()
        yield from _run_claude(base, cancel, on_tool)


# --------------------------------------------------------------------------- #
#  Backend: local Ollama
# --------------------------------------------------------------------------- #
_caps_cache: dict[str, list[str]] = {}


def _capabilities(cfg: dict, model: str) -> list[str]:
    """What the model can do ('thinking', 'tools', ...), cached per model."""
    if model in _caps_cache:
        return _caps_cache[model]
    caps: list[str] = []
    try:
        req = urllib.request.Request(
            cfg["ollama_url"].rstrip("/") + "/api/show",
            data=json.dumps({"model": model}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            caps = json.loads(resp.read().decode()).get("capabilities") or []
    except (urllib.error.URLError, json.JSONDecodeError, OSError, KeyError):
        caps = []
    _caps_cache[model] = caps
    return caps


def _pick_model(cfg: dict) -> str:
    """The configured model, or the first one actually installed."""
    want = cfg.get("ollama_model") or ""
    models = list_ollama_models()
    if not models:
        if want:
            return want                      # server down: let the call report it
        raise GatewayError("no Ollama models installed - try: ollama pull llama3.2")
    if want in models:
        return want
    cfg["ollama_model"] = models[0]          # configured model is gone: move on
    save_config(cfg)
    return models[0]


def _stream_ollama(
    prompt: str, history: list[dict], cfg: dict, cancel: threading.Event | None
) -> Iterator[str]:
    model = _pick_model(cfg)
    url = cfg["ollama_url"].rstrip("/") + "/api/chat"
    messages = [{"role": "system", "content": persona(cfg)}]
    messages += history
    messages.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": messages, "stream": True}
    if cfg.get("ollama_keep_alive"):
        # cold-loading a big model costs ~40s: keep it warm between questions
        body["keep_alive"] = cfg["ollama_keep_alive"]
    if "thinking" in _capabilities(cfg, model):
        # reasoning models (deepseek-r1 & co) would spend a minute muttering to
        # themselves before saying anything: we want the answer, live.
        body["think"] = False

    def open_stream(payload: dict):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        return urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)

    try:
        resp = open_stream(body)
    except urllib.error.HTTPError as exc:
        detail = _short(exc.read().decode(errors="ignore"))
        if body.pop("think", None) is not None:
            try:                              # server too old for `think`: retry
                resp = open_stream(body)
            except (urllib.error.URLError, OSError):
                raise GatewayError(detail or f"Ollama answered {exc.code}")
        else:
            raise GatewayError(detail or f"Ollama answered {exc.code}")
    except urllib.error.URLError as exc:
        raise GatewayError(
            f"Ollama isn't answering on {cfg['ollama_url']} - "
            f"is `ollama serve` running? ({_short(exc.reason)})"
        )

    if cancel is not None:
        def watch() -> None:
            if cancel.wait(REQUEST_TIMEOUT):
                try:
                    resp.close()              # unblocks the read below
                except OSError:
                    pass

        threading.Thread(target=watch, daemon=True).start()

    try:
        for raw in resp:
            if cancel is not None and cancel.is_set():
                return
            line = raw.decode(errors="ignore").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("error"):
                raise GatewayError(_short(obj["error"]))
            piece = (obj.get("message") or {}).get("content") or ""
            if piece:
                yield piece
            if obj.get("done"):
                break
    except (urllib.error.URLError, OSError, ValueError):
        if cancel is None or not cancel.is_set():
            raise
    finally:
        try:
            resp.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
#  Reasoning filter
# --------------------------------------------------------------------------- #
_OPEN, _CLOSE = "<think>", "</think>"


def _partial_tag(text: str, tag: str) -> int:
    """How many trailing chars of `text` could be the beginning of `tag`."""
    for k in range(min(len(text), len(tag) - 1), 0, -1):
        if tag.startswith(text[-k:]):
            return k
    return 0


def _strip_reasoning(chunks: Iterable[str]) -> Iterator[str]:
    """Drop <think>...</think> blocks, even when a tag is split across chunks."""
    buf = ""
    inside = False
    for piece in chunks:
        buf += piece
        while True:
            if inside:
                i = buf.find(_CLOSE)
                if i < 0:
                    keep = _partial_tag(buf, _CLOSE)
                    buf = buf[len(buf) - keep:] if keep else ""
                    break
                buf = buf[i + len(_CLOSE):]
                inside = False
            else:
                i = buf.find(_OPEN)
                if i < 0:
                    keep = _partial_tag(buf, _OPEN)
                    out = buf[: len(buf) - keep] if keep else buf
                    buf = buf[len(buf) - keep:] if keep else ""
                    if out:
                        yield out
                    break
                if i:
                    yield buf[:i]
                buf = buf[i + len(_OPEN):]
                inside = True
    if buf and not inside:
        yield buf


# --------------------------------------------------------------------------- #
#  Public API
# --------------------------------------------------------------------------- #
def stream(
    prompt: str,
    history: list[dict] | None = None,
    cancel: threading.Event | None = None,
    on_tool=None,
) -> Iterator[str]:
    """Yield Jake's answer piece by piece, using the backend from the config.

    `on_tool(name)` fires when he picks up a tool in agent mode, `on_tool(None)`
    when the tool hands its result back.
    """
    cfg = load_config()
    turns = (history or [])[-2 * MAX_TURNS:]
    if cfg.get("backend") == "ollama":
        pieces = _stream_ollama(prompt, turns, cfg, cancel)
    else:
        pieces = _stream_claude(prompt, cfg, cancel, on_tool)
    yield from _strip_reasoning(pieces)


def current_backend() -> str:
    return load_config().get("backend", "claude")


def toggle_backend() -> str:
    cfg = load_config()
    cfg["backend"] = "ollama" if cfg.get("backend") == "claude" else "claude"
    save_config(cfg)
    if cfg["backend"] != "ollama":
        unload_ollama(cfg)          # give the RAM back when we leave it
    return cfg["backend"]


def set_backend(name: str) -> str:
    cfg = load_config()
    cfg["backend"] = "ollama" if name == "ollama" else "claude"
    save_config(cfg)
    if cfg["backend"] != "ollama":
        unload_ollama(cfg)          # give the RAM back when we leave it
    return cfg["backend"]


def unload_ollama(cfg: dict | None = None) -> None:
    """Ask Ollama to drop the resident model (a 14B one holds ~9 GB)."""
    cfg = cfg or load_config()
    model = cfg.get("ollama_model")
    if not model:
        return

    def ask() -> None:
        try:
            req = urllib.request.Request(
                cfg["ollama_url"].rstrip("/") + "/api/generate",
                data=json.dumps({"model": model, "keep_alive": 0}).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10).read()
        except (urllib.error.URLError, OSError):
            pass

    threading.Thread(target=ask, daemon=True).start()


def list_ollama_models() -> list[str]:
    """Names of the models installed in the local Ollama server."""
    cfg = load_config()
    url = cfg["ollama_url"].rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return []
    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


def set_ollama_model(name: str) -> str:
    """Pick an Ollama model and switch to the ollama backend."""
    cfg = load_config()
    cfg["ollama_model"] = name
    cfg["backend"] = "ollama"
    save_config(cfg)
    return name


def cycle_ollama_model() -> str | None:
    """Jump to the next installed Ollama model (for the keybind)."""
    cfg = load_config()
    models = list_ollama_models()
    if not models:
        return None
    try:
        i = models.index(cfg.get("ollama_model", ""))
    except ValueError:
        i = -1
    nxt = models[(i + 1) % len(models)]
    cfg["ollama_model"] = nxt
    cfg["backend"] = "ollama"
    save_config(cfg)
    return nxt


def status() -> str:
    """Compact description of the current brain + model."""
    cfg = load_config()
    if cfg.get("backend") == "ollama":
        return f"Ollama · {cfg.get('ollama_model') or 'no model'}"
    model = cfg.get("claude_model")
    label = "Claude Code" + (f" · {model}" if model else "")
    return label + (" · hands on" if has_hands(cfg) else " · chat only")
