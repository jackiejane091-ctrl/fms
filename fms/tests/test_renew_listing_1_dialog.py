from pathlib import Path


def test_renew_listing_1_dialog_has_renew_specific_labels_and_hides_price_fields():
    source = Path(__file__).parents[1].joinpath("templates", "index.html").read_text(encoding="utf-8")
    assert "function configureMinMaxDialog()" in source
    assert "'Min Renew Listings' : 'Min listings'" in source
    assert "'Max Renew Listings' : 'Max listings'" in source
    assert "mmMinPriceField').style.display = isRenew1 ? 'none'" in source
    assert "mmMaxPriceField').style.display = isRenew1 ? 'none'" in source


def test_renew_listing_1_sends_values_to_engine_renew_count_fields():
    source = Path(__file__).parents[1].joinpath("templates", "index.html").read_text(encoding="utf-8")
    assert "if(actVal === 'renew_listing_1')" in source
    assert "body.renew_min_count = _minA;" in source
    assert "body.renew_max_count = _maxA;" in source


def test_post_and_other_actions_keep_existing_price_filter_path():
    source = Path(__file__).parents[1].joinpath("templates", "index.html").read_text(encoding="utf-8")
    assert "// Preserve the existing Post and other actions' price-filter behavior." in source
    assert "body.min_price = Math.min(_minP, _maxP); body.max_price = Math.max(_minP, _maxP);" in source
