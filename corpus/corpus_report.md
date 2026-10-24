# Corpus report

Built 2026-10-24 from Bungie manifest version 245114.26.10.13.2000-1-bnet.66137.

| Target | Goal | Actual | Met |
|---|---|---|---|
| Documents after dedup | 50+ | 3,411 | yes |
| Tokens | about 250k | 1,318,375 | yes |
| Sources | 3+ | 3 | yes |
| Formats | 2+ | 3 (csv, html, json) | yes |
| Distractors (share of documents) | about 15% | 1.0% | no |
| Distractors (share of tokens) | about 15% | 1.2% | no |
| License note per document | all | 100% | yes |

## By source

| source | format | role | documents | tokens | median_words |
|---|---|---|---|---|---|
| bungie_api_d1_grimoire | csv | lore | 796 | 154,830 | 82 |
| bungie_api_d2 | json | lore | 2,526 | 1,071,072 | 266 |
| destiny_fandom_wiki | html | distractor | 34 | 15,863 | 202 |
| destiny_fandom_wiki | html | lore | 55 | 76,610 | 367 |

## Dropped during cleaning

- Destiny 2: {'empty': 552, 'redacted': 15, 'too_short': 27, 'duplicate': 394}
- Destiny 1: {'too_short': 26, 'duplicate': 0}
- Cross-source exact duplicates: 0
- Wiki titles missing or too short: ['Dominus Ghaul', 'Nezarec (too short after cleaning)', 'Worm gods', 'Pyramid', 'Last City', 'Sword logic', 'Combat Bow', 'Power Level', 'Mod', 'Bright Dust', 'Seasonal Artifact', 'Triumph', 'Seal', 'Transmog', 'Finisher']
