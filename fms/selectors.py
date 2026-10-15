"""Facebook Marketplace selectors — ALL in one place.

Facebook changes its DOM often. When a step breaks, update the selector HERE;
no other module hard-codes Marketplace selectors.

Conventions:
- Prefer robust selectors: aria-labels, data-testid, placeholder text, role names.
- Each constant is a (strategy, value) tuple for Playwright: ("css"|"xpath"|"text"|"label"|"placeholder"|"role", value).
- URLs are absolute.
"""
from __future__ import annotations

# ------------------------------------------------------------------ URLs ---
LOGIN_URL = "https://www.facebook.com/login"
HOME_URL = "https://www.facebook.com/"
MARKETPLACE_URL = "https://www.facebook.com/marketplace/"
MKT_CREATE_ITEM_URL = "https://www.facebook.com/marketplace/create/item/"
MKT_YOUR_LISTINGS_URL = "https://www.facebook.com/marketplace/you/selling/"

# ------------------------------------------------------------------ login ---
# Facebook login page — Facebook serves several page variants, so each field
# has fallback selectors tried in order (first match wins).
LOGIN_EMAIL = ("css", "#email")
LOGIN_EMAIL_FALLBACKS = [
    ("css", "#email"),
    ("css", "input[name='email']"),
    ("css", "[data-testid='royal_email']"),
    ("css", "input[type='text'][autocomplete='username']"),
    ("placeholder", "Email address or phone number"),
    ("placeholder", "Email or phone number"),
]
LOGIN_PASS = ("css", "#pass")
LOGIN_PASS_FALLBACKS = [
    ("css", "#pass"),
    ("css", "input[name='pass']"),
    ("css", "[data-testid='royal_pass']"),
    ("css", "input[type='password']"),
    ("placeholder", "Password"),
]
LOGIN_BUTTON = ("css", "button[name='login']")
LOGIN_BUTTON_FALLBACKS = [
    ("css", "button[name='login']"),
    ("css", "[data-testid='royal_login_button']"),
    ("css", "button[type='submit']"),
    ("text", "Log in"),
    ("text", "Log In"),
]
# Logged-in indicator (profile link / nav)
LOGGED_IN_INDICATOR = ("css", "a[aria-label='Your profile'], a[aria-label*='Profile']")
# 2FA / checkpoint prompts
TWOFA_CODE_INPUT = ("css", "input[name='approvals_code'], input[aria-label*='code' i]")
TWOFA_SUBMIT = ("css", "button[type='submit']")
CHECKPOINT_CONTINUE = ("text", "Continue")

# ------------------------------------------------- create-listing form ---
# "Create new listing" -> "Item for sale" entry points (fallbacks in order)
CREATE_LISTING_BUTTON = ("css", "a[href='/marketplace/create/']")
ITEM_FOR_SALE_OPTION = ("text", "Item for sale")

# Form fields on /marketplace/create/item/
F_TITLE = ("css", "input[aria-label*='Title' i], input[placeholder*='Title' i]")
F_PRICE = ("css", "input[aria-label*='Price' i], input[placeholder*='Price' i]")
F_CATEGORY = ("css", "input[aria-label*='Category' i], [role='combobox'][aria-label*='Category' i]")
F_CONDITION_NEW = ("text", "New")
F_CONDITION_USED_LIKE_NEW = ("text", "Used - like new")
F_DESCRIPTION = ("css", "textarea[aria-label*='Description' i]")
F_AVAILABILITY = ("css", "[aria-label*='Availability' i]")
F_LOCATION = ("css", "input[aria-label*='Location' i], input[placeholder*='Location' i]")
F_LOCATION_FIRST_RESULT = ("css", "[role='listbox'] [role='option']:first-child")
F_TAGS = ("css", "input[aria-label*='tag' i], input[placeholder*='tag' i]")

# Photos / video
F_ADD_PHOTOS = ("css", "input[type='file'][accept*='image']")
F_ADD_VIDEO = ("css", "input[type='file'][accept*='video']")

# Publish
F_PUBLISH_BUTTON = ("css", "button[type='submit'], div[aria-label='Publish'][role='button']")
F_NEXT_BUTTON = ("text", "Next")
F_DRAFT_SAVED_INDICATOR = ("text", "Your listing has been saved")

# Listing visibility (matches the real tool's "Hide From Friends" setting)
F_HIDE_FROM_FRIENDS = ("css", "input[aria-label*='Hide from friends' i], "
                              "[aria-label*='Hide listing from friends' i]")

# Delivery options (real tool's product dialog checkboxes; best-effort text match)
F_DELIVERY_MEETUP = ("text", "Public meetup")
F_DELIVERY_PICKUP = ("text", "Door pickup")
F_DELIVERY_DROPOFF = ("text", "Door drop-off")

# ------------------------------------------------- delete / renew ---
# Best-effort: deletion/renewal UI drifts; update here when it breaks.
F_DELETE_LISTING_BUTTON = ("css", "div[aria-label='Delete'][role='button'], "
                                  "div[aria-label*='Delete listing' i][role='button']")
F_CONFIRM_DELETE_BUTTON = ("css", "div[aria-label='Delete'][role='button'], "
                                  "button[type='submit']")
F_RENEW_BUTTON = ("css", "div[aria-label*='Renew' i][role='button'], "
                         "span:has-text('Renew listing')")

# ------------------------------------------------- marketplace location ---
# Change the Marketplace location shown for the account (best-effort).
MKT_LOCATION_BUTTON = ("css", "[aria-label*='location' i][role='button'], "
                              "a[href*='/marketplace/'][aria-label*='ocation' i]")
MKT_LOCATION_INPUT = ("css", "input[aria-label*='Location' i], "
                             "input[placeholder*='Location' i]")

# ------------------------------------------------- "take action" flags ---
# Listings Facebook flags with "take action" (identity/phone verification)
# show a matching badge/banner on the selling page (best-effort).
F_TAKE_ACTION_BADGE = ("css", "[aria-label*='take action' i], "
                              "span:has-text('Take action')")

# ------------------------------------------------- draft & publish ---
# Best-effort: Facebook's draft UI drifts; update here when it breaks.
F_SAVE_DRAFT_BUTTON = ("css", "div[aria-label='Save draft'][role='button'], "
                              "button[aria-label*='Save draft' i]")
MKT_DRAFTS_URL = "https://www.facebook.com/marketplace/you/drafts/"
F_PUBLISH_DRAFT_BUTTON = ("css", "div[aria-label='Publish'][role='button'], "
                                 "button[type='submit']")
F_DISCARD_DRAFT_BUTTON = ("css", "div[aria-label*='Discard' i][role='button']")

# ------------------------------------------------- edit existing ---
# Relist/Edit: rewrite an existing (sold/out-of-stock) listing instead of
# creating a new one — bypasses new-listing caps on limited accounts.
def MKT_EDIT_URL(listing_id: str) -> str:
    return f"https://www.facebook.com/marketplace/edit/?listing_id={listing_id}"

SCR_MY_LISTING_LINKS = ("css", "a[href*='/marketplace/item/']")
F_SAVE_EDIT_BUTTON = ("css", "div[aria-label='Save'][role='button'], "
                             "button[type='submit']")

# ------------------------------------------------- scrap (read-only) ---
# Best-effort selectors for extracting a listing's public data.
SCR_TITLE = ("css", "h1")
SCR_PRICE = ("css", "span[dir='auto']")
SCR_DESCRIPTION = ("css", "div[data-testid='marketplace_pdp_description'] span, "
                           "div[dir='auto'] span")
SCR_PHOTOS = ("css", "img[src*='scontent']")
SCR_LOCATION = ("css", "span:has-text('Listed'), div[dir='auto']")
SCR_AVAILABILITY = ("text", "In stock")

# ------------------------------------------------------- misc / safety ---
# Signals that the account hit a wall (checked after actions)
SEL_LIMIT_REACHED = ("text", "You've reached the limit")
SEL_CONFIRM_IDENTITY = ("text", "Confirm your identity")
SEL_SOMETHING_WRONG = ("text", "Something's wrong")


def to_playwright(page_or_locator, selector: tuple[str, str]):
    """Convert a (strategy, value) tuple to a Playwright locator/query."""
    strategy, value = selector
    if strategy == "css":
        return page_or_locator.locator(value).first
    if strategy == "xpath":
        return page_or_locator.locator(f"xpath={value}").first
    if strategy == "text":
        return page_or_locator.get_by_text(value, exact=False).first
    if strategy == "label":
        return page_or_locator.get_by_label(value).first
    if strategy == "placeholder":
        return page_or_locator.get_by_placeholder(value).first
    if strategy == "role":
        return page_or_locator.get_by_role(value).first
    raise ValueError(f"Unknown selector strategy: {strategy}")
