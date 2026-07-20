"""Local file finder: deterministic search over explicitly configured folders.

Decision and search logic makes NO model calls and NO network requests. The
only model use is the optional summary step in ``execute_file_search``, which
calls the LOCAL OpenAI-compatible endpoint (Qwen) exclusively — file content
and absolute paths must never reach Claude. Posted chat text and metadata use
paths RELATIVE to the configured root, because chat threads are later read by
models.

The feature is inert until ANALYST_FILE_SEARCH_ROOTS is set (paths.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

SKIP_DIR_NAMES = {
    ".git", ".hg", ".svn", ".obsidian", ".vscode", ".cursor",
    "node_modules", ".venv", "__pycache__", ".trash", ".Trash", "AppData",
}
SEARCHABLE_TEXT = {".md", ".markdown", ".txt", ".text", ".docx"}
LISTABLE = SEARCHABLE_TEXT | {".pdf", ".csv", ".xlsx", ".pptx", ".doc"}

MAX_SUMMARY_BYTES = 1_500_000
MAX_SUMMARY_CHARS = 6000

LOCATION_VERB_RE = re.compile(
    r"(?<!\w)(?:where\s+(?:is|are)|find|locate|look\s+for|do\s+we\s+have"
    r"|got\s+any|show\s+me|search\s+for)\b",
    re.IGNORECASE,
)
FILE_NOUN_RE = re.compile(
    r"\b(?:files?|reports?|docs?|documents?|pdfs?|decks?|presentations?"
    r"|spreadsheets?|memos?|notes?|10-k|10-q)\b",
    re.IGNORECASE,
)
PERIOD_RE = re.compile(r"\b(?:q[1-4]|20\d{2}|quarterly|annual|earnings)\b", re.IGNORECASE)
SUMMARY_RE = re.compile(r"\b(?:summarize|summary|tl;?dr)\b", re.IGNORECASE)
_QUARTER_TOKEN_RE = re.compile(r"^q[1-4]$")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9-]*")

_REPORT_NOUNS = {
    "report", "reports", "doc", "docs", "document", "documents", "pdf", "pdfs",
    "deck", "decks", "presentation", "presentations", "spreadsheet",
    "spreadsheets", "10-k", "10-q",
}
_NOTE_NOUNS = {"note", "notes", "memo", "memos"}
_VERB_WORDS = {
    "where", "is", "are", "find", "locate", "look", "for", "do", "we", "have",
    "got", "any", "show", "me", "search",
}
_REPORT_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".pptx", ".csv"}
_NOTE_EXTS = {".md", ".markdown", ".txt", ".text"}


def _tokens(text: str) -> List[str]:
    return _TOKEN_RE.findall((text or "").casefold())


@dataclass
class FileQuery:
    raw: str
    terms: List[str]
    symbols: List[str]
    periods: List[str]
    wants_summary: bool = False
    noun_kind: str = "file"  # "report" | "note" | "file"

    def public(self) -> Dict[str, Any]:
        return {
            "terms": list(self.terms),
            "symbols": list(self.symbols),
            "periods": list(self.periods),
            "wants_summary": self.wants_summary,
            "noun_kind": self.noun_kind,
        }

    def label(self) -> str:
        bits = self.terms + self.symbols + self.periods
        return " ".join(bits) if bits else self.raw[:60]


@dataclass
class FileMatch:
    rel_path: str
    root_index: int
    name: str
    ext: str
    size_bytes: int
    mtime_iso: str
    score: float
    reasons: List[str] = field(default_factory=list)
    abs_path: Optional[str] = field(default=None, repr=False)
    mtime_ts: float = field(default=0.0, repr=False)

    def public(self) -> Dict[str, Any]:
        # Absolute path is intentionally excluded: chat/thread content is
        # later read by models and must not leak the local directory layout.
        return {
            "rel_path": self.rel_path,
            "root_index": self.root_index,
            "name": self.name,
            "ext": self.ext,
            "size_bytes": self.size_bytes,
            "mtime": self.mtime_iso,
            "score": round(self.score, 1),
            "reasons": list(self.reasons),
        }


def build_query(
    text: str, *, extra_symbols: Optional[List[str]] = None, wants_summary: bool = False
) -> FileQuery:
    """Build a FileQuery from free text without the chat verb/noun gate."""
    from .finance_research import _alias_symbol
    from .router import STOPWORDS
    from .web_search import extract_tickers

    # Strip 10-K/10-Q first: the bare "K"/"Q" otherwise reads as a ticker
    # and shadows the company-name alias ("apple" -> AAPL).
    cleaned = re.sub(r"\b10-[kq]\b", " ", text or "", flags=re.IGNORECASE)
    symbols = [s.upper() for s in extract_tickers(cleaned, limit=2)]
    alias = _alias_symbol(text or "")
    if alias and alias.upper() not in symbols:
        symbols.append(alias.upper())
    for extra in extra_symbols or []:
        if extra and extra.upper() not in symbols:
            symbols.append(extra.upper())

    periods = sorted({p.casefold() for p in PERIOD_RE.findall(text or "")})
    symbol_tokens = {s.casefold() for s in symbols}
    noun_tokens: Set[str] = set()
    noun_kind = "file"
    for match in FILE_NOUN_RE.findall(text or ""):
        token = match.casefold()
        noun_tokens.add(token)
        if token in _REPORT_NOUNS and noun_kind == "file":
            noun_kind = "report"
        elif token in _NOTE_NOUNS and noun_kind == "file":
            noun_kind = "note"

    terms = [
        t
        for t in _tokens(text)
        if t not in STOPWORDS
        and t not in _VERB_WORDS
        and t not in noun_tokens
        and t not in symbol_tokens
        and t not in set(periods)
        and len(t) >= 3
    ]
    return FileQuery(
        raw=str(text or ""),
        terms=sorted(set(terms)),
        symbols=symbols,
        periods=periods,
        wants_summary=wants_summary or bool(SUMMARY_RE.search(text or "")),
        noun_kind=noun_kind,
    )


def match_file_request(text: str) -> Optional[FileQuery]:
    """Chat gate: a file request needs BOTH a location verb and a file noun."""
    value = text or ""
    if not LOCATION_VERB_RE.search(value) or not FILE_NOUN_RE.search(value):
        return None
    return build_query(value)


def _iter_files(root: Path, budget: List[int]):
    for path in root.rglob("*"):
        if budget[0] <= 0:
            return
        budget[0] -= 1
        try:
            # Judge only the parts BELOW the chosen root: the root itself is
            # user-configured, and its ancestors (e.g. AppData in temp dirs)
            # must not disqualify it.
            rel_parts = path.relative_to(root).parts
            if any(
                part in SKIP_DIR_NAMES or part.startswith(".") for part in rel_parts
            ):
                continue
            if not path.is_file():
                continue
            if path.suffix.lower() not in LISTABLE:
                continue
        except (OSError, ValueError):
            continue
        yield path


def _score_file(query: FileQuery, rel_posix: str, ext: str) -> tuple:
    tokens = set(_tokens(rel_posix))
    score = 0.0
    reasons: List[str] = []

    term_hits = sorted(t for t in query.terms if t in tokens)
    if term_hits:
        gained = min(2.0 * len(term_hits), 8.0)
        score += gained
        reasons.append(f"terms {', '.join(term_hits)} (+{gained:g})")

    sym_hits = sorted(s for s in query.symbols if s.casefold() in tokens)
    if sym_hits:
        gained = min(3.0 * len(sym_hits), 6.0)
        score += gained
        reasons.append(f"ticker {', '.join(sym_hits)} (+{gained:g})")

    file_periods = {t for t in tokens if PERIOD_RE.fullmatch(t)}
    period_hits = sorted(set(query.periods) & file_periods)
    quarter_bonus = (
        "quarterly" in query.periods
        and any(_QUARTER_TOKEN_RE.match(t) for t in file_periods)
    )
    if period_hits or quarter_bonus:
        n = len(period_hits) + (1 if quarter_bonus else 0)
        gained = min(2.0 * n, 4.0)
        score += gained
        label = period_hits + (["quarterly->q#"] if quarter_bonus else [])
        reasons.append(f"period {', '.join(label)} (+{gained:g})")

    if (query.noun_kind == "report" and ext in _REPORT_EXTS) or (
        query.noun_kind == "note" and ext in _NOTE_EXTS
    ):
        score += 1.0
        reasons.append(f"filetype {ext} (+1)")

    return score, reasons


def search_files(
    query: FileQuery,
    roots: Optional[List[Path]] = None,
    *,
    limit: int = 10,
    max_files: int = 5000,
) -> List[FileMatch]:
    """Rank files under the configured roots. No content is read here."""
    from .paths import file_search_roots

    roots = roots if roots is not None else file_search_roots()
    matches: List[FileMatch] = []
    budget = [max_files]
    for idx, root in enumerate(roots):
        for path in _iter_files(root, budget):
            try:
                rel = path.relative_to(root).as_posix()
                st = path.stat()
            except (OSError, ValueError):
                continue
            ext = path.suffix.lower()
            score, reasons = _score_file(query, rel, ext)
            if score <= 0:
                continue
            matches.append(
                FileMatch(
                    rel_path=rel,
                    root_index=idx,
                    name=path.name,
                    ext=ext,
                    size_bytes=st.st_size,
                    mtime_iso=datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
                    score=score,
                    reasons=reasons,
                    abs_path=str(path),
                    mtime_ts=st.st_mtime,
                )
            )
    matches.sort(key=lambda m: (-m.score, -m.mtime_ts, m.rel_path))
    return matches[:limit]


def stub_matches(query: FileQuery) -> List[FileMatch]:
    label = (query.symbols[0] if query.symbols else "STUB").upper()
    return [
        FileMatch(
            rel_path=f"stub/{label}_report.md",
            root_index=0,
            name=f"{label}_report.md",
            ext=".md",
            size_bytes=1024,
            mtime_iso="2026-01-01",
            score=5.0,
            reasons=["stub match"],
            abs_path=None,
        )
    ]


def _human_size(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1_000:
        return f"{n / 1_000:.0f} KB"
    return f"{n} B"


def _summarize_with_local_model(match: FileMatch) -> str:
    """Summarize file content with the LOCAL endpoint only — never Claude."""
    from .notes_ingest import read_note_file
    from .synthesize import _call_openai_compatible_messages

    _title, text = read_note_file(Path(match.abs_path))
    prompt = (
        "Summarize this local research document in 3-6 bullets: key facts, "
        "figures, and open questions. Do not invent content.\n\n"
        + text[:MAX_SUMMARY_CHARS]
    )
    return _call_openai_compatible_messages(
        [{"role": "user", "content": prompt}], max_tokens=800
    ).strip()


def execute_file_search(
    ledger: Any, thread_id: str, query: FileQuery, *, stub: bool = False
) -> Dict[str, Any]:
    """Background-job body: search, post results to the thread, maybe summarize."""
    from .paths import file_search_roots
    from .schema import Event, Sensitivity, Surface

    try:
        roots = file_search_roots()
        matches = stub_matches(query) if stub else search_files(query, roots)

        if not matches:
            body = (
                f"No matching files for '{query.label()}' under your "
                f"{len(roots)} configured folder(s)."
            )
        else:
            lines = [
                f"Found {len(matches)} file(s) for '{query.label()}' "
                f"(searched {len(roots)} folder(s)):",
                "",
            ]
            for i, m in enumerate(matches, 1):
                lines.append(
                    f"{i}. {m.rel_path} ({_human_size(m.size_bytes)}, {m.mtime_iso})"
                    f" — {'; '.join(m.reasons)}"
                )
            body = "\n".join(lines)

        summary_note = ""
        attached_artifact_id = None
        if query.wants_summary and matches and not stub:
            top = matches[0]
            if top.ext not in SEARCHABLE_TEXT:
                summary_note = (
                    f"\n\n(Summaries support md/txt/docx only; top match is {top.ext}.)"
                )
            elif top.size_bytes > MAX_SUMMARY_BYTES:
                summary_note = "\n\n(Top match is too large to summarize.)"
            else:
                try:
                    summary = _summarize_with_local_model(top)
                    summary_note = f"\n\n## Summary (local model)\n\n{summary}"
                    art = ledger.attach_artifact(
                        Path(top.abs_path),
                        session_id=thread_id,
                        sensitivity=Sensitivity.INTERNAL.value,
                        copy_into_store=False,
                    )
                    attached_artifact_id = art.artifact_id
                    summary_note += f"\n\nAttached: {top.rel_path}"
                except RuntimeError as exc:
                    summary_note = (
                        "\n\n(Local model offline — matches listed above; "
                        f"start Ollama to enable summaries. {exc})"
                    )

        ledger.append_chat_message(
            thread_id,
            role="assistant",
            content=body + summary_note,
            kind="file_search",
            metadata={
                "query": query.public(),
                "matches": [m.public() for m in matches],
                "roots_count": len(roots),
                "artifact_id": attached_artifact_id,
                "stub": stub,
            },
        )
        ledger.append_event(
            Event(
                type="file_search",
                surface=Surface.CHAT.value,
                session_id=thread_id,
                sensitivity=Sensitivity.INTERNAL.value,
                payload={
                    "terms": query.terms,
                    "symbols": query.symbols,
                    "match_count": len(matches),
                    "roots_count": len(roots),
                    "rel_paths": [m.rel_path for m in matches[:10]],
                },
            )
        )
        return {
            "status": "ok",
            "thread_id": thread_id,
            "file_search": True,
            "match_count": len(matches),
            "roots_count": len(roots),
        }
    except Exception as exc:
        try:
            ledger.append_chat_message(
                thread_id,
                role="system",
                content=f"File search failed: {exc}",
                kind="error",
                metadata={"query": query.public()},
            )
        except Exception:  # noqa: BLE001 — never mask the original failure
            pass
        raise
