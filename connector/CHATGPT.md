# Use Grant Reporting Assistant in the ChatGPT app (no API key)

This is a third way to use Grant Reporting Assistant (GRA). You chat with ChatGPT in the ChatGPT desktop app, signed in with your normal ChatGPT account, and the work counts toward your plan's usual usage limits. GRA runs on your computer and does the counting there, so client records stay on your computer, just as in the browser version and the Claude app.

The ChatGPT app can run commands on your computer by itself, so GRA uses a **private data folder**: **your user folder > GRA Private Data**. The installer sets ChatGPT's own settings so that ChatGPT's commands are blocked from that folder, and GRA reads reports and data files only from there. If the block is ever missing, GRA stops before reading anything.

## What you need

- The **ChatGPT desktop app** for Windows or Mac, signed in with your ChatGPT account. Get it at [chatgpt.com/download](https://chatgpt.com/download). (The ChatGPT website and phone app can't run GRA, because GRA runs on your computer.)
- **Python 3.11 or newer**. If you already use the GRA desktop app or the Claude app extension, you have it. Otherwise:
  - **Windows:** download it from [python.org/downloads](https://www.python.org/downloads/), run the installer, and on its first screen tick **Add python.exe to PATH** before you click **Install Now**.
  - **Mac:** download it from [python.org/downloads/macos](https://www.python.org/downloads/macos/) and open the installer.
- The file **grant-reporting-assistant-chatgpt-&lt;version&gt;.zip** from the [Releases page](https://github.com/nzachary22/Claude-GRA/releases).

## Install it (about two minutes, once)

1. Quit the ChatGPT app if it is open.
2. Unzip the download:
   - **Windows:** right-click the zip file, click **Extract All...**, then **Extract**. A folder named **Grant Reporting Assistant for ChatGPT** opens.
   - **Mac:** double-click the zip file. A folder named **Grant Reporting Assistant for ChatGPT** appears next to it.
3. Run the installer:
   - **Windows:** double-click **Install on Windows.bat**.
     - If Windows says **Windows protected your PC**, click **More info**, then **Run anyway**.
     - If it asks **Do you want to run this file?**, click **Run**.
   - **Mac:** right-click (or Control-click) **Install on Mac.command**, click **Open**, then click **Open** again.
     - If the Mac says it can't be opened, open **System Settings**, click **Privacy & Security**, scroll down, click **Open Anyway** next to the message about Install on Mac.command, and click **Open**.
4. A window shows the installer's progress. It copies GRA to your computer and sets up Word support (a one-time download of about 5 MB).
5. A box shows GRA's private data folder (**GRA Private Data** in your user folder: on Windows `C:\Users\<your name>`, on a Mac your home folder; not in Documents, which Windows often syncs to OneDrive) and asks whether you also keep report or data files in another folder that should be private:
   - Usually, click **No**, and move your files into **GRA Private Data** later.
   - To keep files where they are, click **Yes**, choose the folder that holds them (not your whole home folder or drive), and click **Select Folder** (Mac: **Open**). That folder becomes private too: ChatGPT itself is blocked from it, so don't choose a folder you want ChatGPT to use for other work. It asks again, so you can add more folders. Click **No** when you are done.
6. A box says **Grant Reporting Assistant is installed for the ChatGPT app** and names the private folder. Click **OK**, then close the installer window.
7. Open the ChatGPT app again.

To check that ChatGPT sees GRA: in the ChatGPT app, open **Settings**, then **MCP servers**. **grant_reporting_assistant** is listed and switched on.

### Recommended: keep your chats out of model training

Your chats with GRA contain report tables (totals, never client records). If your ChatGPT account is a personal Free, Plus or Pro account, open **Settings**, then **Data controls**, and turn off **Improve the model for everyone**, so these chats are not used to train models. Business, Enterprise and Edu workspaces are not used for training by default.

## Try it on the fictional sample

1. Start a **new chat** in the ChatGPT app.
2. Type: **Run the Grant Reporting Assistant sample report.**
3. In a few minutes ChatGPT shows the finished tables of a made-up legal aid society's report in the chat, and tells you where the Word and Excel files were saved: **Documents > Grant Reporting Assistant**, in a folder named after the report.
4. If ChatGPT says it can't find Grant Reporting Assistant, switch the toggle above the message box from **Chat** to **Work** (or type **/mcp** to see the tools ChatGPT can use), then ask again.

## Prepare your own report

1. Put your past report (or the grant requirements: PDF, Word, Excel or text) and your data files (CSV, Excel or JSON exports) in **your user folder > GRA Private Data** (or another private folder you added). You can make a subfolder for each report. ChatGPT sees the names of the files there, so keep other files, such as documents named after clients, elsewhere.
2. Start a **new chat** and type something like:

   > Use Grant Reporting Assistant to rebuild the tables in my 2025 report for January 1 to December 31, 2026. The files are in GRA Private Data.

3. If you keep several reports there, name the subfolder, for example: *The files are in GRA Private Data, in the folder "2026 county grant".*

4. ChatGPT lists the files, starts the report, runs GRA's privacy-checked counts and shows the finished tables in the chat. The Word and Excel files, an **AI activity report** (what ChatGPT was shown) and, when something was missing, a **data collection guide** are saved in **Documents > Grant Reporting Assistant**, in a folder named after the report.
5. If ChatGPT lists **Some considerations to check**, answer them in the chat, or ask for any other change ("count only closed cases", "use the intake date"). GRA recalculates, saves the files again, and ChatGPT shows the tables that changed.

To check first what GRA keeps private and what it shares, ask: **Use Grant Reporting Assistant to preview my data files in *(folder)*.**

## Keeping client data private

- **Never attach your data files to the chat** or paste records into it. Give ChatGPT only the folder location; GRA reads the files on your computer and shows ChatGPT only totals.
- GRA has no tool that shows ChatGPT a data file's contents; only totals from its privacy-checked counts come back.
- **ChatGPT itself is blocked from your private folders.** The ChatGPT app can run commands on your computer by itself. The installer sets ChatGPT's permissions so that its commands can't open, list or copy anything in GRA's private data folders or in GRA's working files (the protected database it imports the data into), and so that ChatGPT can't ask you to let a command around that block. GRA's own tools still read the files, because they run separately from ChatGPT's commands.
- **GRA checks the block every time.** Before it reads any file, GRA checks ChatGPT's settings. If the block is missing or weakened, GRA stops, reads nothing, and asks you to run the installer again. In ChatGPT, GRA also reads only files inside the private folders.
- **Leave ChatGPT's permissions on the default.** If you switch a chat to **Full access** in the ChatGPT app, ChatGPT's commands are no longer sandboxed and the block can't hold. Keep chats about client data on the default setting.
- You can see everything GRA gave ChatGPT: open the **AI activity report** saved with the tables.
- GRA checks the report for client names and IDs from your data before ChatGPT sees it, and stops if it finds one.

## If something goes wrong

- **The installer says Python was not found:** install Python 3.11 or newer (see above; on Windows tick **Add python.exe to PATH**), then run the installer again.
- **GRA isn't listed under Settings > MCP servers, or ChatGPT can't find it:** quit the ChatGPT app completely (on Windows, also right-click its icon near the clock and quit it there, if it is shown) and open it again. If it is listed but switched off, switch it on.
- **"outside the folders GRA may read" or "private data folders":** move the files into **your user folder > GRA Private Data**, or run the installer again and add their folder as a private folder (folders you added before are kept). Then quit and reopen the ChatGPT app.
- **"GRA stopped: ... ChatGPT itself could open the data files":** ChatGPT's block is missing, for example because its settings were changed. Run the installer again, then quit and reopen the ChatGPT app.
- **"Word support is still being set up":** check the internet connection, run the installer again, and reopen the ChatGPT app.
- **To update GRA:** quit the ChatGPT app, download the new zip, and run its installer the same way. Your folders are kept.
- **To remove GRA:** quit the ChatGPT app, open the **Uninstall** folder inside **Grant Reporting Assistant for ChatGPT**, and double-click **Uninstall on Windows.bat** (Mac: right-click **Uninstall on Mac.command**, then **Open**). Your saved reports are kept.

## What the installer changes

- It copies GRA into your computer's app data folder (Windows: `AppData\Local\GrantReportingAssistant\ChatGPT connector`; Mac: `Library/Application Support/GrantReportingAssistant/ChatGPT connector`).
- It makes **your user folder > GRA Private Data**, and a folder for GRA's working files (`.grant-reporting-assistant` in your home folder).
- In the ChatGPT app's settings file (`.codex/config.toml` in your home folder) it adds GRA (`[mcp_servers.grant_reporting_assistant]`), a permissions profile (`[permissions.gra-protected]`, ChatGPT's usual access with GRA's private folders and working files set to **deny**), and makes that profile the default. It sets ChatGPT's approval policy to turn down requests to run commands outside its sandbox or to ask for more permissions; other approval prompts are unchanged. GRA's tools run without asking each time, because they only work on your computer behind GRA's privacy checks.
- Anything else in the file is kept. A setting GRA replaces (such as an earlier approval policy) is kept as a comment and put back when you remove GRA, and the previous file is kept as `config.toml.gra-backup`.
