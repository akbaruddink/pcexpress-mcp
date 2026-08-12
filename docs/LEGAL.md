# Legal

**This is not legal advice.** It's a plain-language explanation of this
project's posture, written for transparency. If you plan to operate, fork,
or rely on this software at any real scale, talk to a qualified lawyer in
your own jurisdiction — nothing here substitutes for that, and nothing
here is a guarantee against being sued, having an account actioned, or any
other consequence of using an unofficial client for a service that hasn't
authorized it.

## No affiliation

This project is **not affiliated with, endorsed by, sponsored by, or
officially connected with Loblaw Companies Limited**, Loblaws Inc., or any
of their banners or brands -- Real Canadian Superstore, Loblaws, No
Frills, Zehrs, Your Independent Grocer, T&T Supermarket, President's
Choice, PC Optimum, or PC Express. All product names, logos, trademarks,
and registered trademarks referenced in this repository are the property
of their respective owners. They're used here only *nominatively* -- to
accurately describe what this software talks to and is compatible with --
never to imply sponsorship, partnership, or official status.

## What this project actually does

- Talks to HTTP endpoints already used by Loblaw's own official Android
  app and website, using the same OAuth app identity that ships inside
  every install of the real app (see
  [docs/ARCHITECTURE.md](ARCHITECTURE.md#why-the-oauth-client-idsecret-are-baked-into-configpy)
  for exactly where that value came from -- published by prior, independent
  reverse-engineering work, not extracted here, and not a secret this
  project itself obtained through any confidential or unauthorized means).
- Requires a real human to complete the PC ID login in a real browser
  every time a new credential is needed. This project does not attempt to
  automate, script, or bypass that login, its bot-detection (Akamai), or
  any CAPTCHA.
- Only ever acts on the PC Express account whose owner personally
  completed that login -- see
  [docs/SECURITY.md](SECURITY.md#threat-model-summary) for exactly how
  that's enforced in HTTP/multi-tenant mode.
- Runs entirely on infrastructure the user themselves controls (their own
  laptop, or their own self-hosted server -- see the README's
  "Self-hosting with Docker" and "One-click deploy" sections). The project
  maintainers do not operate a shared instance, do not receive, store, or
  have access to any user's PC Express credentials, search history, or
  cart contents.

## What this project deliberately does not do

- **Does not submit orders or payment.** `place_order` validates a cart
  and hands off a checkout link for the user to finish themselves, in the
  official app or a browser, on an account they're already logged into --
  see [docs/ARCHITECTURE.md](ARCHITECTURE.md#place_order-never-submits-payment)
  for why. No payment-submission endpoint is implemented, called, or even
  known to this project.
- **Does not scrape web pages.** Every request goes to the same
  application-layer API endpoints the official app itself calls, not to
  HTML pages meant for browsers -- see
  [docs/RESEARCH.md](RESEARCH.md) for the endpoint-by-endpoint sourcing.
- **Does not bypass bot detection or rate limits.** No CAPTCHA solving, no
  headless-browser automation of the login, no request-pattern spoofing
  beyond what's needed to make a normal, low-volume API call -- this is
  scoped to personal, low-volume use on your own account (see the
  README's opening description and
  ["Limitations & risks"](../README.md#limitations--risks)), not
  high-throughput or commercial use.
- **Does not redistribute Loblaw's catalog data.** Product info, images,
  and prices are fetched live, per query, to answer the requesting user's
  own question in the moment -- this project does not operate a cache,
  mirror, or public database of that data. (The anonymized snapshots in
  `tests/fixtures/raw_responses/` are a handful of structural examples for
  regression testing, not a catalog dump -- see that directory's README.)

## Terms of Service

Using an unofficial, reverse-engineered client to access a retailer's
ordering platform is very likely outside what Loblaws' website/app Terms
of Use contemplate as acceptable use, even for personal, low-volume use on
your own account -- see the README's
["Limitations & risks"](../README.md#limitations--risks). **It is each
user's own responsibility** to review and decide whether their use
complies with those terms. Running this software is a choice made by the
person running it, on their own account, at their own risk -- the
maintainers make no representation that any particular use complies with
Loblaw's terms and are not responsible for consequences (rate-limiting,
CAPTCHA challenges, account suspension, or any other action Loblaw might
take) that follow from a user's decision to use it.

## Third-party data: Open Food Facts

The optional `get_nutrition_info` tool queries
[Open Food Facts](https://openfoodfacts.org), a free, independent,
community-maintained product database, wholly separate from PC Express/
Loblaw and from this project's own data. Their data is licensed under the
[Open Database License (ODbL)](https://opendatacommons.org/licenses/odbl/1-0/),
which has its own attribution requirements for anyone redistributing that
data (not just querying it live, as this tool does) -- if you build
something that stores, republishes, or redistributes Open Food Facts data
beyond passing a live query's result back to the person who asked, review
the ODbL's attribution terms yourself; this project's own use here (a
live, on-demand, single-product lookup relayed directly to the requester)
does not attempt to speak for what a different downstream use would
require. See [docs/RESEARCH.md](RESEARCH.md#nutrition-enrichment-open-food-facts)
for the real rate limits and usage guidance this project follows.

## No warranty, no liability

This software is licensed under the [MIT License](../LICENSE), which
already states this in the legally operative form, but in plain language:
it is provided **as-is, with no warranty of any kind**, and to the maximum
extent permitted by law, the authors and contributors are not liable for
any damages or claims -- direct, indirect, or otherwise -- arising from
its use. That includes, without limitation, account actions taken by
Loblaw, data loss, financial loss, or third-party claims of any kind.

## If you represent Loblaw Companies Limited

If you're a representative of Loblaw Companies Limited or any of the
banners/brands referenced above and have a concern about this project --
a specific piece of content, a specific behavior, anything -- please open
a [GitHub issue](../../issues) or reach the maintainer through their
GitHub profile. Good-faith concerns will be taken seriously and acted on;
this project has no interest in being adversarial toward the platform it
depends on.
