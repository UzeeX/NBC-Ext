
# app.py
# Streamlit app: National Bank Financial (NBFWM) advisor directory extractor (with Province)
# Run: streamlit run app.py
#
# requirements.txt:
# streamlit
# requests
# beautifulsoup4
# pandas
# openpyxl

import re
import time
import json
from io import BytesIO
from collections import deque
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
import streamlit as st

# Optional Excel support
try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    OPENPYXL_OK = True
except Exception:
    OPENPYXL_OK = False


# ----------------------------- UI / Theme -----------------------------

st.set_page_config(page_title="NBFWM Advisor Extractor (QC)", layout="wide")

st.markdown(
    """
<style>
.block-container { max-width: 1180px; padding-top: 2rem; padding-bottom: 3rem; }
h1, h2, h3 { letter-spacing: -0.02em; }
hr { margin: 1.4rem 0; }
.card {
  border: 1px solid rgba(0,0,0,0.10);
  background: rgba(255,255,255,0.70);
  border-radius: 18px;
  padding: 16px 18px;
  box-shadow: 0 6px 18px rgba(0,0,0,0.06);
}
.small-muted { color: rgba(0,0,0,0.55); font-size: 0.92rem; }
</style>
""",
    unsafe_allow_html=True,
)

st.title("NBFWM Wealth Advisor Directory Extractor")
st.caption("Extracts publicly listed advisor info from nbfwm.ca and exports CSV (optional Excel).")


# ----------------------------- Constants / Regex -----------------------------

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; InovestorDirectoryExtractor/1.1; +https://inovestor.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

BASE_DEFAULT = "https://www.nbfwm.ca"
SEED_PATH_DEFAULT = "/advisor.html"

# Typical NBFWM advisor profiles:
# /advisor/<team-slug>/(our-team|notre-equipe)/<advisor-slug>.html
ADVISOR_HREF_RE = re.compile(r"^/advisor/.+/(our-team|notre-equipe)/.+\.html$", re.I)

EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"\b(?:1[-\s]?)?\(?\d{3}\)?[-\s]?\d{3}[-\s]?\d{4}\b")

# Province extraction
PROV_RE = re.compile(r"\b(BC|AB|SK|MB|ON|QC|NB|NS|PE|NL|NT|NU|YT)\b", re.I)
FULL_PROV_MAP = {
    "quebec": "QC", "québec": "QC",
    "ontario": "ON",
    "new brunswick": "NB", "nouveau-brunswick": "NB",
    "nova scotia": "NS", "nouvelle-écosse": "NS",
    "prince edward island": "PE", "île-du-prince-édouard": "PE",
    "newfoundland and labrador": "NL", "terre-neuve-et-labrador": "NL",
    "manitoba": "MB",
    "saskatchewan": "SK",
    "alberta": "AB",
    "british columbia": "BC", "colombie-britannique": "BC",
    "northwest territories": "NT", "territoires du nord-ouest": "NT",
    "nunavut": "NU",
    "yukon": "YT",
}


# ----------------------------- Helpers -----------------------------

def normalize_phone(p: str) -> str:
    digits = re.sub(r"\D+", "", p or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"
    return (p or "").strip()


def extract_team_name_from_slug(team_slug: str) -> str:
    if not team_slug:
        return ""
    return team_slug.replace("-", " ").strip().title()


def extract_province(address_text: str) -> str:
    if not address_text:
        return ""
    m = PROV_RE.search(address_text)
    if m:
        return m.group(1).upper()

    t = address_text.strip().lower()
    for k, v in FULL_PROV_MAP.items():
        if k in t:
            return v
    return ""


def safe_get(session: requests.Session, url: str, delay_s: float, timeout: int = 30) -> str:
    r = session.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    r.raise_for_status()
    if delay_s and delay_s > 0:
        time.sleep(delay_s)
    return r.text


def same_domain(url: str, base: str) -> bool:
    return urlparse(url).netloc.lower() == urlparse(base).netloc.lower()


def extract_advisor_urls_from_html(html: str, base: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = set()

    # anchors
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if ADVISOR_HREF_RE.match(href):
            links.add(urljoin(base, href))

    # fallback regex scan (sometimes links are embedded)
    for m in re.finditer(r'"/advisor/[^"]+/(?:our-team|notre-equipe)/[^"]+\.html"', html, flags=re.I):
        href = m.group(0).strip('"')
        links.add(urljoin(base, href))

    return sorted(links)


def extract_internal_html_pages_from_html(html: str, page_url: str, base: str) -> list[str]:
    """
    For optional crawling: collect additional internal .html pages under /advisor/
    that might contain more advisor links (team pages, city pages, etc.).
    """
    soup = BeautifulSoup(html, "html.parser")
    found = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full = urljoin(page_url, href)
        if not same_domain(full, base):
            continue
        p = urlparse(full).path.lower()
        if p.startswith("/advisor/") and p.endswith(".html"):
            found.add(full)

    # also scan raw HTML for /advisor/...html
    for m in re.finditer(r'"/advisor/[^"]+\.html"', html, flags=re.I):
        href = m.group(0).strip('"')
        found.add(urljoin(base, href))

    return sorted(found)


def extract_address_hint(soup: BeautifulSoup) -> str:
    """
    Best-effort: address can appear via locator link text, JSON-LD, or nearby text.
    """
    # 1) Locator link visible text
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        if "locator" in href or "locator.nbc.ca" in href:
            txt = a.get_text(" ", strip=True)
            if txt:
                return txt

    # 2) JSON-LD (schema.org)
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text(strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
            # could be dict or list
            items = data if isinstance(data, list) else [data]
            for it in items:
                if isinstance(it, dict) and "address" in it:
                    addr = it.get("address")
                    if isinstance(addr, dict):
                        parts = [
                            addr.get("streetAddress", ""),
                            addr.get("addressLocality", ""),
                            addr.get("addressRegion", ""),
                            addr.get("postalCode", ""),
                        ]
                        hint = ", ".join([p for p in parts if p])
                        if hint:
                            return hint
        except Exception:
            pass

    # 3) fallback: find a short line containing a province code
    text_lines = [ln.strip() for ln in soup.get_text("\n", strip=True).split("\n") if ln.strip()]
    for ln in text_lines:
        if PROV_RE.search(ln):
            # keep it short
            return ln[:180]

    return ""


def parse_advisor_page(html: str, url: str, base: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # Name
    name = ""
    h1 = soup.find("h1")
    if h1:
        name = h1.get_text(" ", strip=True)
    if not name and soup.title:
        name = soup.title.get_text(" ", strip=True).split("|")[0].strip()

    # Email (prefer mailto)
    email = ""
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            email = href.split(":", 1)[1].split("?", 1)[0].strip()
            break
    if not email:
        all_text = soup.get_text("\n", strip=True)
        emails = sorted(set(EMAIL_RE.findall(all_text)))
        email = emails[0] if emails else ""

    # Phone (prefer tel)
    phones = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("tel:"):
            phones.append(normalize_phone(href.split(":", 1)[1]))
    if not phones:
        all_text = soup.get_text("\n", strip=True)
        phones = [normalize_phone(p) for p in PHONE_RE.findall(all_text)]
    phones = [p for p in pd.unique(phones) if p]
    phone = " | ".join(list(phones)[:3])

    # Address & province
    address_hint = extract_address_hint(soup)
    province = extract_province(address_hint)

    # Team slug (fallback team name)
    team_name = ""
    try:
        parts = urlparse(url).path.strip("/").split("/")
        team_slug = parts[1] if len(parts) > 1 and parts[0].lower() == "advisor" else ""
        team_name = extract_team_name_from_slug(team_slug)
    except Exception:
        team_name = ""

    return {
        "name": (name or "").strip(),
        "email": (email or "").strip(),
        "phone": (phone or "").strip(),
        "team_name": (team_name or "").strip(),
        "province": (province or "").strip(),
        "address_hint": (address_hint or "").strip(),
        "profile_url": url,
    }


def dedupe_rows(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["email_norm"] = df["email"].fillna("").str.lower()
    df["name_norm"] = df["name"].fillna("").str.lower()
    df["phone_norm"] = df["phone"].fillna("").str.replace(r"\s+", "", regex=True)

    has_email = df["email_norm"] != ""
    df_email = df[has_email].drop_duplicates(subset=["email_norm"], keep="first")
    df_no_email = df[~has_email].drop_duplicates(subset=["name_norm", "phone_norm"], keep="first")

    out = pd.concat([df_email, df_no_email], ignore_index=True)
    return out.drop(columns=["email_norm", "name_norm", "phone_norm"], errors="ignore")


def df_to_excel_bytes(df: pd.DataFrame) -> bytes:
    if not OPENPYXL_OK:
        raise RuntimeError("openpyxl not installed")

    wb = Workbook()
    ws = wb.active
    ws.title = "Advisors"

    header_font = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="F2F2F2")
    align = Alignment(vertical="center", wrap_text=False)

    # Alternating team fills
    fills = [
        PatternFill("solid", fgColor="FFFFFF"),
        PatternFill("solid", fgColor="F7FBFF"),
    ]

    # Write header
    ws.append(list(df.columns))
    for c in range(1, ws.max_column + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = align

    # Write rows
    for r in df.itertuples(index=False):
        ws.append(list(r))

    # Alternate by team changes
    team_col = None
    if "team_name" in df.columns:
        team_col = df.columns.get_loc("team_name") + 1

    current_team = None
    fill_idx = 0
    for row_i in range(2, ws.max_row + 1):
        if team_col:
            team_val = ws.cell(row=row_i, column=team_col).value
            if team_val != current_team:
                current_team = team_val
                fill_idx = 1 - fill_idx
        for col_i in range(1, ws.max_column + 1):
            cell = ws.cell(row=row_i, column=col_i)
            cell.fill = fills[fill_idx]
            cell.alignment = align

    # Autofit widths
    for col_cells in ws.columns:
        max_len = 0
        col_letter = col_cells[0].column_letter
        for cell in col_cells:
            val = "" if cell.value is None else str(cell.value)
            if len(val) > max_len:
                max_len = len(val)
        ws.column_dimensions[col_letter].width = min(max(10, max_len + 2), 60)

    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()


# ----------------------------- UI Controls -----------------------------

st.markdown('<div class="card">', unsafe_allow_html=True)

c1, c2, c3, c4 = st.columns([1.2, 1.2, 1.2, 1.2])

with c1:
    base_url = st.text_input("Base URL", value=BASE_DEFAULT)
    seed_path = st.text_input("Seed path", value=SEED_PATH_DEFAULT)

with c2:
    qc_only = st.toggle("Québec only (province=QC)", value=True)
    city_contains = st.text_input("City filter (optional)", placeholder="e.g., Montréal, Québec, Laval")

with c3:
    polite_delay = st.slider("Polite delay (seconds)", 0.0, 1.5, 0.25, 0.05)
    max_profiles = st.number_input("Max profiles (0 = no limit)", min_value=0, max_value=50000, value=0, step=100)

with c4:
    deep_crawl = st.toggle("Deep crawl advisor pages (find more links)", value=False,
                           help="If the seed page doesn't contain all links, this crawls more /advisor/*.html pages to discover additional profiles.")
    crawl_page_limit = st.number_input("Crawl page limit", min_value=10, max_value=5000, value=250, step=50, disabled=not deep_crawl)
    include_profile_url = st.toggle("Include profile URL column", value=False)
    include_address_hint = st.toggle("Include address hint column", value=False)
    do_excel = st.toggle("Also generate Excel (.xlsx)", value=False, disabled=not OPENPYXL_OK)

run = st.button("Run Extraction", use_container_width=True)

st.markdown('</div>', unsafe_allow_html=True)

st.markdown(
    '<div class="small-muted">Tip: If you get incomplete results, turn on <b>Deep crawl</b> and increase the crawl page limit. Increase delay if you hit rate limits.</div>',
    unsafe_allow_html=True,
)

st.divider()


# ----------------------------- Main -----------------------------

if not run:
    st.info("Set your options above, then click **Run Extraction**.")
    st.stop()

directory_url = urljoin(base_url, seed_path)
session = requests.Session()

status = st.empty()
progress = st.progress(0)

metrics = st.columns(4)
m_links = metrics[0].metric("Profile links", "0")
m_proc = metrics[1].metric("Processed", "0")
m_kept = metrics[2].metric("Kept", "0")
m_err = metrics[3].metric("Errors", "0")

errors = 0

# Step 1: Load seed
try:
    status.info(f"Loading seed page: {directory_url}")
    seed_html = safe_get(session, directory_url, delay_s=polite_delay)
except Exception as e:
    st.error(f"Failed to load seed page.\n\n{e}")
    st.stop()

advisor_urls = set(extract_advisor_urls_from_html(seed_html, base_url))

# Step 2 (optional): Crawl more internal advisor pages to discover additional profiles
if deep_crawl:
    status.info("Deep crawling to discover more advisor profile links...")
    seen_pages = set()
    q = deque([directory_url])
    internal_pages_checked = 0

    while q and internal_pages_checked < int(crawl_page_limit):
        page = q.popleft()
        if page in seen_pages:
            continue
        seen_pages.add(page)

        try:
            html = safe_get(session, page, delay_s=polite_delay)
        except Exception:
            errors += 1
            continue

        # collect advisor profiles from this page
        advisor_urls.update(extract_advisor_urls_from_html(html, base_url))

        # enqueue more /advisor/*.html pages
        for nxt in extract_internal_html_pages_from_html(html, page, base_url):
            if nxt not in seen_pages:
                q.append(nxt)

        internal_pages_checked += 1

        if internal_pages_checked % 25 == 0:
            status.info(f"Crawled {internal_pages_checked}/{crawl_page_limit} pages… profiles found: {len(advisor_urls)}")

advisor_urls = sorted(advisor_urls)
m_links = metrics[0].metric("Profile links", f"{len(advisor_urls)}")

if not advisor_urls:
    st.warning("No advisor profile links found. Try turning on **Deep crawl**.")
    st.stop()

# Apply max profiles limit
if max_profiles and int(max_profiles) > 0:
    advisor_urls = advisor_urls[: int(max_profiles)]

total = len(advisor_urls)

rows = []
kept = 0

# Step 3: Fetch each advisor profile and parse
for i, url in enumerate(advisor_urls, start=1):
    try:
        status.info(f"Fetching {i}/{total}: {url}")
        html = safe_get(session, url, delay_s=polite_delay)
        row = parse_advisor_page(html, url=url, base=base_url)

        # Province filter
        if qc_only and row.get("province", "") != "QC":
            continue

        # City filter (best-effort: searches address hint)
        if city_contains.strip():
            if city_contains.strip().lower() not in (row.get("address_hint", "").lower()):
                continue

        rows.append(row)
        kept += 1

    except Exception:
        errors += 1

    progress.progress(int((i / total) * 100))
    metrics[1].metric("Processed", f"{i}")
    metrics[2].metric("Kept", f"{kept}")
    metrics[3].metric("Errors", f"{errors}")

status.success("Extraction complete.")

if not rows:
    st.warning("No advisors matched your filters. Try disabling Québec-only or clearing the city filter.")
    st.stop()

df = pd.DataFrame(rows)
df = dedupe_rows(df)

# Final output columns
out_cols = ["name", "email", "phone", "team_name", "province"]
if include_address_hint:
    out_cols.append("address_hint")
if include_profile_url:
    out_cols.append("profile_url")

df_out = df[out_cols].copy()

# Preview
st.subheader("Preview")
st.dataframe(df_out, use_container_width=True, height=560)

# Download CSV
csv_bytes = df_out.to_csv(index=False).encode("utf-8")
st.download_button(
    "Download CSV",
    data=csv_bytes,
    file_name="nbfwm_quebec_advisors.csv" if qc_only else "nbfwm_advisors.csv",
    mime="text/csv",
    use_container_width=True,
)

# Download Excel
if do_excel and OPENPYXL_OK:
    try:
        xlsx_bytes = df_to_excel_bytes(df_out)
        st.download_button(
            "Download Excel (.xlsx)",
            data=xlsx_bytes,
            file_name="nbfwm_quebec_advisors.xlsx" if qc_only else "nbfwm_advisors.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    except Exception as e:
        st.warning(f"Excel export failed: {e}")

with st.expander("Notes / troubleshooting"):
    st.write(
        "- This extracts only information that is publicly shown on advisor pages.\n"
        "- If results look incomplete, enable **Deep crawl** and raise the crawl page limit.\n"
        "- If you see errors/timeouts, increase the polite delay.\n"
        "- Province comes from the address snippet (or JSON-LD if present). If a page has no address hint, province may be blank."
    )

