"""compare_fee_tables (2026-10-24): what a commit of freshly fetched commission tables would change against the committed ones."""
import compare_fee_tables as cft

OLD_T = "Tier ID,Name,Lower %,Upper %,Threshold (£),Min Fee (£)\nt1,Standard Selling Fee,15.00,,,0.25\nt2,Consumer Electronics,7.00,,,0.25\n"
NEW_T_CHANGED = OLD_T.replace("7.00", "8.00")
OLD_C = "Category ID,Commission Tier ID,Tier Name\n1,t1,Standard Selling Fee\n2,t2,Consumer Electronics\n3,t1,Standard Selling Fee\n"
NEW_C = "Category ID,Commission Tier ID,Tier Name\n1,t1,Standard Selling Fee\n2,t1,Standard Selling Fee\n4,t2,Consumer Electronics\n"


def test_unchanged_tables_say_so_in_one_line():
    r = cft.compare(OLD_T, OLD_T, OLD_C, OLD_C)
    assert r["unchanged"] and cft.report(r) == ["TABLES UNCHANGED: the committed tables already match OnBuy"]


def test_differences_are_counted_and_listed():
    r = cft.compare(OLD_T, NEW_T_CHANGED, OLD_C, NEW_C)
    assert not r["unchanged"] and len(r["tiers_changed"]) == 1 and r["tiers_changed"][0][0] == "t2"
    assert r["gone"] == [("3", "Standard Selling Fee")] and r["new"] == [("4", "Consumer Electronics")]
    assert r["changed_tier"] == [("2", "Consumer Electronics", "Standard Selling Fee")]
    lines = cft.report(r)
    assert lines[0] == "TABLES DIFFER: 1 tier(s) changed; categories 1 gone, 1 new, 1 changed tier (commit=yes writes them)"
    assert any(l.startswith("   gone: 3 (Standard Selling Fee)") for l in lines)
    assert any("tier changed: 2 Consumer Electronics -> Standard Selling Fee" in l for l in lines)


def test_a_missing_committed_table_counts_everything_as_new():
    r = cft.compare("", OLD_T, "", NEW_C)
    assert len(r["new"]) == 3 and r["gone"] == [] and len(r["tiers_changed"]) == 2
    assert cft.report(r)[0].startswith("TABLES DIFFER: 2 tier(s) changed; categories 0 gone, 3 new, 0 changed tier")
