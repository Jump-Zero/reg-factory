# Upstream

This directory vendors the MIT-licensed project `lij768423-svg/zkky` successor implementation from `alexan0618/zkky`.

- Repository: https://github.com/alexan0618/zkky
- Commit: `153c4ba159b0923d97ba84753305783f408150aa`
- Reviewed: 2026-08-05

Local integration changes keep the service on loopback, connect the registration queue, move runtime data outside source files, inherit the parent ChatGPT egress, and add local batch AT/card handling.

The checkout-link request sequence and identifier preference were also reviewed against the MIT-licensed `shi-YangYang/plus-extractor` implementation.

- Repository: https://github.com/shi-YangYang/plus-extractor
- Commit: `0483f9e6099bdac64c9fb3bbb2b750d38333dd5a`
- Reviewed: 2026-08-05

The local Python adapter creates a promotion-free baseline Checkout, applies the promotion to that existing session, and prefers a nested `oaics_*` identifier over a provider `cs_*` identifier.
