# M6 upstream-data terms check

Checked: 2026-07-26 (Asia/Shanghai)

Scope: the upstream posture required by DATA-5 and SEC-3. This is a
mechanical release check, not legal advice.

## Sources re-read

1. [Binance Public Data](https://github.com/binance/binance-public-data/blob/master/README.md)
   says that `data.binance.vision` offers public market data as daily and
   monthly downloads, documents programmatic download examples, and requires
   each archive to be checked against its sibling `.CHECKSUM`.
2. [Binance Data Collection](https://data.binance.vision/) remains the bulk
   archive host used by the catalog.
3. [Binance Terms of Use](https://www.binance.com/en/terms), whose document
   served during this check was effective 2026-07-21, retains Binance
   intellectual property, limits the granted Binance-IP licence, prohibits
   infringement of Binance or third-party intellectual-property rights, and
   disclaims the accuracy and currency of platform content.

## Repository posture verified

- TradeEvolve fetches only from the HTTPS `data.binance.vision` bulk archive
  host.
- Every required archive is bound to and checked against its sibling
  `.CHECKSUM`; there is no skip-verification option.
- The tracked tree and Python distributions contain pack manifests, source
  URLs, checksums, and build code, but no fetched archives or derived market
  series.
- The Apache-2.0 project licence applies only to project-authored code and
  documentation. It does not purport to relicense Binance market data.
- Binance references identify data provenance and simulated venue mechanics;
  the README and pack catalog explicitly disclaim affiliation, sponsorship,
  and endorsement.

## Disposition

No material change was found that invalidates the founder-accepted
fetch-don't-redistribute posture. DATA-5 and SEC-3 remain conditioned on the
tracked-tree and built-distribution scans passing immediately before any
release. If the data host, public-data documentation, applicable terms, or
project distribution posture changes, this check must be repeated.
