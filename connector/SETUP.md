# Use Grant Reporting Assistant in the Claude app (no API key)

This is a second way to use Grant Reporting Assistant (GRA). Instead of the app's own screens and an API key, you chat with Claude in the Claude desktop app, signed in with your normal Claude account, and the work counts toward your plan's usual usage limits. GRA runs on your computer as a Claude **extension** and does the counting there, so client records stay on your computer, just as in the app.

The app with an API key still works as before. Use whichever suits you.

## What you need

- The **Claude desktop app** for Windows or Mac, signed in with your Claude account. Get it at [claude.ai/download](https://claude.ai/download). (The Claude website and phone app can't run extensions.)
- **Python 3.11 or newer**. If you already use the GRA desktop app, you have it. Otherwise:
  - **Windows:** download it from [python.org/downloads](https://www.python.org/downloads/), run the installer, and on its first screen tick **Add python.exe to PATH** before you click **Install Now**.
  - **Mac:** download it from [python.org/downloads/macos](https://www.python.org/downloads/macos/) and open the installer.
- The file **grant-reporting-assistant-&lt;version&gt;.mcpb** from the [Releases page](https://github.com/nzachary22/Claude-GRA/releases).

## Install it (about two minutes, once)

1. Open the Claude app.
2. Double-click the **.mcpb** file you downloaded. Claude shows the Grant Reporting Assistant extension with an **Install** button. Click **Install**.
   - If double-clicking doesn't open Claude: in Claude, open **Settings** (click your name, bottom left, then **Settings**), click **Extensions**, then **Advanced settings**, then **Install Extension...**, and choose the .mcpb file.
3. Claude asks for two settings. The suggested ones usually work, so you can click **Save**:
   - **Folders GRA may read:** GRA only opens reports and data files inside these folders. Your home folder is filled in. If your files are somewhere else, such as a Google Drive or shared drive (for example `G:\My Drive\...`), click **Add** and choose that folder too.
   - **Save finished reports in:** your Documents folder is filled in. GRA makes a **Grant Reporting Assistant** folder there.
4. Make sure the switch next to Grant Reporting Assistant is **on** (Enabled).

The first time GRA starts, it sets up Word support in the background (a one-time download of about 5 MB). If the first report says Word support is still being set up, wait a minute and ask again.

### Recommended: keep your chats out of model training

Your chats with GRA contain report tables (totals, never client records). If your Claude account is a personal Free, Pro or Max account, open **Settings**, then **Privacy**, and turn off **Help improve Claude**, so these chats are not used to train models. Team and Enterprise accounts are not used for training.

## Try it on the fictional sample

1. Start a **new chat** in the Claude app.
2. Type: **Run the Grant Reporting Assistant sample report.**
3. Claude asks permission the first time it uses each GRA tool. Click **Always allow** (or **Allow**).
4. In a few minutes Claude puts the finished tables of a made-up legal aid society's report in a **Claude doc** and a **Claude sheet**, gives you their links, and tells you where the Word and Excel files were saved on your computer.

## Prepare your own report

1. Put your past report (or the grant requirements: PDF, Word, Excel or text) and your data files (CSV, Excel or JSON exports) in one folder of their own. Claude sees the names of the files in that folder, so keep other files, such as documents named after clients, elsewhere.
2. Copy the folder's location:
   - **Windows:** open the folder in File Explorer, click the address bar at the top, and press **Ctrl+C**.
   - **Mac:** in Finder, right-click the folder, hold the **Option** key, and click **Copy "..." as Pathname**.
3. Start a **new chat** and type something like:

   > Use Grant Reporting Assistant to rebuild the tables in my 2025 report for January 1 to December 31, 2026. The report and data files are in *(paste the folder location)*.

4. Claude lists the files, starts the report, runs GRA's privacy-checked counts and puts the finished tables in a **Claude doc** and a **Claude sheet** (both hold only the finished tables and notes, which are totals, never client records). If Claude docs and sheets aren't available in your Claude app, Claude shows the tables in the chat instead. The Word and Excel files, an **AI activity report** (what Claude was shown) and, when something was missing, a **data collection guide** are saved in **Documents > Grant Reporting Assistant**, in a folder named after the report.
5. If Claude lists **Some considerations to check**, answer them in the chat, or ask for any other change ("count only closed cases", "use the intake date"). GRA recalculates, Claude updates the same doc and sheet, and GRA saves the files again.

To check first what GRA keeps private and what it shares, ask: **Use Grant Reporting Assistant to preview my data files in *(folder)*.**

## Keeping client data private

- **Never attach your data files to the chat** or paste records into it. Give Claude only the folder location; GRA reads the files on your computer and shows Claude only totals.
- GRA has no tool that shows Claude a data file's contents; only totals from its privacy-checked counts come back. Other extensions are different: if you also turn on a file-reading extension (such as Filesystem) or computer use for that folder, Claude could open the files that way. GRA's rules tell Claude not to, but to be sure, keep such extensions off while you work with client data (Settings > Extensions).
- You can see everything GRA gave Claude: click any GRA step in the chat to see its result, and open the **AI activity report** saved with the tables.
- Attaching your past report itself is fine if it is a scan that GRA can't read; it holds no client records.
- GRA checks the report for client names and IDs from your data before Claude sees it, and stops if it finds one.

## If something goes wrong

- **Claude says the extension failed to start, or GRA's tools don't appear:** Python is probably missing or too old. Install Python 3.11 or newer (see above; on Windows tick **Add python.exe to PATH**), then quit the Claude app completely and open it again.
- **"outside the folders GRA may read":** open **Settings > Extensions > Grant Reporting Assistant > Configure**, add the folder under **Folders GRA may read**, and click **Save**.
- **"Word support is still being set up":** wait a minute and ask Claude to try again. If it keeps happening, check the internet connection and restart the Claude app.
- **To update GRA:** download the new .mcpb file and install it the same way. Your settings are kept.
- **To remove GRA:** **Settings > Extensions > Grant Reporting Assistant > Uninstall**.
