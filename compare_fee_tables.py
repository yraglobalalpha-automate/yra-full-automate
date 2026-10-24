"""Compare the commission tables refresh_fees.py just wrote with the ones committed in the repository (2026-10-24, for the dashboard's
Fees card). The syncs price from the committed tables, so the difference is what a commit would change: tiers whose rates moved, categories
OnBuy dropped (a row in one of them prices with the flat rate until it is moved), new categories, categories that changed tier.

Prints one TABLES line the dashboard reads - "TABLES UNCHANGED: ..." or "TABLES DIFFER: <tiers>; categories N gone, M new, K changed tier
(commit=yes writes them)" - then up to 20 example lines per kind. The committed versions come from `git show HEAD:<file>` (a missing
committed file counts as everything new). Pure compare() / report() for the tests; main() does the git reads. Exit code 0 either way."""
import csv
import io
import subprocess

TIERS = "onbuy_commission_tiers.csv"
CATEGORIES = "onbuy_category_tiers.csv"


def _rows(text):
    return list(csv.DictReader(io.StringIO((text or "").lstrip("\ufeff"))))


def _order(cid):
    return (0, int(cid)) if cid.isdigit() else (1, cid)


def tier_map(text):
    """tier id -> (name, lower %, upper %, threshold, min fee), all as the CSV's strings."""
    out = {}
    for r in _rows(text):
        tid = (r.get("Tier ID") or "").strip()
        if tid:
            out[tid] = tuple((r.get(k) or "").strip() for k in ("Name", "Lower %", "Upper %", "Threshold (£)", "Min Fee (£)"))
    return out


def category_map(text):
    """category id -> (tier id, tier name)."""
    out = {}
    for r in _rows(text):
        cid = (r.get("Category ID") or "").strip()
        if cid:
            out[cid] = ((r.get("Commission Tier ID") or "").strip(), (r.get("Tier Name") or "").strip())
    return out


def compare(old_tiers, new_tiers, old_categories, new_categories):
    """What a commit of the fresh tables would change against the committed ones."""
    ot, nt = tier_map(old_tiers), tier_map(new_tiers)
    oc, nc = category_map(old_categories), category_map(new_categories)
    tiers_changed = [(tid, ot.get(tid), nt.get(tid)) for tid in sorted(set(ot) | set(nt)) if ot.get(tid) != nt.get(tid)]
    gone = sorted(set(oc) - set(nc), key=_order)
    new = sorted(set(nc) - set(oc), key=_order)
    changed = sorted((cid for cid in set(oc) & set(nc) if oc[cid][0] != nc[cid][0]), key=_order)
    return {"tiers_changed": tiers_changed,
            "gone": [(cid, oc[cid][1]) for cid in gone],
            "new": [(cid, nc[cid][1]) for cid in new],
            "changed_tier": [(cid, oc[cid][1], nc[cid][1]) for cid in changed],
            "unchanged": not tiers_changed and not gone and not new and not changed}


def report(result, limit=20):
    """The lines to print; the first is the TABLES line the dashboard reads."""
    if result["unchanged"]:
        return ["TABLES UNCHANGED: the committed tables already match OnBuy"]
    tiers = "tiers unchanged" if not result["tiers_changed"] else f"{len(result['tiers_changed'])} tier(s) changed"
    lines = [f"TABLES DIFFER: {tiers}; categories {len(result['gone'])} gone, {len(result['new'])} new, "
             f"{len(result['changed_tier'])} changed tier (commit=yes writes them)"]
    for tid, old, new in result["tiers_changed"][:limit]:
        lines.append(f"   tier {tid}: {old} -> {new}")
    for cid, name in result["gone"][:limit]:
        lines.append(f"   gone: {cid} ({name or 'no tier'}) - a row still in this category prices with the flat rate")
    for cid, name in result["new"][:limit]:
        lines.append(f"   new: {cid} ({name or 'no tier'})")
    for cid, old, new in result["changed_tier"][:limit]:
        lines.append(f"   tier changed: {cid} {old or 'no tier'} -> {new or 'no tier'}")
    return lines


def committed(path):
    """The file as committed at HEAD ('' when it has no committed version)."""
    try:
        return subprocess.run(["git", "show", f"HEAD:{path}"], capture_output=True, text=True, encoding="utf-8", check=True).stdout
    except (subprocess.CalledProcessError, OSError):
        return ""


def main():
    with io.open(TIERS, encoding="utf-8-sig") as fh:
        new_tiers = fh.read()
    with io.open(CATEGORIES, encoding="utf-8-sig") as fh:
        new_categories = fh.read()
    for line in report(compare(committed(TIERS), new_tiers, committed(CATEGORIES), new_categories)):
        print(line)


if __name__ == "__main__":
    main()
