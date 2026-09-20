import pytest
from kosync_hub.vfs.formatter import (
    build_kavita_filename,
    build_vfs_relpath,
    format_number_or_str,
    sanitize_filename_component,
)


def test_sanitize_filename_component():
    assert sanitize_filename_component("Normal Name") == "Normal Name"
    assert sanitize_filename_component("Fate/Stay Night: Unlimited") == "Fate_Stay Night_ Unlimited"
    assert sanitize_filename_component("Invalid?*<>|Chars") == "Invalid_____Chars"
    assert sanitize_filename_component("Trailing dots... ") == "Trailing dots"
    assert sanitize_filename_component("") == ""
    assert sanitize_filename_component("   ") == ""


def test_format_number_or_str():
    assert format_number_or_str(None) is None
    assert format_number_or_str("") is None
    assert format_number_or_str(1) == "1"
    assert format_number_or_str(1.0) == "1"
    assert format_number_or_str(1.5) == "1.5"
    assert format_number_or_str("1.0") == "1"
    assert format_number_or_str("2") == "2"
    assert format_number_or_str("05") == "05"
    assert format_number_or_str("001") == "001"
    assert format_number_or_str("Special 1") == "Special 1"
    assert format_number_or_str(0) == "0"
    assert format_number_or_str(0.0) == "0"


def test_build_kavita_filename_combinations():
    # 1. Both volume and chapter
    fn1 = build_kavita_filename(
        series="Berserk",
        volume=1,
        chapter=1,
        extension="cbz",
    )
    assert fn1 == "Berserk Vol. 1 Ch. 1.cbz"

    # 2. Volume only
    fn2 = build_kavita_filename(
        series="Berserk",
        volume=1.0,
        chapter=None,
        extension="cbz",
    )
    assert fn2 == "Berserk Vol. 1.cbz"

    # 3. Chapter only
    fn3 = build_kavita_filename(
        series="One Piece",
        volume=None,
        chapter=1054.5,
        extension="epub",
    )
    assert fn3 == "One Piece Ch. 1054.5.epub"

    # 4. Neither volume nor chapter
    fn4 = build_kavita_filename(
        series="Solo Leveling",
        volume=None,
        chapter=None,
        extension="pdf",
    )
    assert fn4 == "Solo Leveling.pdf"

    # 5. Volume range
    fn5 = build_kavita_filename(
        series="Berserk",
        volume="1-3",
        extension="cbz",
    )
    assert fn5 == "Berserk Vol. 1-3.cbz"

    # 6. Chapter range and Volume range
    fn6 = build_kavita_filename(
        series="Bleach",
        volume="1-3",
        chapter="1-20",
        extension="cbz",
    )
    assert fn6 == "Bleach Vol. 1-3 Ch. 1-20.cbz"

    # 7. Redundant prefix cleanup (if user typed "Vol. 1-3" or "Ch. 5-10")
    fn7 = build_kavita_filename(
        series="Bleach",
        volume="Vol. 1-3",
        chapter="Ch. 5-10",
        extension="cbz",
    )
    assert fn7 == "Bleach Vol. 1-3 Ch. 5-10.cbz"

    # 8. Filename with calibre_id in curly brackets
    fn8 = build_kavita_filename(
        series="One Piece",
        volume=10,
        chapter=100,
        calibre_id=42,
        extension="cbz",
    )
    assert fn8 == "One Piece Vol. 10 Ch. 100 {42}.cbz"

    # 9. Filename with calibre_id and no vol/ch
    fn9 = build_kavita_filename(
        series="Solo Leveling",
        calibre_id=99,
        extension="pdf",
    )
    assert fn9 == "Solo Leveling {99}.pdf"


def test_build_vfs_relpath():
    path = build_vfs_relpath(
        language="eng",
        type_="Manga",
        series="Chainsaw Man",
        volume=2,
        chapter=10,
        calibre_id=105,
        extension=".cbz",
    )
    assert path == "eng/Manga/Chainsaw Man/Chainsaw Man Vol. 2 Ch. 10 {105}.cbz"

    # Missing language and type defaults
    path_default = build_vfs_relpath(
        language=None,
        type_=None,
        series="Chainsaw Man",
        volume=1,
        chapter=None,
        extension="cbz",
        default_language="unknown",
        default_type="Unknown",
    )
    assert path_default == "unknown/Unknown/Chainsaw Man/Chainsaw Man Vol. 1.cbz"

    # Missing series falls back to title
    path_no_series = build_vfs_relpath(
        language="jpn",
        type_="Light Novel",
        series=None,
        volume=None,
        chapter=None,
        extension="epub",
        title="Standalone Work",
    )
    assert path_no_series == "jpn/Light Novel/Standalone Work/Standalone Work.epub"
