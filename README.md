# bpq-wx

On-demand weather reports by US zip code for BPQ32 packet radio nodes.

Users connect to your node, type `WX`, enter their zip code once, and get an
NWS forecast right in their session. They can also download the report as a
text file — or a zip-area GFS GRIB file for viewing in XyGrib — **directly in
the session via YAPP**, or from your BBS Files area.

```
WX REPORT  Newington CT 06111
Issued: 08-09-2026 09:20 PM  Source: NWS Hourly

0-24 HOURS
WX: Mostly Clear; Mostly Sunny; Sunny
TEMP: 68-84 F
WIND: NW 6-10 mph
RAIN: Little or no rain expected.

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

## Install

1. Clone this repo somewhere on the node PC.
2. Run it once to generate `wx_config.json`:
   ```
   python bpq_wx.py
   ```
3. Edit `wx_config.json`:
   - `output_dir` — point at your BBS files folder
     (e.g. `C:\Users\<you>\AppData\Roaming\BPQ32\BPQMailChat\Files`)
   - `banner` — the station banner printed at the bottom of saved reports
   - `listen_port` — must match the CMDPORT entry you add below (default 63051)
4. Add the app to `BPQ32.cfg`:

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
5. Restart BPQ32, start the app (a startup batch file or a Windows Terminal
   tab works well), and type `WX` at your node prompt.

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
