# How to Build/Use (macos)

1. Download `uv` python manager by running `curl -LsSf https://astral.sh/uv/install.sh | sh`
2. Clone this repo with `git clone https://github.com/SharpieGod/mtg-card-pfd && cd mtg-card-pdf && uv sync`
3. Run `./build.sh`

If Python is complaining about a `"No module named 'tkinter'"` run `brew install python-tk@3.14`