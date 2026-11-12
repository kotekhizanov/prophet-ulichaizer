# Prophet updates (prophet-ulichaizer)

A Prophet plugin that keeps the Mac's Prophet checkout up to date.

- Checks GitHub every `check_minutes` (plugin setting, 60 by default) whether
  the checkout's upstream branch (`origin/main`) has new commits.
- If it has, an **Update** button appears next to the version at the bottom
  left of Omniscient. Hover it for the window with the current and latest
  versions and the release notes: the merged pull requests the checkout lacks.
- **Update** runs, in the checkout: `git pull --ff-only`, `uv sync`, the
  database (`docker compose -f compose.dev.yaml [-f compose.local.yaml] up -d
  --wait`), `uv run prophet migrate` against the running core's
  `PROPHET_DATABASE_URL` (else the Makefile's `DEV_DB`), then restarts the
  core: a core running `uvicorn --reload` from the checkout (as `make dev` or
  a preview starts it) gets a fresh worker, and whatever started it keeps it;
  otherwise the core on the port is stopped and started again in the
  background, logging to `~/.local/share/prophet-updater/core.log`.
  Every open Omniscient tab follows the update and reloads by itself when the
  updated core answers `/health`.

An update refuses a checkout with uncommitted changes to tracked files.

## Parts

- `prophet-plugin.json` — manifest (id `ulichaizer`, server `http://127.0.0.1:8797`).
- `omniscient.js` — the browser script.
- `server.py` — the server, standard library only; the node runs
  `python3 -u server.py` from the clone.

Environment (all optional): `PROPHET_REPO` (default `~/Projects/prophet_v2`),
`PROPHET_URL` (set by the node), `UPDATER_PORT` (8797), `UPDATER_ORIGINS`
(extra Omniscient origins, comma-separated). Release notes are read with the
`gh` CLI, so private repositories work when `gh` is signed in.

## Install

Omniscient → Settings → Plugins → Add plugin, source
`https://github.com/kotekhizanov/prophet-ulichaizer`, node: this Mac.
