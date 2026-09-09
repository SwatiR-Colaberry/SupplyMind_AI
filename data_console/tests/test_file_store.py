import io
import zipfile
from unittest.mock import patch

import pytest

from data_console.file_store import (
    UnknownUploadError,
    UnsafeArchiveError,
    extract_csvs_from_zip,
    read_upload_columns,
    read_upload_rows,
    save_upload,
)


@pytest.fixture
def uploads_dir(tmp_path):
    with patch("data_console.file_store.UPLOADS_DIR", tmp_path):
        yield tmp_path


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_save_upload_returns_a_usable_id(uploads_dir):
    file_id = save_upload("inventory.csv", b"sku,current_stock\nSKU-1,10\n")
    assert (uploads_dir / f"{file_id}.csv").read_bytes() == b"sku,current_stock\nSKU-1,10\n"


def test_read_upload_columns_returns_just_the_header_row(uploads_dir):
    file_id = save_upload("inventory.csv", b"sku,current_stock\nSKU-1,10\nSKU-2,20\n")
    assert read_upload_columns(file_id) == ["sku", "current_stock"]


def test_read_upload_columns_on_an_empty_file_returns_an_empty_list(uploads_dir):
    file_id = save_upload("empty.csv", b"")
    assert read_upload_columns(file_id) == []


def test_read_upload_rows_returns_every_data_row_as_dicts(uploads_dir):
    file_id = save_upload("inventory.csv", b"sku,current_stock\nSKU-1,10\nSKU-2,20\n")
    assert read_upload_rows(file_id) == [
        {"sku": "SKU-1", "current_stock": "10"},
        {"sku": "SKU-2", "current_stock": "20"},
    ]


def test_a_leading_byte_order_mark_is_stripped_from_the_first_header(uploads_dir):
    file_id = save_upload("inventory.csv", "﻿sku,current_stock\nSKU-1,10\n".encode("utf-8"))
    assert read_upload_columns(file_id) == ["sku", "current_stock"]


def test_a_non_utf8_file_falls_back_to_latin_1_instead_of_crashing(uploads_dir):
    # A real production bug: the DataCo Supply Chain dataset (a real public
    # CSV export) embeds Portuguese city names as Latin-1 bytes that are not
    # valid UTF-8 - reading it used to raise UnicodeDecodeError deep inside
    # this module with no caller prepared to catch it, which crashed the
    # whole upload request and left the browser hanging with no response.
    content = "order_date,quantity,city\n2025-08-15,120,".encode("utf-8") + "São Paulo\n".encode("latin-1")
    file_id = save_upload("orders.csv", content)
    assert read_upload_columns(file_id) == ["order_date", "quantity", "city"]
    assert read_upload_rows(file_id) == [{"order_date": "2025-08-15", "quantity": "120", "city": "São Paulo"}]


def test_reading_an_unknown_file_id_raises(uploads_dir):
    with pytest.raises(UnknownUploadError):
        read_upload_columns("not-a-real-id")
    with pytest.raises(UnknownUploadError):
        read_upload_rows("not-a-real-id")


def test_extract_csvs_from_zip_saves_every_csv_member(uploads_dir):
    archive = _zip_bytes({
        "orders.csv": b"order_dt,qty\n2025-01-01,10\n",
        "inventory.csv": b"item_sku,on_hand\nSKU-1,5\n",
    })

    results = extract_csvs_from_zip(archive)

    names = sorted(name for _, name in results)
    assert names == ["inventory.csv", "orders.csv"]
    by_name = {name: file_id for file_id, name in results}
    assert read_upload_rows(by_name["orders.csv"]) == [{"order_dt": "2025-01-01", "qty": "10"}]
    assert read_upload_rows(by_name["inventory.csv"]) == [{"item_sku": "SKU-1", "on_hand": "5"}]


def test_extract_csvs_from_zip_ignores_non_csv_members(uploads_dir):
    archive = _zip_bytes({"orders.csv": b"a,b\n1,2\n", "README.txt": b"not a csv"})

    results = extract_csvs_from_zip(archive)

    assert [name for _, name in results] == ["orders.csv"]


def test_extract_csvs_from_zip_uses_only_the_basename_of_a_nested_member(uploads_dir):
    archive = _zip_bytes({"data/orders.csv": b"a,b\n1,2\n"})

    results = extract_csvs_from_zip(archive)

    assert [name for _, name in results] == ["orders.csv"]


def test_extract_csvs_from_zip_rejects_a_non_zip_blob(uploads_dir):
    with pytest.raises(UnsafeArchiveError):
        extract_csvs_from_zip(b"this is not a zip file")


def test_extract_csvs_from_zip_rejects_an_archive_with_no_csv_members(uploads_dir):
    archive = _zip_bytes({"README.txt": b"nothing to map here"})
    with pytest.raises(UnsafeArchiveError):
        extract_csvs_from_zip(archive)


def test_extract_csvs_from_zip_rejects_more_than_the_member_cap(uploads_dir):
    from data_console.file_store import MAX_ZIP_MEMBERS

    members = {f"file{i}.csv": b"a,b\n1,2\n" for i in range(MAX_ZIP_MEMBERS + 1)}
    archive = _zip_bytes(members)
    with pytest.raises(UnsafeArchiveError):
        extract_csvs_from_zip(archive)


def test_extract_csvs_from_zip_rejects_a_member_too_large_once_decompressed(uploads_dir):
    archive = _zip_bytes({"orders.csv": b"a,b\n1,2\n"})
    with patch("data_console.file_store.MAX_UPLOAD_BYTES", 5):
        with pytest.raises(UnsafeArchiveError):
            extract_csvs_from_zip(archive)
