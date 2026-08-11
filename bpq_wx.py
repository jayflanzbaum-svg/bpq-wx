#!/usr/bin/env python3
"""
BPQ "WX" application (RF-first)

On-demand weather reports by US zip code, in the style of SPOTS/OPENAI:
- BPQ connects via Telnet port CMDPORT (HOST 2 -> 127.0.0.1:63051)
- BPQ sends the user's callsign as the first line (APPLICATION ... S flag)
- Per-callsign zip code remembered in wx_users.json
- Text forecast comes from the NWS API (same source as daily_grib.py)
- FILE writes the report as a .txt into the BBS Files folder (YAPP download)
- GRIB builds a zip-centered GFS GRIB bundle in the background thread and
  drops it in the BBS Files folder; STATUS reports progress
- WX_* files older than FILE_MAX_AGE_HOURS are pruned automatically

Commands:
  (Enter) / WX   -> refresh 24hr report
  ZIP <#####>    -> set zip code for this user
  48HR           -> 48 hour outlook
  ALERTS         -> active NWS alerts for your area
  FILE           -> write report to BBS Files folder for YAPP download
  GRIB           -> build zip-area GRIB file (background, ~1 minute)
  STATUS         -> check GRIB build progress
  HELP           -> show commands
  Q / BYE / EXIT / QUIT / NODE -> exit
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# -----------------------------
# Defaults / Config
# -----------------------------

DEFAULT_CONFIG = {
    "bpq_app": {"listen_host": "127.0.0.1", "listen_port": 63051},
    "storage": {"user_db_file": "wx_users.json", "zip_db_file": "us_zips.csv"},
    "output_dir": "./files",
    "banner": "BPQ WX SERVICE",
    "file_max_age_hours": 24,
    "forecast_cache_minutes": 10,
    "grib": {
        "forecast_hours": 24,
        "box_lat_half_deg": 1.0,
        "box_lon_half_deg": 1.25,
        "max_concurrent_jobs": 2,
    },
}

CONFIG_FILE = "wx_config.json"

NWS_BASE_URL = "https://api.weather.gov"
GRIB_BASE_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
XYGRIB_URL = "https://opengribs.org/en/downloads"
CYCLE_HOURS = [18, 12, 6, 0]
TIMEOUT = 60

GRIB_VARIABLES = {
    "var_UGRD": "on",
    "var_VGRD": "on",
    "var_APCP": "on",
    "var_PRATE": "on",
    "var_PRMSL": "on",
}

GRIB_LEVELS = {
    "lev_10_m_above_ground": "on",
    "lev_surface": "on",
    "lev_mean_sea_level": "on",
}

MENU_LINE = "<ENTER> | ZIP <#####> | 48HR | ALERTS | DL | GRIB | DL GRIB | FILE | STATUS | HELP | QUIT\r\n"

HELP_TEXT = (
    "Commands:\r\n"
    "  (Enter) / WX   -> refresh 24hr report\r\n"
    "  ZIP <#####>    -> set your zip code (e.g. ZIP 33445)\r\n"
    "  48HR           -> 48 hour outlook\r\n"
    "  ALERTS         -> active NWS alerts for your area\r\n"
    "  DL             -> download report .txt right now via YAPP\r\n"
    "  GRIB           -> build GRIB file for your area (takes ~1 min)\r\n"
    "  DL GRIB        -> download the built GRIB file via YAPP\r\n"
    "  FILE           -> save report as .txt on the BBS instead\r\n"
    "  STATUS         -> check GRIB build progress\r\n"
    "  HELP           -> show commands\r\n"
    "  Q / BYE / EXIT / NODE -> exit\r\n"
)


def nws_headers() -> dict:
    return {
        "User-Agent": "BPQ-WX-APP/1.0",
        "Accept": "application/geo+json, application/json",
    }


# -----------------------------
# Output sanitization (BBS-safe, ASCII + CRLF)
# -----------------------------

def sanitize_for_bbs(s: str) -> str:
    """Normalize to plain ASCII with CR line endings (BPQ native convention).
    The app runs on a TRANS (binary) HOST connection, so the node passes our
    bytes through unmodified - we must emit CR, not CRLF, ourselves."""
    if not s:
        return s
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = (s.replace("\u201c", '"').replace("\u201d", '"')
           .replace("\u2018", "'").replace("\u2019", "'")
           .replace("\u2014", "-").replace("\u2013", "-")
           .replace("\u2026", "..."))
    s = re.sub(r"[^\x09\x0a\x20-\x7e]", "", s)
    return s.replace("\n", "\r")


# -----------------------------
# Zip code database (GeoNames extract: zip,lat,lon,place,state)
# -----------------------------

class ZipDB:
    def __init__(self, path: str):
        self.data: Dict[str, Tuple[float, float, str, str]] = {}
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Zip database not found: {p}")
        for line in p.read_text(encoding="utf-8").splitlines():
            parts = line.split(",")
            if len(parts) < 5:
                continue
            try:
                self.data[parts[0]] = (float(parts[1]), float(parts[2]), parts[3], parts[4])
            except ValueError:
                continue
        print(f"[ZIP] Loaded {len(self.data)} zip codes.")

    def lookup(self, zipcode: str) -> Optional[Tuple[float, float, str, str]]:
        z = (zipcode or "").strip()
        if not re.fullmatch(r"\d{5}", z):
            return None
        return self.data.get(z)


# -----------------------------
# User DB (zip only)
# -----------------------------

class UserDB:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data: Dict[str, Dict] = {}
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get_zip(self, callsign: str) -> Optional[str]:
        cs = (callsign or "").strip().upper()
        return (self.data.get(cs, {}) or {}).get("zip")

    def set_zip(self, callsign: str, zipcode: str) -> None:
        cs = (callsign or "").strip().upper()
        self.data.setdefault(cs, {})["zip"] = zipcode
        self.save()


# -----------------------------
# NWS forecast fetch + summaries (adapted from daily_grib.py)
# -----------------------------

def parse_wind_speed_mph(text: str) -> Tuple[Optional[int], Optional[int]]:
    nums = [int(n) for n in re.findall(r"\d+", text or "")]
    if not nums:
        return None, None
    if len(nums) == 1:
        return nums[0], nums[0]
    return min(nums), max(nums)


def extract_pop(period: dict) -> Optional[int]:
    val = (period.get("probabilityOfPrecipitation") or {}).get("value")
    if val is None:
        return None
    try:
        return int(val)
    except Exception:
        return None


def common_phrases(periods: List[dict], limit: int = 3) -> List[str]:
    terms = [(p.get("shortForecast") or "").strip() for p in periods]
    terms = [t for t in terms if t]
    if not terms:
        return ["Forecast wording unavailable"]
    return [phrase for phrase, _ in Counter(terms).most_common(limit)]


def summarize_period_block(periods: List[dict]) -> Dict[str, str]:
    wind_lows: List[int] = []
    wind_highs: List[int] = []
    wind_dirs: List[str] = []
    pops: List[int] = []
    temps: List[int] = []

    for p in periods:
        lo, hi = parse_wind_speed_mph(p.get("windSpeed", ""))
        if lo is not None:
            wind_lows.append(lo)
        if hi is not None:
            wind_highs.append(hi)
        wd = (p.get("windDirection") or "").strip()
        if wd:
            wind_dirs.append(wd)
        pop = extract_pop(p)
        if pop is not None:
            pops.append(pop)
        t = p.get("temperature")
        if isinstance(t, (int, float)):
            temps.append(int(t))

    dir_mode = Counter(wind_dirs).most_common(1)[0][0] if wind_dirs else "Variable"
    wind_text = "Wind data unavailable."
    if wind_lows and wind_highs:
        wind_text = f"{dir_mode} {min(wind_lows)}-{max(wind_highs)} mph"

    temp_text = "Temp data unavailable."
    if temps:
        temp_text = f"{min(temps)}-{max(temps)} F"

    pop_max = max(pops) if pops else None
    if pop_max is None:
        rain_text = "Rain chance unavailable."
    elif pop_max < 20:
        rain_text = "Little or no rain expected."
    elif pop_max < 40:
        rain_text = f"Isolated showers possible. Rain chance up to {pop_max}%."
    elif pop_max < 60:
        rain_text = f"Scattered showers possible. Rain chance up to {pop_max}%."
    elif pop_max < 80:
        rain_text = f"Numerous showers likely. Rain chance up to {pop_max}%."
    else:
        rain_text = f"Rain likely at times. Rain chance up to {pop_max}%."

    return {
        "wx": "; ".join(common_phrases(periods)),
        "temp": temp_text,
        "wind": wind_text,
        "rain": rain_text,
    }


class ForecastCache:
    """Per-zip hourly-periods cache so <ENTER> refreshes are instant and
    the NWS API is not hammered from RF sessions."""

    def __init__(self, ttl_minutes: int):
        self.ttl = ttl_minutes * 60
        self.lock = threading.Lock()
        self.data: Dict[str, Tuple[float, List[dict]]] = {}

    def get(self, zipcode: str) -> Optional[List[dict]]:
        with self.lock:
            hit = self.data.get(zipcode)
            if hit and (time.time() - hit[0]) < self.ttl:
                return hit[1]
        return None

    def put(self, zipcode: str, periods: List[dict]) -> None:
        with self.lock:
            self.data[zipcode] = (time.time(), periods)
            if len(self.data) > 500:
                oldest = sorted(self.data.items(), key=lambda kv: kv[1][0])[:100]
                for k, _ in oldest:
                    self.data.pop(k, None)


def fetch_hourly_periods(lat: float, lon: float) -> List[dict]:
    with requests.Session() as s:
        r = s.get(f"{NWS_BASE_URL}/points/{lat},{lon}", headers=nws_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        hourly_url = r.json()["properties"]["forecastHourly"]
        r = s.get(hourly_url, headers=nws_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()["properties"]["periods"]


def fetch_alerts(lat: float, lon: float) -> List[dict]:
    r = requests.get(
        f"{NWS_BASE_URL}/alerts/active",
        params={"point": f"{lat},{lon}"},
        headers=nws_headers(),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("features", [])


# -----------------------------
# Report builders
# -----------------------------

def build_report(place: str, state: str, zipcode: str, periods: List[dict], hours48: bool) -> str:
    now = dt.datetime.now()
    lines = [
        f"WX REPORT  {place} {state} {zipcode}",
        f"Issued: {now.strftime('%m-%d-%Y %I:%M %p')}  Source: NWS Hourly",
        "",
    ]

    s1 = summarize_period_block(periods[:24])
    lines += [
        "0-24 HOURS",
        f"WX: {s1['wx']}",
        f"TEMP: {s1['temp']}",
        f"WIND: {s1['wind']}",
        f"RAIN: {s1['rain']}",
    ]

    if hours48:
        s2 = summarize_period_block(periods[24:48])
        lines += [
            "",
            "24-48 HOURS",
            f"WX: {s2['wx']}",
            f"TEMP: {s2['temp']}",
            f"WIND: {s2['wind']}",
            f"RAIN: {s2['rain']}",
        ]

    return "\n".join(lines) + "\n"


def build_alerts_text(place: str, state: str, features: List[dict]) -> str:
    if not features:
        return f"No active NWS alerts for {place} {state}.\n"
    lines = [f"ACTIVE NWS ALERTS - {place} {state} ({len(features)})", ""]
    for f in features[:5]:
        props = f.get("properties", {}) or {}
        event = props.get("event", "Alert")
        severity = props.get("severity", "")
        ends = props.get("ends") or props.get("expires") or ""
        headline = (props.get("headline") or "").strip()
        lines.append(f"* {event} ({severity})")
        if headline:
            lines.append(f"  {headline[:150]}")
        if ends:
            lines.append(f"  Until: {ends[:16].replace('T', ' ')}")
    if len(features) > 5:
        lines.append(f"...and {len(features) - 5} more.")
    return "\n".join(lines) + "\n"


# -----------------------------
# BBS Files output (.txt report + GRIB), with pruning
# -----------------------------

def base_call(callsign: str) -> str:
    cs = (callsign or "UNKNOWN").upper().split("-")[0]
    return re.sub(r"[^A-Z0-9]", "", cs) or "UNKNOWN"


def prune_old_files(output_dir: Path, max_age_hours: int) -> None:
    cutoff = time.time() - max_age_hours * 3600
    for f in output_dir.glob("WX_*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                print(f"[PRUNE] Deleted {f.name}")
        except Exception as e:
            print(f"[PRUNE] Could not delete {f}: {e}")


def write_report_file(output_dir: Path, callsign: str, zipcode: str, report: str,
                      banner: str) -> str:
    date_s = dt.datetime.now().strftime("%m-%d-%Y")
    name = f"WX_{base_call(callsign)}_{zipcode}_{date_s}.txt"
    body = report + (
        f"\n{banner}\n"
        "Generated on demand by the WX app.\n"
    )
    (output_dir / name).write_text(sanitize_for_bbs(body).replace("\r", "\n"), encoding="utf-8")
    return name


# -----------------------------
# GRIB background jobs
# -----------------------------

class GribJob:
    def __init__(self, filename: str):
        self.filename = filename
        self.status = "RUNNING"
        self.hours_done = 0
        self.hours_total = 0
        self.error = ""


class GribManager:
    def __init__(self, output_dir: Path, cfg: Dict):
        self.output_dir = output_dir
        self.cfg = cfg
        self.lock = threading.Lock()
        self.jobs: Dict[str, GribJob] = {}

    def running_count(self) -> int:
        with self.lock:
            return sum(1 for j in self.jobs.values() if j.status == "RUNNING")

    def get(self, callsign: str) -> Optional[GribJob]:
        with self.lock:
            return self.jobs.get(base_call(callsign))

    def start(self, callsign: str, zipcode: str, lat: float, lon: float) -> Tuple[Optional[GribJob], str]:
        cs = base_call(callsign)
        with self.lock:
            existing = self.jobs.get(cs)
            if existing and existing.status == "RUNNING":
                return None, "A GRIB build is already running for you. Type STATUS to check it."
            if sum(1 for j in self.jobs.values() if j.status == "RUNNING") >= int(self.cfg["max_concurrent_jobs"]):
                return None, "GRIB builder is busy. Try again in a few minutes."
            date_s = dt.datetime.now().strftime("%m-%d-%Y")
            job = GribJob(f"WX_{cs}_{zipcode}_{date_s}.grib2.gz")
            job.hours_total = int(self.cfg["forecast_hours"])
            self.jobs[cs] = job
        threading.Thread(target=self._run, args=(job, lat, lon), daemon=True).start()
        return job, ""

    def _cycle_candidates(self, now_utc: dt.datetime) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        for day_offset in [0, 1]:
            day = (now_utc - dt.timedelta(days=day_offset)).date()
            for hh in CYCLE_HOURS:
                cyc = dt.datetime.combine(day, dt.time(hour=hh), tzinfo=dt.timezone.utc)
                if cyc <= now_utc and (day.strftime("%Y%m%d"), f"{hh:02d}") not in out:
                    out.append((day.strftime("%Y%m%d"), f"{hh:02d}"))
        return out

    def _run(self, job: GribJob, lat: float, lon: float) -> None:
        path = self.output_dir / job.filename
        half_lat = float(self.cfg["box_lat_half_deg"])
        half_lon = float(self.cfg["box_lon_half_deg"])
        bbox = {
            "leftlon": f"{lon - half_lon:.2f}",
            "rightlon": f"{lon + half_lon:.2f}",
            "toplat": f"{lat + half_lat:.2f}",
            "bottomlat": f"{lat - half_lat:.2f}",
        }
        try:
            with requests.Session() as session:
                session.headers.update(nws_headers())
                for date_s, cycle_hh in self._cycle_candidates(dt.datetime.now(dt.timezone.utc)):
                    if self._try_cycle(session, job, path, bbox, date_s, cycle_hh):
                        job.status = "DONE"
                        print(f"[GRIB] Done: {job.filename}")
                        return
            job.status = "FAILED"
            job.error = "No GFS cycle produced data."
        except Exception as e:
            job.status = "FAILED"
            job.error = str(e)[:120]
            if path.exists():
                try:
                    path.unlink()
                except Exception:
                    pass
        print(f"[GRIB] {job.status}: {job.filename} {job.error}")

    def _try_cycle(self, session: requests.Session, job: GribJob, path: Path,
                   bbox: Dict[str, str], date_s: str, cycle_hh: str) -> bool:
        success = 0
        job.hours_done = 0
        try:
            with gzip.open(path, "wb", compresslevel=9) as gz:
                for fh in range(1, job.hours_total + 1):
                    params = {
                        "file": f"gfs.t{cycle_hh}z.pgrb2.0p25.f{fh:03d}",
                        "dir": f"/gfs.{date_s}/{cycle_hh}/atmos",
                        "subregion": "",
                        **bbox,
                        **GRIB_VARIABLES,
                        **GRIB_LEVELS,
                    }
                    try:
                        with session.get(GRIB_BASE_URL, params=params, timeout=TIMEOUT, stream=True) as r:
                            if r.status_code == 404:
                                continue
                            r.raise_for_status()
                            if "html" in (r.headers.get("Content-Type") or "").lower():
                                continue
                            got = 0
                            for chunk in r.iter_content(chunk_size=65536):
                                if chunk:
                                    gz.write(chunk)
                                    got += len(chunk)
                            if got:
                                success += 1
                    except Exception as e:
                        print(f"[GRIB] f{fh:03d} failed: {e}")
                    job.hours_done = fh
        except Exception as e:
            print(f"[GRIB] bundle failed: {e}")
            success = 0
        if success == 0 or not path.exists() or path.stat().st_size == 0:
            if path.exists():
                try:
                    path.unlink()
                except Exception:
                    pass
            return False
        return True


# -----------------------------
# YAPP file transfer (sender side)
# -----------------------------
# Protocol per WA7MBL's YAPP spec. Also accepts the YappC (checksummed)
# response so terminals like QtTermTCP can use their preferred mode.

Y_SOH, Y_STX, Y_ETX, Y_EOT = 0x01, 0x02, 0x03, 0x04
Y_ENQ, Y_ACK, Y_NAK, Y_CAN = 0x05, 0x06, 0x15, 0x18

YAPP_BLOCK = 128


class YappAbort(Exception):
    pass


async def _yapp_wait(reader: asyncio.StreamReader, timeout: float) -> Tuple[str, int, bytes]:
    """Wait for a YAPP control frame, skipping stray text bytes (echoes,
    CR/LF) the terminal may emit before its YAPP engine engages."""
    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise asyncio.TimeoutError()
        b = await asyncio.wait_for(reader.readexactly(1), timeout=remain)
        c = b[0]
        if c == Y_ACK:
            b2 = await asyncio.wait_for(reader.readexactly(1), timeout=10)
            return ("ACK", b2[0], b"")
        if c in (Y_NAK, Y_CAN):
            ln = (await asyncio.wait_for(reader.readexactly(1), timeout=10))[0]
            data = b""
            if ln:
                data = await asyncio.wait_for(reader.readexactly(ln), timeout=10)
            return ("NAK" if c == Y_NAK else "CAN", ln, data)
        # anything else: stray byte, keep scanning


async def yapp_send_file(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                         path: Path) -> str:
    """Send one file with YAPP. Returns a user-facing result message."""
    data = path.read_bytes()
    name = path.name

    # Generous timeouts: over 300bd HF the receiver only acks EOF after all
    # queued data has actually drained over RF.
    ack_eof_timeout = max(180.0, len(data) / 15.0)

    try:
        # SI -> expect RR (ACK 01)
        writer.write(bytes([Y_ENQ, 0x01]))
        await writer.drain()
        kind, val, _ = await _yapp_wait(reader, 60)
        if kind == "CAN" or kind == "NAK":
            raise YappAbort("Your terminal refused the transfer.")
        if not (kind == "ACK" and val == 0x01):
            raise YappAbort("Unexpected response starting YAPP.")

        # Header -> expect RF (ACK 02) or YappC RT (ACK ACK)
        hdr = name.encode("ascii", "ignore") + b"\x00" + str(len(data)).encode("ascii") + b"\x00"
        writer.write(bytes([Y_SOH, len(hdr)]) + hdr)
        await writer.drain()
        kind, val, _ = await _yapp_wait(reader, 60)
        if kind in ("NAK", "CAN"):
            raise YappAbort("Your terminal declined the file.")
        if kind == "ACK" and val == 0x02:
            use_checksum = False
        elif kind == "ACK" and val == Y_ACK:
            use_checksum = True
        else:
            raise YappAbort("Unexpected response to YAPP header.")

        # Data frames
        sent = 0
        while sent < len(data):
            chunk = data[sent:sent + YAPP_BLOCK]
            pkt = bytes([Y_STX, len(chunk) & 0xFF]) + chunk
            if use_checksum:
                pkt += bytes([sum(chunk) & 0xFF])
            writer.write(pkt)
            sent += len(chunk)
            if sent % (YAPP_BLOCK * 8) == 0:
                await writer.drain()
        await writer.drain()

        # EOF -> expect AF (ACK 03)
        writer.write(bytes([Y_ETX, 0x01]))
        await writer.drain()
        kind, val, _ = await _yapp_wait(reader, ack_eof_timeout)
        if kind in ("NAK", "CAN"):
            raise YappAbort("Transfer cancelled by your terminal.")
        if not (kind == "ACK" and val == 0x03):
            raise YappAbort("File end was not acknowledged.")

        # EOT -> AT (ACK 04) is a courtesy; don't fail if it never comes
        writer.write(bytes([Y_EOT, 0x01]))
        await writer.drain()
        try:
            await _yapp_wait(reader, 15)
        except asyncio.TimeoutError:
            pass

        mode = "YappC" if use_checksum else "YAPP"
        return f"Transfer complete ({mode}): {name} ({len(data)} bytes)\r\n"

    except YappAbort as e:
        return f"YAPP: {e} File is still on the BBS as {name}\r\n"
    except asyncio.TimeoutError:
        return (f"YAPP: no response from your terminal - it may not support YAPP.\r\n"
                f"The file is on the BBS Files area as {name}\r\n")


# -----------------------------
# Config load
# -----------------------------

def load_config() -> Dict:
    cfg_path = Path(CONFIG_FILE)
    if not cfg_path.exists():
        cfg_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        print(f"[CFG] Created {CONFIG_FILE}.")
        return DEFAULT_CONFIG
    return json.loads(cfg_path.read_text(encoding="utf-8"))


# -----------------------------
# BPQ TCP server / session handler
# -----------------------------

def send(writer: asyncio.StreamWriter, text: str) -> None:
    writer.write(sanitize_for_bbs(text).encode("utf-8", "ignore"))


async def read_line(reader: asyncio.StreamReader, timeout: Optional[float] = None) -> Optional[str]:
    try:
        if timeout:
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        else:
            raw = await reader.readline()
    except (asyncio.TimeoutError, ConnectionResetError, OSError):
        return None
    if not raw:
        return None
    return raw.decode("utf-8", "ignore").strip()


async def get_report_text(ctx: "AppContext", zipcode: str, hours48: bool) -> str:
    info = ctx.zipdb.lookup(zipcode)
    if info is None:
        return f"Unknown zip code: {zipcode}\n"
    lat, lon, place, state = info
    periods = ctx.cache.get(zipcode)
    if periods is None:
        try:
            periods = await asyncio.to_thread(fetch_hourly_periods, lat, lon)
            ctx.cache.put(zipcode, periods)
        except Exception as e:
            return f"NWS forecast fetch failed: {str(e)[:100]}\n"
    return build_report(place, state, zipcode, periods, hours48)


class AppContext:
    def __init__(self, cfg: Dict):
        self.cfg = cfg
        self.zipdb = ZipDB(cfg["storage"]["zip_db_file"])
        self.userdb = UserDB(cfg["storage"]["user_db_file"])
        self.cache = ForecastCache(int(cfg["forecast_cache_minutes"]))
        self.output_dir = Path(cfg["output_dir"])
        self.banner = cfg.get("banner", "BPQ WX SERVICE")
        self.grib = GribManager(self.output_dir, cfg["grib"])


async def prompt_for_zip(ctx: AppContext, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter, callsign: str) -> Optional[str]:
    for _ in range(3):
        send(writer, "Enter your 5-digit US zip code (or Q to quit):\r\n> ")
        await writer.drain()
        ans = await read_line(reader)
        if ans is None or ans.upper() in ("Q", "QUIT", "EXIT", "BYE", "NODE"):
            return None
        z = ans.strip()
        if ctx.zipdb.lookup(z):
            ctx.userdb.set_zip(callsign, z)
            return z
        send(writer, f"'{z}' is not a zip code I know. Try again.\r\n")
    return None


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ctx: AppContext) -> None:
    try:
        callsign = (await read_line(reader, timeout=5.0)) or "UNKNOWN"
        callsign = callsign.upper()
        print(f"[CONN] {callsign}")

        zipcode = ctx.userdb.get_zip(callsign)
        if not zipcode:
            send(writer, "Welcome to WX - weather by zip code.\r\n")
            zipcode = await prompt_for_zip(ctx, reader, writer, callsign)
            if not zipcode:
                return

        send(writer, await get_report_text(ctx, zipcode, hours48=False))
        send(writer, "\r\n" + MENU_LINE + "> ")
        await writer.drain()

        while True:
            line = await read_line(reader)
            if line is None:
                return
            up = line.upper()

            if up in ("Q", "QUIT", "EXIT", "BYE", "NODE"):
                send(writer, "73!\r\n")
                await writer.drain()
                return

            elif up in ("", "WX", "REFRESH"):
                send(writer, await get_report_text(ctx, zipcode, hours48=False))

            elif up == "48HR" or up == "48":
                send(writer, await get_report_text(ctx, zipcode, hours48=True))

            elif up.startswith("ZIP"):
                parts = line.split()
                if len(parts) == 2 and ctx.zipdb.lookup(parts[1]):
                    zipcode = parts[1]
                    ctx.userdb.set_zip(callsign, zipcode)
                    send(writer, await get_report_text(ctx, zipcode, hours48=False))
                else:
                    send(writer, "Usage: ZIP <5-digit US zip> (example: ZIP 33445)\r\n")

            elif up == "ALERTS":
                info = ctx.zipdb.lookup(zipcode)
                if info:
                    lat, lon, place, state = info
                    try:
                        features = await asyncio.to_thread(fetch_alerts, lat, lon)
                        send(writer, build_alerts_text(place, state, features))
                    except Exception as e:
                        send(writer, f"Alerts fetch failed: {str(e)[:100]}\r\n")

            elif up == "DL" or up == "DL TXT":
                report = await get_report_text(ctx, zipcode, hours48=True)
                if report.startswith(("Unknown", "NWS")):
                    send(writer, report)
                else:
                    name = await asyncio.to_thread(write_report_file, ctx.output_dir,
                                                   callsign, zipcode, report, ctx.banner)
                    send(writer,
                         f"Starting YAPP transfer of {name}.\r\n"
                         f"If your terminal supports YAPP it will begin now...\r\n")
                    await writer.drain()
                    result = await yapp_send_file(reader, writer, ctx.output_dir / name)
                    send(writer, result)

            elif up == "DL GRIB":
                job = ctx.grib.get(callsign)
                if job is None:
                    send(writer, "No GRIB built yet. Type GRIB first, then DL GRIB when done.\r\n")
                elif job.status == "RUNNING":
                    send(writer, f"GRIB still building: hour {job.hours_done} of {job.hours_total}. Try again shortly.\r\n")
                elif job.status != "DONE" or not (ctx.output_dir / job.filename).exists():
                    send(writer, "GRIB build failed or file expired. Type GRIB to rebuild.\r\n")
                else:
                    send(writer,
                         f"Starting YAPP transfer of {job.filename}.\r\n"
                         f"If your terminal supports YAPP it will begin now...\r\n")
                    await writer.drain()
                    result = await yapp_send_file(reader, writer, ctx.output_dir / job.filename)
                    send(writer, result)

            elif up == "FILE":
                report = await get_report_text(ctx, zipcode, hours48=True)
                if report.startswith(("Unknown", "NWS")):
                    send(writer, report)
                else:
                    await asyncio.to_thread(prune_old_files, ctx.output_dir,
                                            int(ctx.cfg["file_max_age_hours"]))
                    name = await asyncio.to_thread(write_report_file, ctx.output_dir,
                                                   callsign, zipcode, report, ctx.banner)
                    send(writer, f"Saved. Download from the BBS Files area:\r\n  {name}\r\n")

            elif up == "GRIB":
                info = ctx.zipdb.lookup(zipcode)
                if info:
                    lat, lon, place, state = info
                    await asyncio.to_thread(prune_old_files, ctx.output_dir,
                                            int(ctx.cfg["file_max_age_hours"]))
                    job, err = ctx.grib.start(callsign, zipcode, lat, lon)
                    if job is None:
                        send(writer, err + "\r\n")
                    else:
                        send(writer,
                             f"Building GRIB for {place} {state} (~1 min).\r\n"
                             f"File will appear in the BBS Files area as:\r\n"
                             f"  {job.filename}\r\n"
                             f"Type STATUS to check progress.\r\n")

            elif up == "STATUS":
                job = ctx.grib.get(callsign)
                if job is None:
                    send(writer, "No GRIB build started. Type GRIB to start one.\r\n")
                elif job.status == "RUNNING":
                    send(writer, f"GRIB build RUNNING: hour {job.hours_done} of {job.hours_total}.\r\n")
                elif job.status == "DONE":
                    send(writer, f"GRIB build DONE: {job.filename}\r\nView with XyGrib: {XYGRIB_URL}\r\n")
                else:
                    send(writer, f"GRIB build FAILED: {job.error}\r\n")

            elif up == "HELP" or up == "?":
                send(writer, HELP_TEXT)

            else:
                send(writer, "Unknown command. Type HELP for commands.\r\n")

            send(writer, "\r\n" + MENU_LINE + "> ")
            await writer.drain()

    except (ConnectionResetError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def run_server() -> None:
    cfg = load_config()
    ctx = AppContext(cfg)
    ctx.output_dir.mkdir(parents=True, exist_ok=True)

    host = cfg["bpq_app"]["listen_host"]
    port = int(cfg["bpq_app"]["listen_port"])
    server = await asyncio.start_server(lambda r, w: handle_client(r, w, ctx), host, port)
    print(f"[WX] Listening on {host}:{port}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        print("Shutting down.")
