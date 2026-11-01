# Grant Reporting Assistant V.1.0

A local web application that rebuilds grant-report tables from current client and case data.

- Client records are imported into a SQLite database on your computer.
- The AI receives only the historical report or requirements, the non-identifying database structure, code lists and aggregate query results. It never receives individual records, identifier values, case notes or the database.
- Every number in a finished table comes from a local query result.

Workflow: **Upload → Prepare automatically → Review**, with Copy, Word and Excel export.

## Status

Under construction. See the build plan for milestones.

## Development

The repository may sit in a cloud-synced folder, so keep the virtual environment outside it:

```bash
python -m venv %LOCALAPPDATA%\GrantReportingAssistant\venv
%LOCALAPPDATA%\GrantReportingAssistant\venv\Scripts\python -m pip install -e .[dev]
%LOCALAPPDATA%\GrantReportingAssistant\venv\Scripts\python -m pytest
```

Working files (databases, uploads, exports) are stored in `%LOCALAPPDATA%\GrantReportingAssistant`, outside this repository. Set `GRA_DATA_DIR` to override.

Never commit client data, uploaded files, databases, exports or keys. `.gitignore` excludes them, including the local synthetic test packages.
