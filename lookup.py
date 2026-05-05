#!/usr/bin/env python3
"""
LA Property Lookup Tool
Pulls ZIMAS zoning data and LADBS permit/code enforcement data for any LA address.
Uses Playwright to handle JavaScript-rendered city websites.

Usage:
    python lookup.py "1815 Park Dr"
    python lookup.py "1923 Preston Ave"
    python lookup.py "1815 Park Dr" --output json
"""

import argparse
import asyncio
import json
import re
from datetime import datetime


DIRECTIONALS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW",
                "NORTH", "SOUTH", "EAST", "WEST"}

# Bidirectional map between abbreviated and full street-type forms.
# Used to try both forms against LA city sites that index inconsistently.
SUFFIX_ALTERNATES = {
    "ST": "STREET", "STREET": "ST",
    "AVE": "AVENUE", "AVENUE": "AVE",
    "BLVD": "BOULEVARD", "BOULEVARD": "BLVD",
    "DR": "DRIVE", "DRIVE": "DR",
    "PL": "PLACE", "PLACE": "PL",
    "CT": "COURT", "COURT": "CT",
    "RD": "ROAD", "ROAD": "RD",
    "LN": "LANE", "LANE": "LN",
    "CIR": "CIRCLE", "CIRCLE": "CIR",
    "TER": "TERRACE", "TERRACE": "TER",
    "PKWY": "PARKWAY", "PARKWAY": "PKWY",
    "HWY": "HIGHWAY", "HIGHWAY": "HWY",
    "TRL": "TRAIL", "TRAIL": "TRL",
    "PT": "POINT", "POINT": "PT",
    "SQ": "SQUARE", "SQUARE": "SQ",
    "GLN": "GLEN", "GLEN": "GLN",
    "XING": "CROSSING", "CROSSING": "XING",
    "ALY": "ALLEY", "ALLEY": "ALY",
    "BND": "BEND", "BEND": "BND",
}
STREET_TYPES = set(SUFFIX_ALTERNATES.keys()) | {
    "WAY", "ROW", "MEWS", "PASS", "RUN", "PROMENADE",
}


def parse_address(address: str) -> tuple[str, str]:
    """Split '2051 N Catalina St, Los Angeles, CA 90027' into ('2051', 'Catalina').

    Strips directional prefix and street suffix to match LADBS's index, which
    keys on bare street name (e.g. 'Edgewater', not 'Edgewater Ter')."""
    parts = parse_address_full(address)
    return parts["number"], parts["core"]


def parse_address_full(address: str) -> dict:
    """Parse address into structured components for site-specific formatting."""
    parts_csv = address.split(",", 1)
    street_line = parts_csv[0].strip()
    rest = parts_csv[1].strip() if len(parts_csv) > 1 else ""

    parts = street_line.split()
    number = parts[0] if parts else ""
    name_tokens = parts[1:]

    # Leading directional (N, S, E, W…)
    directional = None
    if name_tokens and name_tokens[0].upper() in DIRECTIONALS:
        directional = name_tokens[0]
        name_tokens = name_tokens[1:]

    # Trailing street type (St, Ave, Ter…)
    suffix = None
    if name_tokens and name_tokens[-1].upper() in STREET_TYPES:
        suffix = name_tokens[-1]
        name_tokens = name_tokens[:-1]

    core = " ".join(name_tokens)
    suffix_alt = None
    if suffix:
        alt = SUFFIX_ALTERNATES.get(suffix.upper())
        if alt:
            # Match the case style of the input suffix
            if suffix.isupper():
                suffix_alt = alt.upper()
            elif suffix[:1].isupper():
                suffix_alt = alt.title()
            else:
                suffix_alt = alt.lower()

    return {
        "number": number,
        "directional": directional,
        "core": core,
        "suffix": suffix,
        "suffix_alt": suffix_alt,
        "rest": rest,
        "original": address,
    }


def ladbs_search_variants(address: str) -> list[tuple[str, str]]:
    """Ordered list of (number, street_query) variants to try on LADBS.

    LADBS's address search is finicky — it indexes some streets by bare name
    and others with suffix; some require an 'N' prefix even when none was given.
    Each variant is tried until one returns results."""
    p = parse_address_full(address)
    n, core, suffix, suffix_alt, direc = (
        p["number"], p["core"], p["suffix"], p["suffix_alt"], p["directional"],
    )
    variants: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(num: str, street: str) -> None:
        if not num or not street:
            return
        key = (num.upper(), street.upper())
        if key not in seen:
            seen.add(key)
            variants.append((num, street))

    # 1. Bare street name (no directional, no suffix) — most flexible match
    add(n, core)
    # 2. Core + alternate suffix form (Ter ↔ Terrace, Ave ↔ Avenue)
    if suffix_alt:
        add(n, f"{core} {suffix_alt}")
    # 3. Core + original suffix
    if suffix:
        add(n, f"{core} {suffix}")
    # 4. With "N" prefix (common LADBS quirk for north-side addresses)
    if not direc:
        add(n, f"N {core}")
    # 5. Original directional + core
    if direc:
        add(n, f"{direc} {core}")

    return variants


def zimas_search_variants(address: str) -> list[str]:
    """Ordered list of full-address strings to try on ZIMAS's Esri geocoder."""
    p = parse_address_full(address)
    n, core, suffix, suffix_alt, direc, rest = (
        p["number"], p["core"], p["suffix"], p["suffix_alt"],
        p["directional"], p["rest"],
    )
    direc_part = f"{direc} " if direc else ""
    rest_part = f", {rest}" if rest else ""

    variants: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        s = s.strip()
        if s and s.upper() not in seen:
            seen.add(s.upper())
            variants.append(s)

    # 1. Original input — best for Esri if it has city/state/zip
    add(p["original"])
    # 2. With alternate suffix form + city/zip
    if suffix_alt:
        add(f"{n} {direc_part}{core} {suffix_alt}{rest_part}")
    # 3. Just street line as parsed (no city/zip)
    if suffix:
        add(f"{n} {direc_part}{core} {suffix}")
    # 4. Alt suffix, no city/zip
    if suffix_alt:
        add(f"{n} {direc_part}{core} {suffix_alt}")
    # 5. Bare core (no suffix)
    if core:
        add(f"{n} {direc_part}{core}")

    return variants


def parse_tab_separated(text: str) -> dict:
    """Parse ZIMAS key-value pairs — supports both tab-separated (old) and 'Key: Value' (new)."""
    data = {}
    skip_keys = {"Search", "Public", "Terms & Conditions", "SEARCH", "REPORTS", "RESOURCES", "HELP"}
    noise = {"Â", "Skip to Main Content", "ZIMAS", "Toggle Menu", "Zoom in", "Zoom out",
             "Select", "Identify", "Radius", "Measure", "Basemap"}

    for line in text.split("\n"):
        line = line.strip()
        # Strip unicode garbage (non-breaking spaces rendered as Â)
        line = line.replace("Â", "").strip()
        if not line or line in noise:
            continue

        # Tab-separated (old ZIMAS)
        if "\t" in line:
            parts = line.split("\t", 1)
            if len(parts) == 2:
                key, val = parts[0].strip(), parts[1].strip()
                if key and val and len(key) > 2 and key not in skip_keys:
                    data[key] = val
            continue

        # Colon-separated (new ZIMAS Angular app)
        if ":" in line:
            idx = line.index(":")
            key = line[:idx].strip()
            val = line[idx + 1:].strip()
            if (key and val and 3 <= len(key) <= 80 and key not in skip_keys
                    and not key.startswith("http") and len(val) < 300):
                data[key] = val

    return data


async def dismiss_zimas_dialog(page):
    """Accept ZIMAS terms and conditions screen (new Angular app) or old jQuery dialog."""
    # New ZIMAS: full-page terms acceptance with a checkbox + continue button
    try:
        checkbox = page.locator("#checkSaveAcceptTerms").first
        if await checkbox.is_visible(timeout=5000):
            await checkbox.click(force=True)
            await page.wait_for_timeout(500)
            # Click the Continue / Accept button
            for sel in [
                "button:has-text('Continue')", "button:has-text('Accept')",
                "button:has-text('Agree')", "input[type='submit']",
                "button[type='submit']", ".btn-primary", "calcite-button",
            ]:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click(force=True)
                        await page.wait_for_timeout(3000)
                        return
                except:
                    continue
            # If no button found, try pressing Enter
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(3000)
            return
    except:
        pass

    # Old ZIMAS: jQuery UI dialog — hide it via JS
    try:
        await page.evaluate("""() => {
            document.querySelectorAll('.ui-dialog').forEach(d => d.style.display = 'none');
            document.querySelectorAll('.ui-widget-overlay').forEach(d => d.style.display = 'none');
            try { $('.ui-dialog-content').dialog('close'); } catch(e) {}
        }""")
        await page.wait_for_timeout(500)
        return
    except:
        pass


async def fill_ladbs_search(page, number: str, street: str):
    """Fill the LADBS split address fields and submit via JS to bypass overlays."""
    print(f"  Searching LADBS: number='{number}', street='{street}'")

    await page.wait_for_timeout(3000)

    # LADBS field IDs: StreetNumber, StreetNameSingle, btnSearch
    await page.locator("#StreetNumber").fill(number)
    await page.locator("#StreetNameSingle").fill(street)
    await page.locator("#btnSearch").click()

    await page.wait_for_timeout(8000)


async def expand_ladbs_sections(page):
    """Click all the + expand buttons on LADBS results page."""
    # The expand buttons are <img> tags inside <a> tags with onclick handlers
    # They look like: <a onclick="..."><img src="...plus..."/></a>
    # Try clicking by the section text links
    for section in ["Parcel Profile Report", "Permit Information", "Code Enforcement", "Certificate of Occupancy", "Retrofit Program"]:
        try:
            # Find the row containing this text and click the + image in it
            row = page.locator(f"tr:has-text('{section}'), div:has-text('{section}')").first
            img = row.locator("img").first
            await img.click(force=True)
            await page.wait_for_timeout(3000)
        except:
            try:
                # Alternative: click the text itself
                link = page.locator(f"a:has-text('{section}')").first
                await link.click()
                await page.wait_for_timeout(3000)
            except:
                pass

    # Also try clicking ALL images that look like expand buttons
    try:
        images = await page.locator("img[src*='plus'], img[src*='expand'], img[src*='open']").all()
        for img in images:
            try:
                await img.click(force=True)
                await page.wait_for_timeout(2000)
            except:
                pass
    except:
        pass

    # Try JavaScript approach to expand all
    try:
        await page.evaluate("""() => {
            // Click all onclick handlers that might expand sections
            document.querySelectorAll('a[onclick], img[onclick]').forEach(el => {
                try { el.click(); } catch(e) {}
            });
        }""")
        await page.wait_for_timeout(3000)
    except:
        pass


async def lookup_zimas(page, address: str) -> dict:
    """Pull zoning and parcel data from ZIMAS."""
    print(f"[ZIMAS] Looking up {address}...")
    result = {
        "source": "ZIMAS",
        "address": address,
        "timestamp": datetime.now().isoformat(),
        "data": {},
        "raw_text": "",
        "error": None,
    }

    try:
        # Block render-blocking third-party scripts that hang the page load on CI
        await page.route("**/*", lambda route: route.abort()
            if any(h in route.request.url for h in [
                "navbar.lacity.gov",
                "go-mpulse.net",
                "akamaihd.net",
                "googletagmanager.com",
                "google-analytics.com",
            ])
            else route.continue_())

        async def _open_zimas():
            await page.goto("https://zimas.lacity.org/", timeout=90000, wait_until="domcontentloaded")
            await page.wait_for_timeout(15000)
            await dismiss_zimas_dialog(page)

        async def _find_search_input():
            search_selectors = [
                "#txtHouseNumber",          # old ZIMAS
                ".esri-search__input",      # Esri search widget
                "input[placeholder*='ddress']",
                "input[placeholder*='earch']",
                "calcite-input input",      # Calcite design system
                "input[type='search']",
                "input[type='text']",
            ]
            for sel in search_selectors:
                try:
                    loc = page.locator(sel).first
                    await loc.wait_for(state="visible", timeout=5000)
                    return loc, sel
                except:
                    continue
            return None, None

        async def _submit_query(query: str, found_input, found_selector: str):
            number_p, street_p = parse_address(query)
            if found_selector == "#txtHouseNumber":
                # Old ZIMAS: split fields
                await found_input.click(force=True)
                await found_input.fill(number_p)
                street_input = page.locator("#txtStreetName").first
                await street_input.click(force=True)
                await street_input.fill(street_p)
                for sel in ["#btnSearch", "#imgSearch", "input[type='submit']", "input[type='image']"]:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=2000):
                            await btn.click(force=True)
                            break
                    except:
                        continue
                else:
                    await page.keyboard.press("Enter")
            else:
                # New ZIMAS Esri: single search field; geocoder works best with the full
                # address-as-given, but we'll cycle through variants if it doesn't resolve.
                await found_input.click(force=True)
                await found_input.fill("")
                await found_input.fill(query)
                await page.wait_for_timeout(2000)
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(8000)

            # Click suggestion if shown
            for sel in [".suggestion", ".esri-search__suggestion", "li[role='option']",
                        "[class*='suggestion']", "calcite-list-item"]:
                try:
                    item = page.locator(sel).first
                    await item.wait_for(timeout=3000)
                    await item.click()
                    await page.wait_for_timeout(5000)
                    break
                except:
                    continue

        await _open_zimas()
        found_input, found_selector = await _find_search_input()
        if found_input is None:
            raise Exception("Could not find any search input on ZIMAS page after page load")

        # ZIMAS's Esri geocoder is forgiving but not perfect — try variants in order
        # until the parcel info pane populates with assessor data.
        queries = zimas_search_variants(address)
        print(f"[ZIMAS] Will try up to {len(queries)} address variants")
        resolved = False
        for i, q in enumerate(queries, start=1):
            print(f"[ZIMAS] attempt {i}/{len(queries)}: {q!r}")
            if i > 1:
                await _open_zimas()
                found_input, found_selector = await _find_search_input()
                if found_input is None:
                    break
            await _submit_query(q, found_input, found_selector)
            body_text = await page.inner_text("body")
            if "NO RESULTS" in body_text.upper() or "no results were returned" in body_text.lower():
                print(f"[ZIMAS] no results for {q!r}, trying next...")
                continue
            # Heuristic: parcel resolved when the Assessor section is loadable
            if any(marker in body_text for marker in ["Assessor Parcel No.", "Year Built", "Zoning:", "APN"]):
                resolved = True
                break
        if not resolved:
            print("[ZIMAS] no variants resolved; collecting whatever loaded")

        # Click through each sidebar section and collect text
        # New ZIMAS uses full labels; old ZIMAS used shorter ones — try both
        tabs_to_click = [
            "Address/Legal Information",
            "Jurisdictional Information",
            "Permitting and Zoning Compliance Information",
            "Planning and Zoning Information",
            "Assessor Information",
            "Case Numbers",
            "Additional Information",
            "Environmental",
            "Seismic Hazards",
            "Housing",
            "Public Safety",
            # Old ZIMAS short names as fallback
            "Address/Legal",
            "Jurisdictional",
            "Permitting and Zoning Compliance",
            "Planning and Zoning",
            "Assessor",
        ]

        clicked = set()
        for tab_name in tabs_to_click:
            short = tab_name.replace(" Information", "")
            if short in clicked:
                continue
            try:
                tab = page.locator(f"text='{tab_name}'").first
                if await tab.is_visible(timeout=2000):
                    await tab.click(force=True)
                    await page.wait_for_timeout(4000)
                    content = await page.inner_text("body")
                    parsed = parse_tab_separated(content)
                    if parsed:
                        result["data"].update(parsed)
                    clicked.add(short)
            except:
                pass

        # Get full page text for raw output
        result["raw_text"] = await page.inner_text("body")

        # Extract key fields into a clean summary
        summary_keys = [
            "Zoning", "Zone(s)", "General Plan Land Use", "Hillside Area (Zoning Code)",
            "Assessor Parcel No. (APN)", "Year Built", "Building Class", "Number of Units",
            "Number of Bedrooms", "Number of Bathrooms", "Building Square Footage",
            "Assessed Land Val.", "Assessed Improvement Val.", "Last Owner Change",
            "Last Sale Amount", "Use Code", "APN Area (Co. Public Works)*",
            "Very High Fire Hazard Severity Zone", "Fire District No. 1",
            "Flood Zone", "Earthquake Fault Zone",
            "Earthquake-Induced Landslide Area", "Earthquake-Induced Liquefaction Area",
            "Nearest Fault (Distance in km)", "Nearest Fault", "Region",
            "Alquist-Priolo Fault Zone", "Landslide", "Liquefaction",
            "HCR: Hillside Construction Regulation",
            "AB 2097: Within a half mile of a Major Transit Stop",
            "Transit Oriented Communities (TOC)",
            "Special Notes", "Council District", "Certified Neighborhood Council",
            "Community Plan Area", "Area Planning Commission",
        ]

        found = len([k for k in summary_keys if k in result["data"]])
        print(f"[ZIMAS] Found {found} key fields, {len(result['data'])} total fields")

    except Exception as e:
        result["error"] = str(e)
        print(f"[ZIMAS] Error: {e}")

    return result


async def lookup_ladbs(page, address: str) -> dict:
    """Pull ALL LADBS data (permits, code enforcement, parcel, CofO, retrofit) in one pass."""
    print(f"[LADBS] Looking up {address}...")
    result = {
        "source": "LADBS",
        "address": address,
        "timestamp": datetime.now().isoformat(),
        "sections": {},
        "summary": {},
        "raw_text": "",
        "error": None,
    }

    try:
        async def _open_search_page():
            await page.goto(
                "https://www.ladbsservices2.lacity.org/OnlineServices/?service=plr",
                timeout=90000,
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(10000)
            try:
                await page.evaluate(
                    "document.querySelectorAll('.modal, [role=\"dialog\"]').forEach(d => d.style.display = 'none')"
                )
            except:
                pass

        # LADBS's address index is inconsistent (street-suffix sensitivity, N-prefix
        # quirks). Try a sequence of address variants until one returns results.
        variants = ladbs_search_variants(address)
        body = ""
        success = False
        for i, (number, street) in enumerate(variants, start=1):
            print(f"  LADBS attempt {i}/{len(variants)}")
            if i > 1:
                await _open_search_page()
            else:
                await _open_search_page()
            await fill_ladbs_search(page, number, street)
            body = await page.inner_text("body")
            if "No Addresses were found" not in body:
                success = True
                break
            print(f"    No results for '{number} {street}', trying next variant...")

        if not success:
            print(f"  No LADBS results across {len(variants)} address variants")
            result["error"] = f"No LADBS results across {len(variants)} address variants"
            return result

        # Close the All Services overlay that covers the results
        try:
            await page.evaluate("""() => {
                // Remove ALL overlay/modal elements that might block interaction
                document.querySelectorAll('.services-overlay, .modal, .overlay, [class*="services-grid"], [class*="ServiceCard"]').forEach(el => {
                    el.remove();
                });
                // Also remove any fixed/absolute positioned overlays
                document.querySelectorAll('div').forEach(el => {
                    const style = window.getComputedStyle(el);
                    if ((style.position === 'fixed' || style.position === 'absolute') &&
                        style.zIndex > 100 && el.querySelector('a.block-display')) {
                        el.remove();
                    }
                });
            }""")
            await page.wait_for_timeout(1000)
        except:
            pass

        # Get the results overview text first
        overview_text = await page.inner_text("body")
        result["raw_text"] = "=== OVERVIEW ===\n" + overview_text

        # Parse section counts from overview
        for section in ["Parcel Profile Report", "Permit Information found", "Code Enforcement Information", "Certificate of Occupancy Information", "Retrofit Program Information"]:
            pattern = rf"{re.escape(section)}[:\s]*(\d+)"
            match = re.search(pattern, overview_text)
            if match:
                result["summary"][section] = int(match.group(1))
                print(f"  {section}: {match.group(1)}")

        # LADBS uses jQuery UI accordion for sections
        # Section header IDs: pcis (permits), ceis (code enforcement), ppr (parcel), cofo (CofO), retrofit
        # Content div IDs: pcisBody, ceisBody, etc.
        # Clicking the h3 header triggers lazy-loading of content
        accordion_sections = [
            ("pcis", "Permit Information", "pcisBody"),
            ("ppr", "Parcel Profile", "pprBody"),
            ("ceis", "Code Enforcement", "ceisBody"),
            ("cofo", "Certificate of Occupancy", "cofoBody"),
            ("retrofit", "Retrofit Program", "retrofitBody"),
        ]

        for header_id, section_name, body_id in accordion_sections:
            try:
                header = page.locator(f"#{header_id}")
                if not await header.count():
                    continue

                # Click the accordion header to expand — use JS click to bypass overlay
                await page.evaluate(f"document.getElementById('{header_id}').click()")
                print(f"  Expanding {section_name}...")

                # Wait for AJAX content to load
                # The section first shows "Retrieving Data..." then loads actual content via AJAX
                for i in range(20):  # max 20 seconds
                    await page.wait_for_timeout(1000)
                    body_html = await page.locator(f"#{body_id}").inner_html()
                    body_text = await page.locator(f"#{body_id}").inner_text()
                    # Content is loaded when we see a table, permit numbers, or specific data
                    if ("</table>" in body_html or
                        "<tr" in body_html or
                        re.search(r"\d{5}-\d{5}-\d{5}", body_text) or
                        "Application / Permit" in body_text or
                        "Soft-story" in body_text or
                        "Non-Ductile" in body_text or
                        "Zone(s)" in body_text or
                        len(body_text) > 100):
                        break
                    if i == 19:
                        print(f"    Timed out waiting for {section_name} data")

                # After the first accordion expands, there may be a NESTED accordion
                # (address-level headers like "1923 N PRESTON AVE 90026" with onclick="showSection(...)")
                # Click all nested accordion headers to expand them too
                nested_headers = await page.locator(f"#{body_id} h3[onclick], #{body_id} .accordianAddress").all()
                for nested in nested_headers:
                    try:
                        await nested.click(force=True)
                        # Wait for nested AJAX to load
                        for _ in range(15):
                            await page.wait_for_timeout(1000)
                            nested_html = await page.locator(f"#{body_id}").inner_html()
                            if ("</table>" in nested_html or
                                re.search(r"\d{5}-\d{5}-\d{5}", nested_html) or
                                "Zone(s)" in nested_html or
                                "Soft-story" in nested_html or
                                "Application / Permit" in nested_html):
                                break
                    except:
                        pass

                section_content = await page.locator(f"#{body_id}").inner_text()
                if section_content.strip() and "Retrieving Data" not in section_content:
                    result["sections"][section_name] = section_content
                    result["raw_text"] += f"\n\n=== {section_name.upper()} ===\n" + section_content
                    print(f"    Got {len(section_content)} chars")

                    # For permits, try clicking into individual permit detail pages
                    if "Permit" in section_name:
                        permit_links = await page.locator(f"#{body_id} a").all()
                        for link in permit_links:
                            try:
                                link_text = (await link.inner_text()).strip()
                                if re.match(r"\d{5}-\d{5}-\d{5}", link_text):
                                    await link.click()
                                    await page.wait_for_timeout(5000)
                                    detail = await page.inner_text("body")
                                    result["sections"][f"permit_{link_text}"] = detail
                                    result["raw_text"] += f"\n\n=== PERMIT DETAIL: {link_text} ===\n" + detail
                                    print(f"    Got permit detail: {link_text}")
                                    await page.go_back()
                                    await page.wait_for_timeout(3000)
                            except:
                                pass
                else:
                    print(f"    No data or still loading")

            except Exception as e:
                print(f"  Error expanding {section_name}: {e}")

        # Parse permits from the expanded/detail text
        full_text = result["raw_text"]

        # Extract permit numbers
        permit_numbers = re.findall(r"(\d{5}-\d{5}-\d{5})", full_text)
        if permit_numbers:
            result["sections"]["permit_numbers"] = list(set(permit_numbers))

        # Extract code enforcement cases
        ce_cases = []
        for line in full_text.split("\n"):
            line = line.strip()
            if any(kw in line.upper() for kw in ["WITHOUT PERMITS", "PRO-ACTIVE", "BUILDING OR WALL", "FENCES WALLS"]):
                ce_cases.append(line)
        if ce_cases:
            result["sections"]["code_enforcement"] = ce_cases

        print(f"[LADBS] Permits found: {len(permit_numbers)}, CE cases: {len(ce_cases)}")

    except Exception as e:
        result["error"] = str(e)
        print(f"[LADBS] Error: {e}")

    return result


def format_markdown(zimas: dict, ladbs: dict) -> str:
    """Format combined results as a clean markdown report."""
    lines = []
    addr = zimas.get("address", ladbs.get("address", "Unknown"))
    lines.append(f"# Property Report: {addr}")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

    # ZIMAS Section
    lines.append("## ZIMAS Data\n")
    if zimas.get("error"):
        lines.append(f"**Error:** {zimas['error']}\n")
    else:
        data = zimas.get("data", {})
        if data:
            # Group into categories
            categories = {
                "Zoning & Land Use": [
                    "Zoning", "Zone(s)", "General Plan Land Use", "Special Notes",
                    "Hillside Area (Zoning Code)", "Specific Plan Area",
                    "HCR: Hillside Construction Regulation",
                    "AB 2097: Within a half mile of a Major Transit Stop",
                    "Transit Oriented Communities (TOC)",
                    "Zoning Information (ZI)",
                ],
                "Assessor / Property": [
                    "Assessor Parcel No. (APN)", "APN Area (Co. Public Works)*",
                    "Use Code", "Year Built", "Building Class",
                    "Number of Units", "Number of Bedrooms", "Number of Bathrooms",
                    "Building Square Footage",
                    "Assessed Land Val.", "Assessed Improvement Val.",
                    "Last Owner Change", "Last Sale Amount",
                ],
                "Hazards": [
                    "Very High Fire Hazard Severity Zone", "Fire District No. 1",
                    "Flood Zone", "Earthquake Fault Zone",
                    "Alquist-Priolo Fault Zone",
                    "Earthquake-Induced Landslide Area", "Earthquake-Induced Liquefaction Area",
                    "Nearest Fault (Distance in km)", "Nearest Fault", "Region",
                    "Landslide", "Liquefaction",
                ],
                "Jurisdiction": [
                    "Council District", "Certified Neighborhood Council",
                    "Community Plan Area", "Area Planning Commission",
                ],
            }

            for cat_name, keys in categories.items():
                found_items = [(k, data[k]) for k in keys if k in data]
                if found_items:
                    lines.append(f"### {cat_name}")
                    for k, v in found_items:
                        lines.append(f"- **{k}:** {v}")
                    lines.append("")

            # Any remaining fields not in categories
            categorized = set()
            for keys in categories.values():
                categorized.update(keys)
            remaining = {k: v for k, v in data.items() if k not in categorized}
            if remaining:
                lines.append("### Other Fields")
                for k, v in sorted(remaining.items()):
                    lines.append(f"- **{k}:** {v}")
                lines.append("")
        else:
            lines.append("No structured data extracted. Check raw output.\n")

    # LADBS Section
    lines.append("## LADBS Data\n")
    if ladbs.get("error"):
        lines.append(f"**Error:** {ladbs['error']}\n")
    else:
        summary = ladbs.get("summary", {})
        if summary:
            lines.append("### Overview")
            for k, v in summary.items():
                lines.append(f"- **{k}:** {v}")
            lines.append("")

        sections = ladbs.get("sections", {})
        if sections.get("permit_numbers"):
            lines.append("### Permit Numbers")
            for pn in sections["permit_numbers"]:
                lines.append(f"- [{pn}](https://www.ladbsservices2.lacity.org/OnlineServices/PermitReport/PcisPermitDetail?id1={pn.split('-')[0]}&id2={pn.split('-')[1]}&id3={pn.split('-')[2]})")
            lines.append("")

        if sections.get("code_enforcement"):
            lines.append("### Code Enforcement Cases")
            for case in sections["code_enforcement"]:
                lines.append(f"- {case}")
            lines.append("")

        if sections.get("permits"):
            lines.append("### Permit Details")
            lines.append("```")
            lines.append(sections["permits"][:5000])
            lines.append("```\n")

    # Raw output — collapsed by default so the report stays readable
    lines.append("---")
    lines.append("## Raw Output\n")
    for name, result in [("ZIMAS", zimas), ("LADBS", ladbs)]:
        raw = result.get("raw_text", "")
        if len(raw) > 15000:
            raw = raw[:15000] + "\n... (truncated)"
        lines.append("<details>")
        lines.append(f"<summary>{name} — Raw ({len(raw):,} chars)</summary>\n")
        lines.append("```")
        lines.append(raw)
        lines.append("```")
        lines.append("</details>\n")

    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser(description="Look up LA property data from ZIMAS and LADBS")
    parser.add_argument("address", help="Full address with city, state, zip (e.g., '1815 Park Dr, Los Angeles, CA 90026')")
    parser.add_argument("--output", choices=["json", "markdown"], default="markdown")
    parser.add_argument("--save", help="Save output to file")
    parser.add_argument("--headed", action="store_true", help="Visible browser")
    parser.add_argument("--screenshots", action="store_true", help="Save screenshots")
    args = parser.parse_args()

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        # ZIMAS
        zimas_page = await context.new_page()
        zimas_result = await lookup_zimas(zimas_page, args.address)
        if args.screenshots:
            try:
                await zimas_page.screenshot(path="screenshot_zimas.png", timeout=10000)
            except:
                pass
        await zimas_page.close()

        # LADBS (single pass for all data)
        ladbs_page = await context.new_page()
        ladbs_result = await lookup_ladbs(ladbs_page, args.address)
        if args.screenshots:
            try:
                await ladbs_page.screenshot(path="screenshot_ladbs.png", timeout=10000)
            except:
                pass
        await ladbs_page.close()

        await browser.close()

    if args.output == "json":
        output = json.dumps({"zimas": zimas_result, "ladbs": ladbs_result}, indent=2, default=str)
    else:
        output = format_markdown(zimas_result, ladbs_result)

    if args.save:
        with open(args.save, "w") as f:
            f.write(output)
        print(f"\nSaved to {args.save}")
    else:
        print("\n" + output)


if __name__ == "__main__":
    asyncio.run(main())
