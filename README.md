The script, Python environment and working data are stored in `/Volumes/Media-SSD/Gaming/apps/tool-SELExophase/`. This installation uses your existing Homebrew Python 3.13.

Verified on 2026-10-27: the interface loads all 306 current games from `cache/`. The cache/report separation is covered by storage tests, including achievement sync, history compilation, individual reports and failed-report preservation. All 125 existing data and report files retained their original checksums during migration. No live API synchronisation or existing report overwrite was performed during these checks.

The volume is mounted over SMB, so installation and startup can be slow.

# Run SELFexophase

## Folder structure

```text
tool-SELExophase/
├── SELFexophase.py   # Existing terminal application
├── headless_exporter.py  # Independent public-API JSON exporter
├── build_site.py     # Allowlisted static build and local preview
├── index.html        # Static activity dashboard
├── assets/           # Local dashboard CSS and JavaScript
├── .github/workflows/sync.yml  # Scheduled refresh and gated Pages deployment
├── .gitignore        # Excludes local data except the two telemetry snapshots
├── run.command      # Double-click launcher for macOS Terminal
├── README.md        # Usage, dependencies, rebuilding and backup instructions
├── .venv/           # Installed Python environment and dependencies
├── tests/           # Exporter, static build and version-control checks
├── cache/           # Downloaded game JSON and achievement working files
└── output/          # Final user-facing CSV and Excel reports
```

The dependency installation command is documented below; no separate `requirements.txt` is needed. The application reads downloaded game JSON and achievement working files from `cache/`. Report generation is staged in `cache/`; a report is moved into `output/` only after its write succeeds. Temporary report files are removed after success or a handled error.

## Quick launch

Double-click `run.command` in Finder. It opens the application in Terminal and resolves paths from its own location. Alternatively, run it from Terminal:

```bash
/Volumes/Media-SSD/Gaming/apps/tool-SELExophase/run.command
```

Run each command block separately in the **same Terminal window**. If a command reports an error, stop at that step.

## Headless JSON export

The headless exporter runs without Textual, a terminal session, pandas or a Python web server. Its only third-party dependency is the already-installed `requests` package. The existing terminal app and CSV/Excel exporters are unchanged.

Run a live export:

```bash
cd /Volumes/Media-SSD/Gaming/apps/tool-SELExophase
EXOPHASE_PLAYER_ID=5143228 .venv/bin/python -B headless_exporter.py
```

The numeric profile defaults to `5143228` and can also be selected with `--player-id`. No cookies, access tokens or API keys are required. The exporter uses the same request headers as the terminal app; remote access restrictions can still prevent a run.

To compile the existing downloaded games without a network request:

```bash
.venv/bin/python -B headless_exporter.py --input cache/all_games_5143228.json
```

Files produced:

- `cache/telemetry_cache.json`: the complete raw snapshot, source profile and retrieval metadata. It is separate from the terminal application's game and achievement cache files.
- `output/data.json`: the versioned `selfexophase-telemetry/v1` dashboard dataset, containing stable source IDs, original titles, source service, release platforms, known playtime, earned achievements and achievement completion percentages.

Default directories resolve beside `headless_exporter.py`, even when launched from another working directory. Override them with `EXOPHASE_CACHE_DIR` / `EXOPHASE_OUTPUT_DIR` or `--cache-dir` / `--output-dir`. Explicit relative overrides and `--input` resolve from the caller's working directory. Keep cache and output as separate, non-nested directories on the same filesystem for atomic report publication.

`metadata.last_updated` is the UTC report generation time. `source_retrieved_at` is the UTC live download time; offline imports explicitly leave it unknown and use `source_mode: "offline"`. `total_known_hours` excludes unknown playtime, and `unknown_playtime_count` reports that gap. Activity status is `played` only when positive playtime or earned achievements provide evidence; otherwise it is `unknown`. Achievement completion does not establish game completion, backlog status or ownership. Service and release platforms remain separate.

The exporter fetches every page and rejects HTTP failures, unconfirmed responses, malformed records, repeated IDs and non-terminating pagination. The endpoint normally returns 50 games per page; a successful short or empty page ends the download. An ambiguous `success: false` response after a full page is held as a failure rather than assumed complete. Empty datasets and missing previously cached games are held for review rather than silently replacing the last good snapshot. Failed runs return a non-zero exit code.

All download and transformation checks finish before either published file is updated. Both JSON files are staged under cache; final file replacement is atomic, and a handled report replacement failure restores the previous raw cache. A sudden process or machine crash can leave a newer complete raw cache beside the older final report. This is not an atomic transaction across both files; the previous complete final report remains intact. Do not run concurrent exporters against the same directories.

Run the headless regression tests:

```bash
.venv/bin/python -B -m unittest discover -s tests -v
```

The exporter itself does not create repositories or enable schedules. The cloud workflow and dashboard below are separate components; external publication requires an explicit decision about the repository and website audience.

## Cloud workflow and dashboard

`index.html` and `assets/` provide a buildless dashboard for `output/data.json`. There is no CDN dependency, JavaScript framework or server-side application. Search, source-service filtering and sorting work in the browser. Unknown playtime remains unknown, achievement percentages do not imply game completion, and download time is separate from report generation time. Dates display in Melbourne time. Source titles use text nodes, and source links are restricted to HTTPS Exophase URLs.

### Local preview

From the tool directory, build into a new empty temporary directory and serve only that public bundle:

```bash
telemetry_preview="$(mktemp -d)"
.venv/bin/python -B build_site.py --destination "$telemetry_preview" --serve
```

Open `http://127.0.0.1:8786/`. Stop the preview with Ctrl+C. Do not open `index.html` through a `file://` URL, which may block JSON loading, or serve the whole project directory, which contains the raw cache.

### Tracked state and published files

`.gitignore` keeps `.venv/`, bytecode, local reports, dated archives, achievement JSON, scratch files and environment files out of Git. Its only cache/output exceptions are `cache/telemetry_cache.json` and `output/data.json`, providing a durable raw snapshot and dashboard dataset for ephemeral runners. These files contain profile activity; the repository is intended to be private.

`build_site.py` copies only `index.html`, the two dashboard assets and `output/data.json`, plus an empty `.nojekyll` file. The raw cache, Python sources and local reports are not included in the Pages deployment artifact. The script refuses non-empty destinations rather than deleting existing files.

### GitHub Actions

`.github/workflows/sync.yml` is prepared for the `main` branch. It runs at `00:00` and `12:00` UTC, on relevant pushes, and on manual dispatch. GitHub schedules are best effort; delays, inactivity disabling and remote API restrictions remain possible. Timestamp updates and bot commits are not an indefinite availability guarantee.

The collection job uses Python 3.13 and installs only `requests==2.34.2`. Official action releases are pinned to verified commit SHAs. Tests run before downloading; all validation and static build checks must pass before the job stages exactly the two snapshot files. Git configuration is repository-local. Push rejection stops deployment; there is no force push. Workflow concurrency prevents overlapping runs from this workflow. Failures preserve the last committed snapshot and previously deployed site.

No personal access token is required. The collection job receives `contents: write` through the repository's `GITHUB_TOKEN`. The deployment job receives only `contents: read`, `pages: write` and `id-token: write`. Repository branch protections or organisation policy can require additional configuration and may block bot pushes; protections are not disabled by this setup.

The Pages job is gated by repository variable `PAGES_ENABLED=true`. Leave it unset until site publication is approved and Pages is configured to use **GitHub Actions**, rather than branch-based publishing. GitHub does not trigger branch-based Pages builds from commits made with `GITHUB_TOKEN`, so this workflow uploads and deploys the verified artifact in the same run.

### Cloud publication status

The repository is live at [exlennis/tool-SELExophase](https://github.com/exlennis/tool-SELExophase) with **private** visibility. The 12-hour workflow is enabled. Its [initial cloud run](https://github.com/exlennis/tool-SELExophase/actions/runs/40483602152) passed all 18 tests, downloaded 309 games and committed the refreshed snapshot. The local checkout was fast-forwarded to that verified state.

Public dashboard publication was approved conditionally on the existing plan supporting private-repository Pages. GitHub rejected activation on 2026-10-27 with HTTP 422: "Your current plan does not support GitHub Pages for this repository." No site is published, and `PAGES_ENABLED` remains unset. The repository remains private; no plan was purchased and no public-repository or alternative-hosting fallback was used. A different hosting arrangement requires another decision.

If Pages eligibility changes later, configure the site with the GitHub Actions workflow source, set `PAGES_ENABLED=true`, manually dispatch `sync.yml`, and verify the actual deployment URL. A normal Pages site makes the compiled activity dataset public even when its repository is private. The raw snapshot is excluded from the deployment artifact. Do not use branch-root publishing, which could expose the raw cache as a website asset.

The Python exporter, static assets and JSON remain portable if moving away from GitHub; scheduling and deployment configuration would need replacing. Repository history provides snapshot versions, not an independent backup policy. Keep the existing local backups.

References: [custom Pages workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages), [publishing-source behaviour](https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site), [Pages availability and visibility](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site).

## 1. Open the existing folder

Open Terminal and run:

```bash
cd /Volumes/Media-SSD/Gaming/apps/tool-SELExophase || exit
pwd
```

The last command should print:

```text
/Volumes/Media-SSD/Gaming/apps/tool-SELExophase
```

`cd` makes the existing tool folder your current working folder. The script and environment are already installed; for normal use, use `run.command` or go to step 5.

## 2. Check the virtual environment

The rebuilt `.venv` is already installed. Do not recreate it for normal use. Virtual environments contain absolute paths; if you move this folder again, rebuild the environment at its new location. [Python virtual environment documentation](https://docs.python.org/3.13/library/venv.html#how-venvs-work)

Check its location:

```bash
.venv/bin/python -c 'import sys; print(sys.executable); print("Isolated:", sys.prefix != sys.base_prefix)'
```

You should see a path inside `tool-SELExophase/.venv/` and:

```text
Isolated: True
```

We’ll call this environment’s Python directly, so there’s no activation or later deactivation step. [Python virtual environment documentation](https://docs.python.org/3/library/venv.html)

## 3. Inspect the script

`SELFexophase.py` is already present. The [upstream project](https://github.com/TripShuti/SELFexophase) provides its source and installation instructions.

To inspect it, open `SELFexophase.py` in your text editor, or read it in Terminal:

```bash
less SELFexophase.py
```

Press `q` to leave the viewer.

## 4. Check the installed packages

The required packages are already installed. To verify them, run:

```bash
.venv/bin/python -m pip --isolated --disable-pip-version-check check
.venv/bin/python -B -c 'import textual, requests, pandas, openpyxl; print("All four packages imported successfully")'
```

For reference, this command installs the same four package versions used in the repaired environment. Only use it when rebuilding or repairing the environment. For a fresh rebuild, first create `.venv` at its final location with `/opt/homebrew/bin/python3.13 -m venv .venv`.

```bash
.venv/bin/python -m pip --isolated --disable-pip-version-check install \
  --no-cache-dir --no-compile \
  --only-binary=:all: \
  textual==8.2.8 requests==2.34.2 pandas==3.0.6 openpyxl==3.1.5
```

The four packages come from the project’s [installation instructions](https://github.com/TripShuti/SELFexophase).

The options mean:

- `--isolated`: ignore personal pip configuration.
- `--no-cache-dir`: avoid retaining pip’s shared download cache.
- `--no-compile`: skip bytecode compilation to reduce file writes over SMB.
- `--only-binary=:all:`: use prepared packages instead of building them locally.

If pip says no compatible distribution exists, stop and share that error.

## 5. Run the application and retrieve your games

Run:

```bash
.venv/bin/python -B SELFexophase.py
```

The app loads the latest `cache/all_games_*.json` automatically. Archived JSON in subfolders is not selected. To retrieve fresh data, use these controls:

1. Press `s` to open synchronisation.
2. Enter your Player Profile ID: `5143228`.
3. Select the `Synchronise` button.
4. Wait for the game list to populate.

Inspect several familiar games and compare the platform lists with your Exophase profile. If the app reports an API error, treat the download as potentially incomplete.

## 6. Export your data

From the main game list:

- Press `e` for an Excel workbook covering the available platform groups.
- Press `c` for a CSV of the currently selected platform tab. Repeat on other tabs as needed.
- Press `q` to quit.

Downloaded game lists (`all_games_*.json`) and per-game achievement working files (`awards_*.json`) go into `cache/`. Synchronising achievements and compiling history both reuse these cached files.

Final CSV and Excel reports go into `output/`, including per-game achievement reports and `exophase_awards_history_5143228.csv` / `.xlsx`. This local copy uses separate `CACHE_DIR = "cache"` and `OUTPUT_DIR = "output"` locations.

## 7. Check and preserve the exports

After quitting, run:

```bash
open /Volumes/Media-SSD/Gaming/apps/tool-SELExophase/output
```

In Finder:

1. Open the Excel workbook or CSV files.
2. Check several game names, platforms and playtime values.
3. Copy the entire `output` folder to your chosen permanent location.
4. Open a file from that permanent copy to confirm it worked.

Back up `cache/` separately as well. Its JSON preserves the original downloaded data and any local game-list edits; removing it means the app must download the data again. The existing dated JSON archive has moved to the matching dated subfolder inside `cache/`.

## 8. Backup and cleanup

Before removing the tool folder or its environment, quit the app and verify separate copies of `output/` and `cache/`. Removing the whole tool folder also removes its saved data and exports. Your existing Homebrew Python installation is separate.

The environment isolates Python packages, but it is not a security sandbox. The app manages `cache/` for working data and `output/` for final reports. No separate config, data, state or temporary directories are required.
