"""Jake the familiar: an animated overlay for Hyprland/Wayland.

- Draws Jake (pycairo) with a cached arcane glow, a ground shadow and a
  speech bubble anchored just above his head.
- Wanders around the screen (with gtk4-layer-shell he moves the surface via
  its margins; without it he stays put and leans on Hyprland window rules).
- Click Jake to open a text box, drag him to move him: whatever you type goes
  to the gateway (Claude Code or Ollama) and the answer lands in the bubble
  LIVE - every token is painted the moment it arrives, no typewriter fake.
"""

from __future__ import annotations

import math
import random
import subprocess
import threading

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

from . import gateway  # noqa: E402
from .conversation import exchanges  # noqa: E402
from .sprites import Sprites  # noqa: E402

# gtk4-layer-shell is optional: without it we fall back to Hyprland rules.
try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell  # noqa: E402

    HAVE_LAYER = True
except (ValueError, ImportError):
    HAVE_LAYER = False

# Size of Jake's canvas (the surface). Jake + bubble + input box live in here.
W, H = 420, 520
JAKE_H = 150            # height we scale Jake's sprite to
ENTRY_ZONE = 46         # room at the bottom for the text box
FPS = 15                # frames per second while Jake is active
# Adaptive tick: the clock only runs fast when something is actually moving.
# Standing still he ticks a few times a second; dozing or hidden, once a second.
MS_ACTIVE = 1000 // FPS  # animating / chatting / thinking
MS_QUIET = 200           # awake but still (enough to catch a fidget or a wake)
MS_SLEEP = 1000          # dozing or hidden: barely a heartbeat
DOZE_AFTER = 90.0        # seconds untouched before Jake nods off (Zzz)

BUBBLE_MAX_H = 300      # taller answers scroll: the newest lines stay visible
BUBBLE_KEEP = 2000      # chars kept on screen (the tail is what matters live)
BUBBLE_LINGER = 14.0    # seconds a reply stays up when the chat is closed
NOTE_LINGER = 3.0       # seconds a one-line notice stays up
PEEK_LINGER = 9.0       # seconds the memory thread stays up outside a chat

# the "memory thread": past messages hanging above the live bubble
CHIPS_MAX = 4           # how many past lines can hang there at once
CHIP_GAP = 8
CHIP_PADY = 5

APP_ID = "sh.jake.familiar"
WM_CLASS = "sh.jake.familiar"   # = app_id: that is how Hyprland matches "class"
LAYER_NS = "jake-familiar"      # layer namespace (for optional layer_rule)

# states
IDLE, WALK, TALK, THINK = "idle", "walk", "talk", "think"

GREETINGS = [
    "Yo {name}! What's the word?",
    "Hey {name}, whatcha need, bro?",
    "Sup {name}! Talk to me.",
    "{name}! My main dude. Shoot.",
    "Rise and shine, {name}. What's up?",
    "Yeah {name}? I'm all ears, buddy.",
]

CSS = b"""
window { background: transparent; }
.jake-entry {
  background: rgba(20,18,30,0.92);
  color: #ffe08a;
  caret-color: #ffe08a;
  border: 1px solid rgba(255,200,90,0.55);
  border-radius: 12px;
  padding: 6px 12px;
  font-family: monospace;
  font-size: 13px;
}
/* agent mode: Jake has hands, and you can see it */
.jake-entry.armed {
  color: #7ef0cf;
  caret-color: #7ef0cf;
  border-color: rgba(90,230,195,0.65);
}
"""

GOLD = (0.9, 0.65, 0.15)        # normal bubble trim
TEAL = (0.25, 0.80, 0.68)       # bubble trim while agent mode is armed
TEAL_INK = (0.05, 0.40, 0.33)   # readable teal for text on the parchment


class JakePet:
    def __init__(self, app: Gtk.Application) -> None:
        self.app = app
        self.sprites = Sprites()

        self.state = IDLE
        self.frame_i = 0                     # index of the current frame
        self.frame_clock = 0.0               # accumulator for the frame switch
        self.facing = 1                      # 1 = looking right, -1 = left
        self.dots = 1                        # animated "thinking" dots

        # surface position (top-left corner) in logical px
        self.mon_w, self.mon_h = 1280, 800   # refreshed after realize
        self.x = 200.0
        self.y = 200.0
        self.tx, self.ty = self.x, self.y    # wandering target
        self.next_wander = 0.0
        self.next_fidget = 0.0
        self.fidget_until = 0.0
        self.last_input = 0.0                 # last time you touched him (for doze)
        self.dozing = False                   # nodded off after a long quiet spell

        # adaptive frame clock: source id + current period, so we can speed it
        # up on interaction and slow it right down when nothing is happening
        self._tick_source: int | None = None
        self._tick_ms = MS_ACTIVE
        self._in_tick = False
        # config hot-reload: notice external edits to config.json and re-sync
        self._cfg_seen_mtime = gateway.config_mtime()
        self._next_cfg_check = 0.0

        self.bubble = ""                     # EXACTLY what Jake has said so far
        self.bubble_until = 0.0              # 0 = stays until something replaces it
        self.chatting = False
        self.hidden = False
        self.busy = False                    # a request is in flight
        self.pending = False                 # ...and no token has landed yet
        self.tool: str | None = None         # tool he is using right now
        self.agent_on = gateway.has_hands()  # cached: config reads are file I/O
        self.wait_from = 0.0                 # when the current question went out
        self.brain_label = ""                # which brain is chewing on it
        self.on_ollama = False               # local models can be slow to wake

        self.history: list[dict] = []        # conversation turns
        self.prompts: list[str] = []         # what you typed (Up/Down recall)
        self.prompt_i = 0
        self.scroll_i: int | None = None     # None = live, 0 = last exchange, 1 = older…
        self.req_id = 0                      # bumps on every request: kills stale replies
        self.cancel: threading.Event | None = None

        self._last_sig = None
        self._input_key: tuple | None = None
        self._drag_from: tuple[float, float] | None = None
        self._dragged = False

        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self.win = Gtk.ApplicationWindow(application=self.app)
        # IMPORTANT: init_for_window BEFORE the window is realized.
        if HAVE_LAYER:
            LayerShell.init_for_window(self.win)
            LayerShell.set_layer(self.win, LayerShell.Layer.OVERLAY)
            LayerShell.set_namespace(self.win, LAYER_NS)
            LayerShell.set_anchor(self.win, LayerShell.Edge.TOP, True)
            LayerShell.set_anchor(self.win, LayerShell.Edge.LEFT, True)
            LayerShell.set_keyboard_mode(
                self.win, LayerShell.KeyboardMode.ON_DEMAND
            )
        self.win.set_default_size(W, H)
        self.win.set_decorated(False)
        self.win.set_resizable(False)

        overlay = Gtk.Overlay()
        self.win.set_child(overlay)

        self.area = Gtk.DrawingArea()
        self.area.set_content_width(W)
        self.area.set_content_height(H)
        self.area.set_draw_func(self._draw)
        overlay.set_child(self.area)

        # text box at the bottom, hidden until you start a chat
        self.entry = Gtk.Entry()
        self.entry.add_css_class("jake-entry")
        self.entry.set_placeholder_text("talk to Jake…")
        self.entry.set_valign(Gtk.Align.END)
        self.entry.set_halign(Gtk.Align.FILL)
        self.entry.set_margin_start(16)
        self.entry.set_margin_end(16)
        self.entry.set_margin_bottom(6)
        self.entry.set_visible(False)
        self.entry.connect("activate", self._on_submit)
        overlay.add_overlay(self.entry)

        # drag Jake to move him; a drag that doesn't move = a click = chat
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.area.add_controller(drag)

        # right click on Jake = swap brain
        right = Gtk.GestureClick()
        right.set_button(3)
        right.connect("pressed", self._on_right_click)
        self.area.add_controller(right)

        # wheel over Jake = walk back through the conversation
        wheel = Gtk.EventControllerScroll()
        wheel.set_flags(Gtk.EventControllerScrollFlags.VERTICAL)
        wheel.connect("scroll", self._on_scroll)
        self.area.add_controller(wheel)

        # Esc / history recall, grabbed before the entry sees them
        ekeys = Gtk.EventControllerKey()
        ekeys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        ekeys.connect("key-pressed", self._on_entry_key)
        self.entry.add_controller(ekeys)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.win.add_controller(keys)

        self.win.connect("realize", self._on_realize)
        self.win.present()
        self._refresh_agent()

        self.last_input = self._now()
        self._tick_source = GLib.timeout_add(self._tick_ms, self._tick)

    def _refresh_agent(self) -> None:
        """Pick up the agent/backend state and show it (teal = he can act)."""
        self.agent_on = gateway.has_hands()
        if self.agent_on:
            self.entry.add_css_class("armed")
            self.entry.set_placeholder_text("tell Jake what to do…")
        else:
            self.entry.remove_css_class("armed")
            self.entry.set_placeholder_text("talk to Jake…")
        self._redraw()

    def _on_realize(self, *_):
        disp = Gdk.Display.get_default()
        mons = disp.get_monitors()
        if mons.get_n_items():
            geo = mons.get_item(0).get_geometry()
            self.mon_w, self.mon_h = geo.width, geo.height
        self.x = self.mon_w - W - 40
        self.y = self.mon_h - H - 40
        self.tx, self.ty = self.x, self.y
        self._apply_position()

    # ------------------------------------------------------------- position
    def _apply_position(self) -> None:
        x = int(max(0, min(self.x, self.mon_w - W)))
        y = int(max(0, min(self.y, self.mon_h - H)))
        if HAVE_LAYER:
            LayerShell.set_margin(self.win, LayerShell.Edge.LEFT, x)
            LayerShell.set_margin(self.win, LayerShell.Edge.TOP, y)
        else:
            # best effort: shove the floating window around via Hyprland
            subprocess.Popen(
                ["hyprctl", "dispatch", "movewindowpixel",
                 f"exact {x} {y},class:^{WM_CLASS}$"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    @staticmethod
    def _now() -> float:
        return GLib.get_monotonic_time() / 1e6

    def _redraw(self) -> None:
        """Repaint now - used for every streamed token, so text is live."""
        self._last_sig = None
        self.area.queue_draw()

    # ----------------------------------------------------------- frame loop
    def _tick(self) -> bool:
        self._in_tick = True
        try:
            return self._tick_body()
        finally:
            self._in_tick = False

    def _tick_body(self) -> bool:
        if self.hidden:
            return self._reschedule(MS_SLEEP)   # paused: a slow heartbeat only
        now = self._now()

        # pick up external edits to config.json (agent / backend / name) live
        if now >= self._next_cfg_check:
            self._next_cfg_check = now + 1.0
            mtime = gateway.config_mtime()
            if mtime != self._cfg_seen_mtime:
                self._cfg_seen_mtime = mtime
                self._refresh_agent()

        if self.state == WALK:
            dx, dy = self.tx - self.x, self.ty - self.y
            dist = math.hypot(dx, dy)
            if dist < 3:
                self.x, self.y = self.tx, self.ty
                self.state = IDLE
            else:
                speed = 14.0
                if HAVE_LAYER:
                    self.x += dx / dist * min(speed, dist)
                    self.y += dy / dist * min(speed, dist)
                    self.facing = 1 if dx >= 0 else -1
                    self._apply_position()
                else:
                    self.x, self.y = self.tx, self.ty
                    self._apply_position()
        elif self.state == IDLE and HAVE_LAYER and self._may_wander(now):
            self._wander()

        # a reply (or a peek at the thread) hangs around a few seconds
        if self.bubble_until and now >= self.bubble_until:
            self.bubble = ""
            self.bubble_until = 0.0
            self.scroll_i = None

        # nod off after a long spell untouched, with nothing on screen
        idle_now = (self.state == IDLE and not self.chatting and not self.busy
                    and not self.pending and not self.bubble
                    and self.scroll_i is None)
        self.dozing = idle_now and (now - self.last_input) > DOZE_AFTER

        # animate: walking, thinking, talking, or the occasional idle fidget
        if self.state in (WALK, TALK, THINK):
            self._advance({WALK: 0.12, TALK: 0.16}.get(self.state, 0.22))
        elif self.dozing:
            self.frame_i = 0                     # fast asleep on his feet
        elif self.state == IDLE and now < self.fidget_until:
            self._advance(0.18)
        elif self.state == IDLE and now >= self.next_fidget:
            self.fidget_until = now + 0.9        # a little shuffle in place
            self.next_fidget = now + random.uniform(7, 18)
        else:
            self.frame_i = 0

        if self.pending:                          # • •• ••• while we wait
            self.dots = int(now * 2.5) % 3 + 1

        # repaint ONLY when something visible changed (battery friendly)
        sig = (self.state, self.frame_i, round(self.x), round(self.y),
               self.facing, len(self.bubble), self.bubble[-24:],
               self.pending, self.dots, self.chatting, self.scroll_i,
               len(self.history), self.dozing)
        if sig != self._last_sig:
            self._last_sig = sig
            self.area.queue_draw()

        return self._reschedule(self._desired_interval(now))

    def _desired_interval(self, now: float) -> int:
        """How fast the clock should run given what Jake is doing."""
        if (self.state in (WALK, TALK, THINK) or self.busy or self.pending
                or self.chatting or now < self.fidget_until):
            return MS_ACTIVE
        return MS_SLEEP if self.dozing else MS_QUIET

    def _reschedule(self, ms: int) -> bool:
        """Keep the running timer if its period is unchanged, else swap it.

        Returning False drops the firing source; the freshly added one (at the
        new period) takes over. Called only from inside the tick.
        """
        if ms == self._tick_ms:
            return True
        self._tick_ms = ms
        self._tick_source = GLib.timeout_add(ms, self._tick)
        return False

    def _wake(self) -> None:
        """Any interaction: mark Jake touched, break a doze, tick fast now."""
        self.last_input = self._now()
        if self.dozing:
            self.dozing = False
            self._last_sig = None
        if self._tick_ms != MS_ACTIVE:
            if not self._in_tick and self._tick_source is not None:
                GLib.source_remove(self._tick_source)
            self._tick_ms = MS_ACTIVE
            self._tick_source = GLib.timeout_add(MS_ACTIVE, self._tick)
        self.area.queue_draw()

    def _advance(self, interval: float) -> None:
        self.frame_clock += 1.0 / FPS
        if self.frame_clock >= interval:
            self.frame_clock = 0.0
            self.frame_i = (self.frame_i + 1) % len(self._anim()[0])

    def _may_wander(self, now: float) -> bool:
        # don't stroll off while you are talking to him or reading his answer,
        # and let him settle (then doze) once he's been left alone a while
        return (not self.chatting and not self.busy and not self.bubble
                and self.scroll_i is None and now >= self.next_wander
                and (now - self.last_input) < DOZE_AFTER)

    def _anim(self):
        if self.state == WALK:
            return self.sprites.walk, self.sprites.walk_glow
        if self.state == TALK:
            return self.sprites.talk, self.sprites.idle_glow
        return self.sprites.idle, self.sprites.idle_glow

    def _wander(self) -> None:
        self.tx = random.uniform(0, self.mon_w - W)
        self.ty = random.uniform(0, self.mon_h - H)
        self.next_wander = self._now() + random.uniform(6, 14)
        self.state = WALK

    # --------------------------------------------------------------- drawing
    def _draw(self, area, cr, width, height) -> None:
        # (default operator is OVER: correct alpha compositing)
        anim, glows = self._anim()
        i = self.frame_i % len(anim)
        surf, glow = anim[i], glows[i]
        sw, sh = self.sprites.size(surf)
        pad = self.sprites.pad
        k = JAKE_H / self.sprites.max_h

        cx = width / 2
        feet_y = height - ENTRY_ZONE - 8
        head_y = feet_y - sh * k          # top of Jake's head

        self._draw_chat(cr, width, head_y - 14)

        # ground shadow
        cr.save()
        cr.translate(cx, feet_y + 2)
        cr.scale(1.0, 0.30)
        cr.set_source_rgba(0, 0, 0, 0.22)
        cr.arc(0, 0, sw * k * 0.42, 0, 2 * math.pi)
        cr.fill()
        cr.restore()

        cr.save()
        cr.translate(cx, feet_y)
        cr.scale(k, k)
        if self.facing < 0:
            cr.scale(-1, 1)
        ox, oy = -sw / 2, -sh
        # pre-baked glow (1 blit), then Jake on top (1 blit)
        cr.set_source_surface(glow, ox - pad, oy - pad)
        cr.paint()
        cr.set_source_surface(surf, ox, oy)
        cr.paint()
        cr.restore()

        # Zzz drifting up when he has dozed off (nothing else on screen then)
        if self.dozing and not self.chatting and not self.bubble:
            self._draw_zzz(cr, cx + sw * k * 0.22, head_y - 4)

        # only Jake himself (and what is actually drawn) eats clicks: the rest
        # of this 420x520 canvas stays click-through for the windows underneath
        boxes = [(int(cx - sw * k / 2) - 8, int(feet_y - sh * k) - 8,
                  int(sw * k) + 16, int(sh * k) + 18)]
        if self.chatting:
            boxes.append((0, int(height - ENTRY_ZONE), int(width), ENTRY_ZONE))
        self._set_input_region(boxes)

    # ------------------------------------------------------- the chat on screen
    def _exchanges(self) -> list[tuple[str, str]]:
        """History as (your line, Jake's line) pairs, newest last."""
        return exchanges(self.history)

    def _draw_chat(self, cr, width, bottom) -> float | None:
        """Live bubble + the memory thread above it. Returns the stack top."""
        header = None
        if self.scroll_i is not None:
            pairs = self._exchanges()
            if pairs:
                idx = max(0, len(pairs) - 1 - self.scroll_i)
                header, text = pairs[idx]
                text = text or "(no answer to that one)"
            else:
                self.scroll_i = None
                text = self.bubble
        elif self.bubble:
            text = self.bubble
        elif self.pending:
            text = self._waiting_text()
        else:
            text = ""
        if not text:
            return None

        room = bottom - 4
        chips = [] if self.scroll_i is not None else self._chip_lines()
        # with chips hanging above, the live bubble gives up some room
        cap = min(BUBBLE_MAX_H, room if not chips else max(150, room * 0.6))
        # mid-answer tool use gets its own dim line under the text
        footer = (f"⚙ {self.tool}…"
                  if self.tool and self.busy and self.bubble and not header
                  else None)
        top = self._draw_bubble(cr, width, bottom, text, header, cap, footer)

        if self.scroll_i is not None:
            pairs = self._exchanges()
            self._draw_counter(cr, width, top - 6, len(pairs) - self.scroll_i,
                               len(pairs))
            return top
        if not chips:
            return top

        # chips are always one line tall, so the stack can be measured upfront
        chip_h = self._chip_height(cr)
        fits = int((top - 10) // (chip_h + CHIP_GAP))
        chips = chips[:max(0, fits)]
        if not chips:
            return top
        stack_top = top - len(chips) * (chip_h + CHIP_GAP)

        # the thread itself, drawn behind the chips
        cr.save()
        cr.set_source_rgba(0.98, 0.78, 0.30, 0.30)
        cr.set_line_width(1.4)
        cr.set_dash([2.5, 4.0])
        cr.move_to(width / 2, stack_top + chip_h / 2)
        cr.line_to(width / 2, top + 5)
        cr.stroke()
        cr.restore()

        y = top - CHIP_GAP
        for i, (role, line) in enumerate(chips):
            y = self._draw_chip(cr, width, y, line, role, 0.94 - 0.19 * i)
            y -= CHIP_GAP
        return stack_top

    def _waiting_text(self) -> str:
        """What the bubble says before the first word lands."""
        if self.tool:
            return f"⚙ {self.tool}…"
        waited = self._now() - self.wait_from
        dots = "•" * self.dots
        if waited < 3:
            return dots
        if self.on_ollama and waited > 6:
            # a big local model can spend ~40s just loading: say so
            return f"⚙ waking {self.brain_label}…  {int(waited)}s"
        return f"{dots}  {int(waited)}s"

    def _chip_lines(self) -> list[tuple[str, str]]:
        """Recent messages for the thread, closest to the bubble first."""
        turns = self.history
        if turns and turns[-1]["role"] == "assistant":
            turns = turns[:-1]          # that one is already in the live bubble
        return [(t["role"], t["content"]) for t in reversed(turns[-CHIPS_MAX:])]

    def _chip_height(self, cr) -> float:
        """Chips hold a single ellipsized line, so they all have this height."""
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription("Sans 9"))
        layout.set_text("Ag", -1)
        return layout.get_pixel_size()[1] + 2 * CHIP_PADY

    def _draw_chip(self, cr, width, bottom, text: str, role: str,
                   fade: float) -> float:
        """One faded past message. Returns its top edge."""
        mine = role == "user"
        padx, pady = 9, CHIP_PADY
        maxw = width * 0.68
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription("Sans 9"))
        layout.set_width(int((maxw - 2 * padx) * Pango.SCALE))
        layout.set_single_paragraph_mode(True)
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        layout.set_text(" ".join(text.split()), -1)
        tw, th = layout.get_pixel_size()

        cw = min(maxw, tw + 2 * padx)
        ch = th + 2 * pady
        cx = width - 24 - cw if mine else 24
        cy = bottom - ch

        self._rounded(cr, cx, cy, cw, ch, ch / 2)
        if mine:
            cr.set_source_rgba(0.13, 0.11, 0.17, 0.86 * fade)
        else:
            cr.set_source_rgba(0.98, 0.95, 0.86, 0.80 * fade)
        cr.fill()

        # the bead where the chip meets the thread
        cr.set_source_rgba(0.98, 0.78, 0.30, 0.55 * fade)
        cr.arc(width / 2, cy + ch / 2, 2.2, 0, 2 * math.pi)
        cr.fill()

        cr.move_to(cx + padx, cy + pady)
        if mine:
            cr.set_source_rgba(1.0, 0.87, 0.55, 0.95 * fade)
        else:
            cr.set_source_rgba(0.12, 0.10, 0.08, 0.95 * fade)
        PangoCairo.show_layout(cr, layout)
        return cy

    def _draw_counter(self, cr, width, bottom, pos: int, total: int) -> None:
        """The little ◂ 2/7 ▸ pill while you flip through the history."""
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription("Sans 8"))
        layout.set_text(f"◂ {pos}/{total} ▸", -1)
        tw, th = layout.get_pixel_size()
        w, h = tw + 18, th + 6
        x, y = (width - w) / 2, bottom - h
        self._rounded(cr, x, y, w, h, h / 2)
        cr.set_source_rgba(0.16, 0.13, 0.20, 0.88)
        cr.fill()
        cr.move_to(x + 9, y + 3)
        cr.set_source_rgba(1.0, 0.87, 0.55, 0.9)
        PangoCairo.show_layout(cr, layout)

    def _small_layout(self, cr, text: str, inner: float, lines: int):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription("Sans Italic 9"))
        layout.set_width(int(inner * Pango.SCALE))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        layout.set_height(-lines)                      # N lines, then ellipsis
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        layout.set_text(" ".join(text.split()), -1)
        return layout

    def _draw_bubble(self, cr, width, bottom, text: str,
                     header: str | None = None,
                     max_h: float = BUBBLE_MAX_H,
                     footer: str | None = None) -> float:
        """The parchment bubble. Returns its top edge."""
        pad = 14
        maxw = width - 56
        inner = maxw - 2 * pad
        trim = TEAL if self.agent_on else GOLD

        head_layout = None
        head_h = 0.0
        if header:
            head_layout = self._small_layout(cr, header, inner, 2)
            head_h = head_layout.get_pixel_size()[1] + 11

        foot_layout = None
        foot_h = 0.0
        if footer:
            foot_layout = self._small_layout(cr, footer, inner, 1)
            foot_h = foot_layout.get_pixel_size()[1] + 10

        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription("Sans 11"))
        layout.set_width(int(inner * Pango.SCALE))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        layout.set_text(text, -1)

        # long answers scroll: drop leading lines so the newest text is visible
        clipped = False
        limit = max(30.0, max_h - 2 * pad - head_h - foot_h)
        while layout.get_pixel_size()[1] > limit and layout.get_line_count() > 1:
            line = layout.get_line_readonly(0)
            cut = line.start_index + line.length
            text = text.encode()[cut:].decode("utf-8", "ignore").lstrip("\n")
            layout.set_text(text, -1)
            clipped = True

        tw, th = layout.get_pixel_size()
        if head_layout is not None:
            tw = max(tw, head_layout.get_pixel_size()[0])
        if foot_layout is not None:
            tw = max(tw, foot_layout.get_pixel_size()[0])
        bw = min(maxw, tw + 2 * pad)
        bh = th + head_h + foot_h + 2 * pad
        bx = (width - bw) / 2
        # bottom of the bubble (base of the tail) sits just above his head
        by = max(4, bottom - bh)

        self._rounded(cr, bx, by, bw, bh, 16)
        cr.set_source_rgba(0.98, 0.95, 0.86, 0.97)
        cr.fill_preserve()
        cr.set_source_rgba(*trim, 0.9)
        cr.set_line_width(2)
        cr.stroke()

        # the little tail
        cr.move_to(width / 2 - 10, by + bh - 1)
        cr.line_to(width / 2, by + bh + 14)
        cr.line_to(width / 2 + 10, by + bh - 1)
        cr.set_source_rgba(0.98, 0.95, 0.86, 0.97)
        cr.fill()

        if head_layout is not None:       # your question, above a hairline
            cr.move_to(bx + pad, by + pad)
            cr.set_source_rgba(0.35, 0.28, 0.18, 0.85)
            PangoCairo.show_layout(cr, head_layout)
            rule = by + pad + head_h - 6
            cr.set_source_rgba(0.55, 0.42, 0.15, 0.35)
            cr.set_line_width(1)
            cr.move_to(bx + pad, rule)
            cr.line_to(bx + bw - pad, rule)
            cr.stroke()

        cr.move_to(bx + pad, by + pad + head_h)
        cr.set_source_rgb(0.12, 0.10, 0.08)
        PangoCairo.show_layout(cr, layout)

        if foot_layout is not None:       # what he is doing right now
            rule = by + pad + head_h + th + 5
            cr.set_source_rgba(*trim, 0.35)
            cr.set_line_width(1)
            cr.move_to(bx + pad, rule)
            cr.line_to(bx + bw - pad, rule)
            cr.stroke()
            cr.move_to(bx + pad, rule + 4)
            cr.set_source_rgba(*TEAL_INK, 0.95)
            PangoCairo.show_layout(cr, foot_layout)

        if clipped:                       # fade the cut-off top edge
            fade_top = by + pad + head_h
            grad = cairo.LinearGradient(0, fade_top, 0, fade_top + 26)
            grad.add_color_stop_rgba(0, 0.98, 0.95, 0.86, 1.0)
            grad.add_color_stop_rgba(1, 0.98, 0.95, 0.86, 0.0)
            cr.save()
            cr.rectangle(bx + 2, fade_top, bw - 4, 26)
            cr.clip()
            cr.set_source(grad)
            cr.paint()
            cr.restore()
        return by

    def _draw_zzz(self, cr, x: float, y: float) -> None:
        """Three little 'z's drifting up-right from a dozing Jake."""
        for i, size in enumerate((11, 15, 20)):
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(
                Pango.FontDescription(f"Sans Bold {size}")
            )
            layout.set_text("z", -1)
            cr.move_to(x + i * 9, y - i * 19)
            cr.set_source_rgba(1.0, 0.86, 0.40, 0.85 - i * 0.20)
            PangoCairo.show_layout(cr, layout)

    @staticmethod
    def _rounded(cr, x, y, w, h, r) -> None:
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def _set_input_region(self, boxes: list[tuple[int, int, int, int]]) -> None:
        """Shrink the clickable area to what is actually drawn."""
        key = tuple(boxes)
        if key == self._input_key:
            return
        self._input_key = key                 # never retry in a loop on failure
        surface = self.win.get_surface()
        if surface is None:
            return
        try:
            region = cairo.Region()
            for x, y, w, h in boxes:
                region.union(cairo.RectangleInt(x, y, w, h))
            surface.set_input_region(region)
        except (AttributeError, TypeError, OSError):
            pass

    # ----------------------------------------------------------- interaction
    def _on_drag_begin(self, gesture, sx, sy) -> None:
        self._wake()
        self._drag_from = (self.x, self.y)
        self._dragged = False

    def _on_drag_update(self, gesture, ox, oy) -> None:
        if not HAVE_LAYER or self._drag_from is None:
            return
        if not self._dragged and abs(ox) + abs(oy) < 6:
            return                            # still just a click
        self._dragged = True
        self.state = IDLE
        self.x, self.y = self._drag_from[0] + ox, self._drag_from[1] + oy
        self.tx, self.ty = self.x, self.y
        self._apply_position()

    def _on_drag_end(self, gesture, ox, oy) -> None:
        if self._dragged:
            self.next_wander = self._now() + 25   # let him rest where you put him
        else:
            self._toggle_chat()
        self._drag_from = None
        self._dragged = False

    def _on_right_click(self, gesture, n_press, x, y) -> None:
        self._wake()
        gateway.toggle_backend()
        self._refresh_agent()
        self._note("Brain swapped: " + gateway.status())

    def _on_scroll(self, ctrl, dx, dy) -> bool:
        self._scroll(1 if dy < 0 else -1)     # wheel up = older
        return True

    def _scroll(self, step: int) -> None:
        """Walk the conversation: +1 = older, -1 = back towards live."""
        self._wake()
        pairs = self._exchanges()
        if not pairs:
            if self.chatting:
                self._note("Nothing in my head yet, bro.")
            return
        current = -1 if self.scroll_i is None else self.scroll_i
        wanted = max(-1, min(len(pairs) - 1, current + step))
        self.scroll_i = None if wanted < 0 else wanted
        if not self.chatting:
            # peek mode: the thread shows up for a moment, then fades away
            self.bubble_until = self._now() + PEEK_LINGER
            self.next_wander = self._now() + PEEK_LINGER
        self._redraw()

    def set_visible(self, visible: bool) -> None:
        """Show/hide the whole of Jake (Copilot key)."""
        self.hidden = not visible
        if visible:
            self._last_sig = None           # force a repaint when he pops back
            self.win.set_visible(True)
            self._wake()
        else:
            if self.chatting:
                self._close_chat()
            self.win.set_visible(False)

    def toggle_visible(self) -> None:
        self.set_visible(self.hidden)   # hidden -> show, visible -> hide

    def _on_key(self, ctrl, keyval, keycode, state) -> bool:
        self._wake()
        if keyval == Gdk.KEY_Escape:
            if self.busy:
                self._stop()
                return True
            if self.chatting:
                self._close_chat()
                return True
        return False

    def _on_entry_key(self, ctrl, keyval, keycode, state) -> bool:
        self._wake()
        if keyval == Gdk.KEY_Escape:
            if self.busy:
                self._stop()
            else:
                self._close_chat()
            return True
        alt = bool(state & Gdk.ModifierType.ALT_MASK)
        if keyval == Gdk.KEY_Page_Up or (alt and keyval == Gdk.KEY_Up):
            self._scroll(1)
            return True
        if keyval == Gdk.KEY_Page_Down or (alt and keyval == Gdk.KEY_Down):
            self._scroll(-1)
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_Down) and self.prompts:
            step = -1 if keyval == Gdk.KEY_Up else 1
            self.prompt_i = max(0, min(len(self.prompts), self.prompt_i + step))
            recalled = (self.prompts[self.prompt_i]
                        if self.prompt_i < len(self.prompts) else "")
            self.entry.set_text(recalled)
            self.entry.set_position(-1)
            return True
        return False

    def _toggle_chat(self) -> None:
        self._close_chat() if self.chatting else self._open_chat()

    def _open_chat(self) -> None:
        self._wake()
        self.chatting = True
        self.state = IDLE
        self.bubble = random.choice(GREETINGS).format(name=gateway.user_name())
        self.bubble_until = 0.0
        self.entry.set_visible(True)
        self.entry.grab_focus()
        self._redraw()

    def _close_chat(self) -> None:
        if self.busy:
            self._stop()
        self.chatting = False
        self.entry.set_visible(False)
        self.bubble = ""
        self.bubble_until = 0.0
        self.scroll_i = None
        self.state = IDLE
        self.next_wander = self._now() + 2
        self._redraw()

    def _note(self, msg: str, seconds: float = NOTE_LINGER) -> None:
        """A short-lived line in the bubble (not part of the conversation)."""
        if self.busy:
            # you just switched something mid-answer: drop the old brain's
            # reply (kept in the thread) instead of mixing it with this note
            self._stop()
        self.bubble = msg
        self.bubble_until = 0.0 if self.chatting else self._now() + seconds
        self.state = IDLE
        self._redraw()

    def _on_submit(self, entry) -> None:
        prompt = entry.get_text().strip()
        entry.set_text("")
        self._send(prompt)

    def _stop(self) -> None:
        """Cancel whatever Jake is generating right now."""
        if self.cancel is not None:
            self.cancel.set()
        self.req_id += 1                     # stale callbacks get dropped
        was_busy = self.busy
        self.busy = False
        self.pending = False
        self.tool = None
        self.state = IDLE
        if was_busy and self.history and self.history[-1]["role"] == "user":
            said = self.bubble.strip()       # keep whatever he got out
            if said:
                self.history.append({"role": "assistant", "content": said})
            else:
                self.history.pop()
        if not self.bubble:
            self.bubble = "Kay, dropped it."
            self.bubble_until = 0.0 if self.chatting else self._now() + NOTE_LINGER
        self._redraw()

    def _send(self, prompt: str) -> None:
        prompt = (prompt or "").strip()
        if not prompt:
            return
        self._wake()
        if prompt.startswith("/"):
            self._command(prompt)
            return
        if self.busy:
            self._stop()                     # a new question wins over the old
        self.prompts.append(prompt)
        self.prompt_i = len(self.prompts)

        self.req_id += 1
        rid = self.req_id
        cancel = self.cancel = threading.Event()
        cfg = gateway.load_config()
        self.on_ollama = cfg.get("backend") == "ollama"
        self.brain_label = (cfg.get("ollama_model") or "the model"
                            if self.on_ollama else "Claude")
        self.wait_from = self._now()
        self.busy = True
        self.pending = True
        self.tool = None
        self.state = THINK
        self.bubble = ""
        self.bubble_until = 0.0
        self.scroll_i = None                 # back to live
        # the question joins the thread right away, above the bubble
        context = list(self.history)
        self.history.append({"role": "user", "content": prompt})
        self._redraw()
        threading.Thread(
            target=self._worker, args=(prompt, rid, cancel, context), daemon=True
        ).start()

    # ------------------------------------------------------- chat commands
    def _command(self, text: str) -> None:
        parts = text[1:].split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""
        name = gateway.user_name()

        if self.busy:
            # a command wins over a running answer: stop it first, otherwise
            # the still-streaming reply would clobber the command's message
            self._stop()

        if cmd == "claude":
            gateway.set_backend("claude")
            self._refresh_agent()
            msg = f"On Claude Code now, {name}."
        elif cmd == "ollama":
            gateway.set_backend("ollama")
            self._refresh_agent()
            msg = f"On Ollama now: {gateway.load_config().get('ollama_model')}"
        elif cmd == "agent":
            want = arg.lower() not in ("off", "no", "0", "false")
            gateway.set_agent(want)
            self._refresh_agent()
            msg = (f"Hands on, {name} - I can poke the machine now. "
                   "Say the word and I'll do it."
                   if self.agent_on else "Hands off. Just talking now.")
        elif cmd in ("model", "models"):
            models = gateway.list_ollama_models()
            if arg:
                match = next(
                    (m for m in models if m == arg or m.startswith(arg)), None
                )
                if match:
                    gateway.set_ollama_model(match)   # switches to Ollama too
                    self._refresh_agent()
                    msg = f"Switched! Running Ollama · {match}"
                elif models:
                    msg = f"No '{arg}' here. I've got: " + ", ".join(models)
                else:
                    msg = "No models yet. Run: ollama pull <name>"
            elif models:
                cfg = gateway.load_config()
                on_ollama = cfg.get("backend") == "ollama"
                cur = cfg.get("ollama_model") if on_ollama else None
                lst = "\n".join(("▸ " if m == cur else "   ") + m for m in models)
                head = ("Ollama · models:" if on_ollama
                        else "You're on Claude Code. Ollama models:")
                msg = head + "\n" + lst
            else:
                msg = "No Ollama models. Run: ollama pull <name>"
        elif cmd == "status":
            msg = f"Brain: {gateway.status()} · {len(self.history) // 2} turns in mind"
        elif cmd == "clear":
            self.history.clear()
            self.scroll_i = None
            gateway.reset_memory()
            msg = f"Clean slate, {name}. Forgot the whole thing."
        elif cmd == "callme":
            if arg:
                msg = f"You got it - {gateway.set_user_name(arg)} it is."
            else:
                msg = f"I call you {name}. Change it with /callme <name>"
        elif cmd == "hide":
            self.set_visible(False)
            return
        elif cmd == "help":
            msg = ("/agent on|off · /claude · /ollama · /model [name] · "
                   "/models · /status · /clear · /callme <name> · /hide")
        else:
            msg = f"'/{cmd}'? Never heard of it, {name}. Try /help"

        self.state = IDLE
        self.bubble = msg
        self.bubble_until = 0.0 if self.chatting else self._now() + BUBBLE_LINGER
        self._redraw()

    # ---------------------------------------------------- backend (thread)
    def _worker(self, prompt: str, rid: int, cancel: threading.Event,
                history: list[dict]) -> None:
        acc = ""

        def on_tool(name: str | None) -> None:
            GLib.idle_add(self._on_tool, rid, name)

        try:
            for piece in gateway.stream(prompt, history, cancel, on_tool):
                if cancel.is_set():
                    break
                acc += piece
                GLib.idle_add(self._on_piece, rid, piece)
        except gateway.GatewayError as exc:
            GLib.idle_add(self._on_error, rid, f"Uh-oh: {exc}")
        except Exception as exc:                       # defensive
            GLib.idle_add(self._on_error, rid, f"Something went sideways: {exc}")
        finally:
            GLib.idle_add(self._on_done, rid, prompt, acc)

    def _on_piece(self, rid: int, piece: str) -> bool:
        """One freshly streamed piece of text -> straight onto the screen."""
        if rid != self.req_id:
            return False                     # answer to an abandoned question
        if self.pending:
            self.pending = False
            self.state = TALK
            self.bubble = ""
            piece = piece.lstrip()
        self.bubble += piece
        if len(self.bubble) > BUBBLE_KEEP:
            self.bubble = self.bubble[-BUBBLE_KEEP:]
        self._redraw()
        return False

    def _on_tool(self, rid: int, name: str | None) -> bool:
        """Agent mode: show the tool he just picked up (⚙ Bash…)."""
        if rid != self.req_id:
            return False
        self.tool = name
        self._redraw()
        return False

    def _on_error(self, rid: int, msg: str) -> bool:
        if rid != self.req_id:
            return False
        self.pending = False
        self.bubble = msg
        self._redraw()
        return False

    def _on_done(self, rid: int, prompt: str, text: str) -> bool:
        if rid != self.req_id:
            return False
        self.busy = False
        self.pending = False
        self.tool = None
        self.state = IDLE
        text = text.strip()
        if text:
            self.history.append({"role": "assistant", "content": text})
            del self.history[:-16]           # keep the last 8 turns
        elif self.history and self.history[-1] == {"role": "user",
                                                   "content": prompt}:
            self.history.pop()               # nothing came back: drop the turn
        # don't wander off while he is being read
        self.next_wander = self._now() + 25
        if not self.chatting and self.bubble:
            self.bubble_until = self._now() + BUBBLE_LINGER
        self._redraw()
        return False

    # ------------------------------------------------------------- IPC hook
    def handle_ipc(self, raw: str) -> None:
        self._wake()
        raw = raw.strip()
        if raw.lower().startswith("ask:"):
            text = raw[4:].strip()
            if self.hidden:
                self.set_visible(True)
            if not self.chatting:
                self._open_chat()
            self._send(text)
            return
        cmd = raw.lower()
        if cmd == "toggle":
            self.toggle_visible()           # Copilot key: show/hide Jake
        elif cmd in ("show", "summon"):
            self.set_visible(True)
        elif cmd == "hide":
            self.set_visible(False)
        elif cmd == "chat":
            self.set_visible(True)
            if not self.chatting:
                self._open_chat()
        elif cmd == "backend":
            gateway.toggle_backend()
            self._refresh_agent()
            self._note("Brain swapped: " + gateway.status())
        elif cmd.startswith("agent"):
            want = not self.agent_on if cmd == "agent" else cmd.endswith("on")
            gateway.set_agent(want)
            self._refresh_agent()
            self._note("Hands on: " + gateway.status() if self.agent_on
                       else "Hands off - chat only.")
        elif cmd == "nextmodel":
            model = gateway.cycle_ollama_model()   # this also lands on Ollama
            self._refresh_agent()
            self._note(f"Model: {model}" if model else "No Ollama models around")
        elif cmd in ("back", "forward"):     # flip through the memory thread
            if self.hidden:
                self.set_visible(True)
            self._scroll(1 if cmd == "back" else -1)
        elif cmd == "stop":
            self._stop()
