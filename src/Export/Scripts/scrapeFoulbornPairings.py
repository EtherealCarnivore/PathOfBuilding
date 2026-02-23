#!/usr/bin/env python3
"""
Scrape Foulborn mutation pairings from the PoE Trade API.

Queries the trade API for Foulborn-mutated items, then compares each item's
explicitMods and mutatedMods against the original unique's mod template to
derive which mods were removed and what replaced them.

Outputs in ModFoulbornPairs.lua format.

Requirements: requests (pip install requests)

Usage:
    python scrapeFoulbornPairings.py --poesessid <SESSID> [--league <league>]
        [--output <path>] [--missing-only --existing <path>]
"""

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

requests = None

TRADE_URL = "https://www.pathofexile.com/api/trade"
SEARCH_URL = f"{TRADE_URL}/search/{{league}}"
FETCH_URL = f"{TRADE_URL}/fetch/{{ids}}?query={{query_id}}"
FETCH_BATCH_SIZE = 10

# ── Unique Mod Parsing ──────────────────────────────────────────────────────

def parse_unique_files(data_dir):
    """Parse all unique .lua files and return {name: [explicit_mod_lines]}.

    Reads each unique data file in src/Data/Uniques/, splits on ']],' or ']]'
    boundaries, and extracts the explicit mod lines (everything after the
    implicit lines).
    """
    uniques = {}
    unique_dir = Path(data_dir) / "Data" / "Uniques"

    for lua_file in sorted(unique_dir.glob("*.lua")):
        if lua_file.name in ("graft.lua",):
            continue
        content = lua_file.read_text(encoding="utf-8", errors="replace")
        # Extract all [[ ... ]] blocks using findall
        raw_blocks = re.findall(r'\[\[(.*?)\]\]', content, re.DOTALL)
        for block in raw_blocks:
            block = block.strip()
            if not block:
                continue
            lines = block.split('\n')
            if len(lines) < 3:
                continue

            # First non-empty line is the name (possibly with variant prefix)
            name_line = lines[0].strip()
            # Strip variant tags like {variant:1,2}
            name = re.sub(r'\{[^}]*\}', '', name_line).strip()
            if not name or name.startswith('--') or name.startswith('return'):
                continue

            # Find Implicits: N line
            implicits_count = 0
            implicits_idx = None
            for i, line in enumerate(lines):
                stripped = line.strip()
                m = re.match(r'^Implicits:\s*(\d+)', stripped)
                if m:
                    implicits_count = int(m.group(1))
                    implicits_idx = i
                    break

            # Metadata line patterns (not mod lines)
            _META_RE = re.compile(
                r'^(Variant:|Selected Variant:|Has Alt Variant:|'
                r'Selected Alt Variant:|League:|Source:|'
                r'Requires |LevelReq:|Quality:|Radius:|Limited to:|'
                r'Implicits:|Crafted:|Elder Item|Shaper Item|'
                r'Has Catalyst|Has Influence|Evasion:|Energy Shield:|Armour:|'
                r'Sockets:|ItemLevelReq:)'
            )

            if implicits_idx is not None:
                # Explicit mods start after implicits line + N implicit lines
                explicit_start = implicits_idx + implicits_count + 1
            else:
                # No Implicits line: skip name (line 0), base type (line 1),
                # then skip any metadata lines; remaining are mods
                explicit_start = 2
                while explicit_start < len(lines):
                    stripped = lines[explicit_start].strip()
                    clean = re.sub(r'\{[^}]*\}', '', stripped).strip()
                    if clean and not _META_RE.match(clean):
                        break
                    explicit_start += 1

            explicit_mods = []
            for line in lines[explicit_start:]:
                stripped = line.strip()
                if not stripped or stripped.startswith('Corrupted'):
                    continue
                # Strip tags like {variant:N}, {crafted}, {tags:...}, {range:...}
                mod_text = re.sub(r'\{[^}]*\}', '', stripped).strip()
                if mod_text:
                    explicit_mods.append(mod_text)

            # Only store the "Current" variant's view -- for matching purposes
            # we want all mods (the union across variants is fine for detection)
            if name not in uniques:
                uniques[name] = explicit_mods
            else:
                # Merge: add any mods not already known
                existing = set(uniques[name])
                for mod in explicit_mods:
                    if mod not in existing:
                        uniques[name].append(mod)
                        existing.add(mod)

    return uniques


# ── Existing Pairs Parsing ──────────────────────────────────────────────────

def parse_existing_pairs(filepath):
    """Parse an existing ModFoulbornPairs.lua file and return the set of unique names."""
    names = set()
    try:
        content = Path(filepath).read_text(encoding="utf-8")
    except FileNotFoundError:
        return names
    for m in re.finditer(r'\["([^"]+)"\]\s*=\s*\{', content):
        names.add(m.group(1))
    return names


def parse_existing_pairs_full(filepath):
    """Parse existing ModFoulbornPairs.lua and return the full data structure.

    Returns {name: [{"removed": [str], "added": [str]} or {"removed": [str], "alternatives": [[str]]}]}
    """
    data = {}
    try:
        content = Path(filepath).read_text(encoding="utf-8")
    except FileNotFoundError:
        return data

    # Simple state-machine parser for the Lua table
    current_unique = None
    current_slot = None
    current_field = None  # "removed", "added", or "alternatives"
    current_alt = None
    in_alt = False

    for line in content.split('\n'):
        stripped = line.strip()

        # Match unique name: ["Name"] = {
        m = re.match(r'^\["([^"]+)"\]\s*=\s*\{', stripped)
        if m:
            current_unique = m.group(1)
            data[current_unique] = []
            continue

        # Match slot opening: { removed = {
        if stripped == '{ removed = {' and current_unique:
            current_slot = {"removed": [], "added": []}
            current_field = "removed"
            continue

        # Match }, added = {
        if stripped == '}, added = {' and current_slot:
            current_field = "added"
            continue

        # Match }, alternatives = {
        if stripped == '}, alternatives = {' and current_slot:
            current_field = "alternatives"
            current_slot["alternatives"] = []
            if "added" in current_slot:
                del current_slot["added"]
            continue

        # Match alternative sub-table opening: {
        if stripped == '{' and current_field == "alternatives":
            current_alt = []
            in_alt = True
            continue

        # Match alternative sub-table closing: },
        if stripped in ('},', '}') and in_alt and current_field == "alternatives":
            if current_alt is not None:
                current_slot["alternatives"].append(current_alt)
            current_alt = None
            in_alt = False
            continue

        # Match slot closing: } },
        if stripped in ('} },', '} }') and current_slot:
            data[current_unique].append(current_slot)
            current_slot = None
            current_field = None
            continue

        # Match unique closing: },
        if stripped in ('},', '}') and current_unique and not current_slot:
            current_unique = None
            continue

        # Match mod string line: "mod text",
        m = re.match(r'^"([^"]*)"', stripped)
        if m and current_slot and current_field:
            mod_text = m.group(1)
            if in_alt and current_alt is not None:
                current_alt.append(mod_text)
            elif current_field == "removed":
                current_slot["removed"].append(mod_text)
            elif current_field == "added":
                current_slot["added"].append(mod_text)

    return data


# ── Fuzzy Mod Matching ──────────────────────────────────────────────────────

def normalize_mod(text):
    """Normalize a mod string for fuzzy matching.

    Collapses range expressions like (40-55) or (40—55) to #,
    replaces standalone numbers with #, and lowercases.
    """
    # First collapse PoB-style range notation: (X—Y) or (X-Y) or (X–Y)
    text = re.sub(r'\((\d+(?:\.\d+)?)[—–\-](\d+(?:\.\d+)?)\)', '#', text)
    # Collapse +X-Y style ranges
    text = re.sub(r'(\d+(?:\.\d+)?)[—–\-](\d+(?:\.\d+)?)', '#', text)
    # Replace remaining standalone numbers
    text = re.sub(r'\d+(?:\.\d+)?', '#', text)
    return text.lower().strip()


def match_mod_to_template(rolled_mod, templates):
    """Find the best matching template for a rolled mod.

    Returns the template string if found, None otherwise.
    """
    norm_rolled = normalize_mod(rolled_mod)
    best_match = None
    best_score = 0

    for template in templates:
        norm_template = normalize_mod(template)
        if norm_rolled == norm_template:
            return template
        # Try partial matching for edge cases
        # Score by longest common subsequence ratio
        score = _lcs_ratio(norm_rolled, norm_template)
        if score > best_score and score > 0.85:
            best_score = score
            best_match = template

    return best_match


def _lcs_ratio(a, b):
    """Compute LCS length ratio between two strings."""
    if not a or not b:
        return 0.0
    m, n = len(a), len(b)
    # Use simplified approach for performance
    dp = [[0] * (n + 1) for _ in range(2)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[i % 2][j] = dp[(i - 1) % 2][j - 1] + 1
            else:
                dp[i % 2][j] = max(dp[(i - 1) % 2][j], dp[i % 2][j - 1])
    lcs_len = dp[m % 2][n]
    return (2.0 * lcs_len) / (m + n)


# ── Trade API ───────────────────────────────────────────────────────────────

def _ensure_requests():
    global requests
    if requests is None:
        try:
            import requests as _req
            requests = _req
        except ImportError:
            print("ERROR: 'requests' package required. Install with: pip install requests", file=sys.stderr)
            sys.exit(1)


class TradeClient:
    def __init__(self, poesessid, league):
        _ensure_requests()
        self.league = league
        self.session = requests.Session()
        self.session.cookies.set("POESESSID", poesessid, domain=".pathofexile.com")
        self.session.headers.update({
            "User-Agent": "PoB-FoulbornScraper/1.0 (contact: github.com/PathOfBuildingCommunity/PathOfBuilding)",
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self._last_request_time = 0

    def search(self):
        """Search for all Foulborn-mutated items. Returns (query_id, result_ids, total)."""
        url = SEARCH_URL.format(league=self.league)
        payload = {
            "query": {
                "status": {"option": "onlineleague"},
                "filters": {
                    "mutated_filter": {
                        "filters": {
                            "mutated": {"option": "true"}
                        }
                    }
                },
                "type": "Any"
            },
            "sort": {"price": "asc"}
        }

        self._rate_limit_wait()
        resp = self.session.post(url, json=payload)
        self._handle_rate_limit(resp)

        if resp.status_code != 200:
            print(f"ERROR: Search failed with status {resp.status_code}", file=sys.stderr)
            print(f"  Response: {resp.text[:500]}", file=sys.stderr)
            sys.exit(1)

        data = resp.json()
        query_id = data.get("id", "")
        result_ids = data.get("result", [])
        total = data.get("total", 0)

        print(f"Search returned {total} total results, {len(result_ids)} IDs in first page", file=sys.stderr)
        return query_id, result_ids, total

    def fetch_items(self, query_id, item_ids):
        """Fetch item details for a batch of IDs. Returns list of item dicts."""
        if not item_ids:
            return []

        ids_str = ",".join(item_ids[:FETCH_BATCH_SIZE])
        url = FETCH_URL.format(ids=ids_str, query_id=query_id)

        self._rate_limit_wait()
        resp = self.session.get(url)
        self._handle_rate_limit(resp)

        if resp.status_code != 200:
            print(f"WARNING: Fetch failed with status {resp.status_code} for batch", file=sys.stderr)
            return []

        data = resp.json()
        return data.get("result", [])

    def fetch_all(self, query_id, item_ids, max_items=None):
        """Fetch all items in batches, respecting rate limits."""
        if max_items:
            item_ids = item_ids[:max_items]

        all_items = []
        total_batches = (len(item_ids) + FETCH_BATCH_SIZE - 1) // FETCH_BATCH_SIZE

        for i in range(0, len(item_ids), FETCH_BATCH_SIZE):
            batch = item_ids[i:i + FETCH_BATCH_SIZE]
            batch_num = (i // FETCH_BATCH_SIZE) + 1
            print(f"  Fetching batch {batch_num}/{total_batches} ({len(batch)} items)...", file=sys.stderr)

            results = self.fetch_items(query_id, batch)
            all_items.extend(results)

        return all_items

    def _rate_limit_wait(self):
        """Ensure minimum delay between requests."""
        elapsed = time.time() - self._last_request_time
        min_delay = 0.6  # Conservative default
        if elapsed < min_delay:
            time.sleep(min_delay - elapsed)
        self._last_request_time = time.time()

    def _handle_rate_limit(self, resp):
        """Parse X-Rate-Limit headers and sleep if approaching limit."""
        state_header = resp.headers.get("X-Rate-Limit-Ip-State", "")
        limit_header = resp.headers.get("X-Rate-Limit-Ip", "")

        if not state_header or not limit_header:
            return

        # Headers can have multiple rules separated by commas
        # Format: "max:window:penalty"
        try:
            rules = limit_header.split(",")
            states = state_header.split(",")

            for rule, state in zip(rules, states):
                r_parts = rule.split(":")
                s_parts = state.split(":")
                if len(r_parts) >= 2 and len(s_parts) >= 2:
                    max_req = int(r_parts[0])
                    window = int(r_parts[1])
                    current = int(s_parts[0])
                    penalty = int(s_parts[2]) if len(s_parts) > 2 else 0

                    if penalty > 0:
                        print(f"  Rate limit penalty: sleeping {penalty}s", file=sys.stderr)
                        time.sleep(penalty)
                        return

                    # If we're close to the limit, sleep for the window duration
                    if current >= max_req - 1:
                        print(f"  Approaching rate limit ({current}/{max_req}), sleeping {window}s", file=sys.stderr)
                        time.sleep(window)
                        return
        except (ValueError, IndexError):
            pass  # Best-effort parsing

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            print(f"  Rate limited (429). Sleeping {retry_after}s...", file=sys.stderr)
            time.sleep(retry_after)


# ── Pairing Derivation ──────────────────────────────────────────────────────

def derive_pairings(fetched_items, original_uniques):
    """Derive mutation pairings from fetched trade items.

    Groups items by original unique name, then for each:
    - Matches explicitMods against original templates to find surviving mods
    - Identifies removed mods (originals not matched)
    - Records mutatedMods as added mods
    - Clusters into mutation slots

    Returns {unique_name: [{"removed": [str], "added": [str] or "alternatives": [[str]]}]}
    """
    # Group fetched items by original unique name
    items_by_unique = defaultdict(list)
    for result in fetched_items:
        item = result.get("item", {})
        name = item.get("name", "")
        # Strip "Foulborn " prefix
        if name.startswith("Foulborn "):
            original_name = name[len("Foulborn "):]
        else:
            original_name = name

        if not original_name:
            continue

        explicit_mods = item.get("explicitMods", [])
        mutated_mods = item.get("mutatedMods", [])

        if not mutated_mods:
            continue

        items_by_unique[original_name].append({
            "explicit_mods": explicit_mods,
            "mutated_mods": mutated_mods,
        })

    print(f"\nFound {len(items_by_unique)} distinct unique names in trade results", file=sys.stderr)
    for name, items in sorted(items_by_unique.items()):
        print(f"  {name}: {len(items)} listings", file=sys.stderr)

    # Derive pairings for each unique
    pairings = {}

    for unique_name, items in sorted(items_by_unique.items()):
        original_templates = original_uniques.get(unique_name, [])
        if not original_templates:
            print(f"  WARNING: No original mod templates found for '{unique_name}', skipping", file=sys.stderr)
            continue

        # Collect all observed (removed, added) pairs
        # A "pair" is a single removed mod matched to the mutated mods observed when it's missing
        slot_observations = defaultdict(list)  # removed_template -> [list of added_mod_sets]

        for item_data in items:
            explicit = item_data["explicit_mods"]
            mutated = item_data["mutated_mods"]

            # Find which original templates are still present (matched by explicit mods)
            matched_templates = set()
            for ex_mod in explicit:
                template = match_mod_to_template(ex_mod, original_templates)
                if template:
                    matched_templates.add(template)

            # Removed mods = original templates not matched by any explicit mod
            removed_templates = [t for t in original_templates if t not in matched_templates]

            # For single-mutation items (1 removed, N mutated), we have a clean slot
            if len(removed_templates) == 1 and len(mutated) >= 1:
                removed = removed_templates[0]
                slot_observations[removed].append(tuple(mutated))
            elif len(removed_templates) >= 1:
                # Multi-mutation: if mutated count matches removed count,
                # we still can't be sure of the pairing order.
                # Record the full set for later cross-referencing.
                # For now, just record each removed with all mutated
                # (will be refined by cross-referencing single-mutation items)
                for removed in removed_templates:
                    slot_observations[removed].append(tuple(mutated))

        # Build final slot list for this unique
        slots = []
        for removed_template, added_sets in sorted(slot_observations.items()):
            # Deduplicate added mod sets
            unique_added = list(set(added_sets))

            if len(unique_added) == 1:
                # Single consistent replacement
                slots.append({
                    "removed": [removed_template],
                    "added": list(unique_added[0]),
                })
            else:
                # Multiple different replacements seen → alternatives
                # Filter out multi-mutation noise: only keep sets with
                # reasonable size (typically 1-2 mods per slot)
                clean_alts = []
                for added in unique_added:
                    if len(added) <= 3:  # Reasonable single-slot replacement
                        clean_alts.append(list(added))

                if len(clean_alts) == 1:
                    slots.append({
                        "removed": [removed_template],
                        "added": clean_alts[0],
                    })
                elif len(clean_alts) > 1:
                    slots.append({
                        "removed": [removed_template],
                        "alternatives": clean_alts,
                    })
                # else: skip if no clean alternatives

        if slots:
            pairings[unique_name] = slots

    return pairings


# ── Lua Output ──────────────────────────────────────────────────────────────

def escape_lua_string(s):
    """Escape a string for use in Lua double-quoted strings."""
    return s.replace('\\', '\\\\').replace('"', '\\"')


def format_pairings_lua(pairings):
    """Format pairings dict as ModFoulbornPairs.lua content."""
    lines = []
    lines.append('-- This file is automatically generated, do not edit!')
    lines.append('-- Foulborn mutation slot pairings scraped from PoE Trade API')
    lines.append('-- Each entry maps a unique name to its mutation slots.')
    lines.append('-- Each slot has \'removed\' (mods to remove) and \'added\' (mods to add).')
    lines.append('-- Slots with multiple alternatives use \'alternatives\' instead of \'added\'.')
    lines.append('')
    lines.append('return {')

    for unique_name in sorted(pairings.keys()):
        slots = pairings[unique_name]
        lines.append(f'\t["{escape_lua_string(unique_name)}"] = {{')

        for slot in slots:
            removed = slot["removed"]
            lines.append('\t\t{ removed = {')
            for mod in removed:
                lines.append(f'\t\t\t"{escape_lua_string(mod)}",')
            if "alternatives" in slot:
                lines.append('\t\t}, alternatives = {')
                for alt_mods in slot["alternatives"]:
                    lines.append('\t\t\t{')
                    for mod in alt_mods:
                        lines.append(f'\t\t\t\t"{escape_lua_string(mod)}",')
                    lines.append('\t\t\t},')
            else:
                added = slot.get("added", [])
                lines.append('\t\t}, added = {')
                for mod in added:
                    lines.append(f'\t\t\t"{escape_lua_string(mod)}",')
            lines.append('\t\t} },')

        lines.append('\t},')

    lines.append('}')
    return '\n'.join(lines) + '\n'


def merge_pairings(existing, new):
    """Merge new pairings into existing, only adding entries for uniques not already present."""
    merged = dict(existing)
    added_count = 0
    for name, slots in new.items():
        if name not in merged:
            merged[name] = slots
            added_count += 1
    return merged, added_count


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scrape Foulborn mutation pairings from PoE Trade API"
    )
    parser.add_argument(
        "--poesessid", required=True,
        help="POESESSID cookie value for trade API authentication"
    )
    parser.add_argument(
        "--league", default="Keepers",
        help="League name to search (default: Keepers)"
    )
    parser.add_argument(
        "--output", default=None,
        help="Output file path (default: stdout)"
    )
    parser.add_argument(
        "--missing-only", action="store_true",
        help="Only scrape uniques not present in --existing file"
    )
    parser.add_argument(
        "--existing", default=None,
        help="Path to existing ModFoulbornPairs.lua for --missing-only mode"
    )
    parser.add_argument(
        "--max-items", type=int, default=None,
        help="Maximum number of items to fetch (for testing)"
    )
    parser.add_argument(
        "--dump-json", default=None,
        help="Dump raw fetched item data to JSON file (for debugging)"
    )
    parser.add_argument(
        "--from-json", default=None,
        help="Load items from a previously dumped JSON file instead of querying the API"
    )

    args = parser.parse_args()

    # Resolve PoB src directory (script lives in src/Export/Scripts/)
    script_dir = Path(__file__).resolve().parent
    src_dir = script_dir.parent.parent  # src/

    print("Loading original unique mod templates...", file=sys.stderr)
    original_uniques = parse_unique_files(src_dir)
    print(f"  Loaded {len(original_uniques)} uniques from data files", file=sys.stderr)

    existing_pairings = {}
    existing_names = set()
    if args.existing:
        existing_pairings = parse_existing_pairs_full(args.existing)
        existing_names = set(existing_pairings.keys())
        print(f"  Loaded {len(existing_names)} existing pairings from {args.existing}", file=sys.stderr)

    if args.from_json:
        print(f"Loading items from {args.from_json}...", file=sys.stderr)
        with open(args.from_json, 'r') as f:
            all_items = json.load(f)
        print(f"  Loaded {len(all_items)} items from JSON dump", file=sys.stderr)
    else:
        print(f"\nSearching trade API for Foulborn items in {args.league}...", file=sys.stderr)
        client = TradeClient(args.poesessid, args.league)
        query_id, result_ids, total = client.search()

        print(f"\nFetching {len(result_ids)} items...", file=sys.stderr)
        all_items = client.fetch_all(query_id, result_ids, max_items=args.max_items)
        print(f"  Fetched {len(all_items)} items total", file=sys.stderr)

    if args.dump_json:
        print(f"\nDumping raw items to {args.dump_json}...", file=sys.stderr)
        with open(args.dump_json, 'w') as f:
            json.dump(all_items, f, indent=2)

    print("\nDeriving mutation pairings...", file=sys.stderr)
    new_pairings = derive_pairings(all_items, original_uniques)
    print(f"  Derived pairings for {len(new_pairings)} uniques", file=sys.stderr)

    if args.missing_only and existing_pairings:
        final_pairings, added = merge_pairings(existing_pairings, new_pairings)
        print(f"  Merged: {added} new uniques added to {len(existing_pairings)} existing", file=sys.stderr)

        # Report which new uniques were found
        for name in sorted(new_pairings.keys()):
            if name not in existing_names:
                slots = new_pairings[name]
                print(f"    NEW: {name} ({len(slots)} mutation slots)", file=sys.stderr)
    else:
        final_pairings = new_pairings

    output = format_pairings_lua(final_pairings)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"\nOutput written to {args.output}", file=sys.stderr)
    else:
        print(output)

    # Summary
    total_slots = sum(len(s) for s in final_pairings.values())
    alt_count = sum(
        1 for slots in final_pairings.values()
        for s in slots if "alternatives" in s
    )
    print(f"\nSummary:", file=sys.stderr)
    print(f"  Uniques: {len(final_pairings)}", file=sys.stderr)
    print(f"  Total mutation slots: {total_slots}", file=sys.stderr)
    print(f"  Slots with alternatives: {alt_count}", file=sys.stderr)


if __name__ == "__main__":
    main()
