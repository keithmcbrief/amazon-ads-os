import csv

import _common as c


def test_dangerous_leads_are_prefixed():
    assert c.escape_csv_text("=cmd|") == "'=cmd|"
    assert c.escape_csv_text("+1234") == "'+1234"
    assert c.escape_csv_text("-foo") == "'-foo"
    assert c.escape_csv_text("@bar") == "'@bar"
    assert c.escape_csv_text("\tboom") == "'\tboom"


def test_safe_text_is_unchanged():
    assert c.escape_csv_text("hello world") == "hello world"
    assert c.escape_csv_text("blue sneakers size 10") == "blue sneakers size 10"
    assert c.escape_csv_text("") == ""


def test_write_rows_csv_keeps_ids_as_plain_strings(tmp_path):
    path = tmp_path / "out.csv"
    columns = ["campaignId", "adGroupId", "searchTerm", "spend"]
    rows = [
        {
            "campaignId": "123456789012345",
            "adGroupId": "987654321098765",
            "searchTerm": "blue sneakers",
            "spend": 12.34,
        },
        {
            "campaignId": "111111111111111",
            "adGroupId": "222222222222222",
            "searchTerm": "=cmd|/c calc",
            "spend": 0,
        },
    ]
    c.write_rows_csv(path, columns, rows)
    out = list(csv.reader(path.open()))
    assert out[0] == columns
    # IDs preserved with no apostrophe prefix, no scientific notation
    assert out[1][0] == "123456789012345"
    assert out[1][1] == "987654321098765"
    # Text field with dangerous lead got escaped
    assert out[2][2].startswith("'=")
    # Safe text untouched
    assert out[1][2] == "blue sneakers"


def test_empty_rows_still_writes_header(tmp_path):
    path = tmp_path / "out.csv"
    columns = ["a", "b"]
    c.write_rows_csv(path, columns, [])
    out = list(csv.reader(path.open()))
    assert out == [["a", "b"]]
