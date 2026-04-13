"""
generate_committees.py – Génère committees.json complet pour les élus du 119e Congrès.

Source : unitedstates/congress-legislators (GitHub, données publiques)
  - legislators-current.yaml  → noms, party, state, chamber, bioguide_id
  - committees-current.yaml   → systemCode → committee name
  - committee-membership-current.yaml → bioguide_id → list of committees

Avantage : données complètes et fiables, pas de limite d'API.
Usage  : python3 generate_committees.py
Durée  : ~30 secondes
"""

import json
import re
import urllib.request
from pathlib import Path

import yaml

BASE = "https://raw.githubusercontent.com/unitedstates/congress-legislators/main"
OUT_FILE = Path(__file__).parent / "committees.json"


def fetch_yaml(filename: str) -> object:
    url = f"{BASE}/{filename}"
    print(f"  Downloading {filename}…")
    req = urllib.request.Request(url, headers={"User-Agent": "CongressTracker/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return yaml.safe_load(r.read().decode("utf-8"))


def normalize_key(name: str) -> str:
    clean = re.sub(r"[^\w\s]", "", name).strip().lower()
    return "_".join(clean.split())


def shorten_name(name: str) -> str:
    """Remove redundant prefixes to keep committee names concise."""
    return (name
        .replace("Committee on ", "")
        .replace("Joint Committee on ", "Joint ")
        .replace("Select Committee on ", "Select - ")
        .replace("Special Committee on ", "Special - ")
        .strip())


def normalize_party(party: str) -> str:
    mapping = {
        "Democrat": "Democrat",
        "Republican": "Republican",
        "Independent": "Independent",
    }
    # congress-legislators uses "Democrat", "Republican", "Independent"
    return mapping.get(party, party)


def main():
    print("🏛️  Congress Committee Generator\n")
    print("📥 Fetching data from unitedstates/congress-legislators…\n")

    # ── 1. Load source data ────────────────────────────────────────────────────
    legislators = fetch_yaml("legislators-current.yaml")
    committees_raw = fetch_yaml("committees-current.yaml")
    memberships = fetch_yaml("committee-membership-current.yaml")

    # ── 2. Build committee code → name map ────────────────────────────────────
    code_to_name: dict[str, str] = {}
    for c in committees_raw:
        code = c.get("thomas_id") or c.get("committee_id", "")
        name = c.get("name", "").strip()
        if code and name:
            short = shorten_name(name)
            code_to_name[code] = short
            # Also index subcommittees
            for sub in c.get("subcommittees", []):
                sub_code = code + sub.get("thomas_id", "")
                sub_name = sub.get("name", "").strip()
                if sub_code and sub_name:
                    code_to_name[sub_code] = short  # map to parent committee

    print(f"\n✅ {len(code_to_name)} committee codes indexed")

    # ── 3. Build bioguide → committees map ────────────────────────────────────
    bio_to_committees: dict[str, list[str]] = {}
    total_memberships = 0

    for comm_code, members in memberships.items():
        parent_code = comm_code[:4] if len(comm_code) > 4 else comm_code
        comm_name = code_to_name.get(comm_code) or code_to_name.get(parent_code, "")
        if not comm_name:
            continue

        for m in (members or []):
            bio_id = m.get("bioguide", "")
            if not bio_id:
                continue
            if bio_id not in bio_to_committees:
                bio_to_committees[bio_id] = []
            if comm_name not in bio_to_committees[bio_id]:
                bio_to_committees[bio_id].append(comm_name)
                total_memberships += 1

    print(f"✅ {len(bio_to_committees)} members with committee assignments")
    print(f"   Total: {total_memberships} assignments\n")

    # ── 4. Build output dict ───────────────────────────────────────────────────
    result = {
        "_doc": (
            f"Commissions des élus du Congrès américain (en cours). "
            f"Source: unitedstates/congress-legislators (GitHub). "
            f"Généré le {__import__('datetime').date.today()}. "
            f"Clé = nom en minuscules avec underscores (first_last ET last_first)."
        )
    }

    with_committees = 0
    processed = 0

    for leg in legislators:
        bio_id = leg.get("id", {}).get("bioguide", "")
        if not bio_id:
            continue

        name_obj = leg.get("name", {})
        first = name_obj.get("first", "")
        last  = name_obj.get("last", "")
        # Some have "official_full"
        full_name = name_obj.get("official_full") or f"{first} {last}".strip()

        # Current term
        terms = leg.get("terms", [])
        if not terms:
            continue
        last_term = terms[-1]
        chamber_raw = last_term.get("type", "")
        chamber = "senate" if chamber_raw == "sen" else "house"
        state = last_term.get("state", "")
        party = normalize_party(last_term.get("party", ""))

        committees = bio_to_committees.get(bio_id, [])

        data = {
            "bio_guide_id": bio_id,
            "chamber":      chamber,
            "party":        party,
            "state":        state,
            "committees":   committees,
        }

        # Build keys
        parts = full_name.split()
        first_last = normalize_key(full_name)
        last_first = normalize_key(f"{parts[-1]} {' '.join(parts[:-1])}") if len(parts) >= 2 else first_last

        # Primary key: first_last
        result[first_last] = data
        # Alias: last_first (different key for enricher.py lookup)
        if last_first != first_last:
            result[last_first] = data

        if committees:
            with_committees += 1
        processed += 1

    # ── 5. Save ────────────────────────────────────────────────────────────────
    OUT_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    pct = with_committees / processed * 100 if processed else 0
    print(f"✅ Done!")
    print(f"   Legislators processed : {processed}")
    print(f"   With committees       : {with_committees} ({pct:.0f}%)")
    print(f"   Output file           : {OUT_FILE}")
    print(f"   File size             : {OUT_FILE.stat().st_size / 1024:.1f} KB")

    # Quick sample
    sample = [(k, v) for k, v in result.items()
              if k != "_doc" and v.get("committees")]
    if sample:
        k, v = sample[0]
        print(f"\n📋 Sample — {k}:")
        print(f"   Party: {v['party']}, Chamber: {v['chamber']}, State: {v['state']}")
        print(f"   Committees: {v['committees'][:3]}")


if __name__ == "__main__":
    main()
