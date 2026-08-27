# Rename map from the Resume-Matcher fork to roleradar.
#
# Applied to every ported file. Kept as a script rather than done by hand so the
# test port produces failures that mean something instead of naming noise, and
# so a later re-port of a module lands identically.
#
# Usage: sed -i '' -f scripts/port_renames.sed <files>

# --- module paths ----------------------------------------------------------
s|app\.services\.scout\.sources|roleradar.scout.sources|g
s|app\.services\.scout\.notify|roleradar.notify.digest|g
s|app\.services\.scout|roleradar.scout|g
s|app\.services\.skill_aliases|roleradar.scout.skill_aliases|g
s|app\.config|roleradar.settings|g
s|app\.models|roleradar.models|g
s|from app import database|from roleradar import db as database|g
s|app\.database|roleradar.db|g

# --- ORM classes and tables ------------------------------------------------
s|\bScoutListing\b|Listing|g
s|\bScoutSource\b|Source|g
s|"scout_listings"|"listings"|g
s|"scout_sources"|"sources"|g
s|ix_scout_listings_|ix_listings_|g

# --- database facade methods ----------------------------------------------
# Order matters: the by_external / by_fingerprint variants must be rewritten
# before the bare get_scout_listing, or their suffixes get orphaned.
s|get_scout_listing_by_external|get_listing_by_external|g
s|get_scout_listing_by_fingerprint|get_listing_by_fingerprint|g
s|create_scout_listing|create_listing|g
s|update_scout_listing|update_listing|g
s|list_scout_listings|list_listings|g
s|count_scout_listings|count_listings|g
s|mark_scout_listings_inactive|mark_listings_inactive|g
s|get_scout_listing|get_listing|g
s|upsert_scout_source|upsert_source|g
s|list_scout_sources|list_sources|g
s|get_scout_source|get_source|g

# --- dict converters -------------------------------------------------------
s|_scout_listing_to_dict|_listing_to_dict|g
s|_scout_source_to_dict|_source_to_dict|g
