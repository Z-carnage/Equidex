# EQUIDEX — Setup & Notices

A keyboard-driven equity research terminal for US & Indian markets. Single-page web app
(`Equidex.html`) served by a small local Python proxy (`serve.py`) that fetches
data server-side from free, keyless public sources.

---

## 1. Requirements

- **Python 3.7+** (uses only the standard library — no `pip install` needed)
- A modern browser (Chrome, Edge, Firefox, or Safari)
- An internet connection (data is fetched live from public sources)

---

## 2. Install / run it

### Option A — the installer (recommended for other computers)

Give people **`EQUIDEX-Setup.exe`** (in the `dist` folder). Double-clicking it opens a setup wizard
(Welcome → License → Location → Install → Finish) that installs EQUIDEX per-user — **no administrator
rights needed** — creates Start-Menu and Desktop shortcuts, and registers it in **Add/Remove
Programs** (so it can be uninstalled the normal way). This is the easiest way to move the app to
another PC: copy that one file over and run it.

### Option B — the portable app (no install)

Double-click **`EQUIDEX.exe`** directly. It opens in its **own desktop window** — no browser, no
address bar — running a small local server on `127.0.0.1` behind it for data; closing the window
stops everything. Nothing is installed; copy this single file anywhere.

Both require the **Microsoft Edge WebView2 runtime**, which is pre-installed on Windows 11 and
current Windows 10. (If it were ever missing, the app falls back to the default browser.) An internet
connection is required for live data.

### Option B — from source (needs Python)

```bash
python serve.py 8777
```

Then open `http://localhost:8777/Equidex.html`. Change the port if `8777` is busy
(e.g. `python serve.py 8080`). Stop the server with `Ctrl+C`.

Either way, the server both serves the static page **and** proxies the data APIs from the same
origin, so the browser makes only same-origin requests (no CORS issues, no API keys, no
third-party proxy).

### Rebuilding the .exe

Requires [PyInstaller](https://pyinstaller.org) and [pywebview](https://pywebview.flowrl.com)
(`pip install pyinstaller pywebview`). The app entry point is `app.py` (opens the native window);
`serve.py` is the server it runs behind the scenes. From the project folder:

```bash
python -m PyInstaller --onefile --windowed --name EQUIDEX --noconfirm ^
  --add-data "Equidex.html;." ^
  --add-data "equidex-inconsolata-dark.png;." ^
  --add-data "equidex-inconsolata-light.png;." ^
  --collect-all webview ^
  app.py
```

The result is `dist/EQUIDEX.exe`. (`--windowed` removes the console so it's a pure app window;
`--collect-all webview` bundles the native-window backend. The `^` line-continuations are for
Windows `cmd`; put it on one line in other shells.)

### Rebuilding the installer

The installer wizard is `setup_wizard.py`; it bundles the app exe built above. After rebuilding
`dist/EQUIDEX.exe`, build the installer with:

```bash
python -m PyInstaller --onefile --windowed --name "EQUIDEX-Setup" --noconfirm ^
  --icon equidex.ico ^
  --add-data "dist/EQUIDEX.exe;." ^
  --add-data "equidex.ico;." ^
  --add-data "equidex_hdr.png;." ^
  --add-data "SETUP.md;." ^
  setup_wizard.py
```

The result is `dist/EQUIDEX-Setup.exe`.

### Files

| File | Purpose |
|------|---------|
| `Equidex.html` | The entire front-end (UI, charts, all tabs) |
| `serve.py` | Static file server + same-origin data proxy |
| `equidex-inconsolata-dark.png` / `equidex-inconsolata-light.png` | Logo assets (dark / light themes) |
| `SETUP.md` | This file |

Before distributing, open `serve.py` and set a **real contact** in `SEC_UA`
(currently a placeholder) — SEC EDGAR's fair-access policy requires a genuine name + email in
the request User-Agent, or it may block requests.

---

## 3. Data sources & their terms

EQUIDEX fetches data at run time from the third-party sources below. **Each is governed by its
own terms of use**, and some market data may be subject to **exchange redistribution licensing**.
You are responsible for complying with those terms when you use or distribute this software.

| Source | Provides |
|--------|----------|
| Yahoo Finance | Quotes, charts, fundamentals, options, analyst data, market movers, extended-hours quotes |
| SEC EDGAR | US filings, XBRL financials, Form 4 insider trades, 13F holdings, M&A filings, Exhibit 21 (US government — public domain) |
| NSE India | Indian quotes, corporate announcements, SEBI PIT insider trades, deal announcements |
| Screener.in | Indian financial statements & quarterly EPS history |
| Upstox (assets) | NSE / MCX instrument reference lists |
| MCX | Commodity futures (gold, silver, crude) |
| Google News (RSS) | Market headlines, linked to their original publishers |
| Nasdaq | IPO & earnings calendars |
| US BLS & World Bank | Economic indicators |
| Wikipedia | S&P 500 constituent changes (CC BY-SA) |
| OpenFIGI | CUSIP → ticker mapping |

> **Note on redistribution.** Yahoo Finance and the exchanges (NYSE/Nasdaq, NSE/BSE, MCX)
> generally restrict commercial use and redistribution of their market data, and quotes are often
> licensed from the exchanges. A private/personal build is materially different from hosting this as
> a public service or charging for it. If you intend to distribute this — especially commercially —
> get legal advice and consider moving to properly licensed data feeds. SEC EDGAR data is US
> government public domain and is not restricted this way.

Company names, tickers and marks in the data belong to their respective owners. "EQUIDEX" and its
logo are marks of this project; it is **not** affiliated with, endorsed by, or a product of
any data provider listed above.

---

## 4. Disclaimer

**Not investment advice.** EQUIDEX is a research and information tool only. Nothing it shows is
investment, financial, legal or tax advice, or a recommendation or solicitation to buy or sell any
security. Do your own research and consult a licensed professional before making any decision.

**No warranty.** All data is provided "as is", without warranty of any kind. Figures come from free
public sources, may be **delayed, incomplete or inaccurate**, and can differ from official exchange
or filing data. Verify every figure against the primary source before relying on it. To the maximum
extent permitted by law, the authors accept no liability for any loss arising from use of this
software or its data.

---

## 5. Third-party open-source components

The following components are used by the app. Their licenses require that the notices below travel
with any distribution.

- **PDF.js** — © Mozilla Foundation and contributors — **Apache License 2.0**. Loaded from cdnjs to
  render NSE announcement PDFs in the browser.
- **Inter** — © The Inter Project Authors — **SIL Open Font License 1.1**. User-interface typeface
  (loaded from Google Fonts).
- **Inconsolata** — © The Inconsolata Project Authors — **SIL Open Font License 1.1**. Monospace &
  wordmark typeface (loaded from Google Fonts).

The Windows application and installer also include the Python runtime, libraries and other
components listed in **`THIRD-PARTY-LICENSES.txt`**, which contains their full license texts.

### Apache License 2.0 (PDF.js)

```
                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use, reproduction,
      and distribution as defined by Sections 1 through 9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under common
      control with that entity. For the purposes of this definition,
      "control" means (i) the power, direct or indirect, to cause the
      direction or management of such entity, whether by contract or
      otherwise, or (ii) ownership of fifty percent (50%) or more of the
      outstanding shares, or (iii) beneficial ownership of such entity.

      "You" (or "Your") shall mean an individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making modifications,
      including but not limited to software source code, documentation
      source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which the
      editorial revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship. For the purposes
      of this License, Derivative Works shall not include works that remain
      separable from, or merely link (or bind by name) to the interfaces of,
      the Work and Derivative Works thereof.

      "Contribution" shall mean any work of authorship, including
      the original version of the Work and any modifications or additions
      to that Work or Derivative Works thereof, that is intentionally
      submitted to Licensor for inclusion in the Work by the copyright owner
      or by an individual or Legal Entity authorized to submit on behalf of
      the copyright owner. For the purposes of this definition, "submitted"
      means any form of electronic, verbal, or written communication sent
      to the Licensor or its representatives, including but not limited to
      communication on electronic mailing lists, source code control systems,
      and issue tracking systems that are managed by, or on behalf of, the
      Licensor for the purpose of discussing and improving the Work, but
      excluding communication that is conspicuously marked or otherwise
      designated in writing by the copyright owner as "Not a Contribution."

      "Contributor" shall mean Licensor and any individual or Legal Entity
      on behalf of whom a Contribution has been received by Licensor and
      subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      copyright license to reproduce, prepare Derivative Works of,
      publicly display, publicly perform, sublicense, and distribute the
      Work and such Derivative Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions of
      this License, each Contributor hereby grants to You a perpetual,
      worldwide, non-exclusive, no-charge, royalty-free, irrevocable
      (except as stated in this section) patent license to make, have made,
      use, offer to sell, sell, import, and otherwise transfer the Work,
      where such license applies only to those patent claims licensable
      by such Contributor that are necessarily infringed by their
      Contribution(s) alone or by combination of their Contribution(s)
      with the Work to which such Contribution(s) was submitted. If You
      institute patent litigation against any entity (including a
      cross-claim or counterclaim in a lawsuit) alleging that the Work
      or a Contribution incorporated within the Work constitutes direct
      or contributory patent infringement, then any patent licenses
      granted to You under this License for that Work shall terminate
      as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of the
      Work or Derivative Works thereof in any medium, with or without
      modifications, and in Source or Object form, provided that You
      meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent notices
          stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative Works
          that You distribute, all copyright, patent, trademark, and
          attribution notices from the Source form of the Work,
          excluding those notices that do not pertain to any part of
          the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of its
          distribution, then any Derivative Works that You distribute must
          include a readable copy of the attribution notices contained
          within such NOTICE file, excluding those notices that do not
          pertain to any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file distributed
          as part of the Derivative Works; within the Source form or
          documentation, if provided along with the Derivative Works; or,
          within a display generated by the Derivative Works, if and
          wherever such third-party notices normally appear. The contents
          of the NOTICE file are for informational purposes only and
          do not modify the License. You may add Your own attribution
          notices within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work, provided
          that such additional attribution notices cannot be construed
          as modifying the License.

      You may add Your own copyright statement to Your modifications and
      may provide additional or different license terms and conditions
      for use, reproduction, or distribution of Your modifications, or
      for any such Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise complies with
      the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state otherwise,
      any Contribution intentionally submitted for inclusion in the Work
      by You to the Licensor shall be under the terms and conditions of
      this License, without any additional terms or conditions.
      Notwithstanding the above, nothing herein shall supersede or modify
      the terms of any separate license agreement you may have executed
      with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use the trade
      names, trademarks, service marks, or product names of the Licensor,
      except as required for reasonable and customary use in describing the
      origin of the Work and reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law or
      agreed to in writing, Licensor provides the Work (and each
      Contributor provides its Contributions) on an "AS IS" BASIS,
      WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
      implied, including, without limitation, any warranties or conditions
      of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or FITNESS FOR A
      PARTICULAR PURPOSE. You are solely responsible for determining the
      appropriateness of using or redistributing the Work and assume any
      risks associated with Your exercise of permissions under this License.

   8. Limitation of Liability. In no event and under no legal theory,
      whether in tort (including negligence), contract, or otherwise,
      unless required by applicable law (such as deliberate and grossly
      negligent acts) or agreed to in writing, shall any Contributor be
      liable to You for damages, including any direct, indirect, special,
      incidental, or consequential damages of any character arising as a
      result of this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of goodwill,
      work stoppage, computer failure or malfunction, or any and all
      other commercial damages or losses), even if such Contributor
      has been advised of the possibility of such damages.

   9. Accepting Warranty or Additional Liability. While redistributing
      the Work or Derivative Works thereof, You may choose to offer,
      and charge a fee for, acceptance of support, warranty, indemnity,
      or other liability obligations and/or rights consistent with this
      License. However, in accepting such obligations, You may act only
      on Your own behalf and on Your sole responsibility, not on behalf
      of any other Contributor, and only if You agree to indemnify,
      defend, and hold each Contributor harmless for any liability
      incurred by, or claims asserted against, such Contributor by reason
      of your accepting any such warranty or additional liability.

   END OF TERMS AND CONDITIONS

   APPENDIX: How to apply the Apache License to your work.

      To apply the Apache License to your work, attach the following
      boilerplate notice, with the fields enclosed by brackets "[]"
      replaced with your own identifying information. (Don't include
      the brackets!)  The text should be enclosed in the appropriate
      comment syntax for the file format. We also recommend that a
      file or class name and description of purpose be included on the
      same "printed page" as the copyright notice for easier
      identification within third-party archives.

   Copyright [yyyy] [name of copyright owner]

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
```

### SIL Open Font License, Version 1.1 (Inter, Inconsolata)

Inter is copyright © The Inter Project Authors. Inconsolata is copyright © The Inconsolata
Project Authors. Both are licensed under the SIL Open Font License, Version 1.1:

```
-----------------------------------------------------------
SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007
-----------------------------------------------------------

PREAMBLE
The goals of the Open Font License (OFL) are to stimulate worldwide
development of collaborative font projects, to support the font creation
efforts of academic and linguistic communities, and to provide a free and
open framework in which fonts may be shared and improved in partnership
with others.

The OFL allows the licensed fonts to be used, studied, modified and
redistributed freely as long as they are not sold by themselves. The
fonts, including any derivative works, can be bundled, embedded,
redistributed and/or sold with any software provided that any reserved
names are not used by derivative works. The fonts and derivatives,
however, cannot be released under any other type of license. The
requirement for fonts to remain under this license does not apply
to any document created using the fonts or their derivatives.

DEFINITIONS
"Font Software" refers to the set of files released by the Copyright
Holder(s) under this license and clearly marked as such. This may
include source files, build scripts and documentation.

"Reserved Font Name" refers to any names specified as such after the
copyright statement(s).

"Original Version" refers to the collection of Font Software components as
distributed by the Copyright Holder(s).

"Modified Version" refers to any derivative made by adding to, deleting,
or substituting -- in part or in whole -- any of the components of the
Original Version, by changing formats or by porting the Font Software to a
new environment.

"Author" refers to any designer, engineer, programmer, technical
writer or other person who contributed to the Font Software.

PERMISSION & CONDITIONS
Permission is hereby granted, free of charge, to any person obtaining
a copy of the Font Software, to use, study, copy, merge, embed, modify,
redistribute, and sell modified and unmodified copies of the Font
Software, subject to the following conditions:

1) Neither the Font Software nor any of its individual components,
in Original or Modified Versions, may be sold by itself.

2) Original or Modified Versions of the Font Software may be bundled,
redistributed and/or sold with any software, provided that each copy
contains the above copyright notice and this license. These can be
included either as stand-alone text files, human-readable headers or
in the appropriate machine-readable metadata fields within text or
binary files as long as those fields can be easily viewed by the user.

3) No Modified Version of the Font Software may use the Reserved Font
Name(s) unless explicit written permission is granted by the corresponding
Copyright Holder. This restriction only applies to the primary font name as
presented to the users.

4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font
Software shall not be used to promote, endorse or advertise any
Modified Version, except to acknowledge the contribution(s) of the
Copyright Holder(s) and the Author(s) or with their explicit written
permission.

5) The Font Software, modified or unmodified, in part or in whole,
must be distributed entirely under this license, and must not be
distributed under any other license. The requirement for fonts to
remain under this license does not apply to any document created
using the Font Software.

TERMINATION
This license becomes null and void if any of the above conditions are
not met.

DISCLAIMER
THE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT
OF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE
COPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
INCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL
DAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM
OTHER DEALINGS IN THE FONT SOFTWARE.
```
