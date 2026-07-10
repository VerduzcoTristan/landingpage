#!/usr/bin/env python3
"""
Briefing Archive — SQLite-backed persistence for daily morning briefings.

Stores each day's briefing articles with metadata (title, URL, source, summary)
in a durable SQLite database. Provides ingestion from cron output files and
query by date range.

Database: ~/.hermes/data/briefings.db

Usage:
  python3 briefing_archive.py ingest <filepath>        # Ingest one cron output file
  python3 briefing_archive.py backfill                 # Ingest all existing files
  python3 briefing_archive.py query --date 2026-06-28  # Get one day's briefing
  python3 briefing_archive.py query --from DATE --to DATE  # Date range query
  python3 briefing_archive.py list                     # List all briefings
  python3 briefing_archive.py stats                    # Summary statistics
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, date
from pathlib import Path

DB_PATH = os.path.expanduser("~/.hermes/data/briefings.db")
CRON_OUTPUT_DIR = os.path.expanduser("~/.hermes/cron/output/7dc1d641173d")

# ── Schema ────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS briefings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    full_date TEXT,
    source_file TEXT,
    article_count INTEGER DEFAULT 0,
    ingested_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    briefing_date TEXT NOT NULL,
    position INTEGER,
    title TEXT,
    source_name TEXT,
    source_url TEXT,
    summary TEXT,
    category TEXT DEFAULT 'general',
    FOREIGN KEY (briefing_date) REFERENCES briefings(date)
);

CREATE INDEX IF NOT EXISTS idx_articles_date ON articles(briefing_date);
CREATE INDEX IF NOT EXISTS idx_briefings_date ON briefings(date);
CREATE INDEX IF NOT EXISTS idx_articles_title ON articles(title);

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title,
    source_name,
    summary,
    category,
    categories
);
"""

# ── Category keywords ───────────────────────────────────────────────────

CATEGORY_KEYWORDS = {
    "AI": [
        "ai ", "artificial intelligence", "llm", "large language model",
        "gpt", "claude", "openai", "anthropic", "deepmind", "deepseek",
        "gemini", "copilot", "machine learning", "neural network",
        "transformer", "chatgpt", "mistral", "llama", "groq",
        "text-to-", "image generation", "diffusion model",
        "ai agent", "ai model", "ai company", "ai safety",
        "alignment", "agi", "superintelligence",
    ],
    "coding": [
        "code", "programming", "developer", "python", "javascript",
        "rust", "typescript", "golang", "api ", "open source",
        "open-source", "software", "compiler",
        "framework", "library", "cli", "terminal", "debug",
        "release", "version", "commit", "pull request",
        "curl", "sqlite", "package", "npm", "pip",
        "wasm", "webassembly",
    ],
    "security": [
        "security", "vulnerability", "hack", "breach", "exploit",
        "cve", "encryption", "privacy", "backdoor", "malware",
        "ransomware", "zero-day", "authentication", "phishing",
        "cyberattack", "cyber", "firewall", "bug bounty",
        "infosec", "data leak", "spy", "surveillance",
    ],
    "homelab": [
        "homelab", "self-hosted", "self hosted", "docker",
        "kubernetes", "nas", "raspberry pi", "home automation",
        "networking", "router", "server", "proxmox",
        "unraid", "truenas", "plex", "jellyfin",
    ],
    "finance": [
        "finance", "crypto", "bitcoin", "stock", "market",
        "economy", "investment", "funding", "ipo", "vc",
        "venture capital", "money", "bank", "trading",
        "price", "valuation", "revenue", "billion", "trillion",
        "acquisition", "merger", "startup funding",
    ],
    "GitHub": [
        "github", "git ", "repo", "repository", "pull request",
        "merge", "fork", "octoverse", "actions", "codespaces",
        "gitlab",
    ],
}


def categorize_article(title: str, source_name: str, summary: str) -> str:
    """Return comma-separated category tags for an article.

    Matches against title first (weighted higher), then source + summary.
    Returns 'general' if no categories match.
    """
    title_lower = (title or "").lower()
    source_lower = (source_name or "").lower()
    summary_lower = (summary or "").lower()

    matched = []
    for category, keywords in CATEGORY_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in title_lower:
                score += 3  # title match weighted higher
            elif kw in source_lower:
                score += 2
            elif kw in summary_lower:
                score += 1
        if score >= 2:
            matched.append(category)

    if not matched:
        return "general"

    return ",".join(sorted(matched))


# ── Parsing ───────────────────────────────────────────────────────────────

def parse_briefing_from_md(filepath: str) -> dict | None:
    """Parse a cron output markdown file and extract structured briefing data.

    Returns a dict with keys: date, full_date, articles (list of dicts),
    or None if no briefing found.
    """
    with open(filepath, "r") as f:
        lines = f.readlines()

    # Find the LAST MORNING BRIEFING line (first hit is usually the prompt template)
    # Exclude underline separator lines and template placeholder "[Day], June [XX]"
    briefing_start = None
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith("MORNING BRIEFING") and "===" not in stripped:
            # Reject template placeholders
            if "[Day]" in stripped or "[XX]" in stripped:
                continue
            briefing_start = i
            break

    if briefing_start is None:
        return None

    # Extract date from header
    header = lines[briefing_start].strip()
    date_match = re.match(
        r"MORNING BRIEFING\s*[—–\-]\s*(.+)", header
    )
    full_date = date_match.group(1).strip() if date_match else ""

    # Parse full_date into YYYY-MM-DD
    parsed_date = _parse_full_date(full_date)

    # Extract articles
    articles = []
    current_article = None
    body_lines = []

    for line in lines[briefing_start + 1 :]:
        stripped = line.strip()

        # Match numbered article headers: "1. TITLE" or "**1. TITLE**"
        m = re.match(r"^(\*{0,2})(\d+)\.\s+(.+?)(\*{0,2})$", stripped)
        if m:
            # Save previous article
            if current_article is not None:
                current_article["summary"] = "\n".join(body_lines).strip()
                articles.append(current_article)

            current_article = {
                "position": int(m.group(2)),
                "title": m.group(3).strip("*"),
                "source_name": "",
                "source_url": "",
                "summary": "",
            }
            body_lines = []
            continue

        if current_article is None:
            continue

        # Match source line: "Source: Name — URL"
        src_match = re.match(
            r"^Source:\s+(.+?)\s+[—–\-]+\s+(https?://\S+)", stripped
        )
        if src_match:
            current_article["source_name"] = src_match.group(1).strip()
            current_article["source_url"] = src_match.group(2).strip()
            continue

        # Collect body lines (skip empty separator lines)
        if stripped and not stripped.startswith("===") and stripped != "":
            body_lines.append(stripped)

    # Save last article
    if current_article is not None:
        current_article["summary"] = "\n".join(body_lines).strip()
        articles.append(current_article)

    return {
        "date": parsed_date,
        "full_date": full_date,
        "articles": articles,
    }


def _parse_full_date(full_date: str) -> str:
    """Convert 'Sunday, June 28, 2026' -> '2026-06-28'."""
    if not full_date:
        return ""
    try:
        for fmt in [
            "%A, %B %d, %Y",
            "%A, %B %d %Y",
            "%A %B %d, %Y",
            "%B %d, %Y",
        ]:
            try:
                dt = datetime.strptime(full_date, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
    except Exception:
        pass
    return ""


# ── Archive class ─────────────────────────────────────────────────────────

class BriefingArchive:
    """SQLite-backed archive for morning briefing articles."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self):
        with self._get_conn() as conn:
            conn.executescript(SCHEMA)
            # Migrate: add categories column if missing (pre-category DBs)
            try:
                conn.execute("ALTER TABLE articles ADD COLUMN categories TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # column already exists
            # Migrate: rebuild FTS if missing 'categories' column (pre-category FTS)
            try:
                fts_cols = conn.execute("PRAGMA table_info('articles_fts')").fetchall()
                fts_col_names = [c[1] for c in fts_cols]
                if "categories" not in fts_col_names:
                    conn.execute("DROP TABLE IF EXISTS articles_fts")
                    conn.execute(
                        "CREATE VIRTUAL TABLE articles_fts USING fts5(title, source_name, summary, category, categories)"
                    )
            except sqlite3.OperationalError:
                pass
            # Check if FTS table is using content-sync mode (legacy) — rebuild if so
            fts_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='articles_fts'"
            ).fetchone()
            if fts_sql and "content=" in (fts_sql[0] or ""):
                # Legacy content-sync FTS — drop and let SCHEMA recreate next time
                # For now, delete all rows and repopulate
                conn.execute("DELETE FROM articles_fts")
                fts_sql = None  # Force repopulation below

            # Populate FTS from existing articles if FTS table is empty
            try:
                fts_count = conn.execute("SELECT COUNT(*) FROM articles_fts").fetchone()[0]
            except sqlite3.OperationalError:
                fts_count = 0
            if fts_count == 0:
                rows = conn.execute(
                    "SELECT id, title, source_name, summary, COALESCE(category,'general'), COALESCE(categories,'') FROM articles"
                ).fetchall()
                for row in rows:
                    conn.execute(
                        "INSERT INTO articles_fts(rowid, title, source_name, summary, category, categories) VALUES (?,?,?,?,?,?)",
                        (row[0], row[1], row[2], row[3], row[4], row[5]),
                    )

    def ingest_file(self, filepath: str) -> dict:
        """Parse and store a single briefing file. Returns summary dict."""
        fpath = Path(filepath)
        if not fpath.exists():
            return {"status": "error", "error": f"File not found: {filepath}"}
        if not fpath.suffix == ".md":
            return {"status": "error", "error": f"Not a markdown file: {filepath}"}

        parsed = parse_briefing_from_md(str(fpath))
        if parsed is None:
            return {"status": "skipped", "reason": "No briefing found in file"}

        briefing_date = parsed["date"]
        if not briefing_date:
            return {"status": "error", "error": "Could not parse date from briefing"}

        with self._get_conn() as conn:
            # Upsert briefing
            conn.execute(
                """INSERT INTO briefings (date, full_date, source_file, article_count)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                       full_date = excluded.full_date,
                       source_file = excluded.source_file,
                       article_count = excluded.article_count,
                       ingested_at = datetime('now')""",
                (
                    briefing_date,
                    parsed["full_date"],
                    str(fpath),
                    len(parsed["articles"]),
                ),
            )

            # Delete existing articles for this date, then re-insert
            old_ids = [
                r[0] for r in conn.execute(
                    "SELECT id FROM articles WHERE briefing_date = ?", (briefing_date,)
                ).fetchall()
            ]
            # Delete from FTS first
            for aid in old_ids:
                conn.execute("DELETE FROM articles_fts WHERE rowid = ?", (aid,))
            # Then delete from articles
            conn.execute("DELETE FROM articles WHERE briefing_date = ?", (briefing_date,))

            for article in parsed["articles"]:
                cat_tags = categorize_article(
                    article["title"], article["source_name"], article["summary"]
                )
                cursor = conn.execute(
                    """INSERT INTO articles
                       (briefing_date, position, title, source_name, source_url, summary, categories)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        briefing_date,
                        article["position"],
                        article["title"],
                        article["source_name"],
                        article["source_url"],
                        article["summary"],
                        cat_tags,
                    ),
                )
                article_id = cursor.lastrowid
                # Sync to FTS
                conn.execute(
                    "INSERT INTO articles_fts(rowid, title, source_name, summary, category, categories) VALUES (?,?,?,?,?,?)",
                    (
                        article_id,
                        article["title"],
                        article["source_name"],
                        article["summary"],
                        "general",
                        cat_tags,
                    ),
                )

        return {
            "status": "ok",
            "date": briefing_date,
            "articles": len(parsed["articles"]),
            "file": str(fpath),
        }

    def backfill(self, cron_dir: str = CRON_OUTPUT_DIR) -> list[dict]:
        """Ingest all .md files in the cron output directory. Returns results."""
        cdir = Path(cron_dir)
        if not cdir.exists():
            return [{"status": "error", "error": f"Directory not found: {cron_dir}"}]

        files = sorted(cdir.glob("*.md"))
        results = []
        for f in files:
            result = self.ingest_file(str(f))
            results.append(result)
        return results

    def get_briefing(self, date_str: str) -> dict | None:
        """Get a single briefing by date (YYYY-MM-DD)."""
        with self._get_conn() as conn:
            briefing = conn.execute(
                "SELECT * FROM briefings WHERE date = ?", (date_str,)
            ).fetchone()
            if not briefing:
                return None

            articles = conn.execute(
                "SELECT * FROM articles WHERE briefing_date = ? ORDER BY position",
                (date_str,),
            ).fetchall()

            return {
                "date": briefing["date"],
                "full_date": briefing["full_date"],
                "source_file": briefing["source_file"],
                "article_count": briefing["article_count"],
                "ingested_at": briefing["ingested_at"],
                "articles": [dict(a) for a in articles],
            }

    def get_briefings(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """Get briefings in a date range, most recent first."""
        conditions = []
        params = []

        if start_date:
            conditions.append("date >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("date <= ?")
            params.append(end_date)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        with self._get_conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM briefings {where} ORDER BY date DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()

            return [dict(r) for r in rows]

    def get_articles(
        self,
        date_str: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Get articles, optionally filtered by date or range."""
        conditions = []
        params = []

        if date_str:
            conditions.append("a.briefing_date = ?")
            params.append(date_str)
        if start_date:
            conditions.append("a.briefing_date >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("a.briefing_date <= ?")
            params.append(end_date)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        with self._get_conn() as conn:
            rows = conn.execute(
                f"""SELECT a.*, b.full_date FROM articles a
                    JOIN briefings b ON a.briefing_date = b.date
                    {where}
                    ORDER BY a.briefing_date DESC, a.position
                    LIMIT ?""",
                params + [limit],
            ).fetchall()

            return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Return summary statistics."""
        with self._get_conn() as conn:
            n_briefings = conn.execute(
                "SELECT COUNT(*) FROM briefings"
            ).fetchone()[0]
            n_articles = conn.execute(
                "SELECT COUNT(*) FROM articles"
            ).fetchone()[0]
            date_range = conn.execute(
                "SELECT MIN(date), MAX(date) FROM briefings"
            ).fetchone()
            top_sources = conn.execute(
                """SELECT source_name, COUNT(*) as cnt FROM articles
                   GROUP BY source_name ORDER BY cnt DESC LIMIT 5"""
            ).fetchall()
            return {
                "total_briefings": n_briefings,
                "total_articles": n_articles,
                "date_range": {
                    "first": date_range[0],
                    "last": date_range[1],
                },
                "top_sources": [dict(r) for r in top_sources],
            }

    def get_categories(self) -> list[str]:
        """Return all unique category values present in articles."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT categories FROM articles WHERE categories != '' AND categories != 'general'"
            ).fetchall()
            all_cats = set()
            for r in rows:
                for c in r[0].split(","):
                    c = c.strip()
                    if c:
                        all_cats.add(c)
            return sorted(all_cats)

    def categorize_all(self, dry_run: bool = False) -> dict:
        """Auto-categorize all existing articles. Returns stats dict."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT id, title, source_name, summary, categories FROM articles"
            ).fetchall()

            total = len(rows)
            changed = 0
            unchanged = 0

            for r in rows:
                new_cats = categorize_article(r["title"], r["source_name"], r["summary"])
                old_cats = r["categories"] or "general"
                if new_cats != old_cats:
                    changed += 1
                    if not dry_run:
                        conn.execute(
                            "UPDATE articles SET categories = ? WHERE id = ?",
                            (new_cats, r["id"]),
                        )
                else:
                    unchanged += 1

            if not dry_run and changed > 0:
                conn.execute("INSERT INTO articles_fts(articles_fts) VALUES('rebuild')")

            return {
                "total": total,
                "changed": changed,
                "unchanged": unchanged,
                "dry_run": dry_run,
            }

    def get_articles_by_category(
        self,
        category: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Get articles matching a category, optionally filtered by date range."""
        conditions = ["a.categories LIKE ?"]
        params = [f"%{category}%"]

        if start_date:
            conditions.append("a.briefing_date >= ?")
            params.append(start_date)
        if end_date:
            conditions.append("a.briefing_date <= ?")
            params.append(end_date)

        where = "WHERE " + " AND ".join(conditions)

        with self._get_conn() as conn:
            rows = conn.execute(
                f"""SELECT a.*, b.full_date FROM articles a
                    JOIN briefings b ON a.briefing_date = b.date
                    {where}
                    ORDER BY a.briefing_date DESC, a.position
                    LIMIT ?""",
                params + [limit],
            ).fetchall()

            return [dict(r) for r in rows]

    def get_category_counts(self) -> list[dict]:
        """Return counts per category across all articles."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT categories FROM articles WHERE categories != '' AND categories != 'general'"
            ).fetchall()
            counts = {}
            for r in rows:
                for c in r[0].split(","):
                    c = c.strip()
                    if c:
                        counts[c] = counts.get(c, 0) + 1
            return [{"category": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])]

    def search_articles(self, query: str, limit: int = 50) -> list[dict]:
        """Full-text search across article titles, summaries, source names, and categories.

        Uses FTS5 for relevance-ranked results. Returns articles with snippet
        context (up to 200 chars around the first match in summary).
        """
        if not query or not query.strip():
            return []

        # Sanitize FTS5 query: escape special chars but allow basic terms
        sanitized = query.strip().replace('"', '""')
        # Quote each word for exact-ish matching
        terms = [f'"{t}"' for t in sanitized.split() if t]
        if not terms:
            return []
        fts_query = " OR ".join(terms)

        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT a.*, b.full_date,
                          snippet(articles_fts, 2, '<mark>', '</mark>', '…', 40) AS snippet
                   FROM articles_fts fts
                   JOIN articles a ON fts.rowid = a.id
                   JOIN briefings b ON a.briefing_date = b.date
                   WHERE articles_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, limit),
            ).fetchall()

            results = []
            for r in rows:
                d = dict(r)
                # Use snippet if available, otherwise truncate summary
                if not d.get("snippet") and d.get("summary"):
                    d["snippet"] = d["summary"][:200] + ("…" if len(d["summary"]) > 200 else "")
                elif not d.get("snippet"):
                    d["snippet"] = d.get("title", "")[:200]
                results.append(d)

            return results


# ── CLI ───────────────────────────────────────────────────────────────────

def cmd_categorize(args):
    archive = BriefingArchive()
    result = archive.categorize_all(dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        label = "Would change" if args.dry_run else "Changed"
        print(f"{label} {result['changed']}/{result['total']} articles")
        if result["unchanged"]:
            print(f"  {result['unchanged']} already correct")


def cmd_categories(args):
    archive = BriefingArchive()
    counts = archive.get_category_counts()
    all_cats = archive.get_categories()
    print(f"{len(all_cats)} categories, {sum(c['count'] for c in counts)} tagged articles:")
    for c in counts:
        print(f"  {c['category']:<12} {c['count']} articles")


def cmd_ingest(args):
    archive = BriefingArchive()
    result = archive.ingest_file(args.filepath)
    print(json.dumps(result, indent=2))


def cmd_backfill(args):
    archive = BriefingArchive()
    results = archive.backfill(args.dir)
    ok = sum(1 for r in results if r.get("status") == "ok")
    skipped = sum(1 for r in results if r.get("status") == "skipped")
    errors = sum(1 for r in results if r.get("status") == "error")
    print(
        f"Backfill complete: {ok} ingested, {skipped} skipped, {errors} errors"
    )
    if args.verbose:
        for r in results:
            print(f"  {r}")


def cmd_query(args):
    archive = BriefingArchive()

    if args.date:
        result = archive.get_briefing(args.date)
        if result is None:
            print(f"No briefing found for {args.date}")
            sys.exit(1)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _print_briefing(result)

    elif args.from_date or args.to_date:
        articles = archive.get_articles(
            start_date=args.from_date,
            end_date=args.to_date,
            limit=args.limit,
        )
        if args.json:
            print(json.dumps(articles, indent=2, default=str))
        else:
            print(f"{len(articles)} articles in range")
            for a in articles:
                print(
                    f"  [{a['briefing_date']}] #{a['position']} "
                    f"{a['title'][:80]} — {a['source_name']}"
                )

    else:
        briefings = archive.get_briefings(limit=args.limit)
        if args.json:
            print(json.dumps(briefings, indent=2, default=str))
        else:
            print(f"{len(briefings)} briefings:")
            for b in briefings:
                print(f"  {b['date']} — {b['full_date']} ({b['article_count']} articles)")


def cmd_list(args):
    archive = BriefingArchive()
    briefings = archive.get_briefings()
    if args.json:
        print(json.dumps(briefings, indent=2, default=str))
    else:
        for b in briefings:
            print(
                f"{b['date']}  {b['full_date']:<40}  "
                f"{b['article_count']} articles  {b['source_file']}"
            )


def cmd_stats(args):
    archive = BriefingArchive()
    stats = archive.get_stats()
    if args.json:
        print(json.dumps(stats, indent=2, default=str))
    else:
        print(f"Total briefings: {stats['total_briefings']}")
        print(f"Total articles:  {stats['total_articles']}")
        print(
            f"Date range:      {stats['date_range']['first']} → "
            f"{stats['date_range']['last']}"
        )
        print("Top sources:")
        for s in stats["top_sources"]:
            print(f"  {s['source_name']:<30} {s['cnt']} articles")


def _print_briefing(result: dict):
    print(f"MORNING BRIEFING — {result['full_date']}")
    print(f"Date: {result['date']} | {result['article_count']} articles")
    print("=" * 60)
    for a in result["articles"]:
        print(f"\n{a['position']}. {a['title']}")
        print(f"   Source: {a['source_name']} — {a['source_url']}")
        for line in a["summary"].split("\n"):
            print(f"   {line}")


def main():
    parser = argparse.ArgumentParser(description="Briefing Archive CLI")
    sub = parser.add_subparsers(dest="command")

    # ingest
    p = sub.add_parser("ingest")
    p.add_argument("filepath", help="Path to .md cron output file")

    # backfill
    p = sub.add_parser("backfill")
    p.add_argument("--dir", default=CRON_OUTPUT_DIR, help="Cron output directory")
    p.add_argument("--verbose", "-v", action="store_true")

    # categorize
    p = sub.add_parser("categorize", help="Auto-categorize all articles")
    p.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    p.add_argument("--json", "-j", action="store_true")

    # categories
    p = sub.add_parser("categories", help="List all categories with counts")

    # query
    p = sub.add_parser("query")
    p.add_argument("--date", "-d", help="Single date (YYYY-MM-DD)")
    p.add_argument("--from", dest="from_date", help="Start date (YYYY-MM-DD)")
    p.add_argument("--to", dest="to_date", help="End date (YYYY-MM-DD)")
    p.add_argument("--limit", "-n", type=int, default=50)
    p.add_argument("--json", "-j", action="store_true", help="JSON output")

    # list
    p = sub.add_parser("list")
    p.add_argument("--json", "-j", action="store_true")

    # stats
    p = sub.add_parser("stats")
    p.add_argument("--json", "-j", action="store_true")

    args = parser.parse_args()

    if args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "backfill":
        cmd_backfill(args)
    elif args.command == "categorize":
        cmd_categorize(args)
    elif args.command == "categories":
        cmd_categories(args)
    elif args.command == "query":
        cmd_query(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "stats":
        cmd_stats(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
