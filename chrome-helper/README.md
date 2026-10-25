# Private LEGO Nova cart helper

This private extension works only with https://lego-nova.vercel.app and https://www.lego.com/en-us/.
It uses your existing LEGO session inside Chrome. It never exports cookies, purchases parts, substitutes parts, or checks out.

## Install once

1. Download this package from **Review parts and cost** in the private app. Unzip it.
2. Open `chrome://extensions` in Chrome. Enable **Developer mode**.
3. Select **Load unpacked**. Choose the extracted `lego-nova-helper` folder.
4. Reload the LEGO Nova app. The **Add to LEGO bag** button becomes available after reviewing fresh prices.

Chrome requires this local installation for an unpublished extension. The site cannot silently install it.

## Fill a reviewed list

Open a model's **Review parts and cost**. Review the refreshed prices and unresolved pieces.
If required, explicitly choose **Add only verified, available pieces**. Select **Add to LEGO bag**.
The helper opens Pick a Brick if needed. Sign in there, then select **Resume request** in the helper.
Keep the app tab open. A request expires after ten minutes; a new review is required afterward.

The helper adds only exact element IDs. It preserves existing quantities and sets, then transfers verified parts groups to the main bag.
It splits large lists into at most 200 distinct pieces per LEGO delivery group, with no bulk call exceeding 400.
The result reports requested, added, missing, and confirmed bag quantities.

A journal in Chrome storage prevents duplicate additions after retries. Only one request can run at a time.
If an add response is lost, the helper reads the cart before resuming. It never blindly repeats a bag transfer.
If a transfer response is lost, review the LEGO bag yourself. Select **I reviewed the previous bag** to release that request.
This release action does not change the cart. Refresh and review a new list before another addition.

The CSV fallback contains `elementId,quantity`, split into 400-piece files. Import it through LEGO Pick a Brick's upload control.

## Verification status

The network adapter follows LEGO's publicly shipped website operations. Automated tests use a simulated shop.
Live additions are intentionally untested until Christian separately approves a named model and reviewed quantities.
The extension stops if LEGO changes its API, price, stock, quantity limit, or cart shape.
