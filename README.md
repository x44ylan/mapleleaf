# Mapleleaf

Mapleleaf is a Windows observer and supervised workflow runner for MapleStory M. It recognizes known screens, checks remaining entries and records attempts in a local SQLite ledger. The dashboard captures the game window and previews navigation.

## Setup

Use Python 3.12 or newer and an existing Appium/NovaWindows driver.

```powershell
python -m pip install -e .
python -m mapleleaf.cli --help
```

Keep your taught vision profile, image templates, quest settings and daily plan in the ignored `config/`, `anchors/` and `quests/` directories. The dashboard loads `config/vision.json`. With the driver and profile ready, run:

```powershell
python -m mapleleaf.cli dashboard
python -m mapleleaf.cli run-dailies --validate
```

The dashboard opens at `http://127.0.0.1:8765`. Run validation separately to check your local plan and templates without connecting to the driver.

The daily workflow still needs supervised validation. Runner commands send game input only with `--live`. Reward claims stay manual.

See [architecture](docs/architecture.md) for configuration paths and runtime boundaries.
