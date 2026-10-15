# FMS ↔ FBM Robo alignment notes (from user's real screenshots, 2026-10-15)

Source: 3 screenshots of the actual FBM Robo **v6.1.2.0** on the user's laptop
(Title bar: "FBM Robo (v6.1.2.0) | ByteSync Solutions - https://fbmrobo.com/ - Expiry: Thursday, September 17, 2026").
Newer than the v6.0.1.0 seen in YouTube videos.

## Screenshot 1 — Main dashboard
- Top bar: Settings | Help | Manage Scripts buttons; icons: pin, Accounts, Products, power.
- Batch Size spinner (1), Script dropdown ("Select Script"), Select Groups dropdown.
- Start row: Start (green), Pause All, Schedule At datetime (08/07/2026 03:00 PM), Active Accounts checkbox (checked), speed selector "1x".
- Panels: Accounts (0) with placeholder "enter accounts here...", Pending (0), In Progress (0), Done Posting (0) showing "All", Failed (0).
- Exclude (0): "enter accounts to exclude..."
- Logs panel with Expand/Collapse + Copy All links.
- Watermark: https://fbmrobo.com/
- ALIGNMENT: our dashboard has the same panels. Differences to adopt: "enter accounts here..." inline paste on main screen, Exclude box on main screen, Schedule At datetime picker, speed selector (1x), top-right icon buttons.

## Screenshot 2 — Script dropdown = 17 ACTIONS (exact list)
1. Post
2. Relist & Edit
3. Draft & Publish
4. Create Drafts
5. Publish Drafts
6. Delete Duplicate
7. Delete Previous
8. Delete Draft
9. Delete Take Action Listings  ← CORRECTED 2026-10-15 by user (was misread as "Delete Fake Action Listings"; these are listings Facebook flagged with "take action"/identity-verification)
10. Delete All Invalid Listings
11. Renew Listing
12. Wait For Next Turn
13. Login
14. Download Images & Titles
15. Browser Launch
16. Change Marketplace Location
17. AI Behaviour Training
18. Scrap Data

GAPS IN FMS (we have: post, drafts, publish_drafts, wait_next_turn, relist_edit, scrap + activity blocks):
- MISSING as actions: Delete Duplicate, Delete Previous, Delete Draft, Delete Fake Action Listings, Delete All Invalid Listings, Renew Listing, Login, Download Images & Titles, Browser Launch, Change Marketplace Location, AI Behaviour Training.
- NOTE: in v6.1.2.0 the main-screen "Script" dropdown lists these ACTIONS directly (not named behavior scripts). The behavior-script concept (My Posting etc.) exists under "Manage Scripts". Our dashboard should mirror this: main run form = action selector with these exact 17 actions (+ script selector for behavior scripts where applicable).

## Screenshot 3 — Settings dialog (exact fields)
- FBM Multi Messenger API Key: "Paste your FBM__ key here" (+ eye icon). Text: "This key authenticates FBM Robo with your FBM Multi Messenger account. Generate it in the Multi Messenger app under Settings → API Keys. Treat it like a password; do not share it."
  → keep as deferred integration point (messenger deferred per user).
- Batch Size: 3 (number input)
- Checkboxes: Shuffle ✓ | Hide From Friends ✓ | Use Human Behaviour ✓
- Note: "Human-like behaviour makes automation look more human, but can slow down listing a little."
- Min Seconds To Post: 25 | Max Seconds To Post: 40
- No Of Browser Can Launch Parallely: 5
- Account Start Gap In Sec: 5
- Retry Count Before Quitting: 3
- Skip Images Used In No Of Days: 8
- Tabs Without Limit: 2
- Tabs Draft And Publish: 6
- Save (green) / Cancel buttons
- DIALOG CUT OFF at bottom — more settings below the fold (user to send scrolled-down screenshot).

NEW SETTINGS TO ADD IN FMS (we lack): Hide From Friends, Account Start Gap In Sec, No Of Browser Can Launch Parallely, Retry Count Before Quitting, Tabs Without Limit, Tabs Draft And Publish.
WE HAVE: batch size, shuffle, human behaviour, min/max seconds, skip images days.

## Settings dialog — COMPLETE (4 more screenshots, 2026-10-15)

User sent the full settings scroll-through. New findings beyond the earlier partial screenshot:

### Typing mode (NEW — not in earlier scope)
Radio options:
- **Human-like typing** (default selected): "Types character by character. Most human-like, slowest, best against strict detection."
- **Fast fill (JS)**: "Instantly sets values. Fastest, no clipboard use. Less human-like; Facebook may treat it more strictly."
- **OS clipboard paste**: "Copies each value and pastes with Ctrl+V. More human-like than JS, but uses your system clipboard (can interfere while you work) and is slower when many browsers run because pastes are queued."
"Human-like typing options": Speed presets: Fast / Normal / Careful (buttons).
- Min delay (ms): 30 | Max delay (ms): 80
- "Estimated speed: ~17.1 chars/sec. A 500-char description takes about 35s (excluding hybrid paste)."
- Thinking pause min (ms): 200 | Thinking pause max (ms): 600
- Extra delay after punctuation (ms): 80
- Newline pause min (ms): 300 | Newline pause max (ms): 900
- "Long text ..." section cut off at the bottom of the screenshot — hybrid paste threshold implied.

### IXBrowser (anti-detect) — OPTIONAL (user: keep optional, default = Chrome)
"Install IXBrowser, enable Local API, and use a plan that includes API access. Per-account Profile ID is set in Manage Accounts (leave empty to auto-create on first launch). Profiles created by FBM Robo are placed in the FBMRobo group in IXBrowser."
- Local API Host: 127.0.0.1 | Local API Port: 53200 | "Reset host & port" button.

### License section (NOT needed in FMS)
"Pause Subscription" button + License Key field exist — FMS has no licensing/subscription (locked decision), so skipped.
(FBM Multi Messenger API Key already known; messenger still deferred.)

## Pending from user (priority list given 2026-10-15)
1. "Long text" settings subsection (slightly cut off) — asked for one more small screenshot.
2. ✅ Accounts screen RECEIVED (see below). 3. Products screen, 4. Manage Scripts screen, 5. run-flow screenshots, 6. per-action option dialogs.

## Manage Groups + Accounts screenshots (2026-10-15, user walked through)

**Version bump: v6.2.0.0** (was v6.1.2.0). Title bar shows Your Id + Expiry Date.

### Pic 1 — Dashboard v6.2.0.0 (new details vs earlier)
- Settings and Help are DROPDOWNS (▼), not plain buttons.
- Batch Size has + / - spinner buttons around the value.
- Script dropdown placeholder: "— Select Script —".
- Row labels: "Start:", "Pause/Continue:" (Pause All button), "Schedule At:" with datetime "15/10/2026 09:22 am" + calendar icon.
- Speed is a blue button "⚡1x".
- Accounts panel has "Filter accounts..." search box.
- Panel headers use colons: "Accounts: (0)", "Pending: (0)", "In Progress: (0)", "Done Posting: (0)", "Failed: (0)"; "Exclude: (0)".
- Bottom right: Expand/Collapse, Copy All; "Logs:" at bottom.
- "Enter accounts here..." textarea in Accounts panel; "Enter accounts to exclude..." in Exclude.

### Pic 2 — "Manage Groups" dialog (opens from Products click per user)
Columns: Id | Group Name | Accounts | Product Counts | Manage Accounts (pencil) | Clone | Edit | Delete.
Top right: + (add group), X (close).
Rows seen: PLUSH 4 (7 accounts, 1 product), JUMBO 4 (7, 1), PLUSH 5 (31, 1), JUMBO 5 (4, 1), 3+2 (41, 1).
→ GAP: FMS groups lack the **Clone** action.

### Pic 3 — Accounts dialog (per group, "Add" flow)
Buttons: Add Single Account | Add Bulk Accounts | Import from File | Remove Selected | Add Proxies to Selected | Remove Proxies from Selected | Check Proxy Status | Remove Not Working Proxies.
"Select All (0 selected)" checkbox + search box.
Table columns: (checkbox) | # | Email | Password | Browser | Anti-detect Profile ID | Active (green toggle switch) | Proxy | Proxy Status | Cookie.
Save / Cancel at bottom.
→ GAPS: Import from File; proxy bulk tools (add/remove/check status/remove dead); Cookie column; per-row Browser value; Active as toggle switch. (Anti-detect Profile ID already covered by pass #2.)

### Pic 4 — Bulk account format (Notepad)
One line per account: `login;password;True/False;Browser` (phone numbers also accepted as login).
User rule: **False instead of True = that ID stays inactive / won't be opened.**
→ FMS bulk parser must accept exactly this format incl. the active flag and phone-number logins. (Credentials themselves NOT recorded — format only.)

## Products screenshots (2026-10-15, arriving one by one)

### Pic 1 — "Manage Products" dialog (RECEIVED)
- Top-right: + (add), car icon (add vehicle listing), house icon (add property listing), X (close).
- Columns: Id | Listing Type | Product Name | Price | Locations | Titles | Images | Category | Clone | Edit | Delete.
- Listing Type: "Item" (so types = Item / Vehicle / Property — Facebook's 3 listing kinds).
- Per product: Locations count (192), Titles count (28/25/29/15/11), Images "- / 344" (used/total rotation tracking), Category path "Furniture||Sofas, love seat..." (|| = category path separator).
- Rows: PLUSH 4, JUMBO 4, PLUSH 5, JUMBO 5, 3+2 — all Price 50, 192 locations.
→ GAPS vs FMS: Listing Type (Vehicle/Property kinds), per-product multi-location list, title variants list, image used/total rotation tracking, category path with || separator, clone product. (Pass #4 scope — starts after all 4 product screenshots received.)
- Pics 2–4 (add-product dialog + product details) PENDING.
### Pic 2 — Add/Edit Product dialog, bottom part (RECEIVED)
- **Titles (0)**: multi-line textarea (title variants, one per line).
- **Locations (0)**: multi-line textarea (location rotation list).
- **Delivery Options**: checkboxes — Public Meetup | Door Pickup | Door Dropoff.
- **Descriptions (1)**: "+ Add Description" button, "Description 1" textarea (multiple descriptions supported, rotation).
- Save (green) / Cancel.
### Pic 3 — Add Product dialog, top part (RECEIVED)
Fields (two columns):
- Product Name (text) | **Append Title Count** (spinner, 0)
- Price (text) | **Condition** (dropdown "Select Condition")
- Tags (text) | **Main Images** (folder picker)
- Category (dropdown "Select Category") | **Extra Images** (folder picker)
- Availability (dropdown "Select Availability") | **Video** (folder picker)
### Pic 4 — Add Product dialog, middle part (RECEIVED — overlaps pics 2–3; order was jumbled, all pieces now present)
Shows: Availability | Video (folder) / Titles (0) | Locations (0) / Delivery Options (Public Meetup, Door Pickup, Door Dropoff) / Descriptions (1) + Add Description.
→ Complete Add Product dialog reconstructed: Product Name, Append Title Count, Price, Condition, Tags, Main Images, Category, Extra Images, Availability, Video, Titles (multi), Locations (multi), Delivery Options (3 checkboxes), Descriptions (multi), Save/Cancel.
→ Vehicle/Property type-specific dialogs: DEFERRED — user said 2026-10-15 not to add them yet. Listing type stays stored with the shared dialog.
## Manage Scripts screenshots (2026-10-15, 5 pics)

### "MY SCRIPTS" dialog
- "Search scripts..." box, "+ New Script" button, "No scripts yet. Click + New Script to create one.", Close button.

### Script builder (drag & drop)
- Left: "AVAILABLE ACTIONS" — "Click or drag to add", "Search actions..." box. Actions grouped in categories.
- Right: "SCRIPT NAME" (e.g. Morning Routine), "SCRIPT ACTIONS" with action-count badge ("0 actions"), drop area text "Drop actions anywhere in this area or click from the left panel".
- Cancel / Save Script buttons.

### Action categories in builder (exact)
- **LISTINGS**: Post, Draft & Publish, Create Drafts, Relist & Edit, **Delete & Relist** ← NEW (19th action, not in the 18-action main dropdown), Renew Listing, Publish Drafts
- **DELETE**: Delete Duplicate, Delete Draft, Delete Previous, Delete Take Action Listings (label confirmed, wraps to two lines), Delete All Invalid Listings
- **ACTIVITY**: Scroll Feed, Watch Videos, Watch Reels, Scroll Marketplace, Read Notifications, Scroll Messages
- **OTHER**: Wait For Next Turn

### Per-action settings dialogs (NEW concept)
- Clicking a script action opens its settings, e.g. **"Scroll Feed Settings"**: Min minutes (0), Max minutes (0), Keep-scrolling chance (%) (55) — "How often it just keeps scrolling instead of pausing to read. Realistic: 40–70%. Higher = breezes through the feed; lower = reads more posts.", Like-a-post chance (%) (15) — "Chance to like a post you're looking at during a reading pause. Keep it low 5–20% so it doesn't look spammy (likes are also spaced ~15s apart).", OK button.
→ Each activity action carries its own tunable parameters.

### Renew Listing Settings dialog (screenshot 2026-10-15)
- When Renew Listing is selected as the action + group selected, a **"Renew Listing Settings"** dialog pops: **Min Actions** (0), **Max Actions** (0), OK button.
→ Run form labels must be "Min Actions" / "Max Actions" (not "Min/Max renew").
- NOTE: title bar shows **Expiry Date: Friday, 16 October 2026** — the user's 2-day access window ends tomorrow. Any last screenshots should come today.
1. **Append Title Count** = the product's TITLE is appended at the END of the DESCRIPTION that many times (1 → title appears once at description end; N → N times). Optional (0 = off). [My earlier "cycling number on title" implementation was WRONG — fix to this.]
2. **Browser Launch Fast vs Browser Launch** = NO difference. Both behave identically.
3. **Listing Number Verification** = IGNORE for now — do NOT add it (removed from action list).
4. **Renew Listing** = renews listings that are due for renewal (e.g. 100 listings showing renew available); user sets lower and upper limit — it renews that many. → renew flow needs min/max count settings.
- Script = ordered sequence of blocks, executed top-to-bottom per account: e.g. Scroll Feed (1 min) → Post (N listings) → Watch Reels (2 min) → Watch Videos … — every added block gets performed in turn.
- Activity blocks carry their duration (min/max minutes) + behavior chances (per-action settings dialogs).
- Post block carries a listing count (1, 2, 3 … as many as needed).
- Scripts are OPTIONAL: a simple action can also run directly from the main dropdown without any script. (Matches FMS design: direct actions + scripts side by side.)
1. ~~Main dashboard Script dropdown (v6.2.0.0) me "Delete & Relist" hai ya nahi?~~ ANSWERED 2026-10-15 (see below): YES.
2. ~~1–2 aur activities ki settings~~ RECEIVED: Watch Reels + Scroll Marketplace settings below.

### Main dashboard Script dropdown — v6.2.0.0 EXACT list (screenshot 2026-10-15)
**20 actions**, in this exact order:
1. Post | 2. Relist & Edit | 3. Draft & Publish | 4. Create Drafts | 5. Publish Drafts | 6. **Browser Launch Fast** (NEW) | 7. Browser Launch | 8. Delete Duplicate | 9. Delete Previous | 10. Delete Draft | 11. Delete Take Action Listings | 12. Delete All Invalid Listings | 13. Renew Listing | 14. **Delete & Relist** (confirmed in main dropdown) | 15. Login | 16. Download Images & Titles | 17. Change Marketplace Location | 18. AI Behaviour Training | 19. **Listing Number Verification** (NEW) | 20. Scrap Data
- NOTE: "Wait For Next Turn" is NOT in the v6.2.0.0 main dropdown (was in v6.1.2.0's 18) — it's script-builder-only now (OTHER category). FMS main dropdown must match this 20-action list exactly.
- NEW actions to implement: Browser Launch Fast (quick browser open, minimal checks), Delete & Relist (delete then immediately relist), Listing Number Verification (count/verify listings per account).

### Activity settings dialogs (3 full examples captured)
- **Scroll Feed Settings**: Min minutes (0), Max minutes (0), Keep-scrolling chance % (55) — "How often it just keeps scrolling instead of pausing to read. Realistic: 40–70%. Higher = breezes through the feed; lower = reads more posts.", Like-a-post chance % (15) — "Chance to like a post you're looking at during a reading pause. Keep it low 5–20% so it doesn't look spammy (likes are also spaced ~15s apart)."
- **Watch Reels Settings**: Min minutes (0), Max minutes (0), Skip chance % (40) — "Chance to skip a reel to the next one. Realistic: 30 to 50%.", Like chance % (45) — "Chance to like a reel you watched. Keep it realistic, around 30 to 50%."
- **Scroll Marketplace Settings**: Min minutes (0), Max minutes (0), Keep-scrolling chance % (50) — "How often it just keeps scrolling instead of pausing to look. Realistic: 40–60%. Marketplace scrolling is slower since you're scanning items.", Hover-a-listing chance % (70) — "During a pause, how often it moves the mouse over a listing or two. Realistic: 50–80%.", Open-a-listing chance % (6) — "Chance to open a listing, look for a few seconds, then go back. Keep it low 3–10% (opens are also spaced ~30s apart)."
- **Watch Videos Settings**: Min minutes (0), Max minutes (0), Skip chance % (20) — "Chance to skip a video early and move on. Realistic: 10 to 30%.", Like chance % (30) — "Chance to like a video after watching it. Keep it realistic, around 20 to 40%."
- **Read Notifications Settings**: Min seconds (0), Max seconds (0). No chances.
- **Scroll Messages Settings**: Min seconds (0), Max seconds (0). No chances.
- All 6 activity settings dialogs now captured exactly from screenshots. Engine honors min/max seconds for the two seconds-based blocks.
