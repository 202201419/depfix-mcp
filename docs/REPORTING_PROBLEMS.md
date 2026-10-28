# Reporting a problem (copy this, fill it in, paste it to me)

```
What I ran:
OS + chip:
uv run python --version:
Output (last 30 lines):
git status (short):
```

`make check` prints one summary at the end; paste its last 30 lines. If a single test fails,
also run `uv run pytest -x -q path/to/test.py` and paste that.
