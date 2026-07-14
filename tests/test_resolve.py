from __future__ import annotations

import pytest

from app import bot, db
from tests.conftest import ASSETS_JSON, PROMPTS_JSON
from tests.test_bot import make_context, make_update


def test_resolve_by_canonical_code_case_insensitive(seeded_db):
    asset, candidates = db.resolve_asset("alibaba")
    assert asset and asset["code"] == "ALIBABA"
    assert candidates == []


def test_resolve_by_alias(seeded_db):
    # У ALIBABA в исходных данных алиас BABA.
    asset, _ = db.resolve_asset("baba")
    assert asset and asset["code"] == "ALIBABA"


def test_resolve_by_full_name(seeded_db):
    name = db.get_asset("BAIDU")["name"]
    asset, _ = db.resolve_asset(name.lower())
    assert asset and asset["code"] == "BAIDU"


def test_resolve_by_name_substring(seeded_db):
    asset, candidates = db.resolve_asset("Tencent")
    codes = [asset["code"]] if asset else [a["code"] for a in candidates]
    assert "TENCENT" in codes


def test_resolve_composite_moex_code_parts(seeded_db):
    """Код "BR, BRM" содержит два контрактных кода — каждый должен резолвиться отдельно."""
    for part in ("BR", "BRM", "br"):
        asset, _ = db.resolve_asset(part)
        assert asset and asset["code"] == "BR, BRM", part


def test_resolve_ambiguous_returns_candidates(tmp_db):
    db.upsert_asset("AAA", "Global Fund Alpha", "ETF")
    db.upsert_asset("BBB", "Global Fund Beta", "ETF")
    asset, candidates = db.resolve_asset("Global Fund")
    assert asset is None
    assert {a["code"] for a in candidates} == {"AAA", "BBB"}


def test_resolve_unknown(seeded_db):
    asset, candidates = db.resolve_asset("несуществующий актив xyz")
    assert asset is None and candidates == []


def test_add_alias_then_resolve(tmp_db):
    db.upsert_asset("TSM", "Taiwan Semiconductor ADR", "ADR")
    assert db.add_alias("TSM", "2330.TW") is True
    assert db.add_alias("TSM", "2330.tw") is False, "алиасы должны быть уникальны без учёта регистра"
    asset, _ = db.resolve_asset("2330.TW")
    assert asset and asset["code"] == "TSM"


def test_migration_is_idempotent_and_covers_json_aliases(seeded_db):
    db.init_db()  # повторная инициализация не должна дублировать идентификаторы
    with db.connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM asset_identifiers").fetchone()[0]
    db.init_db()
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM asset_identifiers").fetchone()[0] == total
    # У каждого актива есть как минимум идентификатор-код.
    with db.connect() as conn:
        without = conn.execute(
            "SELECT COUNT(*) FROM assets a WHERE NOT EXISTS "
            "(SELECT 1 FROM asset_identifiers i WHERE i.asset_code=a.code AND i.kind='code')").fetchone()[0]
    assert without == 0


def test_remove_asset_cascades_identifiers(tmp_db):
    db.upsert_asset("TSM", "Taiwan Semiconductor ADR", "ADR", aliases=["TSMC"])
    db.remove_asset("TSM")
    asset, candidates = db.resolve_asset("TSMC")
    assert asset is None and candidates == []


@pytest.mark.asyncio
async def test_alias_command(tmp_db):
    db.upsert_asset("TSM", "Taiwan Semiconductor ADR", "ADR")
    update = make_update()
    await bot.add_alias_cmd(update, make_context(["Taiwan", "Semiconductor", "ADR", "|", "TSMC"]))
    text = update.effective_message.reply_text.await_args.args[0]
    assert "добавлен" in text and "TSM" in text
    asset, _ = db.resolve_asset("TSMC")
    assert asset and asset["code"] == "TSM"


@pytest.mark.asyncio
async def test_find_command(tmp_db):
    db.upsert_asset("TSM", "Taiwan Semiconductor ADR", "ADR", aliases=["TSMC"])
    update = make_update()
    await bot.find_assets(update, make_context(["TSMC"]))
    text = update.effective_message.reply_text.await_args.args[0]
    assert "TSM" in text and "TSMC" in text


@pytest.mark.asyncio
async def test_addsource_by_alias(tmp_db):
    db.upsert_asset("TSM", "Taiwan Semiconductor ADR", "ADR", aliases=["TSMC"])
    update = make_update()
    await bot.add_source(update, make_context(["tsmc", "|", "https://investor.tsmc.com"]))
    assert [s["url"] for s in db.get_asset("TSM")["sources"]] == ["https://investor.tsmc.com"]


@pytest.mark.asyncio
async def test_remove_by_name(tmp_db):
    db.upsert_asset("TSM", "Taiwan Semiconductor ADR", "ADR")
    update = make_update()
    await bot.remove_asset(update, make_context(["Taiwan", "Semiconductor", "ADR"]))
    assert db.get_asset("TSM") is None


@pytest.mark.asyncio
async def test_add_asset_with_aliases(tmp_db):
    update = make_update()
    await bot.add_asset(update, make_context(
        ["NVDA", "|", "SHARE", "|", "NVIDIA", "Corp", "|", "Nvidia,", "NVD"]))
    asset = db.get_asset("NVDA")
    assert asset and set(asset["aliases"]) == {"Nvidia", "NVD"}
    resolved, _ = db.resolve_asset("nvidia")
    assert resolved and resolved["code"] == "NVDA"
