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
PHONE_RE = re.compile(r"\b(?:1[-\s]?)?\(?\_
