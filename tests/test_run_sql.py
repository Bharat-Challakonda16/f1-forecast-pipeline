from f1_forecast.ops.run_sql import render, split_statements


def test_split_statements_on_line_end_semicolons():
    sql = """-- header comment
CREATE TABLE a AS SELECT 1;

-- section
SELECT 'x;y' AS s; -- trailing comment
SELECT 2
FROM t;
-- only a comment;
"""
    parts = split_statements(sql)
    assert len(parts) == 3
    assert parts[0].endswith("CREATE TABLE a AS SELECT 1")
    assert "'x;y'" in parts[1]  # a semicolon inside a string doesn't split
    assert parts[2].endswith("FROM t")


def test_render_substitutes_catalog():
    assert render("SELECT * FROM ${catalog}.silver.laps", "f1") == "SELECT * FROM f1.silver.laps"
