#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse
from urllib.request import url2pathname

import requests
from bs4 import BeautifulSoup, Tag

# ----------------------------------------------------------------------------
# Status codes (anything other than OK is a flag)
# ----------------------------------------------------------------------------
OK = "OK"
NOT_FOUND = "NOT_FOUND"              # selector matched nothing
EMPTY = "EMPTY"                      # element found but had no value
ATTR_MISSING = "ATTR_MISSING"        # element found but attribute absent
REGEX_NO_MATCH = "REGEX_NO_MATCH"    # value found but regex did not match
CAST_FAILED = "CAST_FAILED"          # value found but could not convert to int/float
SELECTOR_ERROR = "SELECTOR_ERROR"    # invalid CSS selector in config
BLOCKED_ROBOTS = "BLOCKED_ROBOTS"    # robots.txt disallows the URL
BLOCKED_ACCESS = "BLOCKED_ACCESS"    # 401/403/429, login wall, or captcha
HTTP_ERROR = "HTTP_ERROR"            # other 4xx/5xx
FETCH_ERROR = "FETCH_ERROR"          # network error, timeout, missing file

COLUMNS = [
    "target", "page", "item_index", "field", "value", "status", "severity",
    "reason", "note", "source_url", "http_status", "html_line", "html_column",
    "xpath", "selector_used", "attribute", "required", "fetched_at",
]

DEFAULT_UA = "ProjectScraper/1.0 (student project)"


# ----------------------------------------------------------------------------
# Fetching
# ----------------------------------------------------------------------------
class FetchResult:
    def __init__(self, ok, html="", final_url="", http_status=None, status=OK, reason=""):
        self.ok = ok
        self.html = html
        self.final_url = final_url
        self.http_status = http_status
        self.status = status
        self.reason = reason


class Fetcher:
    def __init__(self, user_agent, timeout, delay, respect_robots, retries):
        self.ua = user_agent
        self.timeout = timeout
        self.delay = delay
        self.respect_robots = respect_robots
        self.retries = retries
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self._robots = {}
        self._last_request = 0.0

    def _throttle(self):
        wait = self.delay - (time.time() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.time()

    def _robots_allows(self, url):
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.session.get(base + "/robots.txt", timeout=self.timeout)
                if r.status_code >= 400:
                    rp = None  # no robots.txt -> allowed
                else:
                    rp.parse(r.text.splitlines())
            except requests.RequestException:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.ua, url)

    def get(self, url):
        parsed = urlparse(url)

        # Local files (handy for testing)
        if parsed.scheme == "file":
            try:
                html = Path(url2pathname(parsed.path)).read_text(encoding="utf-8", errors="replace")
                return FetchResult(True, html, url, None)
            except OSError as e:
                return FetchResult(False, final_url=url, status=FETCH_ERROR,
                                   reason=f"could not read local file: {e.strerror or e}")

        if parsed.scheme not in ("http", "https"):
            return FetchResult(False, final_url=url, status=FETCH_ERROR,
                               reason=f"unsupported URL scheme '{parsed.scheme}'")

        if self.respect_robots and not self._robots_allows(url):
            return FetchResult(False, final_url=url, status=BLOCKED_ROBOTS,
                               reason="robots.txt disallows this URL for our user-agent; not fetched")

        last_err = None
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                r = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as e:
                last_err = f"{type(e).__name__}: {e}"
                time.sleep(min(2 ** attempt, 10))
                continue

            code = r.status_code
            if code in (429, 500, 502, 503, 504) and attempt < self.retries:
                ra = r.headers.get("Retry-After", "")
                time.sleep(int(ra) if ra.isdigit() else min(2 ** attempt, 10))
                continue
            if code in (401, 403, 429):
                return FetchResult(False, final_url=r.url, http_status=code, status=BLOCKED_ACCESS,
                                   reason=f"HTTP {code}: access denied / rate limited / login or anti-bot protection")
            if code >= 400:
                return FetchResult(False, final_url=r.url, http_status=code, status=HTTP_ERROR,
                                   reason=f"HTTP {code} {r.reason}")
            return FetchResult(True, r.text, r.url, code)

        return FetchResult(False, final_url=url, status=FETCH_ERROR,
                           reason=f"request failed after {self.retries + 1} attempts ({last_err})")


# ----------------------------------------------------------------------------
# Location helpers
# ----------------------------------------------------------------------------
def xpath_of(node):
    """Absolute XPath of a bs4 element (identity-based, so identical siblings are numbered correctly)."""
    parts = []
    while isinstance(node, Tag) and node.name != "[document]":
        parent = node.parent
        same = parent.find_all(node.name, recursive=False) if parent is not None else [node]
        pos = next(i for i, s in enumerate(same, 1) if s is node)
        parts.append(f"{node.name}[{pos}]" if len(same) > 1 else node.name)
        node = parent
    return "/" + "/".join(reversed(parts))


def line_col(node):
    line = getattr(node, "sourceline", None)
    pos = getattr(node, "sourcepos", None)
    return line, (pos + 1 if pos is not None else None)


# ----------------------------------------------------------------------------
# Value handling
# ----------------------------------------------------------------------------
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def read_value(el, attr):
    if attr in (None, "", "text"):
        return el.get_text(" ", strip=True)
    if attr == "html":
        return el.decode_contents().strip()
    v = el.get(attr)
    if v is None:
        return None
    if isinstance(v, list):
        v = " ".join(v)
    return v.strip()


def cast(value, typ):
    if typ in (None, "", "text", "url"):
        return value
    cleaned = re.sub(r"[^\d.\-]", "", value.replace(",", ""))
    if typ == "float":
        return float(cleaned)
    if typ == "int":
        return int(float(cleaned))
    raise ValueError(f"unknown type '{typ}'")


# ----------------------------------------------------------------------------
# Extraction
# ----------------------------------------------------------------------------
def make_row(ctx, field, status, value="", reason="", note="", el=None, scope=None, attr="text"):
    node = el if el is not None else scope
    line, col = line_col(node) if node is not None else (None, None)
    required = field.get("required", True)
    return {
        "target": ctx["target"],
        "page": ctx["page"],
        "item_index": ctx.get("item_index", ""),
        "field": field["name"],
        "value": value,
        "status": status,
        "severity": "ok" if status == OK else ("error" if required else "warning"),
        "reason": reason,
        "note": note,
        "source_url": ctx["source_url"],
        "http_status": ctx["http_status"] if ctx["http_status"] is not None else "",
        "html_line": line if line is not None else "",
        "html_column": col if col is not None else "",
        "xpath": xpath_of(node) if node is not None else "",
        "selector_used": field.get("selector", "self"),
        "attribute": attr if attr not in (None, "") else "text",
        "required": required,
        "fetched_at": ctx["fetched_at"],
    }


def extract_field(scope, field, ctx):
    sel = field.get("selector", "self")
    attr = field.get("attr", "text")
    multiple = field.get("multiple", False)
    rows = []

    try:
        els = [scope] if sel in ("", "self") else scope.select(sel)
    except Exception as e:  # soupsieve raises several error types
        msg = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        return [make_row(ctx, field, SELECTOR_ERROR, reason=f"invalid selector '{sel}': {msg}",
                         scope=scope, attr=attr)]

    if not els:
        reason = f"selector '{sel}' matched 0 elements (xpath/line show where it searched)"
        if ctx.get("hint"):
            reason += "; " + ctx["hint"]
        return [make_row(ctx, field, NOT_FOUND, reason=reason, scope=scope, attr=attr)]

    chosen = els if multiple else els[:1]
    note = f"{len(els)} elements matched, used the first" if (not multiple and len(els) > 1) else ""

    for el in chosen:
        raw = read_value(el, attr)
        if raw is None:
            rows.append(make_row(ctx, field, ATTR_MISSING,
                                 reason=f"element found but has no '{attr}' attribute",
                                 note=note, el=el, attr=attr))
            continue
        if raw == "":
            rows.append(make_row(ctx, field, EMPTY, reason="element found but its value is empty",
                                 note=note, el=el, attr=attr))
            continue

        value = raw
        if field.get("regex"):
            m = re.search(field["regex"], value)
            if not m:
                rows.append(make_row(ctx, field, REGEX_NO_MATCH, value=raw,
                                     reason=f"regex '{field['regex']}' did not match raw value",
                                     note=note, el=el, attr=attr))
                continue
            value = m.group(1) if m.groups() else m.group(0)

        if field.get("type") == "url" or attr in ("href", "src"):
            value = urljoin(ctx["source_url"], value)

        try:
            value = cast(value, field.get("type"))
        except ValueError:
            rows.append(make_row(ctx, field, CAST_FAILED, value=raw,
                                 reason=f"could not convert '{raw}' to {field.get('type')}",
                                 note=note, el=el, attr=attr))
            continue

        rows.append(make_row(ctx, field, OK, value=value, note=note, el=el, attr=attr))
    return rows


def scrape_target(target, fetcher):
    rows = []
    url = target["url"]
    fields = target["fields"]
    item_selector = target.get("item_selector")
    pag = target.get("pagination") or {}
    max_pages = int(pag.get("max_pages", 1)) if pag else 1
    seen, page = set(), 0

    while url and page < max_pages and url not in seen:
        seen.add(url)
        page += 1
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        res = fetcher.get(url)
        ctx = {"target": target["name"], "page": page, "source_url": res.final_url or url,
               "http_status": res.http_status, "fetched_at": fetched_at}

        # Page could not be pulled: emit one flagged row per expected field
        if not res.ok:
            for f in fields:
                rows.append(make_row(ctx, f, res.status, reason=res.reason,
                                     attr=f.get("attr", "text")))
            break

        soup = BeautifulSoup(res.html, "html.parser")

        # Hints that explain why selectors might match nothing
        body_text = soup.get_text(" ", strip=True)
        if len(body_text) < 200 and re.search(r"captcha|are you a (robot|human)|access denied|verify you are",
                                              res.html, re.I):
            ctx["hint"] = "page looks like a captcha / bot-check wall"
        elif len(body_text) < 200 and soup.find("script"):
            ctx["hint"] = "page has almost no static text and uses scripts - likely JavaScript-rendered; a headless browser would be needed"

        if item_selector:
            try:
                items = soup.select(item_selector)
            except Exception as e:
                rows.append(make_row(ctx, {"name": "(item_selector)", "selector": item_selector},
                                     SELECTOR_ERROR, reason=f"invalid item_selector: {e}", scope=soup))
                break
            if not items:
                reason = f"item_selector '{item_selector}' matched 0 elements - no items to extract"
                if ctx.get("hint"):
                    reason += "; " + ctx["hint"]
                rows.append(make_row(ctx, {"name": "(item_selector)", "selector": item_selector},
                                     NOT_FOUND, reason=reason, scope=soup))
        else:
            items = [soup]

        for i, item in enumerate(items, 1):
            ctx["item_index"] = i if item_selector else ""
            for f in fields:
                rows.extend(extract_field(item, f, ctx))

        # Pagination
        url = None
        if pag.get("next_selector"):
            try:
                nxt = soup.select_one(pag["next_selector"])
            except Exception:
                nxt = None
            if nxt is not None and nxt.get("href"):
                url = urljoin(res.final_url or ctx["source_url"], nxt["href"])
                if page < max_pages:
                    time.sleep(0)  # throttling happens in Fetcher
    return rows


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------
def summarize(rows):
    counts = Counter((r["target"], r["field"], r["status"]) for r in rows)
    return [{"target": t, "field": f, "status": s, "count": n} for (t, f, s), n in sorted(counts.items())]


def csv_safe(v):
    if isinstance(v, str) and v[:1] in ("=", "+", "@", "\t", "\r"):
        return "'" + v
    return v


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: csv_safe(r.get(k, "")) for k in COLUMNS})


def write_json(rows, path, meta):
    payload = {"meta": meta, "summary": summarize(rows), "rows": rows}
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def write_xlsx(rows, path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    red = PatternFill("solid", fgColor="F8D7DA")
    yellow = PatternFill("solid", fgColor="FFF3CD")
    head = PatternFill("solid", fgColor="D9E2F3")

    def put(ws, header, data_rows, shade=False):
        ws.append(header)
        for c in ws[1]:
            c.font = Font(bold=True)
            c.fill = head
        for r in data_rows:
            ws.append([_ILLEGAL.sub("", v) if isinstance(v, str) else v for v in (r[h] for h in header)])
            row_idx = ws.max_row
            for c in ws[row_idx]:
                if isinstance(c.value, str) and c.value[:1] in ("=", "+", "@"):
                    c.data_type = "s"
            if shade and r.get("severity") in ("error", "warning"):
                fill = red if r["severity"] == "error" else yellow
                for c in ws[row_idx]:
                    c.fill = fill
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for i, h in enumerate(header, 1):
            width = max([len(str(h))] + [len(str(c.value)) for c in ws[get_column_letter(i)][1:200] if c.value is not None])
            ws.column_dimensions[get_column_letter(i)].width = min(width + 2, 60)
        for row in ws.iter_rows(min_row=2):
            for c in row:
                c.alignment = Alignment(vertical="top")

    wb = Workbook()
    put(wb.active, COLUMNS, rows, shade=True)
    wb.active.title = "Data"
    put(wb.create_sheet("Flags"), COLUMNS, [r for r in rows if r["status"] != OK], shade=True)
    put(wb.create_sheet("Summary"), ["target", "field", "status", "count"], summarize(rows))
    wb.save(path)


def print_report(rows):
    total = len(rows)
    flagged = [r for r in rows if r["status"] != OK]
    errors = [r for r in flagged if r["severity"] == "error"]
    print(f"\nExtracted {total - len(flagged)} values OK, {len(flagged)} flagged "
          f"({len(errors)} errors on required fields, {len(flagged) - len(errors)} warnings on optional fields).")
    if flagged:
        print("\nFLAGGED (could not pull):")
        for r in flagged:
            loc = f"line {r['html_line']}" if r["html_line"] != "" else "no element"
            where = f"{r['source_url']} [{loc}]"
            item = f" item {r['item_index']}" if r["item_index"] != "" else ""
            print(f"  [{r['severity'].upper():7}] {r['target']} p{r['page']}{item} / {r['field']}: "
                  f"{r['status']} - {r['reason']}\n             at {where}")


# ----------------------------------------------------------------------------
# Config + CLI
# ----------------------------------------------------------------------------
def load_config(path):
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"Cannot read config '{path}': {e}")
    targets = cfg.get("targets")
    if not targets:
        sys.exit("Config needs a non-empty 'targets' list.")
    for i, t in enumerate(targets, 1):
        for key in ("name", "url", "fields"):
            if key not in t:
                sys.exit(f"Target #{i} is missing '{key}'.")
        for j, f in enumerate(t["fields"], 1):
            if "name" not in f:
                sys.exit(f"Target '{t['name']}' field #{j} is missing 'name'.")
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description="Config-driven scraper with source locations and missing-data flags.")
    ap.add_argument("config", help="path to JSON config")
    ap.add_argument("--out", default="results", help="output path prefix (default: results)")
    ap.add_argument("--formats", default="csv,json,xlsx", help="comma list of csv,json,xlsx")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    s = cfg.get("settings", {})
    fetcher = Fetcher(
        user_agent=s.get("user_agent", DEFAULT_UA),
        timeout=s.get("timeout_seconds", 20),
        delay=s.get("delay_seconds", 1.5),
        respect_robots=s.get("respect_robots", True),
        retries=s.get("retries", 2),
    )

    rows = []
    for t in cfg["targets"]:
        print(f"Scraping target '{t['name']}' -> {t['url']}")
        rows.extend(scrape_target(t, fetcher))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    meta = {"config": str(args.config), "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rows": len(rows), "flagged": sum(1 for r in rows if r["status"] != OK)}
    fmts = {f.strip().lower() for f in args.formats.split(",")}
    written = []
    if "csv" in fmts:
        write_csv(rows, f"{out}.csv"); written.append(f"{out}.csv")
    if "json" in fmts:
        write_json(rows, f"{out}.json", meta); written.append(f"{out}.json")
    if "xlsx" in fmts:
        write_xlsx(rows, f"{out}.xlsx"); written.append(f"{out}.xlsx")

    print_report(rows)
    print("\nWrote: " + ", ".join(written))


if __name__ == "__main__":
    main()
