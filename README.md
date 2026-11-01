# Grant Reporting Assistant V.1.0

A local web application that rebuilds grant-report tables from current client and case data.

- Client records are imported into a SQLite database on your computer.
- The AI receives only the historical report or requirements, the non-identifying database structure, code lists and aggregate query results. It never receives individual records, identifier values, case notes or the database.
- Every number in a finished table comes from a local query result.

Workflow: **Upload → Prepare automatically → Review**, with Copy, Word and Excel export.

## Running it

Double-click `start.bat`. The first run sets up a private Python environment (Python 3.11+ required) and then opens the app at `http://127.0.0.1:8765/`. On first use, paste an Anthropic API key once; it is stored in Windows Credential Manager.

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

## Development

The repository may sit in a cloud-synced folder, so keep the virtual environment outside it:

```bash
python -m venv %LOCALAPPDATA%\GrantReportingAssistant\venv
%LOCALAPPDATA%\GrantReportingAssistant\venv\Scripts\python -m pip install -e .[dev]
%LOCALAPPDATA%\GrantReportingAssistant\venv\Scripts\python -m pytest
```

Working files (databases, uploads, exports) are stored in `%LOCALAPPDATA%\GrantReportingAssistant`, outside this repository. Set `GRA_DATA_DIR` to override.

Never commit client data, uploaded files, databases, exports or keys. `.gitignore` excludes them, including the local synthetic test packages.
