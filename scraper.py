"""Collect ParkSJ and SJSU parking fullness every ten minutes."""

import argparse
import csv
import fcntl
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
CSV_FILE = HERE / "parking_history.csv"
LOCAL_TIME = ZoneInfo("America/Los_Angeles")
FIELDS = ["timestamp", "source", "garage_name", "percent_full", "spaces_available"]
INTERVAL = 600


def log(message):
    print(f"[{datetime.now(LOCAL_TIME):%Y-%m-%d %H:%M}] {message}", flush=True)


def fetch(url, verify=True, standard_library=False):
    """Retry a temporary HTTP/network failure once, after five seconds."""
    for attempt in range(2):
        try:
            # ParkSJ's bot filter accepts urllib but rejects requests here.
            if standard_library:
                request = urllib.request.Request(
                    url, headers={"User-Agent": "ParkingHistoryScraper/1.0"})
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.read().decode("utf-8")
            response = requests.get(
                url, timeout=(10, 30), verify=verify,
                headers={"User-Agent": "ParkingHistoryScraper/1.0 (10-minute polling)",
                         "Cache-Control": "no-cache"},
            )
            response.raise_for_status()
            return response.text
        except (requests.RequestException, urllib.error.URLError, TimeoutError) as error:
            status = getattr(error, "code", None)
            if isinstance(error, requests.RequestException) and error.response is not None:
                status = error.response.status_code
            temporary = status is None or status in (408, 429) or status >= 500
            if attempt == 1 or not temporary:
                raise
            log(f"request failed; retrying once: {error}")
            time.sleep(5)


def parse_percent(text):
    text = text.strip()
    if text.lower() == "full":
        return 100
    match = re.fullmatch(r"(\d+)\s*%\s*(?:FULL)?", text, re.IGNORECASE)
    if not match or not 0 <= int(match[1]) <= 100:
        raise ValueError(f"unexpected fullness: {text!r}")
    return int(match[1])


def scrape_parksj(html=None):
    """Read the map's garage tooltips; each includes fullness and free spaces."""
    if html is None:
        html = fetch("https://parksj.org/", standard_library=True)
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for garage in soup.select(".e-hotspot__tooltip"):
        name = "unknown garage"
        try:
            title = garage.find("a")
            if title is None:
                raise ValueError("missing garage name")
            name = re.sub(r"^\d+\.\s*", "", title.get_text(" ", strip=True))
            name = " ".join(name.split())
            if not name:
                raise ValueError("empty garage name")
            fullness = garage.select_one(".meter-gauge")
            if fullness is None:
                raise ValueError("missing fullness")
            percent = parse_percent(fullness.get_text(" ", strip=True))
            available = ""
            spaces = garage.select_one(".lot-available")
            if spaces is not None:
                match = re.fullmatch(
                    r"([\d,]+)\s+of\s+([\d,]+)\s+available",
                    spaces.get_text(" ", strip=True), re.IGNORECASE,
                )
                if match:
                    available = int(match[1].replace(",", ""))
                    if available > int(match[2].replace(",", "")):
                        log(f"ParkSJ {name}: invalid available count; leaving blank")
                        available = ""
                else:
                    log(f"ParkSJ {name}: unrecognized available count; leaving blank")
            rows.append({"source": "parksj", "garage_name": name,
                         "percent_full": percent, "spaces_available": available})
        except (ValueError, TypeError) as error:
            log(f"ParkSJ {name}: skipped: {error}")
    if not rows:
        raise ValueError("no ParkSJ garages could be parsed")
    return rows


def scrape_sjsu(html=None):
    """SJSU places each garage's status in the paragraph after its name."""
    if html is None:
        html = fetch("https://sjsuparkingstatus.sjsu.edu/", verify=str(HERE / "sjsu_ca.pem"))
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for title in soup.select(".garage__name"):
        name = " ".join(title.get_text(" ", strip=True).split())
        try:
            paragraph = title.find_next_sibling()
            fullness = paragraph.select_one(".garage__fullness") if paragraph else None
            if not name or fullness is None:
                raise ValueError("missing name or fullness")
            rows.append({"source": "sjsu", "garage_name": name,
                         "percent_full": parse_percent(fullness.get_text(" ", strip=True)),
                         "spaces_available": ""})
        except (ValueError, TypeError) as error:
            log(f"SJSU {name}: skipped: {error}")
    if not rows:
        raise ValueError("no SJSU garages could be parsed")
    return rows


def row_key(row):
    collected = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
    interval = collected.replace(minute=collected.minute // 10 * 10, second=0)
    return interval, row["source"], row["garage_name"]


def write_rows(rows, csv_file=CSV_FILE):
    """Append new rows, skipping garage/source pairs already in this interval."""
    with open(csv_file, "a+", newline="", encoding="utf-8") as file:
        # Linux file lock also prevents duplicates from overlapping processes.
        fcntl.flock(file, fcntl.LOCK_EX)
        file.seek(0)
        reader = csv.DictReader(file)
        existing = set()
        if reader.fieldnames is not None:
            if reader.fieldnames != FIELDS:
                raise ValueError("existing CSV header does not match expected columns")
            for row in reader:
                try:
                    existing.add(row_key(row))
                except (ValueError, KeyError, TypeError):
                    log("ignoring malformed existing CSV row during duplicate check")
        file.seek(0, 2)
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        if file.tell() == 0:
            writer.writeheader()
        written = 0
        for row in rows:
            key = row_key(row)
            if key not in existing:
                writer.writerow(row)
                existing.add(key)
                written += 1
        return written


def run_collection():
    timestamp = datetime.now(LOCAL_TIME).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for label, scrape in [("ParkSJ", scrape_parksj), ("SJSU", scrape_sjsu)]:
        try:
            garages = scrape()
            rows.extend(dict(garage, timestamp=timestamp) for garage in garages)
            log(f"collected {len(garages)} {label} garages")
        except Exception as error:
            # A source's unexpected format must not prevent the other collection.
            log(f"{label} failed: {error}")
    written = write_rows(rows)
    log(f"wrote {written} rows ({len(rows) - written} duplicates skipped)")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="collect once and exit")
    args = parser.parse_args()
    try:
        while True:
            try:
                run_collection()
            except Exception as error:
                log(f"collection failed: {error}")
            if args.once:
                break
            # Collect immediately on startup, then at :00, :10, ... :50.
            time.sleep(INTERVAL - time.time() % INTERVAL)
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
