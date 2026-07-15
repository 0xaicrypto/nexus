"""Research Chat external knowledge tools (Phase 6).

Design §3.3.4 + §5.4. Eight tools all returning a uniform
``Citation`` shape so the UI can render footnotes uniformly.

  pubmed_search           PubMed E-utilities  (free, no key)
  pmc_full_text           PMC efetch          (free, no key)
  europe_pmc_search       Europe PMC REST     (free)
  oa_pdf_lookup           Unpaywall           (free, polite email)
  semantic_scholar_search Semantic Scholar    (free key optional)
  preprint_search         bioRxiv/medRxiv/arXiv (free)
  guideline_lookup        CSCO/NCCN/ESMO local RAG (Phase 6.5)
  ctcae_v5_lookup         Bundled CTCAE v5 dictionary
  drug_db_query           OpenFDA              (free)

Each tool returns ``list[Citation]``. The Research Chat / agent loop
treats the citations as first-class footnotes: every appended ``[N]``
in an assistant response maps back to a citation row.

Local caching: each external API call goes through a 24-h SQLite cache
keyed by (source, query) so reruns are cheap and offline-friendly.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Literal, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Citation shape (uniform across all sources)
# ─────────────────────────────────────────────────────────────────────


@dataclass
class Citation:
    source:   str         # 'pubmed' | 'pmc' | 'europe_pmc' | 'biorxiv' | …
    title:    str
    authors:  list[str] = field(default_factory=list)
    venue:    str = ""
    year:     Optional[int] = None
    doi:      Optional[str] = None
    pmid:     Optional[str] = None
    pmcid:    Optional[str] = None
    url:      Optional[str] = None
    abstract: Optional[str] = None
    snippet:  Optional[str] = None
    full_text_available: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────


def _cache_db_path() -> str:
    home = os.getenv("RUNE_HOME") or os.path.expanduser("~/.rune")
    base = os.path.join(home, "data")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "external_knowledge_cache.db")


def _cache_get(source: str, key: str, *, ttl_s: int = 86_400) -> Optional[str]:
    try:
        with sqlite3.connect(_cache_db_path()) as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS cache "
                "(source TEXT, k TEXT, v TEXT, expires_at INTEGER, "
                "PRIMARY KEY (source, k))"
            )
            row = c.execute(
                "SELECT v, expires_at FROM cache WHERE source=? AND k=?",
                (source, key),
            ).fetchone()
            if row and row[1] > time.time():
                return row[0]
    except sqlite3.Error as exc:
        logger.debug("cache read failed: %s", exc)
    return None


def _cache_put(source: str, key: str, value: str, *, ttl_s: int = 86_400) -> None:
    try:
        with sqlite3.connect(_cache_db_path()) as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS cache "
                "(source TEXT, k TEXT, v TEXT, expires_at INTEGER, "
                "PRIMARY KEY (source, k))"
            )
            c.execute(
                "INSERT OR REPLACE INTO cache (source, k, v, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (source, key, value, int(time.time()) + ttl_s),
            )
    except sqlite3.Error as exc:
        logger.debug("cache write failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────
# HTTP helper — short timeouts, polite User-Agent + email
# ─────────────────────────────────────────────────────────────────────


_UA = "Rune/Research-Workspace (https://rune-protocol.app; ops@rune-protocol.app)"
_TIMEOUT = 8.0


def _http_get(url: str, *, headers: Optional[dict] = None) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            **(headers or {}),
        })
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        logger.debug("http GET %s failed: %s", url, exc)
        return None


# ─────────────────────────────────────────────────────────────────────
# PubMed E-utilities
# ─────────────────────────────────────────────────────────────────────


def pubmed_search(query: str, *, top_k: int = 10,
                  year_min: Optional[int] = None) -> list[Citation]:
    """Search PubMed via the public E-utilities. Cached 24h."""
    if year_min:
        query = f"{query} AND {year_min}[PDAT]:3000[PDAT]"
    cache_key = f"pubmed::{query}::{top_k}"
    cached = _cache_get("pubmed", cache_key)
    if cached:
        return [Citation(**c) for c in json.loads(cached)]

    # 1. ESearch — list of PMIDs
    esearch = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?"
        f"db=pubmed&retmax={top_k}&retmode=json&term="
        + urllib.parse.quote(query)
    )
    raw = _http_get(esearch)
    if not raw:
        return []
    try:
        ids = json.loads(raw)["esearchresult"]["idlist"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return []
    if not ids:
        return []

    # 2. ESummary — titles, authors, venue, year, DOI
    esummary = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"
        "db=pubmed&retmode=json&id=" + ",".join(ids)
    )
    raw2 = _http_get(esummary)
    if not raw2:
        return []
    try:
        result = json.loads(raw2)["result"]
    except (KeyError, json.JSONDecodeError):
        return []

    out: list[Citation] = []
    for pmid in ids:
        rec = result.get(pmid, {})
        out.append(Citation(
            source="pubmed",
            title=rec.get("title", "").strip(),
            authors=[a.get("name", "") for a in rec.get("authors", [])][:8],
            venue=rec.get("fulljournalname") or rec.get("source", ""),
            year=_year_from(rec.get("pubdate", "")),
            doi=_first_id(rec.get("articleids", []), "doi"),
            pmid=pmid,
            pmcid=_first_id(rec.get("articleids", []), "pmc"),
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        ))
    _cache_put("pubmed", cache_key, json.dumps([c.to_dict() for c in out]))
    return out


def _year_from(pubdate: str) -> Optional[int]:
    try:
        return int(pubdate.split()[0])
    except (ValueError, IndexError):
        return None


def _first_id(article_ids: list, kind: str) -> Optional[str]:
    for x in article_ids or []:
        if x.get("idtype", "").lower() == kind.lower():
            return x.get("value")
    return None


# ─────────────────────────────────────────────────────────────────────
# PMC full-text efetch
# ─────────────────────────────────────────────────────────────────────


def pmc_full_text(pmcid: str) -> Optional[dict]:
    """Pull OA full text XML for a PMCID. Returns sections dict."""
    pmcid = pmcid.replace("PMC", "")
    cache_key = f"pmc::{pmcid}"
    cached = _cache_get("pmc", cache_key, ttl_s=7 * 86400)
    if cached:
        return json.loads(cached)

    url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?"
        f"db=pmc&id={pmcid}&retmode=xml"
    )
    raw = _http_get(url)
    if not raw:
        return None

    # Very light XML parse — we don't need full JATS fidelity for chat
    # context, just section text. Use the stdlib parser.
    out = {"pmcid": f"PMC{pmcid}", "sections": [], "text": ""}
    try:
        from xml.etree import ElementTree as ET
        root = ET.fromstring(raw)
        for sec in root.iter("sec"):
            title_el = sec.find("title")
            title = title_el.text if title_el is not None else ""
            body = " ".join(p.text or "" for p in sec.iter("p"))
            out["sections"].append({"title": title or "(untitled)",
                                    "text": body.strip()})
        out["text"] = "\n\n".join(s["text"] for s in out["sections"])[:20000]
    except Exception as exc:  # noqa: BLE001
        logger.debug("pmc xml parse failed: %s", exc)
    _cache_put("pmc", cache_key, json.dumps(out), ttl_s=7 * 86400)
    return out


# ─────────────────────────────────────────────────────────────────────
# Europe PMC
# ─────────────────────────────────────────────────────────────────────


def europe_pmc_search(query: str, *, top_k: int = 10) -> list[Citation]:
    cache_key = f"europepmc::{query}::{top_k}"
    cached = _cache_get("europe_pmc", cache_key)
    if cached:
        return [Citation(**c) for c in json.loads(cached)]
    url = (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
        f"resultType=core&pageSize={top_k}&format=json&query="
        + urllib.parse.quote(query)
    )
    raw = _http_get(url)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    items = data.get("resultList", {}).get("result", [])
    out = [
        Citation(
            source="europe_pmc",
            title=r.get("title", "").strip(),
            authors=(r.get("authorString") or "").split(", ")[:8],
            venue=r.get("journalTitle") or r.get("source", ""),
            year=int(r["pubYear"]) if r.get("pubYear") else None,
            doi=r.get("doi"),
            pmid=r.get("pmid"),
            pmcid=r.get("pmcid"),
            url=(f"https://europepmc.org/article/MED/{r['pmid']}"
                 if r.get("pmid") else None),
            abstract=r.get("abstractText", ""),
            full_text_available=bool(r.get("hasPDF") == "Y"),
        )
        for r in items
    ]
    _cache_put("europe_pmc", cache_key, json.dumps([c.to_dict() for c in out]))
    return out


# ─────────────────────────────────────────────────────────────────────
# Unpaywall — DOI → OA PDF URL
# ─────────────────────────────────────────────────────────────────────


def oa_pdf_lookup(doi: str, *, email: str = "ops@rune-protocol.app") -> Optional[str]:
    cache_key = f"unpaywall::{doi}"
    cached = _cache_get("unpaywall", cache_key, ttl_s=30 * 86400)
    if cached:
        try:
            return json.loads(cached).get("url")
        except json.JSONDecodeError as e:
            logger.debug("decoding cached unpaywall entry failed: %s", e)
    url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}?email={email}"
    raw = _http_get(url)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    best = data.get("best_oa_location") or {}
    oa_url = best.get("url_for_pdf") or best.get("url")
    _cache_put("unpaywall", cache_key, json.dumps({"url": oa_url}),
               ttl_s=30 * 86400)
    return oa_url


# ─────────────────────────────────────────────────────────────────────
# Semantic Scholar
# ─────────────────────────────────────────────────────────────────────


def semantic_scholar_search(
    query: Optional[str] = None,
    *, paper_id: Optional[str] = None,
    intent: Literal["search", "similar", "references", "citations"] = "search",
    top_k: int = 10,
) -> list[Citation]:
    cache_key = f"ss::{intent}::{query or paper_id}::{top_k}"
    cached = _cache_get("semantic_scholar", cache_key)
    if cached:
        return [Citation(**c) for c in json.loads(cached)]

    if intent == "search":
        url = (
            "https://api.semanticscholar.org/graph/v1/paper/search?"
            f"limit={top_k}&fields=title,authors,venue,year,externalIds,abstract,tldr&query="
            + urllib.parse.quote(query or "")
        )
    elif paper_id and intent in ("similar", "references", "citations"):
        sub = {"similar": "recommendations",
               "references": "references",
               "citations": "citations"}[intent]
        url = (
            f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}/{sub}?"
            f"limit={top_k}&fields=title,authors,venue,year,externalIds,abstract,tldr"
        )
    else:
        return []

    raw = _http_get(url)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    items = data.get("data") or data.get("recommendedPapers") or []
    if intent != "search":
        items = [it.get("citedPaper") or it.get("citingPaper") or it
                 for it in items]

    out: list[Citation] = []
    for it in items:
        if not it:
            continue
        ext = it.get("externalIds") or {}
        out.append(Citation(
            source="semantic_scholar",
            title=(it.get("title") or "").strip(),
            authors=[a.get("name", "") for a in it.get("authors", [])][:8],
            venue=it.get("venue") or "",
            year=it.get("year"),
            doi=ext.get("DOI"),
            pmid=ext.get("PubMed"),
            pmcid=ext.get("PubMedCentral"),
            url=f"https://www.semanticscholar.org/paper/{it.get('paperId')}",
            abstract=it.get("abstract"),
            snippet=(it.get("tldr") or {}).get("text") if it.get("tldr") else None,
        ))
    _cache_put("semantic_scholar", cache_key,
               json.dumps([c.to_dict() for c in out]))
    return out


# ─────────────────────────────────────────────────────────────────────
# Preprint search (bioRxiv / medRxiv / arXiv)
# ─────────────────────────────────────────────────────────────────────


def preprint_search(
    query: str,
    *, servers: Optional[list[str]] = None,
    days_back: int = 365,
    top_k: int = 10,
) -> list[Citation]:
    servers = servers or ["medrxiv", "biorxiv"]
    out: list[Citation] = []
    for srv in servers:
        if srv in ("biorxiv", "medrxiv"):
            out.extend(_rxiv_search(srv, query, days_back, top_k))
        elif srv == "arxiv":
            out.extend(_arxiv_search(query, top_k))
    return out[:top_k * len(servers)]


def _rxiv_search(server: str, query: str, days_back: int,
                 top_k: int) -> list[Citation]:
    cache_key = f"{server}::{query}::{days_back}"
    cached = _cache_get(server, cache_key)
    if cached:
        return [Citation(**c) for c in json.loads(cached)]
    # bioRxiv/medRxiv don't expose a full-text search via the public
    # /details API — we use their /detail interface filtered by a date
    # range, then heuristically grep titles for the query terms.
    from datetime import date, timedelta
    end = date.today()
    start = end - timedelta(days=days_back)
    url = (f"https://api.biorxiv.org/details/{server}/"
           f"{start.isoformat()}/{end.isoformat()}/0")
    raw = _http_get(url)
    if not raw:
        return []
    try:
        coll = json.loads(raw).get("collection", [])
    except json.JSONDecodeError:
        return []
    q_terms = [t.lower() for t in query.split() if t]
    out: list[Citation] = []
    for r in coll:
        title = (r.get("title") or "").lower()
        if not all(t in title for t in q_terms):
            continue
        out.append(Citation(
            source=server,
            title=(r.get("title") or "").strip(),
            authors=(r.get("authors") or "").split("; ")[:8],
            venue=server,
            year=int(r["date"][:4]) if r.get("date") else None,
            doi=r.get("doi"),
            url=(f"https://www.{server}.org/content/"
                 f"{r.get('doi', '')}v{r.get('version', 1)}"),
            abstract=r.get("abstract"),
        ))
        if len(out) >= top_k:
            break
    _cache_put(server, cache_key, json.dumps([c.to_dict() for c in out]))
    return out


def _arxiv_search(query: str, top_k: int) -> list[Citation]:
    cache_key = f"arxiv::{query}::{top_k}"
    cached = _cache_get("arxiv", cache_key)
    if cached:
        return [Citation(**c) for c in json.loads(cached)]
    url = ("http://export.arxiv.org/api/query?"
           f"max_results={top_k}&search_query=all:" + urllib.parse.quote(query))
    raw = _http_get(url)
    if not raw:
        return []
    out: list[Citation] = []
    try:
        from xml.etree import ElementTree as ET
        ns = {"a": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(raw)
        for e in root.findall("a:entry", ns):
            title = (e.find("a:title", ns).text or "").strip()
            id_url = (e.find("a:id", ns).text or "").strip()
            authors = [a.find("a:name", ns).text for a in e.findall("a:author", ns)]
            published = (e.find("a:published", ns).text or "")[:4]
            out.append(Citation(
                source="arxiv",
                title=title,
                authors=authors[:8],
                venue="arXiv",
                year=int(published) if published.isdigit() else None,
                url=id_url,
            ))
    except Exception as exc:  # noqa: BLE001
        logger.debug("arxiv parse failed: %s", exc)
    _cache_put("arxiv", cache_key, json.dumps([c.to_dict() for c in out]))
    return out


# ─────────────────────────────────────────────────────────────────────
# CTCAE v5 (bundled minimal dictionary)
# ─────────────────────────────────────────────────────────────────────


# Built-in fallback table covering the AEs the three reference
# protocols actually monitor — pneumonitis, hepatitis, hematologic.
# Full CTCAE v5 dictionary install is Phase 6.5 — see /data/ctcae/.
_CTCAE_V5_MINI: dict[str, dict] = {
    "pneumonitis": {
        "soc": "Respiratory, thoracic and mediastinal disorders",
        "G1": "Asymptomatic; clinical or diagnostic observations only",
        "G2": "Symptomatic; medical intervention indicated; limiting instrumental ADL",
        "G3": "Severe symptoms; limiting self-care ADL; oxygen indicated",
        "G4": "Life-threatening respiratory compromise; urgent intervention indicated",
        "G5": "Death",
        "io_management": {
            "G2": "Withhold ICI; methylprednisolone 1mg/kg/day; taper over 4-6 weeks",
            "G3-4": "Permanently discontinue; methylprednisolone 1-2mg/kg/day IV",
        },
    },
    "alt_increased": {
        "soc": "Investigations",
        "G1": "ULN-3.0x ULN", "G2": "3.0-5.0x ULN",
        "G3": "5.0-20x ULN", "G4": ">20x ULN", "G5": "—",
    },
    "neutrophil_count_decreased": {
        "soc": "Investigations",
        "G1": "<LLN-1.5 x10^9/L", "G2": "<1.5-1.0",
        "G3": "<1.0-0.5", "G4": "<0.5", "G5": "—",
    },
    "diarrhea": {
        "soc": "Gastrointestinal disorders",
        "G1": "<4 stools/day above baseline", "G2": "4-6 stools/day",
        "G3": "≥7 stools/day; hospitalization", "G4": "Life-threatening",
        "G5": "Death",
    },
}


def ctcae_v5_lookup(term: str) -> Optional[dict]:
    """Look up an AE term in CTCAE v5. Falls back to the minimal
    bundled dictionary; first checks $RUNE_HOME/data/ctcae/v5.json
    for a full dictionary."""
    home = os.getenv("RUNE_HOME") or os.path.expanduser("~/.rune")
    full_path = os.path.join(home, "data", "ctcae", "v5.json")
    if os.path.exists(full_path):
        try:
            with open(full_path) as fp:
                full = json.load(fp)
            for k, v in full.items():
                if k.lower() == term.lower() or term.lower() in k.lower():
                    return v
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("loading CTCAE v5 data failed: %s", e)
    for k, v in _CTCAE_V5_MINI.items():
        if k.lower() == term.lower() or term.lower() in k.lower():
            return v
    return None


# ─────────────────────────────────────────────────────────────────────
# OpenFDA drug query
# ─────────────────────────────────────────────────────────────────────


def drug_db_query(drug_name: str) -> Optional[dict]:
    cache_key = f"openfda::{drug_name}"
    cached = _cache_get("openfda", cache_key, ttl_s=7 * 86400)
    if cached:
        return json.loads(cached)
    url = ("https://api.fda.gov/drug/label.json?"
           f"search=openfda.brand_name:{urllib.parse.quote(drug_name)}"
           "&limit=1")
    raw = _http_get(url)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    results = data.get("results", [])
    if not results:
        return None
    label = results[0]
    out = {
        "brand_name": (label.get("openfda", {}).get("brand_name") or [drug_name])[0],
        "generic_name": (label.get("openfda", {}).get("generic_name") or [""])[0],
        "indications": "\n".join(label.get("indications_and_usage", [])[:1])[:2000],
        "dosage_and_administration":
            "\n".join(label.get("dosage_and_administration", [])[:1])[:2000],
        "warnings": "\n".join(label.get("warnings", [])[:1])[:2000],
        "drug_interactions":
            "\n".join(label.get("drug_interactions", [])[:1])[:2000],
        "renal_impairment":
            "\n".join(label.get("use_in_specific_populations", [])[:1])[:1500],
    }
    _cache_put("openfda", cache_key, json.dumps(out), ttl_s=7 * 86400)
    return out


# ─────────────────────────────────────────────────────────────────────
# Guideline lookup — Phase 6.5 stub
# ─────────────────────────────────────────────────────────────────────


def guideline_lookup(
    query: str,
    *, sources: Optional[list[str]] = None,
    top_k: int = 5,
) -> list[Citation]:
    """Local RAG over CSCO/NCCN/ESMO PDF index. First-iteration looks
    for a flat JSON index at $RUNE_HOME/data/guidelines/index.json;
    falls back to an empty result if the index isn't built yet."""
    sources = sources or ["csco", "nccn", "esmo"]
    home = os.getenv("RUNE_HOME") or os.path.expanduser("~/.rune")
    idx_path = os.path.join(home, "data", "guidelines", "index.json")
    if not os.path.exists(idx_path):
        return []
    try:
        with open(idx_path) as fp:
            idx = json.load(fp)
    except (json.JSONDecodeError, OSError):
        return []
    qterms = [t.lower() for t in query.split() if t]
    matches: list[Citation] = []
    for entry in idx:
        if entry.get("source") not in sources:
            continue
        haystack = " ".join([
            entry.get("title", ""), entry.get("section", ""),
            entry.get("snippet", ""),
        ]).lower()
        if not all(t in haystack for t in qterms):
            continue
        matches.append(Citation(
            source=entry["source"],
            title=entry.get("title", ""),
            venue=entry.get("source", ""),
            year=entry.get("year"),
            url=entry.get("url"),
            snippet=entry.get("snippet"),
        ))
        if len(matches) >= top_k:
            break
    return matches


# ─────────────────────────────────────────────────────────────────────
# Public registry — used by the chat tool dispatch
# ─────────────────────────────────────────────────────────────────────


KNOWLEDGE_TOOLS: dict[str, callable] = {
    "pubmed_search":            pubmed_search,
    "pmc_full_text":            pmc_full_text,
    "europe_pmc_search":        europe_pmc_search,
    "oa_pdf_lookup":            oa_pdf_lookup,
    "semantic_scholar_search":  semantic_scholar_search,
    "preprint_search":          preprint_search,
    "ctcae_v5_lookup":          ctcae_v5_lookup,
    "drug_db_query":            drug_db_query,
    "guideline_lookup":         guideline_lookup,
}
