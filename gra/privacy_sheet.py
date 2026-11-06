"""A one-page handout: how the app works and how client data stays private.

Written for an organization's leadership or IT reviewer. Every statement here
describes what the application actually does; keep it in step with the code.
"""

from __future__ import annotations

import io

from . import __version__

TITLE = "Grant Reporting Assistant: how it works and how client data stays private"

STEPS = [
    ("You upload two things", "last period's report (or the grant requirements) and current exports of client and case "
                              "data, such as CSV or Excel files from your case management system."),
    ("The app builds a private database on this computer", "from the data files. It marks columns holding names, IDs, "
                                                           "birth dates and free-text notes as protected."),
    ("An AI service plans the tables", "it reads the report and a description of your data columns, then asks this "
                                       "computer to run counting queries. A privacy gate on this computer only lets "
                                       "totals and counts back out, never individual records."),
    ("You review and download", "the finished tables in Word or Excel, with notes on any assumptions or missing "
                                "information, and a log of everything that was sent."),
]

STAYS = [
    "Your client and case data files, and the database built from them.",
    "Individual records: names, IDs, birth dates, case notes and other free text are never sent.",
    "Your API key, kept in this computer's secure key store (Windows Credential Manager or the Mac Keychain).",
]

SENT = [
    "The report or grant requirements you upload, in full. Remove client names or stories from it first.",
    "A description of your data: column names and types, and the short category lists of code columns "
    "(for example the case types or outcome codes used).",
    "Totals and counts calculated on this computer (small counts, including 0 and 1, are sent as they are).",
    "Any correction you type, after the app checks it for stored names and IDs.",
]

SAFEGUARDS = [
    "The privacy gate refuses any query that would return individual records, list values, or output an ID.",
    "Birth dates and other sensitive fields can only be used in grouped form (for example age bands).",
    "Before sending, the app checks the text of uploaded reports and corrections for names and IDs from your data. "
    "Images and scanned PDFs cannot be checked, so review them yourself.",
    "The app only accepts connections from this computer; it is not reachable from the network.",
    "OpenAI requests are sent with storage turned off. How each AI service handles data depends on your account terms "
    "with Anthropic or OpenAI.",
    "Every run keeps an AI activity report and a word-for-word log of what was sent, which you can download.",
    "The built-in demo uses fictional data and a recording, so it sends nothing to any AI service.",
]

NOT_CLAIMED = ("This app does not certify compliance with any law or standard. Review what you upload and the "
               "activity report for each run, and follow your organization's own data policies.")


def to_sheet_docx() -> bytes:
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Inches(0.7)
    sec.top_margin = sec.bottom_margin = Inches(0.6)
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(9.5)
    normal.paragraph_format.space_after = Pt(2)
    muted = RGBColor(0x59, 0x59, 0x59)

    h = doc.add_heading(TITLE, level=1)
    h.runs[0].font.size = Pt(15)
    p = doc.add_paragraph(f"Version {__version__}. A free, local app that fills in grant report tables from your own "
                          "data. It runs on your computer; you pay your AI service directly for its use.")
    p.runs[0].font.color.rgb = muted

    def heading(text):
        x = doc.add_heading(text, level=2)
        x.runs[0].font.size = Pt(11.5)
        x.paragraph_format.space_before = Pt(8)
        x.paragraph_format.space_after = Pt(2)

    def bullets(items, style="List Bullet"):
        for item in items:
            para = doc.add_paragraph(style=style)
            if isinstance(item, tuple):
                para.add_run(item[0] + ": ").bold = True
                para.add_run(item[1])
            else:
                para.add_run(item)
            para.paragraph_format.space_after = Pt(1)

    heading("How it works")
    bullets(STEPS, "List Number")
    heading("What stays on this computer")
    bullets(STAYS)
    heading("What is sent to the AI service (Anthropic or OpenAI, your choice)")
    bullets(SENT)
    heading("Safeguards")
    bullets(SAFEGUARDS)
    note = doc.add_paragraph(NOT_CLAIMED)
    note.paragraph_format.space_before = Pt(8)
    note.runs[0].italic = True
    note.runs[0].font.color.rgb = muted

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
