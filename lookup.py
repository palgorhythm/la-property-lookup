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


def parse_address(address: str) -> tuple[str, str]:
    """Split '1923 Preston Ave' into ('1923', 'Preston')."""
    parts = address.strip().split()
    number = parts[0]
    skip = {"N", "S", "E", "W", "ST", "AVE", "BLVD", "DR", "PL", "CT", "RD", "WAY", "LN", "CIR", "NORTH", "SOUTH", "EAST", "WEST"}
    street_parts = [p for p in parts[1:] if p.upper() not in skip]
    street = " ".join(street_parts) if street_parts else parts[1] if len(parts) > 1 else ""
    return number, street


def parse_tab_separated(text: str) -> dict:
    """Parse ZIMAS-style tab-separated key-value pairs from page text."""
    data = {}
    for line in text.split("\n"):
        line = line.strip()
        if "\t" in line:
            parts = line.split("\t", 1)
            if len(parts) == 2:
                key = parts[0].strip()
                val = parts[1].strip()
                if key and val and len(key) > 2 and key not in ("Search", "Public", "Terms & Conditions"):
                    data[key] = val
    return data


async def dismiss_zimas_dialog(page):
    """ZIMAS shows a jQuery UI terms dialog on load."""
    # Try JS approach first — most reliable
    try:
        await page.evaluate("""() => {
            document.querySelectorAll('.ui-dialog').forEach(d => d.style.display = 'none');
            document.querySelectorAll('.ui-widget-overlay').forEach(d => d.style.display = 'none');
            // Also try closing via jQuery if available
            try { $('.ui-dialog-content').dialog('close'); } catch(e) {}
        }""")
        await page.wait_for_timeout(500)
        return
    except:
        pass
    # Fallback: click close button
    for selector in ["a:has-text('close')", ".ui-dialog-titlebar-close", "span:has-text('close')"]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=2000):
                await btn.click(force=True)
                await page.wait_for_timeout(500)
                return
        except:
            continue
    try:
        await page.keyboard.press("Escape")
    except:
        pass


async def fill_ladbs_search(page, address: str):
    """Fill the LADBS split address fields and submit via JS to bypass overlays."""
    number, street = parse_address(address)
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
        await page.goto("https://zimas.lacity.org/", timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        await dismiss_zimas_dialog(page)

        number, street = parse_address(address)
        print(f"[ZIMAS] Searching: number='{number}', street='{street}'")

        num_input = page.locator("#txtHouseNumber").first
        await num_input.wait_for(timeout=10000)
        await num_input.click(force=True)
        await num_input.fill(number)

        street_input = page.locator("#txtStreetName").first
        await street_input.click(force=True)
        await street_input.fill(street)

        # Submit
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

        await page.wait_for_timeout(8000)

        # Click through suggestion if needed
        for sel in [".suggestion", ".esri-search__suggestion", "li[role='option']"]:
            try:
                item = page.locator(sel).first
                await item.wait_for(timeout=2000)
                await item.click()
                await page.wait_for_timeout(5000)
                break
            except:
                continue

        # Click through each data tab and collect text
        tabs_to_click = [
            "Address/Legal",
            "Jurisdictional",
            "Permitting and Zoning Compliance",
            "Planning and Zoning",
            "Assessor",
            "Case Numbers",
            "Additional",
            "Environmental",
            "Seismic Hazards",
            "Housing",
            "Public Safety",
        ]

        all_sections = {}
        for tab_name in tabs_to_click:
            try:
                # ZIMAS uses a sidebar with collapsible sections
                tab = page.locator(f"text='{tab_name}'").first
                if await tab.is_visible(timeout=2000):
                    await tab.click(force=True)
                    await page.wait_for_timeout(2500)
                    # Get the content area text
                    content = await page.inner_text("#infoContent, #resultContent, .info-content, body")
                    parsed = parse_tab_separated(content)
                    if parsed:
                        all_sections[tab_name] = parsed
                        result["data"].update(parsed)
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
        await page.goto(
            "https://www.ladbsservices2.lacity.org/OnlineServices/?service=plr",
            timeout=60000,
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(5000)

        # Close any popup/overlay that might appear
        try:
            await page.evaluate("document.querySelectorAll('.modal, [role=\"dialog\"]').forEach(d => d.style.display = 'none')")
        except:
            pass

        await fill_ladbs_search(page, address)

        # Check if search returned results
        body = await page.inner_text("body")
        if "No Addresses were found" in body:
            # LADBS often requires "N" prefix. Try with "N" added to street name
            print("  No results. Retrying with 'N' prefix...")
            await page.goto(
                "https://www.ladbsservices2.lacity.org/OnlineServices/?service=plr",
                timeout=60000,
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(5000)
            # Modify the address to include N prefix
            number, street = parse_address(address)
            modified = f"{number} N {street}"
            await page.wait_for_timeout(3000)
            await page.locator("#StreetNumber").fill(number)
            await page.locator("#StreetNameSingle").fill(f"N {street}")
            await page.locator("#btnSearch").click()
            await page.wait_for_timeout(8000)

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

    # Raw output
    lines.append("---")
    lines.append("## Raw Output\n")
    for name, result in [("ZIMAS", zimas), ("LADBS", ladbs)]:
        lines.append(f"### {name} — Raw")
        lines.append("```")
        raw = result.get("raw_text", "")
        if len(raw) > 15000:
            raw = raw[:15000] + "\n... (truncated)"
        lines.append(raw)
        lines.append("```\n")

    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser(description="Look up LA property data from ZIMAS and LADBS")
    parser.add_argument("address", help="Street address (e.g., '1815 Park Dr')")
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
