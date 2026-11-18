# GST Reco – Tally ↔ GST portal (Windows desktop app)

Pulls sales and purchase vouchers live from TallyPrime, downloads GSTR-2B / filed GSTR-1 from the GST
portal through an assisted browser, reconciles them, builds the GSTR-1 upload JSON, and prepares a
GSTR-3B working — with Excel reports for everything.

## Install

1. Download **GSTReco-Setup-x.y.z.exe** from the Releases page and run it.
   No admin rights are needed. It installs to your user profile and adds Start-menu and desktop shortcuts.
2. In **TallyPrime**: `F1 (Help) > Settings > Connectivity > Client/Server configuration` →
   *TallyPrime acts as* **Both**, *Enable ODBC* **Yes**, *Port* **9000**. Keep the company open.

The portal steps use your installed **Microsoft Edge** (or Chrome), so no extra browser is needed.

## Updates

Click **⟳ Check for updates** (top-right, or *Help → Check for updates*). The app also checks quietly on
start-up. If a newer version exists, it shows what changed, downloads the installer, verifies its
checksum, installs it silently and reopens. Settings (`%APPDATA%\GSTReco`) and reports
(`Documents\GSTReco`) are kept.

### Publishing a new version (for whoever maintains the code)

1. Make the code change and push it to GitHub.
2. Create a tag with a higher version, either `git tag v1.0.1 && git push origin v1.0.1` or, on GitHub,
   *Releases → Draft a new release → tag `v1.0.1` → Publish*.
3. GitHub Actions (`.github/workflows/release.yml`) runs the self-test, builds the exe and installer on
   Windows, and publishes `GSTReco-Setup-1.0.1.exe` plus `latest.json`. Every installed copy then sees
   the update. The release notes are the commit messages since the previous tag.

The update check reads `https://github.com/<owner>/<repo>/releases/latest/download/latest.json`, so the
repository (or at least its releases) must be **public**. To host updates elsewhere instead (for example
your own website), upload the installer and `latest.json` there and set *Settings → Update address*.
`build_exe.bat` builds the installer locally on a Windows PC with Inno Setup installed.

## Monthly workflow

| Tab | What you do | What the app does |
|---|---|---|
| top bar | Enter your GSTIN, pick From/To month, pick the Tally company (↻ lists open companies) | |
| 1. Tally data | **Fetch from Tally** | Reads ledgers, groups, voucher types, stock items (HSN, GST rate) and all Sales / Purchase / Credit Note / Debit Note vouchers for the period. Highlights problems: missing supplier invoice no., invalid GSTIN, IGST on intra-state, mixed rates. |
| 2. GST portal | **Open portal & log in** – you type username, password, CAPTCHA, OTP | Opens Edge with a saved profile. Detects when you are logged in. |
| | **Download GSTR-2B** | Picks FY / quarter / month on the Returns Dashboard, clicks GSTR-2B → Download → Generate JSON, saves and loads the file. Repeats for each month in the From–To range. |
| 3. Purchase vs 2B | **Run reconciliation** → **Export to Excel** | Matches books vs 2B (see below). Double-click a row for details. |
| 4. Sales & GSTR-1 | **Generate GSTR-1 JSON** → **Upload generated JSON to portal** | Builds B2B, B2CL, B2CS, CDNR, CDNUR and HSN (B2B/B2C split) sections and attaches the file under GSTR-1 → Prepare Offline → Upload. |
| | **Download filed GSTR-1** (tab 2) → **Reco books vs filed GSTR-1** | Invoice-level reco for B2B/B2CL/notes, POS×rate reco for B2CS. |
| 5. GSTR-3B | **Compute** → **Export** | 3.1(a), 3.1(c), 3.1(d), 3.2, 4(A)(3), 4(A)(5), 4(B), ITC per books, "safe ITC" (2B ∩ books), indicative net payable. |

Files are saved under `Documents\GSTReco\<GSTIN>\<period>\` (change in Settings). Raw Tally
responses are kept in `tally_raw\` for audit; you can reload them with *Load Tally XML files…*.

### Matching logic

1. **Matched** – same GSTIN + invoice no. (normalised: case, spaces, `/ - .`, leading zeros
   ignored, so `INV/0012` = `inv-12`) and taxable & tax within the tolerance (default ₹1).
2. **Amount mismatch** – same invoice, amounts differ beyond tolerance.
3. **Probable match** – same GSTIN and amounts, invoice no. written differently
   (e.g. `GS 0098` vs `GS/98/26-27`; financial-year tags are ignored).
4. **GSTIN mismatch** – same invoice no. and amounts, different GSTIN.
5. **Only in books** / **Only in portal** – follow up with the supplier, or book the missing bill.

Several vouchers for the same supplier invoice are combined before matching. Remarks flag date
differences, IGST vs CGST/SGST mix-ups, reverse charge, and 2B "ITC not available".

## What is automated and what is not

* **Login is always yours.** The portal requires a CAPTCHA and an OTP; the app never stores or
  types your password and does not try to bypass the CAPTCHA.
* **Filing is always yours.** The app uploads the GSTR-1 JSON and prepares figures; you review on
  the portal and file with DSC/EVC. GSTR-3B figures are entered on the portal by you.
* Fully unattended download/filing is only possible through the official GST API via a licensed
  **GSP**; the code is structured so a GSP client can replace `gstreco/portal.py` later.

## Things to check on first use

* **Portal buttons:** the portal layout changes from time to time. If an automatic click fails,
  the Activity box tells you which button to click — the downloaded file is still captured. To fix
  it permanently, copy `gstreco\portal_selectors.json` to `%APPDATA%\GSTReco\` and edit it.
* **GSTR-1 JSON:** import the first generated file into the GST Offline Tool to confirm it validates
  before uploading. Nil/exempt/export invoices (no tax) are listed as warnings, not exported. Document
  summary (Table 13) is not generated.
* **GST rate per line:** taken from the stock item's GST details in Tally (rate valid on the
  invoice date). For accounting-mode invoices the rate is derived from tax ÷ taxable; mixed-rate
  accounting invoices get a warning.
* **Tally data:** purchases use the *Supplier Invoice No./Date* (Reference) fields; fill these in Tally
  for good matching. GST booked through Journal vouchers is not picked up.

## Project layout

```
app.py                      desktop UI (tkinter)
gstreco/updater.py          in-app updater
installer/GSTReco.iss       Windows installer (Inno Setup)
.github/workflows/release.yml  build + publish on every version tag
gstreco/tally.py            TallyPrime XML/HTTP client + voucher → GST document
gstreco/portal_data.py      GSTR-2B / GSTR-1 JSON (and ZIP) parsers
gstreco/reco.py             matching engine
gstreco/gstr1_builder.py    GSTR-1 JSON builder
gstreco/gstr3b.py           GSTR-3B working
gstreco/report.py           Excel reports
gstreco/portal.py           assisted browser (Playwright)
gstreco/portal_selectors.json
tests/selftest.py           end-to-end test on sample data:  python tests\selftest.py
```
