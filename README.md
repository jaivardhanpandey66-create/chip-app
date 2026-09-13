<div align="center">

# CHIP — native Linux desktop app

**The CHIP holographic AI agent as a real desktop application.**
Pure Python + GTK + WebKitGTK — no Electron.

</div>

---

## What it is

A native Linux window that wraps the full CHIP agent stack —
`chip_web.py` (streaming agent, tools, PLAN/BUILD, delegation, voice)
and the holographic `chip_ui.html` interface — inside a WebKitGTK window.
One click in your app menu and you're talking to the arc reactor.

## Install

```bash
git clone https://github.com/jaivardhanpandey66-create/chip-app.git
cd chip-app
./install.sh
```

The installer:
- installs the WebKitGTK bridge (`python3-gi`, `gir1.2-webkit2-4.1`) — needs sudo
- installs the `openai` Python package
- builds the optional C++/Rust native cores if toolchains exist
- installs a launcher, icon, and `.desktop` entry into `~/.local`

Launch CHIP from your app menu, or run `~/.local/share/chip-app/chip_app.py`.

## Requirements

- Linux with a desktop session (GTK3)
- Python 3.10+
- WebKitGTK bridge: `sudo apt install python3-gi gir1.2-webkit2-4.1`
- An API key from [OpenRouter](https://openrouter.ai) (free)

## Set your API key (one time)

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."          # or:
mkdir -p ~/.config/chip && echo -n 'sk-or-v1-...' > ~/.config/chip/key
```

## How it works

```
 ┌─────────────────────────────────────────┐
 │  CHIP desktop window (WebKitGTK)        │
 │   ┌───────────────────────────────────┐ │
 │   │  chip_ui.html (holographic UI)    │ │
 │   └──────────────┬────────────────────┘ │
 │   WebKit talks to │ via http://127.0.0.1│
 └──────────────────┼──────────────────────┘
                    ▼
        chip_web.py (embedded subprocess, random free port)
            │ SSE │ ctypes
            ▼     ▼
      agent loop + C++/Rust native cores
```

The app ships and runs its own copy of the CHIP server — completely
separate from any you already run — and picks a free port automatically,
so it never collides with other instances.

## Project layout

| Path | What it is |
|------|------------|
| `chip_app.py` | GTK window + WebKit view + embedded server lifecycle |
| `chip.svg` | App icon (arc reactor) |
| `chip.desktop` | Desktop-entry template for the app menu |
| `install.sh` | One-shot installer (system deps → native cores → launcher) |
| `server/` | The bundled CHIP agent (`chip_web.py`, `chip_ui.html`, native sources) |

## Notes

- The app embeds the same agent as github.com/jaivardhanpandey66-create/chip —
  keep the two in sync if you fork them.
- Your API key is read from the environment or `~/.config/chip/key`, neither
  of which lives in this repository.

MIT.