"""Marketplace ID → country mapping. Avoids the UX bug where the wizard
showed cryptic IDs like ATVPDKIKX0DER and a user picked the wrong country."""
import _common as c


def test_us_marketplace_resolves():
    assert c.marketplace_label("ATVPDKIKX0DER") == "United States (US)"
    assert c.marketplace_country_code("ATVPDKIKX0DER") == "US"


def test_ca_mx_br_in_na_region():
    assert c.marketplace_label("A2EUQ1WTGCTBG2") == "Canada (CA)"
    assert c.marketplace_label("A1AM78C64UM0Y8") == "Mexico (MX)"
    assert c.marketplace_label("A2Q3Y263D00KWC") == "Brazil (BR)"
    for mp in ("ATVPDKIKX0DER", "A2EUQ1WTGCTBG2", "A1AM78C64UM0Y8", "A2Q3Y263D00KWC"):
        assert c.MARKETPLACES[mp][2] == "NA"


def test_eu_region_markets():
    assert c.marketplace_label("A1F83G8C2ARO7P") == "United Kingdom (UK)"
    assert c.marketplace_label("A1PA6795UKMFR9") == "Germany (DE)"
    assert c.MARKETPLACES["A1F83G8C2ARO7P"][2] == "EU"


def test_fe_region_markets():
    assert c.marketplace_label("A1VC38T7YXB528") == "Japan (JP)"
    assert c.marketplace_label("A39IBJ37TRP1C6") == "Australia (AU)"


def test_unknown_marketplace_falls_back_to_country_code():
    assert c.marketplace_label("UNKNOWN_ID_123", country_code="ZZ") == "ZZ"


def test_unknown_marketplace_falls_back_to_raw_id():
    assert c.marketplace_label("UNKNOWN_ID_123") == "UNKNOWN_ID_123"


def test_country_code_helper_prefers_api_value_over_map():
    """If Amazon returns countryCode explicitly, trust it over our static map."""
    assert c.marketplace_country_code("ATVPDKIKX0DER", country_code="us") == "US"


def test_country_code_helper_falls_back_to_map():
    """When countryCode is missing (the real bug), map from marketplace ID."""
    assert c.marketplace_country_code("ATVPDKIKX0DER") == "US"
    assert c.marketplace_country_code("A2EUQ1WTGCTBG2") == "CA"


def test_country_code_helper_returns_empty_when_unknown():
    assert c.marketplace_country_code("UNKNOWN") == ""
    assert c.marketplace_country_code(None) == ""
