# app.py
import re
import time
from io import BytesIO
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
import streamlit as st

# Optional (Excel styling)
try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils.dataframe import dataframe_to_rows
    OPENPYXL_OK = True
except Exception:
    OPENPYXL_OK = False


# ----------------------------- Config -----------------------------

st.set_page_config(page_title="NBF (QC) Advisor Directory Extractor", layout="wide")

st.markdown("""
<style>
/* Apple-ish clean layout */
.block-container { max-width: 1180px; padding-top: 2rem; padding-bottom: 3rem; }
h1, h2, h3 { letter-spacing: -0.02em; }
small, .caption { color: rgba(0,0,0,0.6); }
[data-testid="stMetricValue"] { font-size: 1.2rem; }
hr { margin: 1.4rem 0; }
.card {
  border: 1px solid rgba(0,0,0,0.10);
  background: rgba(255,255,255,0.65);
  border-radius: 18px;
  padding: 16px 18px;
  box-shadow: 0 6px 18px rgba(0,0,0,0.06);
}
</style>
""", unsafe_allow_html=True)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; InovestorDirectoryExtractor/1.0; +https://inovestor.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

BASE_DEFAULT = "https://www.nbfwm.ca"
DIRECTORY_PATH_DEFAULT = "/advisor.html"

# Typical advisor profile pattern observed on NBFWM:
# /advisor/<team-slug>/(our-team|notre-equipe)/<advisor-slug>.html
ADVISOR_HREF_RE = re.compile(r"^/advisor/.+/(our-team|notre-equipe)/.+\.html$", re.I)

EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"\b(?:1[-\s]?)?\(?\d{3}\)?[-\s]?\d{3}[-\s]?\d{4}\b")


# ----------------------------- Helpers -----------------------------

def normalize_phone(p: str) -> str:
    digits = re.sub(r"\D+", "", p or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"
    return p.strip()

def extract_team_name_from_slug(team_slug: str) -> str:
    # Best-effort fallback if page doesn't have a clear "team" label
    if not team_slug:
        return ""
    return team_slug.replace("-", " ").strip().title()

def is_quebec(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    # Catch both French + English + abbreviation
    return (" qc" in t) or ("québec" in t) or (" quebec" in t)

def safe_get(session: requests.Session, url: str, delay_s: float, timeout: int = 30) -> str:
    r = session.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    r.raise_for_status()
    time.sleep(delay_s)
    return r.text

def extract_links_from_directory_html(html: str, base: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")

    links = set()

    # 1) Normal anchors
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if ADVISOR_HREF_RE.match(href):
            links.add(urljoin(base, href))

    # 2) Fallback: regex scan the HTML for advisor-ish links (sometimes embedded in scripts)
    for m in re.finditer(r'"/advisor/[^"]+/(?:our-team|notre-equipe)/[^"]+\.html"', html, flags=re.I):
        href = m.group(0).strip('"')
        if href.startswith("/advisor/"):
            links.add(urljoin(base, href))

    # Stable order
    return sorted(links)

def parse_advisor_page(html: str, url: str, base: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # Name
    name = ""
    h1 = soup.find("h1")
    if h1:
        name = h1.get_text(" ", strip=True)
    if not name:
        # fallback to title
        if soup.title:
            name = soup.title.get_text(" ", strip=True).split("|")[0].strip()

    # Email: prefer mailto links
    email = ""
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            email = href.split(":", 1)[1].split("?")[0].strip()
            break
    if not email:
        text = soup.get_text("\n", strip=True)
        emails = sorted(set(EMAIL_RE.findall(text)))
        email = emails[0] if emails else ""

    # Phone: prefer tel links
    phones = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("tel:"):
            raw = href.split(":", 1)[1]
            phones.append(normalize_phone(raw))
    if not phones:
        text = soup.get_text("\n", strip=True)
        phones = [normalize_phone(p) for p in PHONE_RE.findall(text)]

    phones = [p for p in phones if p]
    phone = " | ".join(pd.unique(phones)[:3])  # keep tidy

    # Address / location snippet (best-effort)
    address_text = ""
    # Locator links often exist; we grab their visible text first
    for a in soup.find_all("a", href=True):
        if "locator" in a["href"]:
            address_text = a.get_text(" ", strip=True)
            break
    if not address_text:
        # fallback: find something containing QC/Québec
        all_text = soup.get_text("\n", strip=True)
        # take a short neighborhood around "QC" occurrences
        idx = all_text.lower().find(" qc")
        if idx != -1:
            address_text = all_text[max(0, idx - 80): idx + 80].replace("\n", " ").strip()

    # Team slug is segment after /advisor/
    team_name = ""
    try:
        path = urlparse(url).path.strip("/").split("/")
        # /advisor/<team-slug>/...
        team_slug = path[1] if len(path) > 1 and path[0].lower() == "advisor" else ""
        team_name = extract_team_name_from_slug(team_slug)
    except Exception:
        team_name = ""

    return {
        "name": name.strip(),
        "email": email.strip(),
        "phone": phone.strip(),
        "team_name": team_name.strip(),
        "address_hint": address_text.strip(),
        "profile_url": url,
    }

def dedupe_rows(df: pd.DataFrame) -> pd.DataFrame:
    # Prefer email as primary unique key; fallback to name+phone
    df = df.copy()
    df["email_norm"] = df["email"].str.lower().fillna("")
    df["name_norm"] = df["name"].str.lower().fillna("")
    df["phone_norm"] = df["phone"].str.replace(r"\s+", "", regex=True).fillna("")

    # Keep first occurrence
    df = df.sort_values(by=["email_norm", "name_norm"]).reset_index(drop=True)

    has_email = df["email_norm"] != ""
    df_email = df[has_email].drop_duplicates(subset=["email_norm"], keep="first")
    df_no_email = df[~has_email].drop_duplicates(subset=["name_norm", "phone_norm"], keep="first")

    out = pd.concat([df_email, df_no_email], ignore_index=True)
    out = out.drop(columns=["email_norm", "name_norm", "phone_norm"], errors="ignore")
    return out

def df_to_excel_bytes(df: pd.DataFrame) -> bytes:
    if not OPENPYXL_OK:
        raise RuntimeError("openpyxl not installed")

    wb = Workbook()
    ws = wb.active
    ws.title = "Advisors"

    # Header styling
    header_font = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="F2F2F2")
    align = Alignment(vertical="center", wrap_text=False)

    # Alternate team colors
    fills = [
        PatternFill("solid", fgColor="FFFFFF"),
        PatternFill("solid", fgColor="F7FBFF"),
    ]

    # Write rows
    for r_idx, row in enumerate(dataframe_to_rows(df, index=False, header=True), start=1):
        ws.append(row)
        if r_idx == 1:
            for c in range(1, len(row) + 1):
                cell = ws.cell(row=r_idx, column=c)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = align
        else:
            # color by team (grouped, but we can do a simple alternating fill on change)
            pass

    # Apply team-based alternating fills
    if "team_name" in df.columns:
        current_team = None
        fill_idx = 0
        for i in range(2, ws.max_row + 1):
            team = ws.cell(row=i, column=df.columns.get_loc("team_name") + 1).value
            if team != current_team:
                current_team = team
                fill_idx = 1 - fill_idx
            for c in range(1, ws.max_column + 1):
                ws.cell(row=i, column=c).fill = fills[fill_idx]
                ws.cell(row=i, column=c).alignment = align

    # Autofit widths
    for col in ws.columns:
        max_len = 0
        col_letter = col[0].column_letter
        for cell in col:
            val = "" if cell.value is None else str(cell.value)
            max_len = max(max_len, len(val))
        ws.column_dimensions[col_letter].width = min(max(10, max_len + 2), 55)

    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()


# ----------------------------- UI -----------------------------

st.title("NBF (Québec) Wealth Advisor Directory Extractor")
st.caption("Pulls publicly listed advisor contact info and exports to CSV (optional Excel). Keep delays respectful to avoid rate-limits.")

with st.container():
    st.markdown('<div class="card">', unsafe_allow_html=True)

    colA, colB, colC, colD = st.columns([1.2, 1.1, 1.1, 1.2])

    with colA:
        base_url = st.text_input("Base site URL", value=BASE_DEFAULT)
        directory_path = st.text_input("Directory path", value=DIRECTORY_PATH_DEFAULT)

    with colB:
        qc_only = st.toggle("Québec only", value=True)
        city_contains = st.text_input("City filter (optional)", placeholder="e.g., Montréal, Québec, Laval")

    with colC:
        delay_s = st.slider("Polite delay (seconds)", 0.0, 1.5, 0.25, 0.05)
        max_profiles = st.number_input("Max profiles (0 = no limit)", min_value=0, max_value=50000, value=0, step=50)

    with colD:
        do_excel = st.toggle("Also generate Excel (.xlsx)", value=False, disabled=not OPENPYXL_OK)
        show_urls = st.toggle("Keep profile URL column", value=False)

    run = st.button("Run Extraction", use_container_width=True)

    st.markdown('</div>', unsafe_allow_html=True)

st.divider()

if run:
    directory_url = urljoin(base_url, directory_path)

    session = requests.Session()

    status = st.empty()
    progress = st.progress(0)

    try:
        status.info(f"Loading directory: {directory_url}")
        directory_html = safe_get(session, directory_url, delay_s=delay_s)
    except Exception as e:
        st.error(f"Failed to load directory page.\n\n{e}")
        st.stop()

    advisor_urls = extract_links_from_directory_html(directory_html, base=base_url)

    if not advisor_urls:
        st.warning("No advisor links found on the directory page. The site might be loading links dynamically (JS).")
        st.stop()

    if max_profiles and max_profiles > 0:
        advisor_urls = advisor_urls[: int(max_profiles)]

    total = len(advisor_urls)
    kept = 0
    rows = []

    metrics = st.columns(4)
    m_total = metrics[0].metric("Profile links found", f"{total}")
    m_proc = metrics[1].metric("Processed", "0")
    m_kept = metrics[2].metric("Kept", "0")
    m_err = metrics[3].metric("Errors", "0")
    errors = 0

    for idx, url in enumerate(advisor_urls, start=1):
        try:
            status.info(f"Fetching {idx}/{total}: {url}")
            html = safe_get(session, url, delay_s=delay_s)
            row = parse_advisor_page(html, url=url, base=base_url)

            # QC filter
            if qc_only:
                if not is_quebec(row.get("address_hint", "")):
                    continue

            # City filter (best-effort against address hint)
            if city_contains.strip():
                if city_contains.strip().lower() not in (row.get("address_hint", "").lower()):
                    continue

            rows.append(row)
            kept += 1

        except Exception:
            errors += 1

        progress.progress(int((idx / total) * 100))
        m_proc = metrics[1].metric("Processed", f"{idx}")
        m_kept = metrics[2].metric("Kept", f"{kept}")
        m_err = metrics[3].metric("Errors", f"{errors}")

    status.success("Extraction complete.")

    if not rows:
        st.warning("No advisors matched your filters. Try turning off Québec-only or clearing the city filter.")
        st.stop()

    df = pd.DataFrame(rows)

    # Clean columns + dedupe
    df = dedupe_rows(df)

    # Final columns
    cols = ["name", "email", "phone", "team_name"]
    if show_urls:
        cols.append("profile_url")
    df_out = df[cols].copy()

    # Display preview
    st.subheader("Preview")
    st.dataframe(df_out, use_container_width=True, height=520)

    # Downloads
    csv_bytes = df_out.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download CSV",
        data=csv_bytes,
        file_name="nbfwm_quebec_advisors.csv" if qc_only else "nbfwm_advisors.csv",
        mime="text/csv",
        use_container_width=True
    )

    if do_excel and OPENPYXL_OK:
        try:
            xlsx_bytes = df_to_excel_bytes(df_out)
            st.download_button(
                "Download Excel (.xlsx)",
                data=xlsx_bytes,
                file_name="nbfwm_quebec_advisors.xlsx" if qc_only else "nbfwm_advisors.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )
        except Exception as e:
            st.warning(f"Excel export failed: {e}")

    with st.expander("Notes / troubleshooting"):
        st.write(
            "- If the directory loads results dynamically (JS), the link discovery may return fewer results.\n"
            "- Increase delay if you see timeouts or if results are incomplete.\n"
            "- City filter is best-effort; it searches the page’s address text.\n"
            "- This extracts only info publicly shown on advisor pages."
        )
else:
    st.info("Set your filters, then click **Run Extraction**.")
