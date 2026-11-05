# Grant Reporting Assistant V.1.1

A local web application that rebuilds grant-report tables from current client and case data.

- Client records are imported into a SQLite database on your computer.
- The AI receives only the historical report or requirements, the non-identifying database structure, code lists and aggregate query results. It never receives individual records, identifier values, case notes or the database.
- Every number in a finished table comes from a local query result.

Workflow: **Upload → Prepare automatically → Review**, with Copy, Word and Excel export.

## Running it

Requirements: Windows, Python 3.11 or newer from [python.org](https://www.python.org/downloads/) (tick "Add python.exe to PATH" when installing), an API key from Anthropic (Claude) or OpenAI, and an internet connection while preparing a report.

1. Download `grant-reporting-assistant-1.1.0.zip` from [Releases](https://github.com/nzachary22/Claude-GRA/releases) and unzip it to a local folder, such as `Documents\Grant Reporting Assistant`. Avoid OneDrive, Google Drive or Dropbox folders.
2. Double-click `start.bat`. The first run sets up a private Python environment in `%LOCALAPPDATA%\GrantReportingAssistant` (about a minute), adds a **Grant Reporting Assistant** shortcut to the desktop and the Start menu, and opens the app at `http://127.0.0.1:8765/`. From then on, start the app from that shortcut.
3. Choose Anthropic (Claude) or OpenAI and paste your API key once; it is stored in Windows Credential Manager. Both keys can be saved, and Settings switches between them.
4. Keep the black window open while using the app; close it to stop.

If Windows shows "Windows protected your PC" the first time, choose **More info**, then **Run anyway**. If the folder is moved, run `start.bat` from its new place once and the shortcuts are updated.

To update, replace the program folder with the new version and run `start.bat`; it re-runs setup when needed, and saved runs and the API key are kept. To remove everything, use **Delete all saved runs** in Settings, delete the program folder, the Grant Reporting Assistant shortcuts on the desktop and in the Start menu, and `%LOCALAPPDATA%\GrantReportingAssistant`, and remove the "GrantReportingAssistant" entry from Windows Credential Manager.

Using the app:

1. **Upload** the last report or the grant requirements, the current data exports, and the reporting period.
2. **Prepare report**. Import, calculations and table assembly run automatically, with progress and a Cancel button.
3. **Review** the draft tables with their assumptions and gaps, copy them, or download Word or Excel. Use **Change** on a note, or **Request a correction**, to recalculate.

## How client data is protected

| Part | What it does |
| --- | --- |
| `gra/importer.py` | Loads exports into SQLite and labels each column: identifier, sensitive, free text, category, date, measure. |
| `gra/schema.py` | Describes the data to the AI without any identifying values; free-text columns are not even named. |
| `gra/gate.py` | Checks and runs each AI query read-only. Only aggregates leave; identifiers work only for joins and distinct counts; probes, listings, unsafe statements and revealing errors are refused. |
| `gra/tables.py` | Fills every number from a local query result and computes totals; the AI cannot type a figure in. |
| `gra/engine.py` | Runs the bounded AI loop (query, turn and time limits, cancel, corrections). |
| `gra/llm.py`, `gra/llm_openai.py` | Connect to the chosen AI service. Both services get exactly the same instructions, tools and protected data description; OpenAI requests are sent with storage turned off. |

## Before using real client data

- **Preview what the AI will see** (upload screen) imports the exports on this computer and shows how each column will be treated and the exact data description the AI would receive. Nothing is sent. Check that no name, ID or address shows as Shared.
- Uploaded reports are sent to the AI in full. Before sending, the app checks their text for names, IDs and notes from the uploaded data and stops if it finds a full name or ID. Images and scanned PDFs cannot be checked.
- Corrections that mention a stored name, ID or note are refused.
- **Delete this run's files** (review screen) and **Delete all saved runs** (Settings) remove uploads, databases and AI records from this computer.
- Keep real exports outside the repository and any cloud-synced folder.

## What leaves the computer

**AI activity report** beside the downloads is a short Word document for a run, including failed and cancelled runs: what stayed on this computer, what was sent, and a table of each information packet to scan. Response summaries come from the data actually sent, and the sent text is checked against stored identifiers and notes. Technical details offers the full word-for-word record, written as each request goes out, in which uploaded PDFs and images are listed by name, size and fingerprint. The app keeps the five most recent runs, including their uploads and databases, and deletes older ones.

## Development

The repository may sit in a cloud-synced folder, so keep the virtual environment outside it:

```bash
python -m venv %LOCALAPPDATA%\GrantReportingAssistant\venv
%LOCALAPPDATA%\GrantReportingAssistant\venv\Scripts\python -m pip install -e .[dev]
%LOCALAPPDATA%\GrantReportingAssistant\venv\Scripts\python -m pytest
```

Working files (databases, uploads, exports) are stored in `%LOCALAPPDATA%\GrantReportingAssistant`, outside this repository. Set `GRA_DATA_DIR` to override.

To build the release zip from a clean commit: `python tools/build_release.py`. It packs only `gra/`, `start.bat`, `pyproject.toml`, `LICENSE` and `README.md` into `dist/` and prints the size and SHA-256.

Never commit client data, uploaded files, databases, exports or keys. `.gitignore` excludes them, including the local synthetic test packages.

## License

MIT. See [LICENSE](LICENSE). Provided as is, without warranty or support.
