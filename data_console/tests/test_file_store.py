from unittest.mock import patch

import pytest

from data_console.file_store import UnknownUploadError, read_upload_columns, read_upload_rows, save_upload


@pytest.fixture
def uploads_dir(tmp_path):
    with patch("data_console.file_store.UPLOADS_DIR", tmp_path):
        yield tmp_path


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


def test_reading_an_unknown_file_id_raises(uploads_dir):
    with pytest.raises(UnknownUploadError):
        read_upload_columns("not-a-real-id")
    with pytest.raises(UnknownUploadError):
        read_upload_rows("not-a-real-id")
