# How to Use (macos)
1. Open the Terminal or Command Line application.
2. Download `uv` python manager by running `curl -LsSf https://astral.sh/uv/install.sh | sh`
3. Clone this repo with `git clone https://github.com/SharpieGod/mtg-card-pdf && cd mtg-card-pdf && uv sync`
4. Run `uv run mtg.py`

If Python is complaining about a `"No module named 'tkinter'"` run `brew install python-tk@3.14` then try again.