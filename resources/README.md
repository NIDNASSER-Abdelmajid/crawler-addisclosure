# resources/

This folder stores resource files managed at runtime.

## easylist_selectors.json

Populated and updated by `Helpers/easylist_updater.py`.  Contains the merged
CSS selector list used by AdCollector to detect ad elements.

To fetch the latest rules and update this file:

```
python Helpers/easylist_updater.py
```

Schema:
```json
{
  "last_updated": "ISO-8601 timestamp",
  "source": "URL the rules were fetched from",
  "selectors": ["#ad-banner", ".ad-slot", ...]
}
```

Until this file is created, AdCollector falls back to the built-in selector
list in `Helpers/easylist_selectors.py`.
