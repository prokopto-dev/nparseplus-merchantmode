"""merchant_mode.catalog — item-id provenance.

The failure this guards against is silent: a wrong id still renders the right
name and only misbehaves when somebody clicks it. So the catalog never resolves
a disagreement quietly — it reports one.
"""

from __future__ import annotations

from merchant_mode.catalog import IdStatus, ItemCatalog

FUNGI = "Fungus Covered Scale Tunic"


def test_an_id_from_your_own_dump_is_authoritative() -> None:
    catalog = ItemCatalog()
    catalog.learn_owned("Cloak of Flames", 11621)
    resolved = catalog.resolve("Cloak of Flames")
    assert resolved is not None
    assert resolved.item_id == 11621
    assert resolved.status is IdStatus.OWNED
    assert resolved.trusted


def test_agreement_between_sources_is_confirmed() -> None:
    catalog = ItemCatalog()
    catalog.learn_owned("Cloak of Flames", 11621)
    catalog.learn_remote("Cloak of Flames", 11621)
    resolved = catalog.resolve("Cloak of Flames")
    assert resolved.status is IdStatus.CONFIRMED
    assert resolved.trusted


def test_pigparse_alone_is_unverified_and_says_so() -> None:
    catalog = ItemCatalog()
    catalog.learn_remote("Guise of the Deceiver", 1234)
    resolved = catalog.resolve("Guise of the Deceiver")
    assert resolved.item_id == 1234
    assert resolved.status is IdStatus.UNVERIFIED
    assert not resolved.trusted


def test_disagreement_is_reported_not_silently_resolved() -> None:
    # The id-2735 case: databases disagree, and a wrong id looks right on screen.
    catalog = ItemCatalog()
    catalog.learn_owned(FUNGI, 2735)
    catalog.learn_remote(FUNGI, 9999)
    resolved = catalog.resolve(FUNGI)
    assert resolved.status is IdStatus.CONFLICT
    assert not resolved.trusted
    assert resolved.item_id == 2735  # the dump wins for the link...
    assert resolved.alternate_id == 9999  # ...but the other candidate is kept


def test_conflicts_are_listable_for_the_ui() -> None:
    catalog = ItemCatalog()
    catalog.learn_owned(FUNGI, 2735)
    catalog.learn_remote(FUNGI, 9999)
    catalog.learn_owned("Cloak of Flames", 11621)
    assert [item.name for item in catalog.conflicts()] == [FUNGI]


def test_lookup_is_case_and_whitespace_insensitive() -> None:
    catalog = ItemCatalog()
    catalog.learn_owned("Cloak of Flames", 11621)
    assert catalog.resolve("  cloak OF flames  ").item_id == 11621


def test_an_unknown_name_resolves_to_nothing() -> None:
    assert ItemCatalog().resolve("Never Heard Of It") is None


def test_unresolved_reports_what_to_ask_pigparse_about() -> None:
    catalog = ItemCatalog()
    catalog.learn_owned("Cloak of Flames", 11621)
    assert catalog.unresolved(["Cloak of Flames", "Manastone"]) == ["Manastone"]


def test_nonsense_ids_are_ignored() -> None:
    catalog = ItemCatalog()
    catalog.learn_owned("Zero", 0)
    catalog.learn_remote("Negative", -1)
    catalog.learn_remote("Missing", None)
    assert catalog.resolve("Zero") is None
    assert catalog.resolve("Negative") is None
    assert catalog.resolve("Missing") is None


def test_blank_names_are_ignored() -> None:
    catalog = ItemCatalog()
    catalog.learn_owned("   ", 42)
    assert len(catalog) == 0


def test_catalog_round_trips_through_storage() -> None:
    catalog = ItemCatalog()
    catalog.learn_owned("Cloak of Flames", 11621)
    catalog.learn_remote(FUNGI, 2735)
    restored = ItemCatalog.from_dict(catalog.to_dict())
    assert restored.resolve("Cloak of Flames").status is IdStatus.OWNED
    assert restored.resolve(FUNGI).status is IdStatus.UNVERIFIED


def test_malformed_storage_yields_an_empty_catalog() -> None:
    assert len(ItemCatalog.from_dict(None)) == 0
    assert len(ItemCatalog.from_dict({"owned": "not a dict"})) == 0
    assert len(ItemCatalog.from_dict({"owned": {"thing": "not an int"}})) == 0
