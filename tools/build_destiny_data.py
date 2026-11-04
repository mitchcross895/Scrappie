"""Regenerate scrappie/data/raids.json and items.json from Bungie's public manifest.

Run from the repo root after a Destiny 2 content patch:
    python tools/build_destiny_data.py
Needs about 1 GB of memory and a 300 MB download, and no API key.
--source DIR (repeatable) reads <Component>.json from DIR instead of downloading it.
The bot never runs this file.
"""
import argparse
import collections
import gzip
import json
import pathlib
import re
import unicodedata
import urllib.request

ROOT = "https://www.bungie.net"
USER_AGENT = "scrappie-data-build"
DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "scrappie" / "data"

# Release order. Each weekly slot moves one step along this list and the two slots stay 4 places
# apart: that fits all 22 weeks from 2026-06-09 to 2026-11-03 on d2rad.com/rotation-history.
# Not documented by Bungie. Values are the exact Collections sourceString for each raid.
RAID_SOURCES = {
    "Last Wish": "Source: Last Wish raid.",
    "Garden of Salvation": 'Source: "Garden of Salvation" Raid',
    "Deep Stone Crypt": 'Source: "Deep Stone Crypt" Raid',
    "Vault of Glass": 'Source: "Vault of Glass" Raid',
    "Vow of the Disciple": 'Source: "Vow of the Disciple" Raid',
    "King's Fall": 'Source: "King\'s Fall" Raid',
    "Root of Nightmares": 'Source: "Root of Nightmares" Raid',
    "Crota's End": 'Source: "Crota\'s End" Raid',
    "Salvation's Edge": 'Source: "Salvation\'s Edge" Raid',
}
COMPONENTS = [
    "DestinyInventoryItemDefinition", "DestinyCollectibleDefinition", "DestinyEquipableItemSetDefinition",
    "DestinySandboxPerkDefinition", "DestinyActivityDefinition", "DestinyActivityModifierDefinition",
    "DestinyMilestoneDefinition", "DestinyObjectiveDefinition",
]
WEEKLY_OBJECTIVE = "Weekly Raid Challenge"
WEAPON_TIERS = {"Legendary", "Exotic"}
VARIANT_TAG = re.compile(r"\s*\((?:Adept|Timelost|Harrowed)\)$")
CLASS_NAMES = {0: "Titan", 1: "Hunter", 2: "Warlock"}

def fetch(path: str) -> bytes:
    request = urllib.request.Request(ROOT + path, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(request, timeout=300) as response:
        body = response.read()
        return gzip.decompress(body) if response.headers.get("Content-Encoding") == "gzip" else body

def load_components(sources: list[pathlib.Path], paths: dict[str, str]) -> dict[str, dict]:
    tables = {}
    for name in COMPONENTS:
        local = next((s / f"{name}.json" for s in sources if (s / f"{name}.json").is_file()), None)
        tables[name] = json.loads(local.read_bytes() if local else fetch(paths[name]))
        print(f"{name}: {len(tables[name])}")
    return tables

def name_of(definition: dict | None) -> str:
    return ((definition or {}).get("displayProperties") or {}).get("name") or ""

def normalise(text: str) -> str:
    stripped = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", "", stripped).casefold()

def sources_by_item(items: dict, collectibles: dict) -> dict[str, str]:
    out = {}
    for key, item in items.items():
        collectible = collectibles.get(str(item.get("collectibleHash"))) or {}
        out[key] = collectible.get("sourceString") or ""
    return out

def modifier_is_shown(modifier: dict) -> bool:
    name = name_of(modifier).strip()
    description = (modifier.get("displayProperties") or {}).get("description") or ""
    if not name or not modifier.get("displayInActivitySelection"):
        return False
    if name == "Master Modifiers" or "Boosts Gained" in name:  # A summary row and a personal drop-rate counter
        return False
    return not (name.endswith(" Challenge") or description.startswith("A challenge awaits"))  # Raid challenges

def build_armor_set(raid: str, source: str, t: dict, sources: dict) -> dict:
    items, perks = t["DestinyInventoryItemDefinition"], t["DestinySandboxPerkDefinition"]
    matches = [s for s in t["DestinyEquipableItemSetDefinition"].values()
               if sum(sources.get(str(h)) == source for h in s.get("setItems") or []) >= 12]
    assert len(matches) == 1, (raid, "armor sets", len(matches))
    armor_set = matches[0]
    pieces = collections.defaultdict(list)
    for item_hash in armor_set["setItems"]:
        item = items[str(item_hash)]
        pieces[CLASS_NAMES[item["classType"]]].append(name_of(item))
    bonuses = []
    for perk in sorted(armor_set["setPerks"], key=lambda p: p["requiredSetCount"]):
        definition = perks[str(perk["sandboxPerkHash"])]
        bonuses.append({"pieces": perk["requiredSetCount"], "name": name_of(definition),
                        "description": definition["displayProperties"]["description"]})
    assert [b["pieces"] for b in bonuses] == [2, 4], (raid, bonuses)
    return {"name": name_of(armor_set), "pieces": {k: sorted(pieces[k]) for k in sorted(pieces)}, "bonuses": bonuses}

def build_weapons(raid: str, source: str, t: dict, sources: dict) -> list[dict]:
    weapons = {}
    for key, item in t["DestinyInventoryItemDefinition"].items():
        tier = (item.get("inventory") or {}).get("tierTypeName")
        if item.get("itemType") == 3 and sources[key] == source and tier in WEAPON_TIERS:
            weapons[VARIANT_TAG.sub("", name_of(item))] = {"type": item["itemTypeDisplayName"], "exotic": tier == "Exotic"}
    assert len(weapons) >= 6 and sum(w["exotic"] for w in weapons.values()) <= 1, (raid, weapons)
    ordered = sorted(weapons.items(), key=lambda kv: (not kv[1]["exotic"], kv[0]))
    return [{"name": name, **details} for name, details in ordered]

def build_raids(t: dict, version: str, sources: dict) -> dict:
    activities = t["DestinyActivityDefinition"]
    milestones = {name_of(m): key for key, m in t["DestinyMilestoneDefinition"].items() if name_of(m) in RAID_SOURCES}
    assert len(milestones) == len(RAID_SOURCES), sorted(milestones)
    raids = {}
    for name, source in RAID_SOURCES.items():
        raids[milestones[name]] = {
            "name": name,
            "master_activities": sorted(int(k) for k, a in activities.items() if name_of(a) == f"{name}: Master"),
            "weapons": build_weapons(name, source, t, sources),
            "armor_set": build_armor_set(name, source, t, sources),
        }
    weekly = sorted(int(k) for k, o in t["DestinyObjectiveDefinition"].items() if name_of(o) == WEEKLY_OBJECTIVE)
    assert len(weekly) == len(RAID_SOURCES), weekly
    modifiers = t["DestinyActivityModifierDefinition"]
    return {
        "manifest_version": version,
        "rotation": list(RAID_SOURCES),
        "weekly_objectives": weekly,
        "modifiers": {k: name_of(m).strip() for k, m in sorted(modifiers.items(), key=lambda kv: int(kv[0])) if modifier_is_shown(m)},
        "ignored_modifiers": sorted(int(k) for k, m in modifiers.items() if not modifier_is_shown(m)),
        "raids": raids,
    }

def item_label(item: dict) -> str:
    kind = item.get("itemTypeDisplayName") or "Item"
    class_name = CLASS_NAMES.get(item.get("classType"))
    if item["itemType"] == 3 or class_name is None or kind.startswith(class_name):
        return kind
    return f"{class_name} {kind}"

def build_items(t: dict) -> list[list]:
    groups = collections.defaultdict(list)
    for key, item in t["DestinyInventoryItemDefinition"].items():
        if item.get("itemType") in (2, 3) and item.get("equippable") and name_of(item) and not item.get("redacted") and not item.get("blacklisted"):
            groups[(name_of(item), item_label(item))].append((item.get("index", 0), int(key)))
    rows = [[name, label, [h for _, h in sorted(copies, reverse=True)]] for (name, label), copies in groups.items()]
    return sorted(rows, key=lambda r: (normalise(r[0]), r[1]))

def write_lines(path: pathlib.Path, rows: list) -> None:
    """One array element per line, CRLF, so regenerated data gives a readable diff."""
    body = ",\r\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in rows)
    path.write_bytes(f"[\r\n{body}\r\n]\r\n".encode("utf-8"))

def write_raids(path: pathlib.Path, raids: dict) -> None:
    text = json.dumps(raids, ensure_ascii=False, indent=1)
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8") + b"\r\n")

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", action="append", type=pathlib.Path, default=[])
    parser.add_argument("--out", type=pathlib.Path, default=DATA_DIR)
    args = parser.parse_args()
    manifest = json.loads(fetch("/Platform/Destiny2/Manifest/"))
    assert manifest.get("ErrorCode") == 1, manifest.get("ErrorStatus")
    version = manifest["Response"]["version"]
    t = load_components(args.source, manifest["Response"]["jsonWorldComponentContentPaths"]["en"])
    sources = sources_by_item(t["DestinyInventoryItemDefinition"], t["DestinyCollectibleDefinition"])
    raids, items = build_raids(t, version, sources), build_items(t)
    args.out.mkdir(exist_ok=True)
    write_raids(args.out / "raids.json", raids)
    write_lines(args.out / "items.json", items)
    print(f"{version}: {len(raids['raids'])} raids, {len(items)} item names")

if __name__ == "__main__":
    main()