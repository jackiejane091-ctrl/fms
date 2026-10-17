# FMS — Roadmap
*(Simple lafzon me: kya ban gaya, kya banega, aur kis order me. Last updated: 2026-10-15)*

---

## ✅ Phase 1 — HO GAYA (mukammal + tested)

**Buniyaad tayyar hai.** 10/10 tests pass.

- **Accounts:** Groups banao, Facebook accounts add karo (single/bulk/import), email/phone + password se login, cookie optional, 2FA optional, activate/deactivate
- **Proxies:** Har account par apna proxy (optional), validation + dead proxy cleanup
- **Products:** Product banao — naam, price, tags, category, title list (rotation), images folder, locations list, video (optional)
- **Listing Engine:** **Wave batching** (batch size N = N Chrome ek saath, har ek apni ID complete kare, phir agli wave), min/max ads per account, random price, delays, pause/resume, rukne par resume, live run board, logs
- **Human-like behavior:** Insaan jaisi typing (ruk-ruk ke), mouse movement, har account ka alag browser profile
- **Dashboard:** Local panel — Dashboard, Accounts, Products, Runs, Logs pages

---

## 🔨 Phase 2 — AGAY (behavior aur smart features)

**Maqsad:** Tool ko "smart" aur "human-like" banana.

1. **Script Engine** — Khud ke scripts banao: Post → Scroll → Drafts → Reels dekho → Publish. Har account ka behavior alag, taake Facebook pakar na sake. "Wait For Next Turn" action.
2. **Draft & Publish (full)** — Pehle drafts banao, phir sab ek saath publish ya gap ke saath one-by-one (limit walay accounts ke liye)
3. **Manual-Publish Helper** — Forms auto-fill hon, Publish tum khud karo (sab se safe tareeqa)
4. **Scrap Module** — Competitor ki achi listings se data uthao, apna banao
5. **Relist/Edit** — Limit walay accounts par purani listings edit karke nayi jaisi banao
6. **UI Review Round** — 🎨 *Yehi wo waqt hai jab rang, buttons, pages ki changes hongi — tum dekh ke bataogi kya badalna hai*
7. **Multi-Tab Posting (FBM Robo style)** — *(Tumhari 17 Oct wali spec, same-to-same)*
   - Setting me **"Open tabs without limit"** me number likho (tumhara: 2) → utne tabs khulenge
   - Flow: browser open → ID ka **home feed** → **Marketplace** → **create item** wala page → osi browser me 2nd tab **duplicate** (setting me 3 likho ge to 3 bar duplicate — jitne number, utne tabs)
   - Tab 1: listing mukammal → **publish** → Tab 2 pe jao: listing → publish → agay tabs hon to aglay pe, warna wapis **Tab 1** pe — create item wala page **reload** karke agali listing → Tab 2 ke sath bhi yehi
   - **Kisi tab pe error aye to wait nahi** — aglay tab pe jao, wahan listing karo; jab wapis error walay tab pe ao to create item wala page reload karke listing banao — **time waste nahi**
   - **Manual fix sirf 1 listing ke liye** — agar listing 1 ho to error pe **ruk jao** (browser khula rakho), tum hath se sahi karo; listings **ziada** hon to error wali ko **skip** karke agli listing pe chalo
   - **Per-listing timing** — setting me minimum/maximum time to post (tumhara: **45–60 seconds**) = **browser open se publish tak** ka total time, per listing. Na itna tez ke bot lage, na itna slow ke manual se ziada time lage

---

## 🚀 Phase 3 — AKHIR (verification, polish, final exe)

1. **Verification Helper** — Phone verification batch me (ek number, multiple accounts)
2. **Analytics** — Kitni listings, kitne messages/clicks, kaun sa account safe chal raha
3. **FMS.exe (final)** — Ek single file: download → double-click → chal gaya. Koi Python nahi, koi command nahi.
4. **Final UI Review** — Aakhri look-and-feel check tum se

---

## 📦 Release process — split bundle (v0.2.0+)

*(Technical — sirf release ke waqt parhna hai)*

**Model:** chhota `FMS.exe` (~50 MB, app only) + `browsers-windows.zip` (~600 MB, ek dafa) + dashboard me **Update button**.

**Files:**
- Version file (Drive, shared "anyone with the link"): `fms_version.json`
  → direct URL: `https://drive.google.com/uc?id=1ct7xfbLlUVRQ7WFAZ_0USm1wv7ijLNpN&export=download`
  → baked into the exe as `VERSION_URL` in `fms/updater.py`
- Schema: `{"version", "exe_url", "exe_size", "browsers_url", "changelog"}`

**Nayi release ke steps:**
1. `fms/__init__.py` me `__version__` barhao.
2. Repo ko GitHub par push karo → Actions tab → **"Build FMS (split bundle)"** → Run workflow
   (ya `v*` tag push karo). Do artifacts milen ge:
   - `FMS-windows-app` → `FMS.exe` (chhota)
   - `browsers-windows` → `browsers-windows.zip` (sirf tab jab Playwright version badle)
3. Artifacts download karo → user ke Google Drive par upload karo (Drive root, FMS.exe ke paas),
   dono ko **"anyone with the link"** share karo.
4. Drive par `fms_version.json` ka content update karo:
   `files update --params '{"fileId":"1ct7xfbLlUVRQ7WFAZ_0USm1wv7ijLNpN"}' --upload release/fms_version.json`
   (`exe_url`/`exe_size` = nayi exe, `browsers_url` = zip, `version` = naya number, `changelog` = Roman Urdu me).
5. User ko kuch nahi karna — dashboard ka **Update button** nayi version khud dhoondh ke
   sirf chhoti exe download karega; FMS band karke dobara kholne par update lag jayega
   (purani exe `FMS.prev.exe` me backup rehti hai).

**Pehli dafa (user ke laptop par):** FMS kholne par dashboard me banner aayega —
"Download browsers" (one-time, ~600 MB). Uske baad browsers dobara download nahi honge.

- **Messenger Module** — Tumhara messenger jab ban jaye to uski **API key** FMS me add kar ke integration kar lein ge. Abhi is par kaam nahi ho raha.

---

## 🔁 Kaam karne ka tareeqa (tum + main, sath sath)

```
Main banata hun → Tum mobile par screenshots dekh ke review karti ho
     → Changes batati ho → Main theek karta hun → Agla step
```

- Har phase ke **end par tumhara review lazmi** hai — usi waqt design changes (rang, button, page) bhi hongi
- Beech me bhi kuch samajh aaye to kabhi bhi bata sakti ho — roadmap pathar par nahi likha
- **Testing:** Har phase 1–2 tumhare apne accounts par test hoga, phir agay barhein ge

---

## ⚠️ Do honest baatein

1. **Facebook kabhi bhi pakar sakta hai** — automation ka ye risk hamesha rehta hai. Isi liye human-like behavior aur slow ramping design me shamil hai, lekin koi guarantee nahi de sakta.
2. **Facebook apna design badalta rehta hai** — agar listing wala page badal gaya to selectors update karne parein ge. Iske liye ek hi file hai (`selectors.py`) — chhota kaam hai, main kar dunga.

---

## 📍 Abhi hum yahan hain

**Phase 1 + Phase 2 complete (21/21 tests green).** Ab 1 listing kamyab post karna baqi hai — uske baad Multi-Tab Posting aur Phase 3 (Verification, Analytics, final exe).

Tum roadmap parho. Agar order theek lage to bolo **"Phase 2 shuru karo"** — aur agar kuch upar-neeche karna hai (koi feature pehle chahiye, koi baad me), to wo bhi bata do.
