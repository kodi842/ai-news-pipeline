"""Конфиг: sources.yaml (города/источники) + settings.yaml (параметры процесса)."""
import pytest

from tgtraff.config import load_config


@pytest.fixture
def cfg():
    return load_config()


def test_loads_three_pilot_cities(cfg):
    assert set(cfg.cities) == {"magnitogorsk", "novokuznetsk", "cherepovets"}


def test_city_carries_name_and_timezone(cfg):
    che = cfg.cities["cherepovets"]
    assert che.name == "Череповец"
    assert che.tz == "Europe/Moscow"
    assert cfg.cities["novokuznetsk"].tz == "Asia/Novokuznetsk"


def test_verified_tg_source_has_username_and_no_filter(cfg):
    src = {s.key: s for s in cfg.cities["cherepovets"].tg_sources}["example_channel_7"]
    assert src.source_type == "tg"
    assert src.status == "verified"
    assert src.priority == "high"
    assert src.city_filter is None
    assert src.city == "cherepovets"


def test_regional_source_keeps_city_filter(cfg):
    reg = {s.key: s for s in cfg.cities["magnitogorsk"].tg_regional}
    assert reg["news_74ru"].city_filter == "магнитогорск"
    assert reg["news_74ru"].source_type == "tg_regional"


def test_unresolved_source_has_query_but_no_key(cfg):
    unresolved = [s for s in cfg.cities["magnitogorsk"].tg_sources if s.status == "resolve"]
    assert unresolved, "в конфиге есть записи status: resolve — они должны парситься"
    assert all(s.key is None for s in unresolved)
    assert any(s.query == "Магнитогорск 360°" for s in unresolved)


def test_collectable_tg_sources_are_verified_with_username(cfg):
    srcs = cfg.collectable_tg_sources()
    assert len(srcs) >= 20
    assert all(s.key and s.status == "verified" for s in srcs)
    assert {s.source_type for s in srcs} == {"tg", "tg_regional"}


def test_collectable_sources_limited_to_active_cities(tmp_path):
    settings = tmp_path / "settings.yaml"
    settings.write_text("active_cities: [cherepovets]\n", encoding="utf-8")
    cfg = load_config(settings_path=settings)
    assert {s.city for s in cfg.collectable_tg_sources()} == {"cherepovets"}


def test_warmup_settings_have_daily_quota_and_human_hours(cfg):
    w = cfg.warmup
    assert (w.per_day_min, w.per_day_max) == (5, 10)
    assert w.hour_from < w.hour_to
    assert w.gap_minutes_min < w.gap_minutes_max


def test_paths_are_absolute(cfg):
    assert cfg.paths.db.is_absolute()
    assert cfg.paths.media_dir.is_absolute()
    assert cfg.paths.session.is_absolute()
    assert "secrets" in cfg.paths.session.parts


# ---- отпечаток устройства (должен совпадать с тем, на котором создана сессия) ----

def test_device_defaults(monkeypatch):
    for var in ("TG_DEVICE_MODEL", "TG_SYSTEM_VERSION", "TG_APP_VERSION", "TG_LANG_CODE"):
        monkeypatch.delenv(var, raising=False)
    d = load_config().device
    assert d.model and d.system_version and d.app_version
    assert d.lang_code == "en"


def test_device_is_overridable_from_env(monkeypatch):
    monkeypatch.setenv("TG_DEVICE_MODEL", "7RWRZ-PRO")
    monkeypatch.setenv("TG_SYSTEM_VERSION", "Windows 10")
    monkeypatch.setenv("TG_APP_VERSION", "7.0.9 x64")
    monkeypatch.setenv("TG_LANG_CODE", "ru")
    d = load_config().device
    assert (d.model, d.system_version, d.app_version, d.lang_code) == \
           ("7RWRZ-PRO", "Windows 10", "7.0.9 x64", "ru")


def test_device_kwargs_feed_telethon_client(monkeypatch):
    monkeypatch.setenv("TG_DEVICE_MODEL", "7RWRZ-PRO")
    kw = load_config().device.as_kwargs()
    assert kw["device_model"] == "7RWRZ-PRO"
    assert set(kw) == {"device_model", "system_version", "app_version", "lang_code", "system_lang_code"}


# ---- ревью фазы 1: молчащие источники (status: dormant) ----

def test_dormant_source_is_not_collectable():
    """Канал, который перестал публиковать, держим в конфиге с пометкой, но не читаем:
    иначе он тянет квоту прогрева и висит в алерте «источник молчит > 24 ч»."""
    from tgtraff.config import SourceSpec
    assert SourceSpec(city="novokuznetsk", source_type="tg", key="x", status="dormant").is_collectable is False


def test_dormant_channels_are_marked_in_config(cfg):
    """@example_channel_11 молчит с апреля 2026 (условие ревью фазы 1)."""
    nvkz = {s.key: s for s in cfg.cities["novokuznetsk"].tg_sources}
    assert nvkz["example_channel_11"].status == "dormant"
    assert "example_channel_11" not in {s.key for s in cfg.collectable_tg_sources()}


def test_config_never_collects_anything_but_verified(cfg):
    """Единственный статус, по которому источник читается, — verified."""
    assert all(s.status == "verified" for s in cfg.collectable_tg_sources())


# ---- официальные источники, живущие на чужой платформе (§13) ----

def test_vk_public_of_an_agency_is_marked_official():
    """МЧС и администрации ведут ВК-паблики: читает их ВК-коллектор (source_type=vk),
    но для медиа-каскада фазы 5 это «официальный источник» — приоритет 1."""
    from tgtraff.config import SourceSpec
    assert SourceSpec(city="cherepovets", source_type="vk", key="example_channel_8", official=True).is_official
    assert not SourceSpec(city="cherepovets", source_type="vk", key="cityche").is_official


def test_sources_from_official_section_are_official_by_type():
    """Записи из блока official помечать флагом руками не нужно."""
    from tgtraff.config import SourceSpec
    assert SourceSpec(city="cherepovets", source_type="official", key="https://x/rss").is_official


def test_agency_vk_publics_are_in_config(cfg):
    vk = {s.key: s for s in cfg.collectable_sources("vk")}
    assert "example_channel_8" in vk and vk["example_channel_8"].is_official
    assert vk["example_channel_8"].city_filter == "череповец"      # областная лента — фильтр по городу


# --- фаза 4: rewriter, relevance, safety, деньги ----------------------------

def test_llm_settings_default_to_haiku(cfg):
    assert cfg.llm.model.startswith("claude-haiku")
    assert cfg.llm.enabled is True


def test_rewriter_and_relevance_blocks_are_loaded(cfg):
    assert cfg.rewriter.max_tokens > 0
    assert cfg.relevance.use_llm is True
    assert cfg.media.use_template_cover is True


def test_autopublish_list_matches_the_owners_decision(cfg):
    """§59 (решение владельца 2026-09-05): режим включён вручную и только на
    нейтральных категориях. dtp/fire/crime — руками до недельной статистики:
    жертвы, имена, презумпция невиновности."""
    assert cfg.safety.autopublish_categories == ["weather", "utility", "city", "other"]
    assert cfg.safety.may_autopublish("safe", "city") is True
    for risky in ("dtp", "fire", "crime"):
        assert cfg.safety.may_autopublish("safe", risky) is False, risky
    assert cfg.safety.may_autopublish("manual_only", "city") is False


def test_anthropic_key_comes_from_env_only(monkeypatch, tmp_path):
    from tgtraff.config import load_config
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    cfg = load_config(root=tmp_path if False else None)
    assert cfg.anthropic_key == "sk-test-123"


def test_sync_sources_writes_official_flag_to_db(tmp_path, cfg):
    """Медиа-каскад читает official из базы — значит, конфиг надо синхронизировать."""
    from tgtraff.app import sync_sources
    from tgtraff.db import Database
    db = Database(tmp_path / "t.db")
    assert sync_sources(cfg, db) > 0
    in_db = db.conn.execute("SELECT COUNT(*) c FROM sources WHERE official=1").fetchone()["c"]
    in_config = sum(1 for city in cfg.active() for s in city.all_sources()
                    if s.is_official and s.key)
    assert in_db == in_config > 0
    db.close()


# --- фаза V1: завод клипов ----------------------------------------------------

def test_clips_block_is_read_from_settings(tmp_path):
    """Параметры завода клипов — конфиг, а не константы в коде: лимит на город,
    порог виральности, голос озвучки правятся без пересборки образа."""
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        "active_cities: [magnitogorsk]\n"
        "clips:\n"
        "  enabled: true\n"
        "  clips_per_day: 5\n"
        "  min_score: 7\n"
        "  favorite_categories: [dtp, fire]\n"
        "  tts_voice: ru-RU-SvetlanaNeural\n"
        "  platforms: [VK Клипы]\n", encoding="utf-8")
    cfg = load_config(settings_path=settings)
    assert cfg.clips.enabled is True
    assert cfg.clips.clips_per_day == 5
    assert cfg.clips.min_score == 7
    assert cfg.clips.favorite_categories == ("dtp", "fire")
    assert cfg.clips.tts_voice == "ru-RU-SvetlanaNeural"
    assert cfg.clips.platforms == ("VK Клипы",)


def test_clips_defaults_when_block_is_absent(tmp_path):
    """Старый settings.yaml без блока clips завод не ломает."""
    settings = tmp_path / "settings.yaml"
    settings.write_text("active_cities: [magnitogorsk]\n", encoding="utf-8")
    cfg = load_config(settings_path=settings)
    assert cfg.clips.clips_per_day == 3
    assert cfg.clips.render_parallelism == 1


def test_clip_output_dirs_are_absolute(tmp_path):
    """Процесс клипов запускается из разных каталогов (CLI, контейнер) —
    относительный путь к data/clips выстрелил бы файлами не там."""
    settings = tmp_path / "settings.yaml"
    settings.write_text("active_cities: [magnitogorsk]\nclips:\n  out_dir: data/clips\n",
                        encoding="utf-8")
    cfg = load_config(settings_path=settings)
    from pathlib import Path

    assert Path(cfg.clips.out_dir).is_absolute()
    assert cfg.clips.out_dir.endswith("data/clips")
