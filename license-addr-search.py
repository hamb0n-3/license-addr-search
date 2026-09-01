#!/usr/bin/env python3
import argparse
import logging
import csv
import random
import re
import sys
import time
from typing import Dict, List, Optional

from camoufox.sync_api import Camoufox

DCA_URL = "https://search.dca.ca.gov/"
DEFAULT_TIMEOUT = 30_000  # ms
SLOW_KEY_DELAY = (35, 85)  # ms per keystroke

logger = logging.getLogger("dca_camoufox")


def setup_logging(verbosity: int = 0, log_file: Optional[str] = None) -> None:
    level = logging.WARNING if verbosity <= 0 else (logging.INFO if verbosity == 1 else logging.DEBUG)
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logger.debug("Logging initialized: level=%s, file=%s", logging.getLevelName(level), log_file)


# ------------------------- utility: humanized actions ------------------------- #

def human_sleep(min_s: float = 0.3, max_s: float = 0.9) -> None:
    time.sleep(random.uniform(min_s, max_s))


def human_type(locator, text: str) -> None:
    for ch in text:
        locator.type(ch, delay=random.randint(*SLOW_KEY_DELAY))
    human_sleep()
    logger.debug("Typed %d characters.", len(text))


# ----------------------- utility: label-driven selectors ---------------------- #

def control_by_label(page, label_text: str):
    logger.debug("Locating control by label: %r", label_text)
    label = page.locator(f"label:has-text('{label_text}')").first
    label.wait_for(state="visible", timeout=DEFAULT_TIMEOUT)

    target_id = None
    try:
        target_id = label.get_attribute("for")
    except Exception:
        pass

    if target_id:
        ctrl = page.locator(f"#{css_escape(target_id)}")
        if ctrl.count() > 0:
            logger.debug("Found control by for=#%s", target_id)
            return ctrl

    # proximity fallbacks
    container = label.locator("xpath=ancestor::*[self::div or self::form or self::fieldset][1]")
    prox = container.locator("input, select, textarea").first
    if prox.count() == 0:
        prox = label.locator("xpath=following::*[self::input or self::select or self::textarea][1]")
    logger.debug("Using proximity fallback for label %r (found=%s)", label_text, prox.count() > 0)
    return prox


def css_escape(s: str) -> str:
    return re.sub(r'([^\w\-])', r'\\\1', s)


def click_button_by_text(page, text: str):
    logger.debug("Clicking button by text: %r", text)
    last_err = None

    def patterns(scope):
        return [
            scope.get_by_role("button", name=re.compile(text, re.I)),
            scope.locator(f"button:has-text('{text}')"),
            scope.locator(f"[role=button]:has-text('{text}')"),
            scope.locator(f"a:has-text('{text}')"),
            scope.locator(f"input[type=submit][value*='{text}' i]"),
            scope.locator(f"input[type=button][value*='{text}' i]"),
            scope.locator(f"[aria-label*='{text}' i]"),
            # Common UI libs:
            scope.locator(f".mat-mdc-button:has-text('{text}')"),
            scope.locator(f".mdc-button:has-text('{text}')"),
            scope.locator(f".p-button:has-text('{text}')"),   # PrimeNG
            scope.locator(f".btn:has-text('{text}')"),        # Bootstrap
            # Fallback: any element whose own text node is exactly the target, but still a clickable parent
            scope.locator(f"//*/text()[normalize-space()='{text}']/parent::*[self::button or self::a or @role='button']"),
        ]

    for scope in iter_live_scopes(page):
        for i, loc in enumerate(patterns(scope)):
            try:
                btn = loc.locator(":visible").first
                if btn.count() == 0:
                    continue
                btn.scroll_into_view_if_needed()
                btn.wait_for(state="visible", timeout=DEFAULT_TIMEOUT)
                try:
                    btn.click()
                except Exception:
                    # last resort: JS click to bypass overlays
                    btn.evaluate("el => el.click()")
                human_sleep()
                logger.debug("Clicked search control via strategy #%d", i + 1)
                return
            except Exception as e:
                last_err = e

    raise RuntimeError(f"Could not locate a clickable '{text}' control") from last_err


def check_radio_by_text(page, text: str):
    logger.debug("Checking radio by text: %r", text)
    radio = page.get_by_role("radio", name=re.compile(text, re.I))
    if radio.count() > 0:
        try:
            radio.first.check()
            human_sleep()
            return True
        except Exception:
            pass
    try:
        page.locator(f"label:has-text('{text}')").first.click()
        human_sleep()
        return True
    except Exception:
        return False


def select_option_by_visible_text(select_locator, option_text: str) -> bool:
    if select_locator.count() == 0:
        return False
    try:
        select_locator.select_option(label=option_text)
        human_sleep()
        logger.debug("Selected option by label: %r", option_text)
        return True
    except Exception:
        options = select_locator.locator("option")
        count = options.count()
        for i in range(count):
            if option_text.strip().lower() in options.nth(i).inner_text().strip().lower():
                value = options.nth(i).get_attribute("value")
                select_locator.select_option(value=value)
                human_sleep()
                logger.debug("Selected option by value for text %r: %r", option_text, value)
                return True
        return False


# ---------------------------- frame-safe helpers ------------------------------ #

def safe_count(scope, selector: str) -> int:
    try:
        return scope.locator(selector).count()
    except Exception as e:
        if "Frame was detached" in str(e) or "Target closed" in str(e) or "Execution context was destroyed" in str(e):
            logger.debug("Skipping detached scope while counting %r", selector)
            return 0
        logger.debug("count(%r) failed on scope: %s", selector, e)
        return 0


def iter_live_scopes(page):
    # Yield page first
    yield page
    # Then try frames, but guard against detachment
    frames = []
    try:
        frames = list(getattr(page, "frames", []))
    except Exception:
        frames = []
    for f in frames:
        try:
            # quick ping
            _ = f.locator("html").count()
            yield f
        except Exception:
            continue


def _has_results_like(scope) -> bool:
    if safe_count(scope, "table tbody tr") > 0:
        return True
    if safe_count(scope, "[role='row']") > 1:
        return True
    if safe_count(scope, ".mat-row, .cdk-row") > 0:
        return True
    # Additional common data grids
    if safe_count(scope, ".mdc-data-table__row") > 0:  # Material 3
        return True
    if safe_count(scope, ".p-datatable-tbody tr") > 0:  # PrimeNG
        return True
    if safe_count(scope, ".ag-center-cols-container .ag-row") > 0:  # AG Grid
        return True
    if safe_count(scope, "[data-rowindex]") > 1:  # MUI DataGrid heuristic
        return True
    return False


def _has_no_results_like(scope) -> bool:
    texts = [
        "No results", "No Results", "No records found", "No record found",
        "Your search did not return", "0 results", "No matching records",
        "We could not find any results", "There were no results",
        "No licensee found", "No licensees found",
        # Common validation / gating messages
        "Please select a Board", "Please select a Board/Bureau/Program",
        "Please enter at least", "Enter at least", "This field is required",
        "Minimum of", "Too many results",
        # Bot/WAF/captcha hints
        "I'm not a robot", "reCAPTCHA", "Access denied", "The requested URL was rejected"
    ]

    for t in texts:
        try:
            if scope.locator(f"text={t}").count() > 0:
                return True
        except Exception:
            continue
    return False


# ---------------------------- high-level operations --------------------------- #

def wait_for_app_ready(page):
    logger.info("Waiting for app to become ready at %s", DCA_URL)
    page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_TIMEOUT)
    if page.get_by_text("The requested URL was rejected", exact=False).count() > 0:
        logger.error("Blocked by WAF")
        raise RuntimeError(
            "Access was blocked by the site (WAF). Try running non-headless, slower, or from a different network."
        )

    candidates = [
        "License Number",
        "Personal Name",
        "Business Name",
        "First Name",
        "Last Name",
        "Board/Bureau/Program",
        "License Type",
        "City",
        "County",
        "Zip Code",
        "Search by",
    ]
    for _ in range(60):
        for label_text in candidates:
            if page.locator(f"text={label_text}").first.count() > 0:
                logger.debug("Detected form label: %s", label_text)
                return
        human_sleep(0.2, 0.4)
    logger.warning("Could not positively identify the form labels; proceeding anyway.")


def set_search_mode(page, mode: str):
    logger.info("Setting search mode: %s", mode)
    if mode == "license":
        _ = (
            check_radio_by_text(page, "License Number")
            or check_radio_by_text(page, "Search by License")
            or check_radio_by_text(page, "License #")
        )
    else:
        _ = (
            check_radio_by_text(page, "Personal Name")
            or check_radio_by_text(page, "Name")
            or check_radio_by_text(page, "Business Name")
        )


def fill_name_filters(
    page,
    last: Optional[str],
    first: Optional[str],
    exact: bool,
    board: Optional[str],
    license_type: Optional[str],
    city: Optional[str],
    county: Optional[str],
):
    logger.info(
        "Filling name filters (last=%r, first=%r, exact=%s, board=%r, license_type=%r, city=%r, county=%r)",
        last, first, exact, board, license_type, city, county
    )
    if last:
        last_input = control_by_label(page, "Last Name")
        last_input.click()
        last_input.fill("")
        human_type(last_input, last)

    if first:
        first_input = control_by_label(page, "First Name")
        first_input.click()
        first_input.fill("")
        human_type(first_input, first)

    try:
        if exact:
            chk = page.get_by_role("checkbox", name=re.compile("Exact", re.I)).first
            if chk.count() > 0:
                chk.check()
                human_sleep()
                logger.debug("Checked 'Exact' name match.")
    except Exception:
        pass

    if board:
        board_select = control_by_label(page, "Board/Bureau/Program")
        if board_select.count() > 0:
            ok = select_option_by_visible_text(board_select, board)
            if not ok:
                logger.warning("Could not match board option: %s", board)

    if license_type:
        lt_select = control_by_label(page, "License Type")
        if lt_select.count() > 0:
            ok = select_option_by_visible_text(lt_select, license_type)
            if not ok:
                logger.warning("Could not match license type: %s", license_type)

    if city:
        city_input = control_by_label(page, "City")
        if city_input.count() > 0:
            city_input.click()
            city_input.fill("")
            human_type(city_input, city)

    if county:
        county_select = control_by_label(page, "County")
        if county_select.count() > 0:
            ok = select_option_by_visible_text(county_select, county)
            if not ok:
                logger.warning("Could not match county: %s", county)


def fill_license_number(page, license_number: str, board: Optional[str]):
    logger.info("Filling license number: %r (board=%r)", license_number, board)
    lic_input = control_by_label(page, "License Number")
    lic_input.click()
    lic_input.fill("")
    human_type(lic_input, license_number)

    if board:
        board_select = control_by_label(page, "Board/Bureau/Program")
        if board_select.count() > 0:
            ok = select_option_by_visible_text(board_select, board)
            if not ok:
                logger.warning("Could not match board option: %s", board)


def submit_search(page):
    # 1) Normal path: click a Search-like button (try common synonyms).
    for label in ["Search", "Search Licenses", "Search Licensees", "Find", "Lookup", "Go", "Submit", "Apply Filters"]:
        try:
            click_button_by_text(page, label)
            logger.info("Submitted search via %r button.", label)
            return
        except Exception:
            continue

    # 2) Form-scoped fallback: choose the *right* button inside the form (avoid Reset/Clear/etc.)
    try:
        for scope in iter_live_scopes(page):
            form = scope.locator("form:has(#lastName, #firstName)").first
            if form.count() == 0:
                # nearest ancestor form of #lastName or #firstName
                last = scope.locator("#lastName").first
                if last.count() > 0:
                    form = last.locator("xpath=ancestor::form[1]")
                else:
                    first = scope.locator("#firstName").first
                    if first.count() > 0:
                        form = first.locator("xpath=ancestor::form[1]")

            if form.count() > 0:
                candidates = form.locator(
                    "button:visible, input[type=submit]:visible, input[type=button]:visible, "
                    "[role=button]:visible, .mat-mdc-button:visible, .mdc-button:visible, .p-button:visible, .btn:visible"
                )
                count = candidates.count()
                best_i, best_score, best_label = -1, -10_000, ""
                NEG = ("reset", "clear", "back", "cancel", "new", "start over", "filter")
                POS = ("search", "find", "lookup", "submit", "go")
                for i in range(count):
                    el = candidates.nth(i)
                    label_parts = []
                    try:
                        label_parts.append((el.inner_text() or "").strip())
                    except Exception:
                        pass
                    for attr in ("aria-label", "value", "title"):
                        try:
                            v = el.get_attribute(attr) or ""
                            label_parts.append(v.strip())
                        except Exception:
                            continue
                    label = " ".join([p for p in label_parts if p]).strip()
                    ll = label.lower()
                    score = 0
                    if any(p in ll for p in POS):
                        score += 10
                    if "search" in ll:
                        score += 20  # strong bonus
                    if any(n in ll for n in NEG):
                        score -= 50
                    try:
                        tag = el.evaluate("el => el.tagName.toLowerCase()")
                        typ = (el.get_attribute("type") or "").lower()
                        if tag == "input" and typ == "submit":
                            score += 3
                    except Exception:
                        pass
                    if score > best_score:
                        best_i, best_score, best_label = i, score, label
                if best_i >= 0 and best_score > 0:
                    btn = candidates.nth(best_i)
                    btn.scroll_into_view_if_needed()
                    try:
                        btn.click()
                    except Exception:
                        btn.evaluate("el => el.click()")
                    human_sleep()
                    logger.info("Submitted search via form button fallback (ranked): %r", best_label or "<unlabeled>")
                    return
                # As a last attempt inside the form, press Enter in a field (lets Angular listen)
                fld = form.locator("#firstName, #lastName, #licenseNumber").first
                if fld.count() > 0:
                    fld.press("Enter")
                    human_sleep()
                    logger.info("Submitted search via Enter key (form-scoped).")
                    return
    except Exception:
        pass

    # 3) Last resort: press Enter in a field (page-scoped so Angular listens)
    try:
        field = page.locator("#firstName, #lastName").first
        if field.count() > 0:
            field.press("Enter")
            human_sleep()
            logger.info("Submitted search via Enter key (field-scoped).")
            return
    except Exception:
        pass

    raise RuntimeError("Could not trigger search; the UI may have changed.")


def wait_for_results(page):
    logger.info("Waiting for results …")
    start = time.monotonic()
    seen_spinner = False
    while time.monotonic() - start < 90:
        # Evaluate both on main page and any live frames
        for scope in iter_live_scopes(page):
            if safe_count(scope, "[class*='spinner'], [role='progressbar']") > 0:
                seen_spinner = True
            if _has_results_like(scope):
                logger.debug("Detected results.")
                return "rows"
            if _has_no_results_like(scope):
                logger.info("Search returned no results or a gating message.")
                return "empty"
        # yield to SPA/XHR
        try:
            page.wait_for_load_state("networkidle", timeout=1_000)
        except Exception:
            pass
        human_sleep(0.25, 0.55)
    logger.warning("Result state unknown after timeout%s.", " (spinner seen)" if seen_spinner else "")
    return "unknown"


def scrape_results(page, limit: int = 250) -> List[Dict[str, str]]:
    logger.debug("Scraping results (limit=%d)…", limit)
    results: List[Dict[str, str]] = []

    # Path 1: standard table across scopes
    for scope in iter_live_scopes(page):
        if safe_count(scope, "table thead th") > 0:
            headers = [h.inner_text().strip() for h in scope.locator("table thead th").all()]
            rows = scope.locator("table tbody tr")
            row_count = min(rows.count(), limit)
            for i in range(row_count):
                row = rows.nth(i)
                cols = row.locator("td")
                entry = {}
                for j in range(min(len(headers), cols.count())):
                    entry[headers[j]] = cols.nth(j).inner_text().strip()
                link = row.locator("a").first
                if safe_count(row, "a") > 0:
                    entry["_detail_link"] = link.get_attribute("href")
                    entry["_detail_text"] = link.inner_text().strip()
                results.append(entry)
            if results:
                logger.info("Scraped %d rows from HTML table.", len(results))
                return results

    # Path 2: ARIA/Material grids across scopes
    for scope in iter_live_scopes(page):
        rows = scope.locator("[role='row'], .mat-row, .cdk-row")
        if rows.count() > 1:
            header_cells = rows.nth(0).locator("[role='columnheader'], [role='gridcell'], .mat-header-cell")
            headers = [c.inner_text().strip() for c in header_cells.all()]
            for idx in range(1, min(rows.count(), limit + 1)):
                cells = rows.nth(idx).locator("[role='gridcell'], .mat-cell, .cdk-cell")
                entry = {}
                for j in range(min(len(headers), cells.count())):
                    key = headers[j] or f"col{j+1}"
                    entry[key] = cells.nth(j).inner_text().strip()
                if safe_count(rows.nth(idx), "a") > 0:
                    link = rows.nth(idx).locator("a").first
                    entry["_detail_link"] = link.get_attribute("href")
                    entry["_detail_text"] = link.inner_text().strip()
                results.append(entry)
            if results:
                logger.info("Scraped %d rows from ARIA/Material grid.", len(results))
                return results

    # Fallback: card-ish results
    for scope in iter_live_scopes(page):
        cards = scope.locator("div[class*='result'], div[class*='card']")
        if cards.count() > 0:
            for i in range(min(cards.count(), limit)):
                try:
                    txt = cards.nth(i).inner_text().strip()
                except Exception:
                    continue
                results.append({"text": re.sub(r"\n+", " | ", txt)})
            if results:
                logger.info("Scraped %d card-like results.", len(results))
                return results

    return results


def open_first_result_detail(page):
    logger.info("Opening first result detail page (if available)…")
    for scope in iter_live_scopes(page):
        cand = scope.locator("table tbody tr a, [role='row'] a, .mat-row a, .cdk-row a").first
        if cand.count() > 0:
            href = cand.get_attribute("href")
            try:
                cand.click()
            except Exception:
                cand.evaluate("el => el.click()")
            human_sleep(0.8, 1.2)
            try:
                fields = ["License Number", "Licensee Name", "Status", "Expiration Date", "Issue Date"]
                print("\nTop fields from detail page:")
                for f in fields:
                    val = extract_field_value_by_label(page, f)
                    if val:
                        print(f"  {f}: {val}")
            except Exception:
                pass
            return


def extract_field_value_by_label(page, label_text: str) -> Optional[str]:
    logger.debug("Extracting detail field: %r", label_text)
    dt = page.locator(f"dt:has-text('{label_text}')").first
    if dt.count() > 0:
        dd = dt.locator("xpath=following-sibling::dd[1]")
        if dd.count() > 0:
            return dd.inner_text().strip()
    label = page.locator(f"text={label_text}").first
    if label.count() > 0:
        sib = label.locator("xpath=following::*[self::span or self::div][1]")
        if sib.count() > 0:
            return sib.inner_text().strip()
    return None


def pretty_print_results(results: List[Dict[str, str]]):
    preferred = [
        "Name", "License Type", "License Number", "Status", "City", "County", "State", "Board",
        "Licensee Name", "Business Name"
    ]
    cols = []
    if results:
        all_keys = set().union(*[set(r.keys()) for r in results])
        cols = [c for c in preferred if c in all_keys]
        cols += [c for c in sorted(all_keys) if c not in set(cols) and not c.startswith("_")]

    if cols:
        print("\nResults:")
        print(" | ".join(cols))
        print("-" * (len(" | ".join(cols)) + 2))

    for r in results:
        if cols:
            row = " | ".join(r.get(c, "")[:120].replace("\n", " ").strip() for c in cols)
            print(row)
        else:
            print(r)


def save_csv(results: List[Dict[str, str]], path: str):
    if not results:
        return
    all_keys = sorted(set().union(*[set(r.keys()) for r in results]))
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_keys)
        writer.writeheader()
        for r in results:
            writer.writerow(r)
    logger.debug("CSV written to %s with %d columns.", path, len(all_keys))


def parse_args():
    p = argparse.ArgumentParser(description="Automate search.dca.ca.gov with Camoufox")
    p.add_argument("--mode", choices=["name", "license"], default="name", help="Search by name or license number")
    p.add_argument("--first", dest="first", help="First name (name mode)")
    p.add_argument("--last", dest="last", help="Last name (name mode)")
    p.add_argument("--exact", action="store_true", help="Exact name match if the site provides the checkbox")
    p.add_argument("--license-number", dest="license_number", help="License number (license mode)")
    p.add_argument("--board", help="Board/Bureau/Program visible option text")
    p.add_argument("--license-type", dest="license_type", help="License Type visible option text")
    p.add_argument("--city", help="City filter (name mode)")
    p.add_argument("--county", help="County visible option text (name mode)")
    p.add_argument("--csv", dest="csv_path", help="Path to save results CSV")
    p.add_argument("--headless", dest="headless", action="store_true", default=True, help="Run headless (default True)")
    p.add_argument("--headed", dest="headless", action="store_false", help="Run with a visible browser window")
    p.add_argument("--open-first-detail", action="store_true", help="Open the first result's detail page")
    p.add_argument("-v", "--verbose", action="count", default=0,
                   help="Increase logging verbosity (-v=INFO, -vv=DEBUG).")
    p.add_argument("--log-file", help="Optional path to write logs to file (in addition to stderr).")
    return p.parse_args()


def run_search(
    mode: str,
    last: Optional[str],
    first: Optional[str],
    exact: bool,
    board: Optional[str],
    license_type: Optional[str],
    city: Optional[str],
    county: Optional[str],
    license_number: Optional[str],
    headless: bool,
    csv_path: Optional[str],
    open_first_detail: bool,
):
    logger.info("Launching browser (headless=%s)…", headless)
    t0 = time.monotonic()
    with Camoufox(humanize=True, headless=headless) as browser:
        page = browser.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT)
        logger.info("Navigating to %s", DCA_URL)
        page.goto(url=DCA_URL, wait_until="domcontentloaded")
        human_sleep(0.8, 1.4)

        wait_for_app_ready(page)
        set_search_mode(page, mode)

        logger.debug(
            "Search parameters: mode=%s last=%r first=%r exact=%s board=%r license_type=%r city=%r county=%r license_number=%r csv=%r open_first_detail=%s",
            mode, last, first, exact, board, license_type, city, county, license_number, csv_path, open_first_detail
        )
        if mode == "license":
            if not license_number:
                raise ValueError("License-number mode requires --license-number.")
            fill_license_number(page, license_number, board)
        else:
            if not last and not first:
                raise ValueError("Name mode requires at least --last or --first.")
            fill_name_filters(page, last, first, exact, board, license_type, city, county)

        submit_search(page)
        outcome = wait_for_results(page)

        if outcome == "empty":
            print("No results.")
            logger.info("No results; ending run.")
            return
        elif outcome == "unknown":
            print("⚠️  Could not confirm results; the site may have changed.")
            logger.warning("Unknown result state; ending run.")
            return

        results = scrape_results(page, limit=250)
        if not results:
            print("No parseable results (UI may have changed).")
            logger.warning("No parseable results.")
            return

        pretty_print_results(results)
        logger.info("Parsed %d result rows.", len(results))

        if csv_path:
            save_csv(results, csv_path)
            logger.info("Saved %d rows to CSV: %s", len(results), csv_path)
            print(f"\nSaved {len(results)} rows to: {csv_path}")

        if open_first_detail:
            open_first_result_detail(page)

        if not headless:
            print("\nLeaving the browser open for inspection. Close the window to exit.")
            logger.info("Run completed in %.2fs; holding the browser window open.", time.monotonic() - t0)
            while True:
                time.sleep(0.5)
    logger.info("Run completed in %.2fs", time.monotonic() - t0)


if __name__ == "__main__":
    args = parse_args()
    try:
        setup_logging(args.verbose, args.log_file)
        run_search(
            mode=args.mode,
            last=args.last,
            first=args.first,
            exact=args.exact,
            board=args.board,
            license_type=args.license_type,
            city=args.city,
            county=args.county,
            license_number=args.license_number,
            headless=args.headless,
            csv_path=args.csv_path,
            open_first_detail=args.open_first_detail,
        )
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    except Exception as e:
        logger.exception("Fatal error: %s", e)
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
