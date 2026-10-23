# Local desktop-pet skins

These Codex Pet V2 spritesheets are used only by this local StockDeskPet build.

| Skin | Community page | SHA-256 |
| --- | --- | --- |
| Ella Wave | https://codexpet.xyz/pets/community/ella-wave/ | `C4653393786DA7CD15413FBA07628C62DE84DBD1994C24DFA6A5B11274824081` |
| GUGUGAGA | https://codexpet.xyz/pets/community/endminguga/ | `BB2C9152EC5809362E36A507D4855F0963FAFB6EEC0C8B91B908A6A0AEE2D9FE` |
| ikunchick by LarryNigi | https://codexpet.xyz/pets/community/ikunchick/ | `3DE0B3A5BEB84A081F0CCC7438381A153C8C592EB49A9C941CEF174A91B32467` |
| Maid-DeepSeek-Whale by DeaDumBM | https://codexpet.xyz/pets/community/maid-deepseek-whale/ | `DB33B20086A14867D4D4595D4F9BFD76B2479ABBB5D712BBB51A2540483C1130` |
| DeepSeek Whale by NIKO | https://codexpet.xyz/pets/community/deepseek/ | `28B90D92B7F619C9C4C9D07DC271275F77CF38B732396CB1F868FDDE08EF98AD` |

These downloads contain an 8 x 9 WebP spritesheet with 192 x 208 pixel cells. Keep the
creator credit and upstream source with any copy. The user confirmed developer permission
for personal use; these files should not be redistributed as part of a public release
without checking the upstream terms again.

The desktop runtime maps the standard atlas rows to interactions as follows: row 0 for
idle, rows 1/2 for right/left drag, row 3 for click/wave, and row 7 for quote refresh.

## DeepSeek expression skin

- Display name: DeepSeek 女仆 · 表情版; skin key: `deepseek-expressions`.
- Source: https://github.com/keleus/deepseek-pet
- Upstream commit: `b9b61a35c3b2443ecdeab0ed442d6b33c603d343`.
- Files: all 30 original WebP files from `src/client/assets/`, copied unchanged into
  `assets/skins/deepseek-expressions/` (including the upstream auxiliary decoration).
- License: MIT, copyright (c) 2026 keleus; the full notice is included in
  `assets/LICENSE-DeepSeekPet.txt`. This section is separate from the CodexPet
  community-skin permissions above.

The native renderer uses whole transparent images, cached at display size, with
subtle breathing, click bounce and directional drag tilt. Idle expressions rotate
every 30 seconds of accumulated idle time; uninterrupted idle reaches hungry at
10 minutes, pillow at 30 minutes and sleeping at 60 minutes. Clicking or dragging
wakes the character. Manual refresh uses the work/thinking images and companion
keypress frames. Background quote refresh never resets the idle state.

## DSH animated maid skin

- Display name: DSH 蓝毛女仆 · 动画版; key: `dsh-maid`.
- Source: https://github.com/PC2005-cloud/dsh-pet
- Commit: `48007b64ac0bebb2e97ac88344aed62de8399dd4`.
- Assets: 106 unchanged, transparent 220 x 124, 12 fps GIF previews (about 64 MiB).
- License: MIT; full notice in `assets/LICENSE-DSHPet.txt`.
- Import tool: `scripts/import_dsh_assets.py <upstream-checkout>`; never executes
  upstream code. Source hashes, revision, categories and action pools are in
  `dsh-maid/manifest.json`.

One skin represents one character; the 106 actions are grouped in the context
menu rather than exposed as separate skins. The player streams only the current
GIF using Qt, preserves its transparent full-width canvas, pauses while hidden,
and returns from a one-shot action to the current idle/manual-refresh state.
Random idle actions are optional. Coin-pouch and work-state animations in the
menu are visual-only and do not represent account balance or market direction.

Clicks randomly select from the five upstream click-response candidates, retaining
the last successful click across idle/refresh/drag transitions to prevent immediate
repetition. A single candidate remains repeatable; an empty/invalid pool or missing
candidate asset falls back to the default wave (or idle if no default is defined).
