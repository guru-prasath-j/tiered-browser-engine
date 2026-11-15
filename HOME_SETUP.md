# Browser Helper: setup guide

Browser Helper is a chat page on your own computer. You type a task in plain words, for example
"compare the price of the Redmi Note 14 on Amazon and Flipkart", and it does the task in a browser
window you can watch, then replies in the chat.

- It runs only on your computer. Nobody else can use it.
- It never types your passwords, OTPs or card numbers. You type those yourself in the browser window.
- It always asks "Shall I go ahead?" before it submits, books, sends or orders anything.

## What you need

1. **Python 3.10 or newer**: install from https://www.python.org/downloads/
   (on Windows, tick **"Add python.exe to PATH"** during install).
2. **A free Google Gemini key**: open https://aistudio.google.com/apikey, sign in with Google, click
   **Create API key**, and copy it. The setup asks you to paste it once.

## Install (one time, about 5 minutes)

1. Download this project: on https://github.com/guru-prasath-j/tiered-browser-engine click the green
   **Code** button, then **Download ZIP**, and unzip it (for example into your Documents folder).
2. Run the installer inside the unzipped folder:
   - **Mac:** double-click **`Install (Mac).command`**. If macOS says it can't be opened, right-click it,
     choose **Open**, then **Open** again. Or open Terminal in the folder and run `bash install.sh`.
   - **Windows:** right-click **`install.ps1`** and choose **Run with PowerShell**.
3. When asked, paste your Gemini key and press Enter (the key stays hidden while you paste).

## Use it

- Double-click **Browser Helper** on your Desktop. A small window opens (keep it open), and a chat page
  opens in your browser.
- Type what you want and press Enter. A separate browser window does the work, and the answer appears in the chat.
- **Sites where you have an account:** when it asks, sign in yourself in that browser window, then type
  `done` in the chat. You stay signed in next time.
- **"I'm not a robot" checks:** solve them in the browser window; it waits for you.
- To see what happened step by step (and where something failed), click **Logs** at the top of the chat page.
  Logs are deleted automatically after 24 hours.
- To stop, close the small window (or press Ctrl+C in it).

## If something goes wrong

| What you see | What to do |
|---|---|
| "Your Gemini key looks wrong" | Run the installer again, or run `.venv/bin/browser-engine-home --set-key` in the folder (Windows: `.venv\Scripts\browser-engine-home --set-key`) |
| "The free Gemini limit was hit" | Wait a minute and try again |
| "Could not reach the helper" | The small window was closed: double-click Browser Helper on the Desktop again |
| Chat page doesn't open | Open http://127.0.0.1:8770 in your browser |

## Recommended: free local models (both layers)

The helper reads websites in two layers:

1. **Reads the page** (fast): remembered steps, button labels, and a small local model.
2. **Looks at a screenshot** (when a site can't be read normally): a local "eyes" model.

Both run on your own computer, for free. Install [Ollama](https://ollama.com), then run once:

```
ollama pull qwen2.5:7b
ollama pull qwen2.5vl:7b
```

With the "eyes" model installed, the helper works in **careful mode**: before every step it looks
at the screen, closes popups (only with buttons like "Maybe later" or "No thanks", never "Accept"),
clicks the right button even when two look alike, and double-checks prices it reads against what's
on screen. It's slower the first time on a new website, but it remembers what it saw, so repeating a
task is fast.

(The installer offers to do this for you if Ollama is already installed. It's about 11 GB, and a computer
with 16 GB of memory or more works best.) Without them the helper still works, using Gemini for its
thinking, but it can't look at screenshots. The black window shows which layers are on when it starts,
and the chat page shows "screenshots: on" or "off" at the top.
