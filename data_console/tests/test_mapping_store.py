import json

from data_console.mapping_store import DatasetMapping, MappingStore


def test_get_on_an_unset_dataset_returns_none(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")

    assert store.get("inventory") is None


def test_save_then_get_returns_the_same_mapping(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")
    mapping = DatasetMapping(status="mapped", table="acme_inventory", column_mapping={"sku": "item_sku"})

    store.save("inventory", mapping)

    assert store.get("inventory") == mapping


def test_save_persists_across_separate_store_instances_pointed_at_the_same_file(tmp_path):
    path = tmp_path / "mappings.json"
    MappingStore(path).save("inventory", DatasetMapping(status="mapped", table="inventory", column_mapping={"sku": "sku"}))

    reloaded = MappingStore(path).get("inventory")

    assert reloaded == DatasetMapping(status="mapped", table="inventory", column_mapping={"sku": "sku"})


def test_saving_one_dataset_does_not_disturb_another(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")
    store.save("inventory", DatasetMapping(status="mapped", table="inventory", column_mapping={"sku": "sku"}))

    store.save("delivery_records", DatasetMapping(status="unavailable"))

    assert store.get("inventory").status == "mapped"
    assert store.get("delivery_records").status == "unavailable"


def test_clear_removes_a_mapping(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")
    store.save("inventory", DatasetMapping(status="mapped", table="inventory", column_mapping={"sku": "sku"}))

    store.clear("inventory")

    assert store.get("inventory") is None


def test_clear_on_an_unset_dataset_does_not_raise(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")

    store.clear("inventory")  # should not raise


def test_load_all_returns_every_saved_mapping(tmp_path):
    store = MappingStore(tmp_path / "mappings.json")
    store.save("inventory", DatasetMapping(status="mapped", table="inventory", column_mapping={"sku": "sku"}))
    store.save("customer_orders", DatasetMapping(status="unavailable"))

    all_mappings = store.load_all()

    assert set(all_mappings.keys()) == {"inventory", "customer_orders"}


def test_load_all_on_a_missing_file_returns_empty_dict(tmp_path):
    store = MappingStore(tmp_path / "does_not_exist.json")

    assert store.load_all() == {}


def test_load_all_tolerates_a_corrupted_file_rather_than_raising(tmp_path):
    path = tmp_path / "mappings.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = MappingStore(path)

    assert store.load_all() == {}


def test_written_file_is_readable_plain_json(tmp_path):
    path = tmp_path / "mappings.json"
    MappingStore(path).save("inventory", DatasetMapping(status="mapped", table="inventory", column_mapping={"sku": "sku"}))

    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["inventory"]["table"] == "inventory"
    assert raw["inventory"]["column_mapping"] == {"sku": "sku"}
