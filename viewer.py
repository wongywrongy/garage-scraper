"""A small, read-only web page for the collected parking CSV.

Run: python3 viewer.py
Share: tailscale serve --bg --http=8765 http://127.0.0.1:8765
"""

import csv
import fcntl
import html
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

CSV_FILE = Path(__file__).resolve().parent / "parking_history.csv"
FIELDS = ["timestamp", "source", "garage_name", "percent_full", "spaces_available"]
PORT = 8765


def read_csv():
    """Take a consistent snapshot while the scraper may be appending."""
    try:
        with CSV_FILE.open("rb") as file:
            fcntl.flock(file, fcntl.LOCK_SH)
            return file.read()
    except FileNotFoundError:
        return b""


def render_page(data):
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"))))
    rows = [row for row in rows if all(row.get(field) is not None for field in FIELDS)]
    rows.sort(key=lambda row: row["timestamp"], reverse=True)
    latest = html.escape(rows[0]["timestamp"]) if rows else "None yet"
    table_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(row[field])}</td>" for field in FIELDS) + "</tr>"
        for row in rows[:100]
    )
    content = f"""
    <div class="table-scroll" role="region" aria-label="Collected parking data" tabindex="0">
      <table>
        <caption>Newest {min(len(rows), 100)} of {len(rows):,} collected rows</caption>
        <thead><tr><th scope="col">Collected (Pacific)</th><th scope="col">Source</th>
          <th scope="col">Garage</th><th scope="col">% full</th>
          <th scope="col">Spaces available</th></tr></thead>
        <tbody>{table_rows}</tbody>
      </table>
    </div>""" if rows else "<p>No data collected yet.</p>"
    download = '<a class="download" href="/download">Download CSV</a>' if data else ""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Parking history</title>
  <style>
    body {{ margin: 0; background: #fff; color: #202124; font: 16px/1.5 system-ui, sans-serif; }}
    main {{ max-width: 1100px; margin: 32px auto; padding: 0 20px; }}
    h1 {{ margin: 0 0 12px; font-size: 28px; }}
    p {{ margin: 8px 0; }}
    nav {{ display: flex; align-items: center; gap: 20px; margin: 20px 0 28px; }}
    a {{ color: #174ea6; text-underline-offset: 3px; }}
    a:hover {{ color: #0d3474; }}
    .download {{ background: #174ea6; color: #fff; padding: 10px 16px; border-radius: 4px; text-decoration: none; }}
    .download:hover {{ background: #0d3474; color: #fff; }}
    :focus-visible {{ outline: 3px solid #174ea6; outline-offset: 3px; }}
    .table-scroll {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }}
    caption {{ text-align: left; padding-bottom: 12px; }}
    th, td {{ text-align: left; padding: 10px 12px; border-bottom: 1px solid #dadce0; }}
    th {{ background: #f1f3f4; white-space: nowrap; }}
    td:first-child {{ white-space: nowrap; }}
    th:nth-child(n+4), td:nth-child(n+4) {{ text-align: right; }}
    @media (max-width: 600px) {{ main {{ margin: 20px auto; padding: 0 12px; }} }}
  </style>
</head>
<body><main>
  <h1>Parking history</h1>
  <p>Total collected rows: <strong>{len(rows):,}</strong></p>
  <p>Latest collection: <strong>{latest}</strong> (America/Los_Angeles)</p>
  <nav aria-label="Data actions">{download}<a href="/">Refresh</a></nav>
  {content}
</main></body>
</html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path not in ("/", "/download"):
            self.send_error(404)
            return
        try:
            data = read_csv()
            if path == "/download":
                if not data:
                    self.send_error(404, "No CSV collected yet")
                    return
                body = data
                content_type = "text/csv; charset=utf-8"
            else:
                body = render_page(data)
                content_type = "text/html; charset=utf-8"
        except (OSError, UnicodeError, csv.Error) as error:
            print(f"Could not read parking history: {error}", flush=True)
            self.send_error(500, "Could not read parking history. Try refreshing.")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if path == "/download":
            self.send_header("Content-Disposition", 'attachment; filename="parking_history.csv"')
        self.end_headers()
        self.wfile.write(body)


def main():
    with ThreadingHTTPServer(("127.0.0.1", PORT), Handler) as server:
        print(f"Parking viewer: http://127.0.0.1:{PORT}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("Stopped", flush=True)


if __name__ == "__main__":
    main()
