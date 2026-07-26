# Jake the familiar 

Jake from Adventure Time as a **desktop familiar** on Hyprland/Wayland: an
animated overlay that doubles as an **arcane gateway** to **Claude Code** and
to **local models via Ollama** — and, when you arm him, a little agent with
real hands on your machine.

- Sprites cut from the sprite sheet (transparent), animated by **frame
  switching** (idle / walk / talk), with a cached glow.
- Wanders the screen in an "arcane" way (via `gtk4-layer-shell`), and **dozes
  off** (💤) when you leave him alone — a click, a key, anything wakes him. The
  frame clock slows right down while he sleeps or hides, to spare the battery.
- Click him and type: the answer lands in his speech bubble **live**, exactly
  as the model produces it — no fake typewriter, no waiting for the full reply.
- He calls you **Finn**. Because you are.

## Using him

| What | How |
|---|---|
| Show / hide Jake | **Copilot key** (or `SUPER + SHIFT + J`) |
| Open the chat | **click on Jake** — type, hit Enter |
| Move him | **drag him** anywhere with the left button |
| Swap brain (Claude ⇄ Ollama) | **right click** on Jake, or `SUPER + SHIFT + B` |
| Cycle Ollama model | `SUPER + SHIFT + M` |
| Read past messages | **mouse wheel over Jake** (also `PageUp`/`PageDown`, `Alt+↑/↓`) |
| Recall what you typed | `↑` / `↓` in the text box |
| Stop him mid-answer | `Esc` (a second `Esc` closes the chat) |
| Start manually | `~/jake/run.sh` |

Jake starts on login (he is in the Hyprland autostart), together with
`ollama serve`.

### He only eats the clicks he needs

The canvas around Jake is click-through: only his body (and the text box while
you are chatting) takes input, so he never blocks the window underneath.

## The memory thread 🧵

The conversation is not stored away in some window you have to open — it hangs
over his head.

- While you chat, past messages float above the live bubble as small faded
  **chips** strung on a dashed golden thread: **yours on the right** (dark),
  **his on the left** (parchment). The older they are, the more they fade.
- **Wheel over Jake** walks back through the exchanges. The one you land on
  opens full-text in the bubble, your question in italics above a hairline,
  with a `◂ 2/7 ▸` counter. Wheel forward to the newest and you are live again.
- Do it with the chat closed and you get a **peek**: the thread shows up, then
  fades on its own after a few seconds.
- Nothing is drawn when you are not talking to him. Zero pixels, zero noise.

`/clear` wipes the thread (and the brain's memory of the conversation).

## Agent mode ⚡

With the **Claude brain**, Jake can actually operate the machine: read and edit
files, run shell commands, search the web. Ask him to do something and he does
it, then reports back in a line.

- **On by default** (`agent: true` in the config), but only the Claude backend
  has tools — on Ollama he is chat-only.
- When he is armed you can **see** it: the bubble trim and the text box turn
  **teal**, and the placeholder becomes *"tell Jake what to do…"*.
- While he works, the bubble shows what he is holding: `⚙ Bash…`, `⚙ Read…`.
- He works from your home directory (`~`).
- Toggle it: `/agent on`, `/agent off`, or `~/jake/bin/jake-summon agent`.

> **Heads up:** armed, Jake runs tools without asking (Claude Code's
> `bypassPermissions`). His instructions tell him to check with you before
> anything irreversible — deleting, overwriting, killing processes, touching
> system config — but that is a persona rule, not a sandbox. If you want him
> harmless, `/agent off` and the teal goes away.

### Slash commands

Type them in Jake's text box:

| Command | Effect |
|---|---|
| `/agent [on\|off]` | give Jake hands, or take them away |
| `/claude` | switch to Claude Code |
| `/ollama` | switch to local Ollama |
| `/model <name>` | pick the Ollama model (prefix works: `/model llama`) |
| `/models` | list installed Ollama models (▸ = current) |
| `/status` | current brain, model, agent state, turns in memory |
| `/clear` | forget the conversation (thread + brain) |
| `/callme <name>` | change what he calls you |
| `/hide` | vanish |
| `/help` | the list above |

## The brain (backend)

Config lives in `~/.config/jake/config.json`:

```json
{
  "backend": "ollama",          // "claude" | "ollama"
  "claude_model": "",           // empty = the claude CLI default
  "ollama_url": "http://localhost:11434",
  "ollama_model": "deepseek-r1:14b",
  "ollama_keep_alive": "2m",    // how long Ollama keeps the model resident
  "user_name": "Finn",          // what he calls you
  "agent": true                 // hands on (Claude backend only)
}
```

Edit this file while Jake is running and he notices on his own within a second
(no restart): change `user_name`, flip `agent`, switch `backend` — the teal and
the persona follow the file live.

**claude** — runs the `claude` CLI as a JSON event stream
(`--output-format stream-json --include-partial-messages`), which is what makes
the bubble fill in token by token; plain text output would only arrive once the
whole answer is done. Each question resumes the previous session, so he
remembers the conversation on his own.

**ollama** — the local server. Reasoning models (`deepseek-r1` & co) are asked
for the answer only (`think: false`), so you get words in the bubble instead of
a minute of silent muttering; any stray `<think>` block is filtered out anyway.
If the configured model is gone, he falls back to the first one installed.
Models: `ollama list`, `ollama pull <name>`.

### Why is he slow?

It is the brain you picked, not the pet. Claude answers in a couple of seconds.
A local 14B model on an iGPU spends ~40s just loading itself, then types at a
few tokens per second — so while you wait, the bubble tells you what is going
on (`⚙ waking deepseek-r1:14b… 12s`) instead of blinking dots forever.

If you want local **and** fast, pull something small — `ollama pull llama3.2:3b`
— and `/model llama`.

**And it costs RAM.** A 14B model sits in memory at ~9.5 GB while it is loaded.
So: `ollama_keep_alive` is `2m` — resident long enough for a back-and-forth,
gone shortly after you stop — and **switching back to the Claude brain unloads
it immediately** (`/claude`, right click, `SUPER + SHIFT + B`). Check what is
resident with `ollama ps`.

## The Copilot key

The bind assumes the Copilot key emits `SUPER + SHIFT + F23` (the common case
on Linux). If pressing it does nothing, find out what it really emits:

```
~/jake/bin/jake-detectkey      # press the key, copy the printed bind line
```

and replace the Copilot bind in `~/.config/hypr/hyprland.lua`.

### Optional extra binds

`jake-summon` forwards any command, so you can bind the new ones too:

```lua
hl.bind("SUPER + SHIFT + A", hl.dsp.exec_cmd(os.getenv("HOME") .. "/jake/bin/jake-summon agent"))
hl.bind("SUPER + SHIFT + comma",  hl.dsp.exec_cmd(os.getenv("HOME") .. "/jake/bin/jake-summon back"))
hl.bind("SUPER + SHIFT + period", hl.dsp.exec_cmd(os.getenv("HOME") .. "/jake/bin/jake-summon forward"))
```

Full IPC vocabulary: `summon`, `toggle`, `hide`, `chat`, `backend`, `agent`,
`nextmodel`, `back`, `forward`, `stop`, `ask:<text>`.

## Layout

```
jake/
├── jake/
│   ├── __main__.py    app startup + IPC listener (unix socket)
│   ├── pet.py         GTK4 overlay: drawing, animation, wandering, chat,
│   │                  memory thread, input shaping
│   ├── sprites.py     frame loading (assets/frames/*.png) + cached glow
│   └── gateway.py     swappable backends (Claude / Ollama), persona,
│                      live streaming, agent tools, reasoning filter
├── assets/
│   ├── spritesheet.png
│   └── frames/        idle_0..3.png, walk_0..3.png (transparent cutouts)
├── bin/
│   ├── jake-summon    what the Copilot key calls (IPC, or starts him)
│   └── jake-detectkey finds the keysym of the Copilot key
└── run.sh
```

## Dependencies

- Python 3, PyGObject + GTK4, pycairo (already there).
- **gtk4-layer-shell** (optional but recommended): the overlay floats above
  everything and roams freely. Without it Jake sits still as a floating window
  (rules in `hyprland.lua`). Install with:
  `sudo pacman -S gtk4-layer-shell`
- **claude** CLI for the Claude brain and agent mode.
- **Ollama** installed in `~/.local` (standalone binary, no root).

## Development

The GTK overlay (`pet.py`, `sprites.py`) needs a real Wayland session, but the
brain (`gateway.py`) and the conversation bookkeeping (`conversation.py`) are
pure Python and covered by tests that run anywhere:

```
python3 -m unittest discover -s tests      # no dependencies needed
# or, if you have the dev extras:
pip install -e '.[dev]'
pytest            # tests
ruff check .      # lint
mypy jake         # types
```

The tests pin the fiddly bits: the `<think>…</think>` filter surviving tags
split across streamed chunks, the atomic/hot-reloading config store, and the
(question, answer) pairing behind the memory thread.
