"""Tests for Obsidian / Apple Notes / Google Docs note sync."""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

import pytest

from analyst_ledger.gdocs_sync import scan_gdocs
from analyst_ledger.ledger import Ledger
from analyst_ledger.notes_ingest import (
    extract_docx_text,
    ingest_note_text,
    parse_frontmatter,
    wants_ledger,
)
from analyst_ledger.obsidian_sync import scan_obsidian
from analyst_ledger.apple_notes_sync import scan_apple_notes
from analyst_ledger.schema import Surface


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("ANALYST_LEDGER_DATA", str(tmp_path / "data"))
    return Ledger()


def test_frontmatter_and_opt_in():
    meta, body = parse_frontmatter(
        "---\nledger: true\nsensitivity: confidential\nsymbol: NVDA\n---\n\nHello #research\n"
    )
    assert meta["ledger"] is True
    assert meta["symbol"] == "NVDA"
    assert "Hello" in body
    assert wants_ledger(meta, body)
    assert wants_ledger({}, "plain text with #ledger tag")
    assert not wants_ledger({}, "plain text without tag")


def test_obsidian_opt_in_only(ledger: Ledger, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "skip.md").write_text("# personal\nno ledger\n", encoding="utf-8")
    (vault / "keep.md").write_text(
        "---\nledger: true\nsymbol: AAPL\n---\n\nThesis fragment\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ANALYST_OBSIDIAN_VAULT", str(vault))
    n = scan_obsidian(ledger=ledger, vault=vault, require_opt_in=True)
    assert n == 1
    assert scan_obsidian(ledger=ledger, vault=vault, require_opt_in=True) == 0
    events = ledger.list_events(limit=50)
    types = {e["type"] for e in events}
    assert "note" in types
    assert "external_note" in types
    ext = [e for e in events if e["type"] == "external_note"][0]
    assert ext["surface"] == Surface.OBSIDIAN.value
    assert ext["payload"]["source"] == "obsidian"


def test_obsidian_hash_tag(ledger: Ledger, tmp_path):
    vault = tmp_path / "vault2"
    vault.mkdir()
    (vault / "tagged.md").write_text("Morning ideas #ledger\n", encoding="utf-8")
    n = scan_obsidian(ledger=ledger, vault=vault, require_opt_in=True)
    assert n == 1


def test_gdocs_folder_md_and_txt(ledger: Ledger, tmp_path, monkeypatch):
    export = tmp_path / "gdocs"
    export.mkdir()
    monkeypatch.setenv("ANALYST_GDOCS_EXPORT", str(export))
    (export / "NVDA memo.txt").write_text("Checked peers vs NVDA\n", encoding="utf-8")
    n = scan_gdocs(ledger=ledger, export_dir=export)
    assert n == 1
    assert scan_gdocs(ledger=ledger, export_dir=export) == 0
    notes = [e for e in ledger.list_events(limit=20) if e["type"] == "note"]
    assert any("peers" in (e["payload"].get("text") or "") for e in notes)


def _write_minimal_docx(path: Path, text: str) -> None:
    # Minimal OOXML package
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    document = Element(f"{{{ns}}}document")
    body = SubElement(document, f"{{{ns}}}body")
    p = SubElement(body, f"{{{ns}}}p")
    r = SubElement(p, f"{{{ns}}}r")
    t = SubElement(r, f"{{{ns}}}t")
    t.text = text
    xml = tostring(document, encoding="utf-8")
    content_types = (
        b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>"""
        b"""<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">"""
        b"""<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>"""
        b"""<Default Extension="xml" ContentType="application/xml"/>"""
        b"""<Override PartName="/word/document.xml" """
        b"""ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>"""
        b"""</Types>"""
    )
    rels = (
        b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>"""
        b"""<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">"""
        b"""<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>"""
        b"""</Relationships>"""
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", xml)


def test_gdocs_docx(ledger: Ledger, tmp_path):
    export = tmp_path / "gdocs2"
    export.mkdir()
    docx = export / "thesis.docx"
    _write_minimal_docx(docx, "Docx body from Google export")
    assert "Docx body" in extract_docx_text(docx)
    n = scan_gdocs(ledger=ledger, export_dir=export)
    assert n == 1


def test_apple_notes_skip_export_ingest(ledger: Ledger, tmp_path, monkeypatch):
    export = tmp_path / "apple"
    export.mkdir()
    monkeypatch.setenv("ANALYST_APPLE_NOTES_EXPORT", str(export))
    (export / "abc_Morning.md").write_text(
        "---\nledger: true\nsource: apple_notes\n---\n\n# Morning\n\nYF checks\n",
        encoding="utf-8",
    )
    n = scan_apple_notes(ledger=ledger, skip_export=True)
    assert n == 1
    ext = [e for e in ledger.list_events(limit=20) if e["type"] == "external_note"]
    assert ext[0]["surface"] == Surface.APPLE_NOTES.value


def test_ingest_respects_restricted_surface(ledger: Ledger):
    result = ingest_note_text(
        ledger,
        title="Deal",
        text="---\nledger: true\nsensitivity: restricted\n---\n\nMNPI room\n",
        surface=Surface.OBSIDIAN.value,
        source="obsidian",
        require_opt_in=True,
    )
    assert result is not None
    assert result["sensitivity"] == "restricted"
