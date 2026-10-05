# Grocy food labels on the Brother QL-1110NWB

Version 0.16.1 adds label rendering to the existing printer add-on. Grocy can run
in Docker on your Ubuntu/Proxmox host and call the add-on over the LAN. Fridge
Assistant continues to use the existing `/print` endpoint. Both use the same
Brother network driver and loaded-roll settings.

The design takes inspiration from your Fridge Assistant label: a location
banner, prominent product name and product ID, scan code, stored date, date panel
and optional contents/quantity. A QR code encodes the **exact incoming Grocycode**,
including its stock-entry suffix. The displayed product ID is a reading aid;
it is not a substitute for the stock-specific scan code.

## Install from the Grocy edition branch

Add this URL in Home Assistant's add-on store repository settings:

```text
https://github.com/adec/label-printer-addon#grocy-labels
```

Install **Label Printer — Grocy Edition**. The branch-specific repository is a
separate add-on installation from the original/default-branch copy. Copy your
existing printer options and stop the old copy before starting the Grocy edition,
as both expose port 8000. Both Fridge Assistant and Grocy can then use this single
instance. The previous add-on remains available if you want to revert.

Future Grocy edition updates are published on `grocy-labels`, with the version
in `label_printer/config.yaml` increased. The upstream Brother contribution and
`main` branch remain separate from these Grocy-specific enhancements.

For your currently loaded continuous roll, use:

```yaml
brother_host: "YOUR_PRINTER_IP"
brother_port: 9100
brother_label: "auto"
brother_length_mm: 100
brother_cut: true
brother_size_mismatch: "reject"
```

`auto` uses the existing Brother roll detection, including its HTTP fallback.
If detection is unavailable, select `"62"` manually and change the configured
roll whenever you physically swap media. The 62 mm continuous roll has a
696-pixel printable width at 300 dpi. A configured 100 mm cut produces a
696 × 1181 pixel canvas. Continuous cut length remains a configuration choice.

Pre-cut rectangular rolls use their detected native dimensions. Short landscape
labels rotate the layout to fit the roll. Very small labels or dense scan codes
return an error rather than printing unreadable output. The prepared examples
include 62 × 100, 62 × 29 and 29 × 90 mm media; confirm your exact DK roll with a
preview and test print. Optional contents are omitted when space is insufficient.

## Configure Docker Grocy

Edit Grocy's persistent `data/config.php` through the volume mounted into your
container. The host path depends on your Compose configuration. Set:

```php
Setting('FEATURE_FLAG_LABEL_PRINTER', true);
Setting('LABEL_PRINTER_WEBHOOK', 'http://HOME_ASSISTANT_LAN_IP:8000/grocy/print');
Setting('LABEL_PRINTER_RUN_SERVER', true);
Setting('LABEL_PRINTER_HOOK_JSON', true);
Setting('LABEL_PRINTER_PARAMS', []);
```

Replace `HOME_ASSISTANT_LAN_IP` with the address reachable from your Ubuntu
Docker host/container. `localhost` inside the Grocy container refers to Grocy
itself. Use the published LAN port, not a Home Assistant ingress URL. Keep the
API on your trusted LAN, as the existing printer API has no authentication.

Restart/reload Grocy as required by your container setup, then use its stock
label printing actions. Enable Grocy's best-before date tracking if you want
it to supply a due date. Existing Grocy versions can also use
`LABEL_PRINTER_HOOK_JSON=false`; this implementation accepts both formats.

Current Grocy server-side stock label payloads include `details` and
`stock_entry`. These provide the purchased date and stock-entry amount/unit.
Older/minimal requests still print the name, code and supplied date. Missing
dates and quantities are left out. A date value of `2999-12-31` becomes
"No expiry date", matching Grocy's no-expiry sentinel.

## Product categories and date types

The category chip now uses Grocy's **product group name**. A product without a
product group has no chip; the old static `category: DISHES` parameter is ignored.

Grocy keeps the date meaning on `details.product.due_type`: `1` prints
**BEST BEFORE**, `2` prints **EXPIRY DATE**. The underlying stock date field is
called `best_before_date` for both types, so its name alone does not determine
which heading to use. Unknown date types print the neutral **DUE DATE** heading.
Static `date_label` parameters no longer override the product's date meaning.

Set these options in the printer add-on Configuration, save and restart:

```yaml
grocy_url: "http://YOUR_UBUNTU_DOCKERHOST:GROCY_PUBLISHED_PORT"
grocy_api_key: "YOUR_GROCY_API_KEY"
```

Create an API key in Grocy's API keys settings. The base URL must be reachable
from Home Assistant and can include a subpath, such as `/grocy`. This is the
Grocy service address, not the printer or Home Assistant address. Both fields
are optional, but both are required when enabling API enrichment.

Current server-side stock print webhooks normally supply product metadata.
The adapter fetches `/api/stock/products/{id}` when the date type or group ID is
missing, and resolves the group name through
`/api/objects/product_groups/{id}`. If a stock entry's actual location differs
from the product default, it also resolves `/api/objects/locations/{id}`.
Without API configuration, provided metadata is used and missing categories
are omitted. No stock-entry quantity or date is guessed from aggregate product
stock, and the exact incoming Grocycode is retained.

API requests use `GROCY-API-KEY`, have bounded timeouts and are not cached or
retried. Failed configured lookups return HTTP 502 and prevent printing;
credentials are not returned in error details. Redirects are refused so the
API key is not forwarded to another URL.

For stock labels the incoming `due_date` remains authoritative, followed by
`stock_entry.best_before_date` if no explicit date was supplied. Product lookup
results such as `next_due_date` never replace the date for this specific batch.

You can still set cosmetic options in Grocy, for example:

```php
Setting('LABEL_PRINTER_PARAMS', ['quantity_label' => 'SERVINGS']);
```

Contents are not inferred from the product name. Portion identifiers such as
`BA16-1` are not generated automatically: Grocy's stock identity stays intact.

## Preview before printing

`GET /grocy/image` and `POST /grocy/image` return a PNG without sending a job.
They use the currently configured/detected roll. This example supplies sample
details, not data read from your Grocy database:

```sh
curl --fail-with-body http://HOME_ASSISTANT_LAN_IP:8000/grocy/image \
  -H 'Content-Type: application/json' \
  -d '{"product":"Chicken Curry","grocycode":"grcy:p:42:stock123","due_date":"DD: 2026-12-12","stored_date":"2026-09-05","location":"Freezer","details":{"product":{"id":"42","due_type":1,"product_group_id":null}},"contents":"Chicken curry with rice","quantity":"4 portions","quantity_label":"Servings"}' \
  -o grocy-preview.png
```

To print that sample deliberately, change the endpoint to `/grocy/print` and
remove `-o grocy-preview.png`. It will print a sample code, not a real stock entry.
For an integration test, use an actual Grocy print action instead.

Fields accepted: `product` (or `battery`, `chore`, `recipe`), `grocycode`,
`due_date`, `details`, `stock_entry`, and optional `location`,
`stored_date`, `contents`, `quantity`, `quantity_label`,
`display_code`, `copies` (1–10). JSON, plain form fields, JSON-encoded nested
objects and PHP bracket-encoded nested form objects are supported.

`display_code` changes only the human-readable identifier; scanning always
returns the original `grocycode`. Do not assume that a display alias works in
Grocy's manual code entry.

## Results and verification

Successful `/grocy/print` responses preserve the existing transport response:
`submitted: true` means raster data reached the printer socket; it does not
confirm a physical label emerged. Validation failures return 422; unavailable
or unconfigured printers return 503; failed Grocy enrichment returns 502. A roll swap can reject the request at
transport preflight. There are no automatic retries, because a partial network
send might already have printed a label. Grocy jobs appear in the existing print
journal with source `grocy`.

The tests decode the rendered QR payload, exercise form/JSON requests, verify
previews do not print, and send real Brother raster data to a loopback TCP
receiver. Physical printing and the Home Assistant container build remain to
be verified for this new label feature.
