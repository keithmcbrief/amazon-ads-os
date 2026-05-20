import _common as c


def _base_args():
    return dict(
        profile_id="12345",
        region="NA",
        slug="sp-campaigns",
        start="2026-04-01",
        end="2026-04-30",
        columns=c.REPORT_SPECS["sp-campaigns"]["columns"],
        group_by=c.REPORT_SPECS["sp-campaigns"]["groupBy"],
    )


def test_hash_is_stable():
    a = c.request_hash(**_base_args())
    b = c.request_hash(**_base_args())
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_hash_changes_when_dates_change():
    a = c.request_hash(**_base_args())
    args = _base_args()
    args["end"] = "2026-05-01"
    b = c.request_hash(**args)
    assert a != b


def test_hash_changes_when_profile_changes():
    a = c.request_hash(**_base_args())
    args = _base_args()
    args["profile_id"] = "99999"
    b = c.request_hash(**args)
    assert a != b


def test_hash_changes_when_specs_version_changes(monkeypatch):
    a = c.request_hash(**_base_args())
    monkeypatch.setattr(c, "REPORT_SPECS_VERSION", c.REPORT_SPECS_VERSION + 1)
    b = c.request_hash(**_base_args())
    assert a != b


def test_hash_changes_when_api_version_changes(monkeypatch):
    a = c.request_hash(**_base_args())
    monkeypatch.setattr(c, "ADS_API_VERSION", "v4")
    b = c.request_hash(**_base_args())
    assert a != b


def test_hash_is_order_independent_for_columns():
    a = c.request_hash(**_base_args())
    args = _base_args()
    args["columns"] = list(reversed(args["columns"]))
    b = c.request_hash(**args)
    assert a == b
