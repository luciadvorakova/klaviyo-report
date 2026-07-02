"""Generate a single self-contained HTML SPA report for all clients.

Usage:
    python scripts/generate_report.py
    python scripts/generate_report.py --timeframe last_30_days
    python scripts/generate_report.py --out report.html

Opens directly in any browser — no server needed.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import click
import httpx
from rich.console import Console
from rich.progress import Progress

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.accounts import Account, load_registry
from lib.benchmarks import BENCHMARKS, benchmark_open, flow_category, grade
from lib.klaviyo import KlaviyoClient

console = Console()
ROOT = Path(__file__).resolve().parent.parent


# ── helpers ──────────────────────────────────────────────────────────────────

def strip_emoji(s: str) -> str:
    """Remove leading emoji characters from a string."""
    return re.sub(
        r'^[\U00010000-\U0010ffff\U00002600-\U000027FF\U0001F300-\U0001FAFF\s]+',
        '', s
    ).strip()


def pct(v):
    return None if v is None else round(v * 100, 1)


def money(v):
    return None if v is None else round(v)


def aggregate_flow(messages: list[dict]) -> dict:
    """Weighted-average rates by delivered count, sum conversion_value."""
    total_del = sum(m["statistics"].get("delivered") or 0 for m in messages)
    total_rec = sum(m["statistics"].get("recipients") or 0 for m in messages)
    total_rev = sum(m["statistics"].get("conversion_value") or 0 for m in messages)

    def wavg(key):
        if not total_del:
            return None
        return sum(
            (m["statistics"].get(key) or 0) * (m["statistics"].get("delivered") or 0)
            for m in messages
        ) / total_del

    return {
        "recipients": total_rec,
        "delivered": total_del,
        "open_rate": wavg("open_rate"),
        "click_rate": wavg("click_rate"),
        "conversion_rate": wavg("conversion_rate"),
        "unsubscribe_rate": wavg("unsubscribe_rate"),
        "bounce_rate": wavg("bounce_rate"),
        "conversion_value": total_rev,
        "revenue_per_recipient": total_rev / total_rec if total_rec else None,
    }


def red_flags(account: dict) -> list[dict]:
    flags = []
    industry = account.get("industry", "default")
    ind_benchmarks = BENCHMARKS.get(industry, BENCHMARKS["default"])
    for f in account.get("flows", []):
        name = strip_emoji(f["flow_name"])
        cat = flow_category(name)
        s = f["statistics"]
        g_open = grade("open", s.get("open_rate"), cat, industry)
        g_conv = grade("conv", s.get("conversion_rate"), cat, industry)
        if g_open == "bad":
            bench = benchmark_open(cat, industry)
            actual = s.get("open_rate") or 0
            gap = round((bench - actual) / bench * 100) if bench else 0
            flags.append({
                "name": name,
                "metric": "open",
                "actual": pct(actual),
                "bench": round(bench * 100),
                "gap": max(0, gap),
            })
        elif g_conv == "bad" and cat != "sunset":
            cat_b = ind_benchmarks.get(cat, ind_benchmarks["default"])
            bench_conv = cat_b["conv"]["good"]
            actual_conv = s.get("conversion_rate") or 0
            gap = round((bench_conv - actual_conv) / bench_conv * 100) if bench_conv else 0
            flags.append({
                "name": name,
                "metric": "conv",
                "actual": pct(actual_conv),
                "bench": round(bench_conv * 100),
                "gap": max(0, gap),
            })
    return flags


# ── additional API calls ──────────────────────────────────────────────────────

def fetch_message_names(client: KlaviyoClient, msg_ids: list[str]) -> dict[str, str]:
    """Fetch display names for a list of flow message IDs.

    Handles 429 rate limits with retry (up to 3 attempts per ID).
    Falls back to the raw msg_id only on persistent failure.
    """
    names: dict[str, str] = {}
    for mid in msg_ids:
        if not mid:
            continue
        for attempt in range(4):
            try:
                resp = client._http.get(
                    f"/flow-messages/{mid}/",
                    params={"fields[flow-message]": "name"},
                )
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", "2"))
                    time.sleep(wait)
                    continue  # retry
                if resp.status_code in (200, 201):
                    data = resp.json().get("data", {})
                    name = data.get("attributes", {}).get("name")
                    names[mid] = name if name else mid
                else:
                    names[mid] = mid
                break  # done — success or non-retryable
            except Exception:
                names[mid] = mid
                break
    return names


def fetch_campaigns(client: KlaviyoClient) -> list[dict]:
    """Fetch all email campaigns sorted by created_at desc.

    GET /campaigns/?filter=equals(messages.channel,"email")
         &fields[campaign]=name,status,send_time&sort=-created_at
    """
    campaigns: list[dict] = []
    url: str | None = "/campaigns/"
    params = {
        "filter": 'equals(messages.channel,"email")',
        "fields[campaign]": "name,status,send_time",
        "sort": "-created_at",
    }
    first = True
    while url:
        try:
            if first:
                r = client._http.get(url, params=params)
                first = False
            else:
                # Klaviyo next URLs are absolute: https://a.klaviyo.com/api/campaigns/?...
                # Strip the base so httpx doesn't double-prefix /api
                next_path = url.replace("https://a.klaviyo.com/api", "")
                r = client._http.get(next_path)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", "2")))
                continue
            r.raise_for_status()
            body = r.json()
            for item in body.get("data", []):
                attrs = item.get("attributes", {})
                campaigns.append({
                    "id": item["id"],
                    "name": attrs.get("name", item["id"]),
                    "status": attrs.get("status", ""),
                    "send_time": attrs.get("send_time"),
                })
            url = body.get("links", {}).get("next")
        except Exception as e:
            console.print(f"[yellow]  campaigns fetch error: {e}[/yellow]")
            break
    return campaigns


def fetch_campaign_report(
    client: KlaviyoClient,
    metric_id: str,
    timeframe_key: str,
) -> dict[str, dict]:
    """POST /campaign-values-reports/ and return dict[campaign_id → statistics]."""
    body = {
        "data": {
            "type": "campaign-values-report",
            "attributes": {
                "statistics": [
                    "open_rate", "click_rate", "conversion_value",
                    "delivered", "recipients", "conversion_rate",
                    "unsubscribe_rate", "bounce_rate", "spam_complaint_rate",
                ],
                "conversion_metric_id": metric_id,
                "timeframe": {"key": timeframe_key},
            },
        }
    }
    try:
        r = client._http.post("/campaign-values-reports/", json=body)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")))
            r = client._http.post("/campaign-values-reports/", json=body)
        r.raise_for_status()
        results = (
            r.json()
            .get("data", {})
            .get("attributes", {})
            .get("results", [])
        )
        return {
            row["groupings"]["campaign_id"]: row["statistics"]
            for row in results
            if row.get("groupings", {}).get("campaign_id")
        }
    except Exception:
        return {}


def fetch_forms_report(client: KlaviyoClient, timeframe_key: str) -> list[dict]:
    """Fetch signup form list + performance stats.
    Returns list of {form_id, name, status, submit_rate, views, submits}
    """
    # Step 1: list forms
    forms = []
    try:
        r = client._http.get("/forms/", params={"fields[form]": "name,status,ab_test"})
        r.raise_for_status()
        for item in r.json().get("data", []):
            forms.append({"id": item["id"], "name": item.get("attributes", {}).get("name", item["id"]), "status": item.get("attributes", {}).get("status", "")})
    except Exception:
        return []
    if not forms:
        return []

    # Step 2: form values report
    # Valid statistics: viewed_form, viewed_form_uniques, submits, submit_rate,
    #   closed_form, qualified_form, etc.  (NOT "views" — that is invalid)
    stats_map = {}
    try:
        body = {"data": {"type": "form-values-report", "attributes": {
            "statistics": ["viewed_form", "viewed_form_uniques", "submits", "submit_rate"],
            "timeframe": {"key": timeframe_key},
            "group_by": ["form_id"],
        }}}
        r = client._http.post("/form-values-reports/", json=body)
        if r.status_code in (200, 201):
            for row in r.json().get("data", {}).get("attributes", {}).get("results", []):
                fid = row.get("groupings", {}).get("form_id")
                if fid:
                    stats_map[fid] = row.get("statistics", {})
    except Exception:
        pass

    out = []
    for f in forms:
        s = stats_map.get(f["id"], {})
        out.append({
            "form_id": f["id"],
            "name": f["name"],
            "status": f["status"],
            "views": int(s.get("viewed_form") or 0),
            "unique_views": int(s.get("viewed_form_uniques") or 0),
            "submits": int(s.get("submits") or 0),
            "submit_rate": s.get("submit_rate"),
        })
    out.sort(key=lambda x: -(x["submits"] or 0))
    return out


def generate_action_steps(account: dict) -> list[dict]:
    """Generate prioritised action items from the analysed data."""
    steps = []
    slug = account.get("slug", "")
    name = account.get("name", slug)

    # From flow red flags
    for flag in account.get("_flags", []):
        if flag["metric"] == "open":
            steps.append({
                "priority": "high",
                "category": "flows",
                "title": f"[{name}] Fix low open rate in '{flag['name']}'",
                "description": f"Open rate {flag['actual']}% is {flag['gap']}% below benchmark ({flag['bench']}%). Review subject lines, preview text, send timing, and sender name.",
                "tags": ["email", "flows", "open-rate"],
            })
        elif flag["metric"] == "conv":
            steps.append({
                "priority": "high",
                "category": "flows",
                "title": f"[{name}] Fix low conversion in '{flag['name']}'",
                "description": f"Conversion rate {flag['actual']}% is {flag['gap']}% below benchmark ({flag['bench']}%). Review CTA, offer, and email copy.",
                "tags": ["email", "flows", "conversion"],
            })

    # Flows with high bounce rate
    for f in account.get("flows", []):
        s = f.get("statistics", {})
        bounce = s.get("bounce_rate") or 0
        if bounce > 0.02:
            steps.append({
                "priority": "medium",
                "category": "deliverability",
                "title": f"[{name}] High bounce rate in '{strip_emoji(f['flow_name'])}'",
                "description": f"Bounce rate {round(bounce*100,1)}% exceeds 2% threshold. Consider list cleaning and suppression.",
                "tags": ["deliverability", "bounce"],
            })

    # Flows with high unsub rate
    for f in account.get("flows", []):
        s = f.get("statistics", {})
        unsub = s.get("unsubscribe_rate") or 0
        if unsub > 0.005:
            steps.append({
                "priority": "medium",
                "category": "deliverability",
                "title": f"[{name}] High unsubscribe rate in '{strip_emoji(f['flow_name'])}'",
                "description": f"Unsub rate {round(unsub*100,1)}% exceeds 0.5% threshold. Review email frequency and content relevance.",
                "tags": ["deliverability", "unsubscribe"],
            })

    # Low-performing popups (submit rate < 2%)
    for form in account.get("forms", []):
        sr = form.get("submit_rate") or 0
        if form.get("views", 0) > 100 and sr < 0.02:
            steps.append({
                "priority": "medium",
                "category": "popups",
                "title": f"[{name}] Improve popup submit rate for '{form['name']}'",
                "description": f"Submit rate {round(sr*100,1)}% with {form['views']} views. A/B test the offer, timing, and copy.",
                "tags": ["popup", "conversion"],
            })

    # Deduplicate by title
    seen = set()
    unique = []
    for s in steps:
        if s["title"] not in seen:
            seen.add(s["title"])
            unique.append(s)

    return unique


# ── data fetching ─────────────────────────────────────────────────────────────

def fetch_exchange_rates() -> dict:
    """Fetch today's exchange rates with CZK as base (1 CZK = x foreign currency).
    Returns dict {USD: czk_per_usd, EUR: czk_per_eur, PLN: czk_per_pln, ...}
    Falls back to hardcoded approximate rates on failure.
    """
    import urllib.request as _ur
    FALLBACK = {"EUR": 25.2, "USD": 23.1, "PLN": 5.7, "GBP": 29.5, "HUF": 0.063}
    try:
        url = "https://api.frankfurter.app/latest?base=CZK&symbols=EUR,USD,PLN,GBP,HUF"
        with _ur.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        foreign_per_czk = data.get("rates", {})
        # We want CZK per foreign unit: czk_per_X = 1 / foreign_per_czk[X]
        rates = {k: round(1.0 / v, 4) for k, v in foreign_per_czk.items() if v}
        rates["CZK"] = 1.0
        return rates
    except Exception:
        return {**FALLBACK, "CZK": 1.0}


def fetch_account_data(acc: Account, timeframe: str) -> dict:
    """Fetch all data for one account and return the normalized dict."""
    try:
        with KlaviyoClient(acc.api_key) as kl:
            account_info = kl.get_account()
            account_name = (
                account_info.get("contact_information", {}).get("organization_name")
                or acc.display_name
            )
            currency = account_info.get("preferred_currency", "")
            timezone = account_info.get("timezone", "UTC")

            metric_id = kl.placed_order_metric_id()
            if not metric_id:
                return {
                    "slug": acc.slug,
                    "name": account_name,
                    "currency": currency,
                    "locale": acc.locale,
                    "status": acc.status,
                    "note": acc.notes,
                    "industry": acc.industry,
                    "flows_total": 0,
                    "flows_live": 0,
                    "total_revenue": 0,
                    "error": "No Placed Order metric found",
                    "flows": [],
                    "campaigns": [],
                    "forms": [],
                    "deliverability": {},
                    "action_steps": [],
                }

            # --- flows ---
            all_flows = list(kl.list_flows())
            live_flows = [
                f for f in all_flows
                if f.get("attributes", {}).get("status") == "live"
            ]
            flow_name_map = {
                f["id"]: f.get("attributes", {}).get("name", f["id"])
                for f in all_flows
            }
            live_id_set = {f["id"] for f in live_flows}

            flow_messages_raw: list[dict] = []
            if live_flows:
                flow_messages_raw = kl.flow_report(
                    conversion_metric_id=metric_id,
                    timeframe_key=timeframe,
                )

            # collect all message IDs for name lookup
            msgs_by_flow: dict[str, list] = defaultdict(list)
            for m in flow_messages_raw:
                fid = m.get("groupings", {}).get("flow_id", "")
                if fid in live_id_set:
                    msgs_by_flow[fid].append(m)

            all_msg_ids = [
                m.get("groupings", {}).get("flow_message_id", "")
                for m in flow_messages_raw
                if m.get("groupings", {}).get("flow_message_id")
            ]
            msg_name_map = fetch_message_names(kl, list(set(all_msg_ids)))

            # build flow objects
            flow_agg: list[dict] = []
            for fid, msgs in msgs_by_flow.items():
                message_list = []
                for m in sorted(
                    msgs,
                    key=lambda x: -(x["statistics"].get("conversion_value") or 0),
                ):
                    mid = m.get("groupings", {}).get("flow_message_id", "?")
                    message_list.append({
                        "msg_id": mid,
                        "msg_name": msg_name_map.get(mid, mid),
                        "statistics": m["statistics"],
                    })
                flow_agg.append({
                    "flow_id": fid,
                    "flow_name": strip_emoji(flow_name_map.get(fid, fid)),
                    "statistics": aggregate_flow(msgs),
                    "messages": message_list,
                })

            # flows with no messages in report period
            for f in live_flows:
                if f["id"] not in msgs_by_flow:
                    flow_agg.append({
                        "flow_id": f["id"],
                        "flow_name": strip_emoji(flow_name_map.get(f["id"], f["id"])),
                        "statistics": {},
                        "messages": [],
                    })

            flow_agg.sort(key=lambda x: -(x["statistics"].get("conversion_value") or 0))
            total_rev = sum(
                f["statistics"].get("conversion_value") or 0 for f in flow_agg
            )

            # --- campaigns ---
            campaigns_raw = fetch_campaigns(kl)
            campaign_stats = fetch_campaign_report(kl, metric_id, timeframe)

            campaigns_out: list[dict] = []
            for c in campaigns_raw:
                stats = campaign_stats.get(c["id"])
                campaigns_out.append({
                    "id": c["id"],
                    "name": c["name"],
                    "status": c["status"],
                    "send_time": c["send_time"],
                    "statistics": stats,  # None if not in report
                })

            # sort by send_time desc
            def _sort_key(c):
                t = c.get("send_time") or ""
                return t

            campaigns_out.sort(key=_sort_key, reverse=True)

            # --- forms ---
            forms_data = fetch_forms_report(kl, timeframe)

            # --- deliverability: campaign-based, same timeframe as rest of report ---
            # Uses campaign sends (same as Klaviyo deliverability page) — campaigns only,
            # not flows. The selected timeframe applies here too for consistency.
            camp_with_stats = [c["statistics"] for c in campaigns_out if c.get("statistics")]
            if camp_with_stats:
                total_del = sum(s.get("delivered") or 0 for s in camp_with_stats)
                def _cwavg(key):
                    if not total_del: return None
                    return sum((s.get(key) or 0) * (s.get("delivered") or 0) for s in camp_with_stats) / total_del
                deliverability = {
                    "open_rate": _cwavg("open_rate"),
                    "click_rate": _cwavg("click_rate"),
                    "bounce_rate": _cwavg("bounce_rate"),
                    "unsubscribe_rate": _cwavg("unsubscribe_rate"),
                    "spam_complaint_rate": _cwavg("spam_complaint_rate"),
                    "total_delivered": total_del,
                    "source": "campaigns",
                }
            else:
                deliverability = {}

        account_out = {
            "slug": acc.slug,
            "name": account_name,
            "currency": currency,
            "locale": acc.locale,
            "status": acc.status,
            "note": acc.notes,
            "industry": acc.industry,
            "flows_total": len(all_flows),
            "flows_live": len(live_flows),
            "total_revenue": total_rev,
            "error": None,
            "flows": flow_agg,
            "campaigns": campaigns_out,
            "forms": forms_data,
            "deliverability": deliverability,
            "action_steps": [],
        }
        account_out["_flags"] = red_flags(account_out)
        account_out["action_steps"] = generate_action_steps(account_out)
        del account_out["_flags"]
        return account_out

    except Exception as exc:
        return {
            "slug": acc.slug,
            "name": acc.display_name,
            "currency": "",
            "locale": acc.locale,
            "status": acc.status,
            "note": acc.notes,
            "industry": acc.industry,
            "flows_total": 0,
            "flows_live": 0,
            "total_revenue": 0,
            "error": str(exc),
            "flows": [],
            "campaigns": [],
            "forms": [],
            "deliverability": {},
            "action_steps": [],
        }


# ── HTML/SPA generation ───────────────────────────────────────────────────────

def _benchmarks_js_json() -> str:
    """Serialize BENCHMARKS into a JS-friendly JSON dict keyed by industry→category→metric.
    Returns a JSON string: { industry: { category: { open: {good, warn}, ctr: {...}, conv: {...} } } }
    """
    from lib.benchmarks import BENCHMARKS
    out: dict = {}
    for industry, cats in BENCHMARKS.items():
        out[industry] = {}
        for cat, metrics in cats.items():
            out[industry][cat] = {
                "open": metrics["open"],
                "click": metrics["ctr"],
                "conv": metrics["conv"],
            }
    return json.dumps(out, ensure_ascii=False)


def build_html(accounts_data: list[dict], timeframe: str, generated_at: str, gh_repo: str | None = None) -> str:
    """Build the complete self-contained SPA HTML string."""
    # Annotate red flags and action steps into data so they're available client-side
    for acc in accounts_data:
        acc["_flags"] = red_flags(acc)
        if not acc.get("action_steps"):
            acc["action_steps"] = generate_action_steps(acc)

    exchange_rates = fetch_exchange_rates()

    data_json = json.dumps(
        {"accounts": accounts_data, "timeframe": timeframe, "generated_at": generated_at,
         "exchange_rates": exchange_rates,
         "gh_repo": gh_repo or ""},
        ensure_ascii=False,
    ).replace("</script>", "<\\/script>")

    benchmarks_json = _benchmarks_js_json().replace("</script>", "<\\/script>")

    # Build the template (defined inline above) and substitute placeholders
    html = _HTML_TEMPLATE.replace("{data_json}", data_json).replace("{benchmarks_json}", benchmarks_json)
    return html


# Store the template HTML with placeholder tokens
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Klaviyo Report — Digismoothie</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #ffffff; --bg2: #f5f5f3; --bg3: #ececea;
  --text: #1a1a18; --text2: #6b6b68; --text3: #9b9b97;
  --border: rgba(0,0,0,0.1); --border2: rgba(0,0,0,0.18);
  --green: #1D9E75; --green-bg: #EAF3DE; --green-text: #3B6D11;
  --amber: #BA7517; --amber-bg: #FAEEDA; --amber-text: #854F0B;
  --blue: #378ADD; --blue-bg: #E6F1FB; --blue-text: #185FA5;
  --red: #E24B4A; --red-bg: #FCEBEB; --red-text: #A32D2D;
  --purple: #7F77DD; --purple-bg: #EEEDFE; --purple-text: #3C3489;
  --radius: 8px; --radius-lg: 12px;
  --sidebar: 220px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1c1c1a; --bg2: #252523; --bg3: #2e2e2b;
    --text: #f0f0ed; --text2: #a0a09c; --text3: #6b6b67;
    --border: rgba(255,255,255,0.08); --border2: rgba(255,255,255,0.16);
    --green-bg: #0a2e1f; --green-text: #6dd4a8;
    --amber-bg: #2e1f05; --amber-text: #f0b55a;
    --blue-bg: #0a1e33; --blue-text: #7ab8f0;
    --red-bg: #2e0a0a; --red-text: #f07878;
    --purple-bg: #1a1830; --purple-text: #a9a4f5;
  }
}
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: var(--bg3); color: var(--text); font-size: 14px; line-height: 1.5;
}
.layout { display: flex; min-height: 100vh; }

/* Sidebar */
.sidebar {
  width: var(--sidebar); flex-shrink: 0; background: var(--bg);
  border-right: 0.5px solid var(--border); position: sticky;
  top: 0; height: 100vh; overflow-y: auto; display: flex; flex-direction: column;
}
.sidebar-head { padding: 18px 16px 12px; border-bottom: 0.5px solid var(--border); }
.sidebar-logo { font-size: 14px; font-weight: 700; letter-spacing: -0.01em; }
.sidebar-sub { font-size: 11px; color: var(--text2); margin-top: 2px; }
.nav-section { padding: 6px 0 2px; }
.nav-section-label {
  font-size: 10px; color: var(--text3); padding: 8px 16px 3px;
  text-transform: uppercase; letter-spacing: .06em;
}
.nav-item {
  display: flex; align-items: center; gap: 8px; padding: 7px 14px;
  font-size: 12.5px; color: var(--text2); text-decoration: none;
  border-left: 2px solid transparent; cursor: pointer; transition: all .1s;
}
.nav-item:hover { background: var(--bg2); color: var(--text); }
.nav-item.active { border-left-color: var(--blue); color: var(--text); background: var(--bg2); font-weight: 500; }
.nav-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.dot-green { background: var(--green); }
.dot-amber { background: var(--amber); }
.dot-red { background: var(--red); }
.dot-gray { background: var(--text3); }
.dot-blue { background: var(--blue); }
.nav-label-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.nav-flag {
  font-size: 10px; background: var(--red-bg); color: var(--red-text);
  padding: 1px 6px; border-radius: 999px; font-weight: 600; flex-shrink: 0;
}
.sidebar-foot {
  margin-top: auto; padding: 10px 14px; border-top: 0.5px solid var(--border);
  font-size: 11px; color: var(--text3); line-height: 1.6;
}

/* Main */
.main { flex: 1; min-width: 0; }
.topbar {
  background: var(--bg); border-bottom: 0.5px solid var(--border);
  padding: 12px 24px; position: sticky; top: 0; z-index: 10;
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
}
.topbar-left { min-width: 0; }
.topbar-title { font-size: 15px; font-weight: 600; }
.topbar-meta { font-size: 11px; color: var(--text2); margin-top: 1px; }
.topbar-right { display: flex; gap: 20px; flex-shrink: 0; }
.metric-mini { text-align: right; }
.metric-mini-val { display: block; font-size: 17px; font-weight: 600; line-height: 1.1; }
.metric-mini-lbl { font-size: 11px; color: var(--text3); }

/* Content */
.content { padding: 20px 24px 40px; }

/* Cards */
.cards { display: flex; gap: 14px; margin-bottom: 20px; flex-wrap: wrap; }
.card {
  background: var(--bg); border: 0.5px solid var(--border);
  border-radius: var(--radius-lg); padding: 14px 18px; flex: 1; min-width: 140px;
}
.card-val { font-size: 26px; font-weight: 700; line-height: 1.1; }
.card-lbl { font-size: 12px; color: var(--text2); margin-top: 4px; }
.card-sub { font-size: 11px; color: var(--text3); margin-top: 2px; }

/* Badges */
.badge {
  display: inline-block; font-size: 11px; padding: 2px 8px;
  border-radius: 999px; font-weight: 500; flex-shrink: 0;
}
.badge-green { background: var(--green-bg); color: var(--green-text); }
.badge-amber { background: var(--amber-bg); color: var(--amber-text); }
.badge-blue { background: var(--blue-bg); color: var(--blue-text); }
.badge-red { background: var(--red-bg); color: var(--red-text); }
.badge-gray { background: var(--bg3); color: var(--text2); }

/* Section card */
.section-card {
  background: var(--bg); border: 0.5px solid var(--border);
  border-radius: var(--radius-lg); overflow: hidden;
}
.section-card-head {
  font-size: 12.5px; font-weight: 500; padding: 12px 16px;
  border-bottom: 0.5px solid var(--border); color: var(--text2);
}

/* Overview table */
.ov-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.ov-table th {
  text-align: left; color: var(--text3); font-weight: 400; font-size: 11px;
  text-transform: uppercase; letter-spacing: .04em; padding: 8px 14px;
  border-bottom: 0.5px solid var(--border);
}
.ov-table th.num { text-align: right; }
.ov-table td { padding: 10px 14px; border-bottom: 0.5px solid var(--border); vertical-align: middle; }
.ov-table tr:last-child td { border-bottom: none; }
.ov-table tbody tr { cursor: pointer; transition: background .1s; }
.ov-table tbody tr:hover td { background: var(--bg2); }
.ov-name-cell { display: flex; align-items: center; gap: 8px; }
.num { text-align: right; }
.red-num { color: var(--red); font-weight: 600; }
.green-num { color: var(--green); font-weight: 500; }
.muted { color: var(--text3); }

/* Account header */
.acct-header {
  background: var(--bg); border: 0.5px solid var(--border);
  border-radius: var(--radius-lg); padding: 16px 20px; margin-bottom: 0;
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 16px; flex-wrap: wrap;
  border-bottom: none;
  border-radius: var(--radius-lg) var(--radius-lg) 0 0;
}
.acct-title-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 4px; }
.acct-name { font-size: 18px; font-weight: 700; }
.acct-note { font-size: 12px; color: var(--text2); margin-top: 4px; }

/* Tabs */
.tabs-bar {
  display: flex; gap: 0;
  background: var(--bg);
  border: 0.5px solid var(--border);
  border-top: none;
  border-bottom: none;
  overflow: hidden;
}
.tab-btn {
  padding: 11px 18px; font-size: 13px; font-weight: 500; cursor: pointer;
  border: none; background: none; color: var(--text2);
  border-bottom: 2px solid transparent; transition: all .1s;
}
.tab-btn:hover { color: var(--text); background: var(--bg2); }
.tab-btn.active { color: var(--blue); border-bottom-color: var(--blue); }
.tab-content {
  background: var(--bg); border: 0.5px solid var(--border);
  border-top: 0.5px solid var(--border); border-radius: 0 0 var(--radius-lg) var(--radius-lg);
  overflow: hidden;
}
.tab-pane { display: none; }
.tab-pane.active { display: block; }

/* Alerts */
.flags-banner {
  padding: 12px 18px; background: var(--red-bg);
  border-bottom: 0.5px solid var(--border);
}
.flags-banner-title { font-size: 12px; font-weight: 600; color: var(--red-text); margin-bottom: 6px; }
.flag-item { font-size: 12.5px; color: var(--red-text); padding: 2px 0; }
.ok-banner {
  padding: 10px 18px; font-size: 13px; color: var(--green-text);
  background: var(--green-bg); border-bottom: 0.5px solid var(--border);
}
.error-banner {
  padding: 14px 18px; font-size: 13px; color: var(--amber-text); background: var(--amber-bg);
}
.empty-msg { padding: 24px 18px; color: var(--text3); font-size: 13px; text-align: center; }

/* Flow table */
.flow-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.flow-table th {
  text-align: left; color: var(--text3); font-weight: 400; font-size: 11px;
  text-transform: uppercase; letter-spacing: .04em; padding: 8px 14px;
  border-bottom: 0.5px solid var(--border);
}
.flow-table th.num { text-align: right; }
.flow-table td { padding: 9px 14px; border-bottom: 0.5px solid var(--border); color: var(--text); }
.flow-table tr:last-child td { border-bottom: none; }
.flow-table tbody tr.clickable { cursor: pointer; transition: background .1s; }
.flow-table tbody tr.clickable:hover td { background: var(--bg2); }
.flow-name-cell { display: flex; align-items: center; gap: 6px; }
.flow-name { flex: 1; }
.bench { font-size: 10px; color: var(--text3); margin-left: 4px; }
.rev { font-weight: 500; color: var(--green); }
.grade-green { color: var(--green); font-weight: 500; }
.grade-amber { color: var(--amber); font-weight: 500; }
.grade-red { color: var(--red); font-weight: 600; }

/* Campaign table */
.camp-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.camp-table th {
  text-align: left; color: var(--text3); font-weight: 400; font-size: 11px;
  text-transform: uppercase; letter-spacing: .04em; padding: 8px 14px;
  border-bottom: 0.5px solid var(--border);
}
.camp-table th.num { text-align: right; }
.camp-table td { padding: 9px 14px; border-bottom: 0.5px solid var(--border); }
.camp-table tr:last-child td { border-bottom: none; }
.camp-name { max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* Deliverability */
.deliv-cards { display: flex; gap: 14px; padding: 16px 18px; border-bottom: 0.5px solid var(--border); flex-wrap: wrap; }
.deliv-card {
  background: var(--bg2); border: 0.5px solid var(--border);
  border-radius: var(--radius); padding: 12px 16px; flex: 1; min-width: 120px;
}
.deliv-card-val { font-size: 22px; font-weight: 600; line-height: 1.1; }
.deliv-card-lbl { font-size: 11px; color: var(--text2); margin-top: 3px; }
.deliv-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.deliv-table th {
  text-align: left; color: var(--text3); font-weight: 400; font-size: 11px;
  text-transform: uppercase; letter-spacing: .04em; padding: 8px 14px;
  border-bottom: 0.5px solid var(--border);
}
.deliv-table th.num { text-align: right; }
.deliv-table td { padding: 9px 14px; border-bottom: 0.5px solid var(--border); }
.deliv-table tr:last-child td { border-bottom: none; }
.insights-box {
  padding: 14px 18px; border-top: 0.5px solid var(--border);
  background: var(--bg2); font-size: 12.5px; color: var(--text2); line-height: 1.7;
}
.insights-box strong { color: var(--text); }

/* Deliverability score card */
.deliv-score-row { display: flex; gap: 16px; padding: 16px 18px; border-bottom: 0.5px solid var(--border); align-items: flex-start; flex-wrap: wrap; }
.deliv-score-circle { width: 80px; height: 80px; border-radius: 50%; display: flex; align-items: center; justify-content: center; flex-direction: column; flex-shrink: 0; border: 4px solid var(--green); }
.deliv-score-num { font-size: 24px; font-weight: 700; line-height: 1; }
.deliv-score-lbl { font-size: 10px; color: var(--text2); margin-top: 2px; }
.deliv-metrics-grid { flex: 1; display: flex; flex-direction: column; gap: 6px; min-width: 260px; }
.deliv-metric-row { display: flex; align-items: center; gap: 10px; font-size: 13px; }
.deliv-metric-icon { width: 18px; height: 18px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 10px; flex-shrink: 0; }
.icon-green { background: var(--green-bg); color: var(--green); }
.icon-amber { background: var(--amber-bg); color: var(--amber); }
.icon-red { background: var(--red-bg); color: var(--red); }
.deliv-metric-name { width: 130px; color: var(--text2); }
.deliv-metric-val { font-weight: 500; min-width: 50px; }
.deliv-metric-rec { font-size: 11px; color: var(--text3); }

/* Popup table */
.popup-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.popup-table th { text-align: left; color: var(--text3); font-weight: 400; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; padding: 8px 14px; border-bottom: 0.5px solid var(--border); }
.popup-table th.num { text-align: right; }
.popup-table td { padding: 9px 14px; border-bottom: 0.5px solid var(--border); }
.popup-table tr:last-child td { border-bottom: none; }
.submit-bar-wrap { display: flex; align-items: center; gap: 8px; }
.submit-bar { height: 6px; border-radius: 3px; background: var(--green); min-width: 2px; }

/* Action steps */
.action-steps-section { margin-top: 16px; }
.action-steps-header { display: flex; align-items: center; justify-content: space-between; padding: 12px 18px; border-bottom: 0.5px solid var(--border); }
.action-steps-title { font-size: 13px; font-weight: 600; }
.sync-btn { font-size: 12px; font-weight: 500; padding: 6px 14px; border-radius: var(--radius); border: 0.5px solid var(--border2); background: var(--bg); color: var(--text); cursor: pointer; transition: all .1s; display: flex; align-items: center; gap: 6px; }
.sync-btn:hover { background: var(--blue); border-color: var(--blue); color: #fff; }
.sync-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.step-item { display: flex; align-items: flex-start; gap: 12px; padding: 10px 18px; border-bottom: 0.5px solid var(--border); }
.step-item:last-child { border-bottom: none; }
.step-priority { font-size: 10px; font-weight: 600; padding: 2px 7px; border-radius: 999px; flex-shrink: 0; margin-top: 2px; }
.priority-high { background: var(--red-bg); color: var(--red-text); }
.priority-medium { background: var(--amber-bg); color: var(--amber-text); }
.priority-low { background: var(--blue-bg); color: var(--blue-text); }
.step-body { flex: 1; }
.step-title { font-size: 13px; font-weight: 500; margin-bottom: 2px; }
.step-desc { font-size: 12px; color: var(--text2); line-height: 1.5; }
.step-tags { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 4px; }
.step-tag { font-size: 10px; background: var(--bg3); color: var(--text3); padding: 1px 7px; border-radius: 999px; }
.sync-result { padding: 10px 18px; font-size: 12.5px; border-top: 0.5px solid var(--border); display: none; }
.sync-result.success { background: var(--green-bg); color: var(--green-text); }
.sync-result.error { background: var(--red-bg); color: var(--red-text); }
.no-steps { padding: 20px 18px; color: var(--text3); font-size: 13px; text-align: center; }

/* Timeframe + refresh controls */
.topbar-controls { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.flow-expand-icon {
  display: inline-block; width: 14px; font-size: 10px;
  color: var(--text3); margin-right: 4px; transition: transform 0.15s;
}
.flow-detail-row td { background: var(--bg2); }
.flow-detail-table {
  width: 100%; border-collapse: collapse; font-size: 12px;
}
.flow-detail-table th {
  padding: 6px 10px; text-align: left; font-size: 11px;
  color: var(--text2); font-weight: 500; border-bottom: 1px solid var(--bg3);
  background: var(--bg2);
}
.flow-detail-msg td {
  padding: 6px 10px; border-bottom: 1px solid var(--bg3);
  color: var(--text2); font-size: 12px;
}
.flow-detail-msg:last-child td { border-bottom: none; }
.flow-detail-msg .msg-num {
  color: var(--text3); width: 28px; font-size: 11px;
}

.tf-select {
  font-size: 12px; padding: 5px 10px; border-radius: var(--radius);
  border: 0.5px solid var(--border2); background: var(--bg2); color: var(--text);
  cursor: pointer; outline: none; transition: border-color .1s;
}
.tf-select:hover { border-color: var(--blue); }
.tf-select:focus { border-color: var(--blue); }
.refresh-btn { font-size: 12px; padding: 6px 12px; border-radius: var(--radius); border: 0.5px solid var(--border2); background: var(--bg); color: var(--text2); cursor: pointer; transition: all .1s; display: flex; align-items: center; gap: 5px; }
.refresh-btn:hover { background: var(--blue); border-color: var(--blue); color: #fff; }
.refresh-btn.spinning { opacity: 0.6; cursor: not-allowed; }

/* Modal */
.modal-backdrop {
  display: none; position: fixed; inset: 0; z-index: 100;
  background: rgba(0,0,0,0.45); backdrop-filter: blur(3px);
  align-items: center; justify-content: center;
}
.modal-backdrop.open { display: flex; }
.modal-card {
  background: var(--bg); border-radius: var(--radius-lg);
  border: 0.5px solid var(--border2); box-shadow: 0 24px 60px rgba(0,0,0,0.2);
  width: 90vw; max-width: 860px; max-height: 85vh;
  display: flex; flex-direction: column; overflow: hidden;
}
.modal-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 20px; border-bottom: 0.5px solid var(--border); flex-shrink: 0;
}
.modal-title { font-size: 14px; font-weight: 600; }
.modal-close {
  background: none; border: 0.5px solid var(--border2); border-radius: var(--radius);
  width: 28px; height: 28px; cursor: pointer; font-size: 16px; color: var(--text2);
  display: flex; align-items: center; justify-content: center; transition: all .1s;
}
.modal-close:hover { background: var(--bg2); color: var(--text); }
.modal-body { overflow-y: auto; flex: 1; }
.modal-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.modal-table th {
  text-align: left; color: var(--text3); font-weight: 400; font-size: 11px;
  text-transform: uppercase; letter-spacing: .04em; padding: 8px 16px;
  border-bottom: 0.5px solid var(--border); position: sticky; top: 0; background: var(--bg);
}
.modal-table th.num { text-align: right; }
.modal-table td { padding: 9px 16px; border-bottom: 0.5px solid var(--border); }
.modal-table tr:last-child td { border-bottom: none; }
.modal-table tbody tr:hover td { background: var(--bg2); }
.msg-num { color: var(--text3); width: 30px; }

/* View toggle */
.view { display: none; }
.view.active { display: block; }

/* ── Password protection ──────────────────────────────────────────── */
#pw-overlay {
  position: fixed; inset: 0; background: var(--bg);
  display: flex; align-items: center; justify-content: center;
  z-index: 9999; flex-direction: column; gap: 16px;
}
#pw-overlay.hidden { display: none; }
.pw-box {
  background: var(--bg2); border: 1px solid var(--bg3);
  border-radius: 12px; padding: 40px 48px; text-align: center;
  display: flex; flex-direction: column; gap: 16px; min-width: 320px;
}
.pw-logo { font-size: 22px; font-weight: 700; color: var(--text); }
.pw-sub { font-size: 13px; color: var(--text2); }
.pw-input {
  padding: 10px 14px; border-radius: 8px; border: 1px solid var(--bg3);
  background: var(--bg); color: var(--text); font-size: 15px;
  outline: none; text-align: center; letter-spacing: 2px;
}
.pw-input:focus { border-color: var(--blue); }
.pw-btn {
  padding: 10px; border-radius: 8px; background: var(--blue);
  color: #fff; border: none; font-size: 14px; font-weight: 600;
  cursor: pointer;
}
.pw-btn:hover { opacity: 0.9; }
.pw-error { color: var(--red); font-size: 13px; min-height: 18px; }

</style>
</head>
<body>

<div id="pw-overlay">
  <div class="pw-box">
    <div class="pw-logo">Digismoothie</div>
    <div class="pw-sub">Klaviyo Agency Report</div>
    <input class="pw-input" id="pw-input" type="password" placeholder="Heslo" autofocus
      onkeydown="if(event.key==='Enter') checkPw()">
    <button class="pw-btn" onclick="checkPw()">Vstoupit</button>
    <div class="pw-error" id="pw-error"></div>
  </div>
</div>

<div class="layout">

  <aside class="sidebar" id="sidebar">
    <div class="sidebar-head">
      <div class="sidebar-logo">Digismoothie</div>
      <div class="sidebar-sub">Klaviyo Report</div>
    </div>
    <nav class="nav-section">
      <div class="nav-section-label">Overview</div>
      <div class="nav-item" id="nav-overview" onclick="showView('overview')">
        <span class="nav-dot dot-blue"></span>
        <span class="nav-label-text">All Accounts</span>
      </div>
    </nav>
    <nav class="nav-section" id="nav-accounts">
      <div class="nav-section-label">Accounts</div>
    </nav>
    <div class="sidebar-foot" id="sidebar-foot"></div>
  </aside>

  <div class="main">
    <div class="topbar">
      <div class="topbar-left">
        <div class="topbar-title" id="topbar-title">Klaviyo Agency Report</div>
        <div class="topbar-meta" id="topbar-meta"></div>
      </div>
      <div class="topbar-right" id="topbar-right">
        <div class="topbar-controls">
          <select class="tf-select" id="tf-select" title="Timeframe" onchange="window._currentTf = this.value; refreshReport(this.value);">
            <option value="last_7_days">7 dní</option>
            <option value="last_30_days" selected>30 dní</option>
            <option value="last_90_days">90 dní</option>
            <option value="last_365_days">365 dní</option>
          </select>
          <button class="refresh-btn" id="refresh-btn" onclick="refreshReport()" title="Načíst znovu s vybraným timeframem">
            &#x21BB; Refresh
          </button>
        </div>
      </div>
    </div>

    <div class="content">
      <div class="view active" id="view-overview">
        <div class="cards" id="overview-cards"></div>
        <div class="section-card">
          <div class="section-card-head">All Accounts</div>
          <table class="ov-table">
            <thead>
              <tr>
                <th>Account</th>
                <th>Status</th>
                <th class="num">Live flows</th>
                <th class="num">Red flags</th>
                <th class="num">Revenue</th>
                <th>Note</th>
              </tr>
            </thead>
            <tbody id="overview-tbody"></tbody>
          </table>
        </div>
      </div>

      <div class="view" id="view-account">
        <div class="acct-header" id="acct-header"></div>
        <div class="tabs-bar" id="tabs-bar">
          <button class="tab-btn active" onclick="showTab('flows')">Flows</button>
          <button class="tab-btn" onclick="showTab('campaigns')">Campaigns</button>
          <button class="tab-btn" onclick="showTab('deliverability')">Deliverability</button>
          <button class="tab-btn" onclick="showTab('popups')">Popups</button>
          <button class="tab-btn" onclick="showTab('action-steps')">Action Steps</button>
        </div>
        <div class="tab-content">
          <div class="tab-pane active" id="tab-flows"></div>
          <div class="tab-pane" id="tab-campaigns"></div>
          <div class="tab-pane" id="tab-deliverability"></div>
          <div class="tab-pane" id="tab-popups"></div>
          <div class="tab-pane" id="tab-action-steps"></div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="modal-backdrop" onclick="closeModalBackdrop(event)">
  <div class="modal-card">
    <div class="modal-header">
      <div class="modal-title" id="modal-title"></div>
      <button class="modal-close" onclick="closeModal()">&#x2715;</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<script>
const DATA = {data_json};
let _currentTf = DATA.timeframe || 'last_30_days';

// ── Utility ───────────────────────────────────────────────────────────────────

function pct(v) { return (v == null) ? '—' : (v * 100).toFixed(1) + ' %'; }
function num(v) { return (v == null) ? '—' : Math.round(v).toLocaleString('cs-CZ'); }
function rev(v) { return (v == null || v === 0) ? '—' : Math.round(v).toLocaleString('cs-CZ'); }
function dateStr(s) {
  if (!s) return '—';
  try { return new Date(s).toLocaleDateString('en-GB', {day:'2-digit',month:'short',year:'numeric'}); } catch(e) { return s; }
}

const BENCHMARKS = {benchmarks_json};

function gradeClass(metric, value, category, industry) {
  if (value == null) return '';
  const ind = BENCHMARKS[industry] || BENCHMARKS['default'];
  const cat = (ind && ind[category]) ? ind[category] : (ind && ind['default']);
  const t = cat ? cat[metric] : null;
  if (!t) return '';
  if (value >= t.good) return 'grade-green';
  if (value >= t.warn) return 'grade-amber';
  return 'grade-red';
}

function flowCategory(name) {
  const n = (name || '').toLowerCase();
  if (n.includes('welcome')) return 'welcome';
  if (n.includes('checkout')) return 'checkout_abandon';
  if (n.includes('cart')) return 'cart_abandon';
  if ((n.includes('post') && (n.includes('purchase') || n.includes('order'))) || n.includes('postpurchase')) return 'post_purchase';
  if (n.includes('browse')) return 'browse_abandon';
  if (n.includes('sunset')) return 'sunset';
  if (n.includes('winback') || n.includes('win-back')) return 'winback';
  return 'default';
}

function benchOpen(category, industry) {
  const ind = BENCHMARKS[industry] || BENCHMARKS['default'];
  const cat = (ind && ind[category]) ? ind[category] : (ind && ind['default']);
  return cat ? cat.open.good : 0.25;
}

function badge(status) {
  const map = {active:'green',setup:'amber',progress:'blue',sent:'green',draft:'gray',cancelled:'red',scheduled:'blue'};
  const lbl = {active:'Active',setup:'Setup',progress:'In progress',sent:'Sent',draft:'Draft',cancelled:'Cancelled',scheduled:'Scheduled'};
  const s = (status || '').toLowerCase();
  const c = map[s] || 'gray';
  const l = lbl[s] || status || '—';
  return `<span class="badge badge-${c}">${l}</span>`;
}

function dotColor(acc) {
  if (acc.error && !acc.flows.length) return 'red';
  if ((acc._flags || []).length > 0) return 'amber';
  return 'green';
}

// ── State ─────────────────────────────────────────────────────────────────────

let currentView = 'overview';
let currentAccSlug = null;
let currentTab = 'flows';

// ── Routing ───────────────────────────────────────────────────────────────────

function showView(view, slug) {
  document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));

  currentView = view;
  if (view === 'overview') {
    document.getElementById('view-overview').classList.add('active');
    document.getElementById('nav-overview').classList.add('active');
    document.getElementById('topbar-title').textContent = 'Klaviyo Agency Report';
    renderTopbarOverview();
  } else if (view === 'account' && slug) {
    currentAccSlug = slug;
    document.getElementById('view-account').classList.add('active');
    const navEl = document.getElementById('nav-acct-' + slug);
    if (navEl) navEl.classList.add('active');
    renderAccountView(slug);
    // reset to flows tab
    showTab('flows', true);
  }
}

function showTab(tab, skipReset) {
  currentTab = tab;
  const tabNames = ['flows', 'campaigns', 'deliverability', 'popups', 'action-steps'];
  document.querySelectorAll('.tab-btn').forEach((b, i) => {
    b.classList.toggle('active', tabNames[i] === tab);
  });
  document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
}

// ── Sidebar ───────────────────────────────────────────────────────────────────

function renderSidebar() {
  const nav = document.getElementById('nav-accounts');
  const items = DATA.accounts.map(acc => {
    const dot = dotColor(acc);
    const flags = (acc._flags || []).length;
    const flagBadge = flags > 0 ? `<span class="nav-flag">${flags}</span>` : '';
    return `<div class="nav-item" id="nav-acct-${acc.slug}" onclick="showView('account','${acc.slug}')">
      <span class="nav-dot dot-${dot}"></span>
      <span class="nav-label-text">${acc.name}</span>
      ${flagBadge}
    </div>`;
  }).join('');
  nav.innerHTML = '<div class="nav-section-label">Accounts</div>' + items;

  document.getElementById('sidebar-foot').innerHTML =
    `${DATA.generated_at}<br>Timeframe: ${DATA.timeframe}`;
}

// ── Topbar ────────────────────────────────────────────────────────────────────

function renderTopbarOverview() {
  const connected = DATA.accounts.filter(a => !a.error || a.flows.length).length;
  const liveFlows = DATA.accounts.reduce((s, a) => s + (a.flows_live || 0), 0);
  const totalFlags = DATA.accounts.reduce((s, a) => s + (a._flags || []).length, 0);
  document.getElementById('topbar-right').innerHTML = `
    <div class="metric-mini"><span class="metric-mini-val">${connected}/${DATA.accounts.length}</span><span class="metric-mini-lbl">Connected</span></div>
    <div class="metric-mini"><span class="metric-mini-val">${liveFlows}</span><span class="metric-mini-lbl">Live flows</span></div>
    <div class="metric-mini"><span class="metric-mini-val" style="color:var(--red)">${totalFlags}</span><span class="metric-mini-lbl">Red flags</span></div>
    <div class="topbar-controls">
      <select class="tf-select" id="tf-select" title="Timeframe" onchange="window._currentTf = this.value; refreshReport(this.value);">
        <option value="last_7_days">7 dní</option>
        <option value="last_30_days" selected>30 dní</option>
        <option value="last_90_days">90 dní</option>
        <option value="last_365_days">365 dní</option>
      </select>
      <button class="refresh-btn" id="refresh-btn" onclick="refreshReport()" title="Refresh">&#x21BB; Refresh</button>
    </div>
  `;
  const _tfSel = document.getElementById('tf-select');
  if (_tfSel) _tfSel.value = _currentTf;
  document.getElementById('topbar-meta').textContent = DATA.generated_at + ' · ' + DATA.timeframe;
}

// ── Overview ──────────────────────────────────────────────────────────────────

function renderOverview() {
  const connected = DATA.accounts.filter(a => !a.error || a.flows.length).length;
  const liveFlows = DATA.accounts.reduce((s, a) => s + (a.flows_live || 0), 0);
  const totalFlags = DATA.accounts.reduce((s, a) => s + (a._flags || []).length, 0);
  // Convert all revenues to CZK for aggregation
  const totalRevCzk = DATA.accounts.reduce((s, a) => {
    if (!a.total_revenue) return s;
    const r = DATA.exchange_rates?.[a.currency] || (a.currency === 'CZK' ? 1 : null);
    return s + (r ? a.total_revenue * r : 0);
  }, 0);

  document.getElementById('overview-cards').innerHTML = `
    <div class="card"><div class="card-val">${connected}</div><div class="card-lbl">Connected</div><div class="card-sub">of ${DATA.accounts.length} accounts</div></div>
    <div class="card"><div class="card-val">${liveFlows}</div><div class="card-lbl">Live flows</div><div class="card-sub">across all accounts</div></div>
    <div class="card"><div class="card-val" style="color:var(--red)">${totalFlags}</div><div class="card-lbl">Red flags</div><div class="card-sub">flows below benchmark</div></div>
    <div class="card"><div class="card-val" style="color:var(--green)">${num(totalRevCzk)} Kč</div><div class="card-lbl">Celkový revenue</div><div class="card-sub">přepočteno na CZK</div></div>
  `;

  document.getElementById('overview-tbody').innerHTML = DATA.accounts.map(acc => {
    const dot = dotColor(acc);
    const flags = (acc._flags || []).length;
    const flagCell = flags > 0
      ? `<span class="red-num">${flags}</span>`
      : `<span class="green-num">0</span>`;
    const rate = DATA.exchange_rates?.[acc.currency] || (acc.currency === 'CZK' ? 1 : null);
    const revCzkAmt = (acc.total_revenue && rate) ? acc.total_revenue * rate : null;
    const revOrigLabel = (acc.currency && acc.currency !== 'CZK' && acc.total_revenue)
      ? ` <span style="color:var(--text3);font-size:11px">(${rev(acc.total_revenue)}\xa0${acc.currency})</span>` : '';
    const revStr = revCzkAmt
      ? `<span style="color:var(--green)">${rev(revCzkAmt)}\xa0Kč</span>${revOrigLabel}`
      : (acc.total_revenue ? rev(acc.total_revenue) + '\xa0' + acc.currency : '—');
    return `<tr onclick="showView('account','${acc.slug}')">
      <td><div class="ov-name-cell"><span class="nav-dot dot-${dot}"></span><strong>${acc.name}</strong></div></td>
      <td>${badge(acc.status)}</td>
      <td class="num">${acc.flows_live || 0}</td>
      <td class="num">${flagCell}</td>
      <td class="num"><span class="rev">${revStr}</span></td>
      <td style="font-size:12px;color:var(--text2)">${acc.note || ''}</td>
    </tr>`;
  }).join('');
}

// ── Account view ──────────────────────────────────────────────────────────────

function renderAccountView(slug) {
  const acc = DATA.accounts.find(a => a.slug === slug);
  if (!acc) return;

  const flags = acc._flags || [];
  const flagCount = flags.length;

  document.getElementById('topbar-title').textContent = acc.name;
  document.getElementById('topbar-meta').textContent = DATA.generated_at + ' · ' + DATA.timeframe;
  document.getElementById('topbar-right').innerHTML = `
    <div class="metric-mini"><span class="metric-mini-val">${acc.flows_live || 0}/${acc.flows_total || 0}</span><span class="metric-mini-lbl">Live / total flows</span></div>
    <div class="metric-mini"><span class="metric-mini-val" style="color:${flagCount > 0 ? 'var(--red)' : 'var(--green)'}">${flagCount}</span><span class="metric-mini-lbl">Red flags</span></div>
    <div class="metric-mini"><span class="metric-mini-val" style="color:var(--green)">${rev(acc.total_revenue)}</span><span class="metric-mini-lbl">Rev (${acc.currency})</span></div>
    <div class="topbar-controls">
      <select class="tf-select" id="tf-select" title="Timeframe" onchange="window._currentTf = this.value; refreshReport(this.value);">
        <option value="last_7_days">7 dní</option>
        <option value="last_30_days" selected>30 dní</option>
        <option value="last_90_days">90 dní</option>
        <option value="last_365_days">365 dní</option>
      </select>
      <button class="refresh-btn" id="refresh-btn" onclick="refreshReport()" title="Refresh">&#x21BB; Refresh</button>
    </div>
  `;
  const _tfSel = document.getElementById('tf-select');
  if (_tfSel) _tfSel.value = _currentTf;

  document.getElementById('acct-header').innerHTML = `
    <div>
      <div class="acct-title-row">
        <span class="acct-name">${acc.name}</span>
        ${badge(acc.status)}
      </div>
      ${acc.note ? `<div class="acct-note">${acc.note}</div>` : ''}
    </div>
  `;

  renderFlowsTab(acc);
  renderCampaignsTab(acc);
  renderDelivTab(acc);
  renderPopupsTab(acc);
  renderActionStepsTab(acc);
}

// ── Flows tab ─────────────────────────────────────────────────────────────────

function renderFlowsTab(acc) {
  const el = document.getElementById('tab-flows');
  const flows = acc.flows || [];
  const flags = acc._flags || [];

  if (acc.error && !flows.length) {
    el.innerHTML = `<div class="error-banner">⚠ ${acc.error}</div>`;
    return;
  }

  let html = '';
  if (flags.length > 0) {
    const items = flags.map(f =>
      `<div class="flag-item">● <strong>${f.name}</strong> — ${f.metric} <strong>${f.actual != null ? f.actual.toFixed(1) : '—'} %</strong> vs benchmark ${f.bench} % (−${f.gap} %)</div>`
    ).join('');
    html += `<div class="flags-banner"><div class="flags-banner-title">Red Flags (${flags.length})</div>${items}</div>`;
  } else if (flows.length > 0) {
    html += `<div class="ok-banner">✓ All flows meet benchmarks.</div>`;
  }

  if (!flows.length) {
    html += `<div class="empty-msg">No live flows found for this account.</div>`;
    el.innerHTML = html;
    return;
  }

  const rows = flows.map(f => {
    const s = f.statistics || {};
    const cat = flowCategory(f.flow_name);
    const bench = benchOpen(cat, acc.industry);
    const gc = gradeClass('open', s.open_rate, cat, acc.industry);
    const openCell = s.open_rate != null
      ? `<span class="${gc}">${pct(s.open_rate)}</span><span class="bench">›${(bench * 100).toFixed(0)} %</span>`
      : `<span class="muted">—</span><span class="bench">›${(bench * 100).toFixed(0)} %</span>`;
    const hasMsgs = f.messages && f.messages.length > 0;
    const rid = `flow-${acc.slug}-${f.flow_id}`;
    let detailRow = '';
    if (hasMsgs) {
      const msgRows = f.messages.map((m, i) => {
        const ms = m.statistics || {};
        return `<tr class="flow-detail-msg">
          <td class="msg-num">${i + 1}</td>
          <td>${m.msg_name || m.msg_id}</td>
          <td class="num">${num(ms.recipients)}</td>
          <td class="num">${pct(ms.open_rate)}</td>
          <td class="num">${pct(ms.click_rate)}</td>
          <td class="num">${pct(ms.conversion_rate)}</td>
          <td class="num rev">${rev(ms.conversion_value)}</td>
        </tr>`;
      }).join('');
      detailRow = `<tr class="flow-detail-row" id="${rid}-detail" style="display:none">
        <td colspan="6" style="padding:0">
          <table class="flow-detail-table">
            <thead><tr>
              <th>#</th><th>Email</th>
              <th class="num">Recipients</th>
              <th class="num">Open</th>
              <th class="num">CTR</th>
              <th class="num">Conv</th>
              <th class="num">Revenue</th>
            </tr></thead>
            <tbody>${msgRows}</tbody>
          </table>
        </td>
      </tr>`;
    }
    return `<tr class="${hasMsgs ? 'clickable' : ''}" id="${rid}" ${hasMsgs ? `onclick="toggleFlowDetail('${rid}')"` : ''}>
      <td><div class="flow-name-cell"><span class="flow-expand-icon" id="${rid}-icon">${hasMsgs ? '▶' : ''}</span><span class="flow-name">${f.flow_name}</span></div></td>
      <td class="num">${num(s.recipients)}</td>
      <td class="num">${openCell}</td>
      <td class="num">${pct(s.click_rate)}</td>
      <td class="num">${pct(s.conversion_rate)}</td>
      <td class="num rev">${rev(s.conversion_value)}</td>
    </tr>${detailRow}`;
  }).join('');

  html += `<table class="flow-table">
    <thead><tr>
      <th>Flow</th>
      <th class="num">Recipients</th>
      <th class="num">Open rate</th>
      <th class="num">CTR</th>
      <th class="num">Conv rate</th>
      <th class="num">Revenue (${acc.currency})</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;

  el.innerHTML = html;
}

// ── Campaigns tab ─────────────────────────────────────────────────────────────

function renderCampaignsTab(acc) {
  const el = document.getElementById('tab-campaigns');
  const campaigns = acc.campaigns || [];

  if (!campaigns.length) {
    el.innerHTML = `<div class="empty-msg">No campaigns found for this account.</div>`;
    return;
  }

  const rows = campaigns.map(c => {
    const s = c.statistics;
    return `<tr>
      <td class="camp-name" title="${c.name}">${c.name}</td>
      <td>${badge((c.status || '').toLowerCase())}</td>
      <td>${dateStr(c.send_time)}</td>
      <td class="num">${s ? num(s.delivered) : '—'}</td>
      <td class="num">${s ? pct(s.open_rate) : '—'}</td>
      <td class="num">${s ? pct(s.click_rate) : '—'}</td>
      <td class="num">${s ? pct(s.conversion_rate) : '—'}</td>
      <td class="num rev">${s ? rev(s.conversion_value) : '—'}</td>
    </tr>`;
  }).join('');

  el.innerHTML = `<table class="camp-table">
    <thead><tr>
      <th>Campaign</th>
      <th>Status</th>
      <th>Send date</th>
      <th class="num">Delivered</th>
      <th class="num">Open %</th>
      <th class="num">CTR</th>
      <th class="num">Conv %</th>
      <th class="num">Revenue (${acc.currency})</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

// ── Deliverability tab ────────────────────────────────────────────────────────────────────────────

function renderDelivTab(acc) {
  const el = document.getElementById('tab-deliverability');
  const flows = (acc.flows || []).filter(f => f.statistics && (f.statistics.delivered || 0) > 0);

  // Use campaign-based deliverability stats (accurate) or fall back to flow-based
  const d = acc.deliverability || {};
  const hasCampDeliv = d.source === 'campaigns' && d.total_delivered > 0;
  const avgOpen   = hasCampDeliv ? d.open_rate   : null;
  const avgClick  = hasCampDeliv ? d.click_rate  : null;
  const avgBounce = hasCampDeliv ? d.bounce_rate : null;
  const avgUnsub  = hasCampDeliv ? d.unsubscribe_rate : null;
  const avgSpam   = hasCampDeliv ? d.spam_complaint_rate : null;
  const hasData   = hasCampDeliv || flows.length > 0;

  // Health score (campaign-based) — weighted 30+20+20+20+10 = 100 max
  let score = 0;
  if (avgOpen != null)   { score += avgOpen   > 0.33  ? 30 : avgOpen   > 0.20  ? 15 : 0; }
  if (avgClick != null)  { score += avgClick  > 0.015 ? 20 : avgClick  > 0.008 ? 10 : 0; }
  if (avgBounce != null) { score += avgBounce < 0.005 ? 20 : avgBounce < 0.01  ? 10 : 0; }
  if (avgUnsub != null)  { score += avgUnsub  < 0.002 ? 20 : avgUnsub  < 0.005 ? 10 : 0; }
  if (avgSpam != null)   { score += avgSpam   < 0.00005 ? 10 : avgSpam < 0.0001 ? 5 : 0; }
  const scoreColor = score >= 70 ? 'var(--green)' : score >= 40 ? 'var(--amber)' : 'var(--red)';

  function metricIcon(val, goodThresh, warnThresh, lowerIsBetter) {
    if (val == null) return '<div class="deliv-metric-icon icon-amber">?</div>';
    const good = lowerIsBetter ? val <= goodThresh : val >= goodThresh;
    const warn = lowerIsBetter ? val <= warnThresh : val >= warnThresh;
    if (good) return '<div class="deliv-metric-icon icon-green">✓</div>';
    if (warn) return '<div class="deliv-metric-icon icon-amber">!</div>';
    return '<div class="deliv-metric-icon icon-red">✗</div>';
  }

  const tfLabel = {'last_7_days':'7 dní','last_30_days':'30 dní','last_90_days':'90 dní','last_365_days':'365 dní'}[DATA.timeframe] || DATA.timeframe;
  const scoreSection = `<div style="padding:8px 18px 0;font-size:11px;color:var(--text3)">Kampaňová data · ${tfLabel} · ${hasCampDeliv ? num(d.total_delivered) + ' doručeno' : 'žádná data'}</div>
  <div class="deliv-score-row">
    <div style="display:flex;flex-direction:column;align-items:center;gap:4px;flex-shrink:0">
      <div class="deliv-score-circle" style="border-color:${scoreColor}">
        <span class="deliv-score-num" style="color:${scoreColor}">${hasData ? score : '&mdash;'}</span>
        <span class="deliv-score-lbl">/ 100</span>
      </div>
      <span style="font-size:9px;color:var(--text3);text-align:center;max-width:80px;line-height:1.3">DS Health Score<br>(not Klaviyo)</span>
    </div>
    <div class="deliv-metrics-grid">
      <div class="deliv-metric-row">
        ${metricIcon(avgOpen, 0.33, 0.20, false)}
        <span class="deliv-metric-name">Open rate</span>
        <span class="deliv-metric-val">${pct(avgOpen)}</span>
        <span class="deliv-metric-rec">rec &gt; 33.0 %</span>
      </div>
      <div class="deliv-metric-row">
        ${metricIcon(avgClick, 0.015, 0.008, false)}
        <span class="deliv-metric-name">Click rate</span>
        <span class="deliv-metric-val">${pct(avgClick)}</span>
        <span class="deliv-metric-rec">rec &gt; 1.2 %</span>
      </div>
      <div class="deliv-metric-row">
        ${metricIcon(avgBounce, 0.005, 0.01, true)}
        <span class="deliv-metric-name">Bounce rate</span>
        <span class="deliv-metric-val">${pct(avgBounce)}</span>
        <span class="deliv-metric-rec">rec &lt; 1.0 %</span>
      </div>
      <div class="deliv-metric-row">
        ${metricIcon(avgUnsub, 0.002, 0.005, true)}
        <span class="deliv-metric-name">Unsubscribe rate</span>
        <span class="deliv-metric-val">${pct(avgUnsub)}</span>
        <span class="deliv-metric-rec">rec &lt; 0.3 %</span>
      </div>
      <div class="deliv-metric-row">
        ${metricIcon(avgSpam, 0.00005, 0.0001, true)}
        <span class="deliv-metric-name">Spam complaint rate</span>
        <span class="deliv-metric-val">${avgSpam != null ? (avgSpam * 100).toFixed(3) + ' %' : '—'}</span>
        <span class="deliv-metric-rec">rec &lt; 0.01 %</span>
      </div>
    </div>
  </div>
  ${hasCampDeliv ? '' : '<div class="insights-box" style="border-bottom:0.5px solid var(--border)">Based on flow data — add campaigns to see full deliverability picture.</div>'}`;

  const tableRows = flows.map(f => {
    const s = f.statistics;
    const bc = (s.bounce_rate || 0) > 0.02 ? 'grade-red' : (s.bounce_rate || 0) > 0.01 ? 'grade-amber' : 'grade-green';
    const uc = (s.unsubscribe_rate || 0) > 0.005 ? 'grade-red' : (s.unsubscribe_rate || 0) > 0.002 ? 'grade-amber' : 'grade-green';
    return `<tr>
      <td>${f.flow_name}</td>
      <td class="num">${num(s.delivered)}</td>
      <td class="num ${bc}">${pct(s.bounce_rate)}</td>
      <td class="num ${uc}">${pct(s.unsubscribe_rate)}</td>
    </tr>`;
  }).join('');

  const table = flows.length
    ? `<table class="deliv-table">
      <thead><tr>
        <th>Flow</th>
        <th class="num">Delivered</th>
        <th class="num">Bounce rate</th>
        <th class="num">Unsub rate</th>
      </tr></thead>
      <tbody>${tableRows}</tbody>
    </table>`
    : `<div class="empty-msg">No flow data with deliveries found.</div>`;

  // Campaign health section (last 5 campaigns with stats)
  const recentCamps = (acc.campaigns || [])
    .filter(c => c.statistics && c.statistics.delivered)
    .slice(0, 5);
  let campSection = '';
  if (recentCamps.length) {
    const campRows = recentCamps.map(c => {
      const s = c.statistics;
      const bc = (s.bounce_rate || 0) > 0.02 ? 'grade-red' : (s.bounce_rate || 0) > 0.01 ? 'grade-amber' : 'grade-green';
      const uc = (s.unsubscribe_rate || 0) > 0.005 ? 'grade-red' : (s.unsubscribe_rate || 0) > 0.002 ? 'grade-amber' : 'grade-green';
      const sc = (s.spam_complaint_rate || 0) > 0.0001 ? 'grade-red' : 'grade-green';
      return `<tr>
        <td class="camp-name" title="${c.name}">${c.name}</td>
        <td class="num">${num(s.delivered)}</td>
        <td class="num">${pct(s.open_rate)}</td>
        <td class="num ${bc}">${pct(s.bounce_rate)}</td>
        <td class="num ${uc}">${pct(s.unsubscribe_rate)}</td>
        <td class="num ${sc}">${s.spam_complaint_rate != null ? (s.spam_complaint_rate * 100).toFixed(3) + ' %' : '&mdash;'}</td>
      </tr>`;
    }).join('');
    campSection = `<div style="padding:12px 18px 6px;font-size:12px;font-weight:600;color:var(--text2);border-top:0.5px solid var(--border)">Recent campaign health</div>
    <table class="deliv-table">
      <thead><tr>
        <th>Campaign</th>
        <th class="num">Delivered</th>
        <th class="num">Open %</th>
        <th class="num">Bounce %</th>
        <th class="num">Unsub %</th>
        <th class="num">Spam %</th>
      </tr></thead>
      <tbody>${campRows}</tbody>
    </table>`;
  }

  const locale = (acc.locale || '').toLowerCase();
  let insightHtml = '<strong>Thresholds:</strong> Bounce &gt; 2 % is critical; unsub &gt; 0.5 % warrants review.';
  if (locale === 'cs' || locale === 'sk') {
    insightHtml += '<br><strong>CZ/SK note:</strong> seznam.cz uses aggressive spam filters — high bounces from @seznam.cz addresses are common. Monitor domain-level bounce segmentation.';
  } else if (locale === 'pl') {
    insightHtml += '<br><strong>PL note:</strong> onet.pl and wp.pl have stricter filters. Ensure sending domain is authenticated (SPF/DKIM/DMARC) to reduce inbox placement issues.';
  }

  el.innerHTML = scoreSection + table + campSection + `<div class="insights-box">${insightHtml}</div>`;
}

// ── Popups tab ─────────────────────────────────────────────────────────────────────────────────

function renderPopupsTab(acc) {
  const el = document.getElementById('tab-popups');
  const forms = acc.forms || [];
  if (!forms.length) {
    el.innerHTML = `<div class="empty-msg">No signup forms found for this account.</div>`;
    return;
  }
  const maxViews = Math.max(...forms.map(f => f.views || 0), 1);
  const rows = forms.map(f => {
    const sr = f.submit_rate != null ? (f.submit_rate * 100).toFixed(1) : '&mdash;';
    const srClass = f.submit_rate == null ? 'muted' : f.submit_rate >= 0.03 ? 'grade-green' : f.submit_rate >= 0.01 ? 'grade-amber' : 'grade-red';
    const barW = Math.round(((f.views || 0) / maxViews) * 100);
    const barColor = f.submit_rate == null ? 'var(--text3)' : f.submit_rate >= 0.03 ? 'var(--green)' : f.submit_rate >= 0.01 ? 'var(--amber)' : 'var(--red)';
    const statusBadge = f.status === 'enabled' ? '<span class="badge badge-green">Live</span>' :
                        f.status === 'disabled' ? '<span class="badge badge-gray">Off</span>' :
                        `<span class="badge badge-amber">${f.status || '?'}</span>`;
    return `<tr>
      <td>${f.name}</td>
      <td>${statusBadge}</td>
      <td class="num">${(f.views || 0).toLocaleString('cs-CZ')}</td>
      <td class="num">${(f.unique_views || 0).toLocaleString('cs-CZ')}</td>
      <td class="num">${(f.submits || 0).toLocaleString('cs-CZ')}</td>
      <td class="num">
        <div class="submit-bar-wrap">
          <div class="submit-bar" style="width:${barW}px;max-width:60px;background:${barColor}"></div>
          <span class="${srClass}">${sr} %</span>
        </div>
      </td>
    </tr>`;
  }).join('');
  el.innerHTML = `<table class="popup-table">
    <thead><tr>
      <th>Form name</th><th>Status</th>
      <th class="num">Views</th><th class="num">Unique views</th><th class="num">Submits</th><th class="num">Submit rate</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>
  <div class="insights-box">Benchmark: submit rate &gt; 3 % is good, 1&ndash;3 % needs work, &lt; 1 % is critical.</div>`;
}

// ── Action Steps tab ────────────────────────────────────────────────────────────────────────

function renderActionStepsTab(acc) {
  const el = document.getElementById('tab-action-steps');
  const steps = acc.action_steps || [];
  const slug = acc.slug;
  let html = `<div class="action-steps-header">
    <div class="action-steps-title">Action Steps (${steps.length})</div>
    ${steps.length ? `<button class="sync-btn" id="sync-btn-${slug}" onclick="syncToClickUp('${slug}')">&#x2B06; Sync to ClickUp</button>` : ''}
  </div>`;
  if (!steps.length) {
    html += `<div class="no-steps">&#10003; No action items — this account looks healthy!</div>`;
  } else {
    steps.forEach(s => {
      const tags = (s.tags || []).map(t => `<span class="step-tag">${t}</span>`).join('');
      html += `<div class="step-item">
        <span class="step-priority priority-${s.priority}">${s.priority.toUpperCase()}</span>
        <div class="step-body">
          <div class="step-title">${s.title.replace(/^\[[^\]]+\]\s*/, '')}</div>
          <div class="step-desc">${s.description}</div>
          <div class="step-tags">${tags}</div>
        </div>
      </div>`;
    });
  }
  html += `<div class="sync-result" id="sync-result-${slug}"></div>`;
  el.innerHTML = html;
}

// ── ClickUp Sync ─────────────────────────────────────────────────────────────────────────────────────

async function syncToClickUp(slug) {
  const acc = DATA.accounts.find(a => a.slug === slug);
  if (!acc) return;
  const btn = document.getElementById(`sync-btn-${slug}`);
  const result = document.getElementById(`sync-result-${slug}`);
  if (btn) { btn.disabled = true; btn.textContent = 'Syncing…'; }
  try {
    const resp = await fetch('/api/sync-clickup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tasks: acc.action_steps || [] }),
    });
    const data = await resp.json();
    if (result) {
      result.style.display = 'block';
      if (data.ok) {
        result.className = 'sync-result success';
        const c = data.created?.length || 0;
        const s = data.skipped?.length || 0;
        result.textContent = `✓ Created ${c} task${c !== 1 ? 's' : ''}${s ? `, skipped ${s} duplicate${s !== 1 ? 's' : ''}` : ''}.`;
      } else {
        result.className = 'sync-result error';
        result.textContent = `✗ Error: ${data.error || 'Unknown error'}`;
      }
    }
  } catch (e) {
    if (result) { result.style.display = 'block'; result.className = 'sync-result error'; result.textContent = `✗ ${e.message}`; }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⬆ Sync to ClickUp'; }
  }
}

// ── Refresh ────────────────────────────────────────────────────────────────────────────────────────────

async function refreshReport(tf) {
  const timeframe = tf || _currentTf || 'last_30_days';
  // GitHub Pages / static mode: no backend server available
  if (DATA.gh_repo) {
    const url = `https://github.com/${DATA.gh_repo}/actions/workflows/report.yml`;
    window.open(url, '_blank');
    return;
  }
  const btn = document.getElementById('refresh-btn');
  const sel = document.getElementById('tf-select');
  if (btn) { btn.classList.add('spinning'); btn.textContent = '⏳ Načítám…'; btn.disabled = true; }
  if (sel) sel.disabled = true;
  try {
    const resp = await fetch('/api/refresh?timeframe=' + timeframe);
    const data = await resp.json();
    if (data.ok) {
      if (btn) btn.textContent = '✓ Hotovo…';
      setTimeout(() => location.reload(), 600);
    } else {
      alert('Refresh failed: ' + data.error);
      if (btn) { btn.textContent = '↻ Refresh'; btn.classList.remove('spinning'); btn.disabled = false; }
      if (sel) sel.disabled = false;
    }
  } catch (e) {
    alert('Refresh error: ' + e.message);
    if (btn) { btn.textContent = '↻ Refresh'; btn.classList.remove('spinning'); btn.disabled = false; }
    if (sel) sel.disabled = false;
  }
}

// ── Modal ─────────────────────────────────────────────────────────────────────

function toggleFlowDetail(rid) {
  const detail = document.getElementById(rid + '-detail');
  const icon = document.getElementById(rid + '-icon');
  if (!detail) return;
  const open = detail.style.display !== 'none';
  detail.style.display = open ? 'none' : 'table-row';
  if (icon) icon.textContent = open ? '▶' : '▼';
}

function openModal(slug, flowId) {
  const acc = DATA.accounts.find(a => a.slug === slug);
  if (!acc) return;
  const flow = acc.flows.find(f => f.flow_id === flowId);
  if (!flow) return;

  document.getElementById('modal-title').textContent = flow.flow_name;

  const msgs = flow.messages || [];
  const rows = msgs.map((m, i) => {
    const s = m.statistics || {};
    return `<tr>
      <td class="msg-num">${i + 1}</td>
      <td>${m.msg_name || m.msg_id}</td>
      <td class="num">${num(s.recipients)}</td>
      <td class="num">${pct(s.open_rate)}</td>
      <td class="num">${pct(s.click_rate)}</td>
      <td class="num">${pct(s.conversion_rate)}</td>
      <td class="num rev">${rev(s.conversion_value)}</td>
    </tr>`;
  }).join('');

  document.getElementById('modal-body').innerHTML = `<table class="modal-table">
    <thead><tr>
      <th>#</th>
      <th>Message name</th>
      <th class="num">Recipients</th>
      <th class="num">Open</th>
      <th class="num">CTR</th>
      <th class="num">Conv</th>
      <th class="num">Revenue (${acc.currency})</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;

  document.getElementById('modal-backdrop').classList.add('open');
}

function closeModal() {
  document.getElementById('modal-backdrop').classList.remove('open');
}

function closeModalBackdrop(e) {
  if (e.target === document.getElementById('modal-backdrop')) closeModal();
}

document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// ── Init ──────────────────────────────────────────────────────────────────────

renderSidebar();
renderOverview();
renderTopbarOverview();
showView('overview');

// Pre-select the timeframe that was used to generate this report
(function() {
  const sel = document.getElementById('tf-select');
  if (sel && DATA.timeframe) sel.value = DATA.timeframe;
  if (DATA.timeframe) _currentTf = DATA.timeframe;
})();

// ── Password protection ───────────────────────────────────────────────────────
const PW_HASH = '086a7e747c433e97d9c5b0fd20ba9bacc6638dc74474038da88b5da22aa58025';
async function hashPw(pw) {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(pw));
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2,'0')).join('');
}
async function checkPw() {
  const val = document.getElementById('pw-input').value;
  const h = await hashPw(val);
  if (h === PW_HASH) {
    localStorage.setItem('ds_auth', '1');
    document.getElementById('pw-overlay').classList.add('hidden');
  } else {
    document.getElementById('pw-error').textContent = 'Špatné heslo';
    document.getElementById('pw-input').value = '';
    document.getElementById('pw-input').focus();
  }
}
if (localStorage.getItem('ds_auth') === '1') {
  document.getElementById('pw-overlay').classList.add('hidden');
}
</script>
</body>
</html>"""


# ── main ──────────────────────────────────────────────────────────────────────

@click.command()
@click.option(
    "--timeframe",
    default="last_30_days",
    help="Timeframe key: last_30_days | last_90_days | last_365_days",
)
@click.option(
    "--out",
    default=None,
    help="Output file path (default: outputs/report_YYYY-MM-DD.html)",
)
@click.option(
    "--gh-repo",
    default=None,
    envvar="GH_REPO",
    help="GitHub repo slug (owner/repo) — embeds GitHub Actions link in static HTML",
)
def main(timeframe: str, out: str | None, gh_repo: str | None) -> None:
    reg = load_registry()
    accounts_data: list[dict] = []
    generated_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    with Progress(console=console) as bar:
        task = bar.add_task("Fetching data", total=len(reg.accounts))
        for acc in reg.accounts:
            data = fetch_account_data(acc, timeframe)
            accounts_data.append(data)
            if data.get("error") and not data.get("flows"):
                console.print(f"[red]✗[/red] {acc.slug}: {data['error']}")
            else:
                n_flows = len(data.get("flows", []))
                n_flags = len(red_flags(data))
                n_camps = len(data.get("campaigns", []))
                console.print(
                    f"[green]✓[/green] {acc.slug}: "
                    f"{n_flows} flows · {n_flags} flags · {n_camps} campaigns"
                )
            bar.advance(task)

    html = build_html(accounts_data, timeframe, generated_at, gh_repo=gh_repo)

    out_path = (
        Path(out) if out
        else ROOT / "outputs" / f"report_{dt.date.today().isoformat()}.html"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    console.print(f"\n[bold green]Report generated:[/bold green] {out_path}")
    console.print(f'Open in browser: [cyan]open "{out_path}"[/cyan]')


if __name__ == "__main__":
    main()
