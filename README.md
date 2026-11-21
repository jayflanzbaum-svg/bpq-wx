# bpq-wx

On-demand weather reports by US zip code for BPQ32 packet radio nodes.

Users connect to your node, type `WX`, enter their zip code once, and get an
NWS forecast right in their session. They can also download the report as a
text file — or a zip-area GFS GRIB file for viewing in XyGrib — **directly in
the session via YAPP**, or from your BBS Files area.

```
WX REPORT  Newington CT 06111
Issued: 08-09-2026 09:20 PM  Source: NWS Hourly

Next 24 Hrs
  WX:Mostly Clear; Mostly Sunny; Sunny
  TEMP:68-84 F
  WIND:NW 6-10 mph
  RAIN:Little or no rain expected.

<ENTER> | ZIP <#####> | 48HR | ALERTS | DL | GRIB | DL GRIB | FILE | STATUS | HELP | QUIT
```

## Features

- **Per-user memory** — each callsign's zip code is remembered between connects
- **24hr / 48hr forecasts** — wind, temperature, and rain summaries from the
  NWS hourly forecast API
- **ALERTS** — active NWS warnings/watches for the user's area
- **DL** — sends the report as a `.txt` straight to the user's terminal via
  YAPP (plain YAPP and checksummed YappC both supported; QtTermTCP, EasyTerm,
  and most packet terminals work)
- **GRIB / DL GRIB** — builds a gzip-compressed GFS GRIB bundle (wind, rain,
  pressure; 24 forecast hours) centered on the user's zip code, in a
  background thread, then YAPP-sends it on request
- **FILE** — alternatively drops files in your BBS Files area for YAPP
  download through the BBS
- Files self-prune after 24 hours; forecasts are cached briefly so RF users
  get instant refreshes
- RF-friendly output: plain ASCII, CRLF, short lines

## Requirements

- Python 3.10+
- `pip install requests`
- A BPQ32 node with a Telnet port
- US-only: forecasts come from the US National Weather Service

## Install (Windows, no Python experience needed)

You do not need to know how to program to run this - just follow these steps
on the BPQ32 node PC.

1. **Install Python.** Go to
   [python.org/downloads](https://www.python.org/downloads/) and download the
   latest Python 3 installer. Run it, and on the very first screen **check
   the box at the bottom that says "Add python.exe to PATH"** before clicking
   Install - this is the step everyone misses, and without it Windows won't
   know what `python` means.
2. **Download this app.** On this page, click the green **Code** button →
   **Download ZIP**. Unzip it somewhere easy to find, e.g. `C:\bpq-wx`.
   (If you're comfortable with git, `git clone` works too and makes updates a
   one-line `git pull` later.)
3. **Open a terminal in that folder.** In File Explorer, open the `bpq-wx`
   folder, click in the empty address bar at the top, type `cmd`, and press
   Enter. A black command-prompt window opens already sitting in that folder.
4. **Install the one dependency this app needs:**
   ```
   pip install requests
   ```
5. **Run it once** to generate `wx_config.json`:
   ```
   python bpq_wx.py
   ```
   Leave this window open for now - you'll see status messages print here
   whenever the app is running, which is useful for troubleshooting. Press
   `Ctrl+C` to stop it before continuing.
6. **Edit `wx_config.json`** (right-click it → Open with → Notepad):
   - `output_dir` — point at your BBS files folder. **A Windows path needs
     its backslashes doubled**, because in JSON a single backslash starts an
     escape code:
     ```json
     "output_dir": "C:\\Users\\<you>\\AppData\\Roaming\\BPQ32\\BPQMailChat\\Files"
     ```
     Forward slashes work too, and are easier to get right —
     `"C:/Users/<you>/AppData/Roaming/BPQ32/BPQMailChat/Files"`.
   - `banner` — the station banner printed at the bottom of saved reports
   - `listen_port` — must match the CMDPORT entry you add below (default 63051)
7. Add the app to `BPQ32.cfg`:

   In your **Telnet port** `CONFIG` block, add the listen port to `CMDPORT`
   (space-separated list; note its zero-based position — that's the HOST
   number):
   ```
   CMDPORT=63051
   ```
   In the **APPLICATIONS** section (adjust the application number, HOST
   index, and your callsign/alias):
   ```
   APPLICATION 6,WX,C 7 HOST 0 S TRANS,MYCALL-15,NODEWX,255
   ```
   `C 7` is your Telnet port number; `HOST 0` is the CMDPORT position;
   `S` returns the user to the node prompt when the app exits; **`TRANS`
   is required for the in-session YAPP downloads** — it puts the connection
   in binary (FBB) mode so the file transfer bytes pass through untouched
   (BPQ32 6.0.20.1 or later). BPQ sends the connecting user's callsign to
   the app automatically.
8. Restart BPQ32, then start the app the same way as step 5 - open a command
   prompt in the `bpq-wx` folder and run `python bpq_wx.py`. Leave that window
   open; closing it stops the app. (Once this is working, you can put that
   same command in a `.bat` file and double-click it instead of retyping it,
   or add it to Windows startup.) Connect to your node and type `WX`.

## Troubleshooting

- **A wall of red `Traceback` text ending in `json.decoder.JSONDecodeError`** —
  `wx_config.json` has a typo. The app now prints the offending line with a
  `^` under the spot instead of a traceback, e.g.:
  ```
  [CFG] wx_config.json is not valid JSON: Invalid \escape
  [CFG] line 10:   "output_dir": "C:\BPQ-WX\files",
  [CFG]                             ^
  ```
  `Invalid \escape` is almost always a Windows path written with single
  backslashes. Double them (`"C:\\BPQ-WX\\files"`) or use forward slashes
  (`"C:/BPQ-WX/files"`). Watch for this one even when the file *does* load:
  `"C:\temp\files"` is legal JSON — `\t` and `\f` are real escape codes — and
  silently becomes a path with a tab and a formfeed in it. The app checks
  `output_dir` for that at startup and stops with the same advice.

- **FILE (or GRIB) says it saved, but you can't find the file** — `output_dir`
  in `wx_config.json` almost certainly isn't pointing at your actual BPQ32
  Files folder. On every startup the app prints the exact folder it's writing
  to, e.g.:
  ```
  [CFG] Files (FILE/GRIB) will be written to: C:\bpq-wx\files
  [CFG] This must match your BBS Files folder (see output_dir in wx_config.json).
  ```
  Compare that path against your BBS Files folder (in BPQMailChat this is
  usually `%APPDATA%\BPQ32\BPQMailChat\Files`). If they don't match, edit
  `output_dir` in `wx_config.json` and restart the app.

## Zip code database

`us_zips.csv` is derived from the [GeoNames postal code data](https://download.geonames.org/export/zip/)
(US.zip), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
41k+ zip codes resolve offline — no geocoding API needed.

## Data sources

- Forecasts and alerts: [NWS API](https://www.weather.gov/documentation/services-web-api) (api.weather.gov)
- GRIB: [NOAA NOMADS](https://nomads.ncep.noaa.gov/) GFS 0.25-degree filter
- GRIB viewer for users: [XyGrib](https://opengribs.org/en/downloads)

## License

MIT — see [LICENSE](LICENSE).
