# license-addr-search

Queries the California DCA license search (search.dca.ca.gov) and pulls the public
license records into a CSV. Drives a stealth browser (Camoufox) with human-like
typing and delays so the site behaves normally. Search by name or by license number.

For authorized OSINT / due-diligence on public professional-license records.

## Usage

```
# by name
python license-addr-search.py --first Jane --last Doe --board "Contractors State License Board" --csv out.csv

# by license number
python license-addr-search.py --mode license --license-number 1234567 --csv out.csv

# watch it run
python license-addr-search.py --first Jane --last Doe --headed
```

Flags: `--board`, `--license-type`, `--city`, `--county`, `--exact`,
`--open-first-detail`, `--headed` (visible browser), `-v` (verbose).

Requires `camoufox`.
