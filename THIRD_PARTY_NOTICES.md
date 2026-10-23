# Third-party notices

The original StockDeskPet source code is available under the MIT License in
`LICENSE`. The third-party files listed below are excluded from that project
license and remain subject to their respective upstream licenses or permissions.

## Microsoft Fluent UI Emoji

- Source: https://github.com/microsoft/fluentui-emoji
- License: MIT
- Files used:
  - `assets/ox_3d.png`
  - `assets/bear_3d.png`
  - `assets/chart_increasing_3d.png`
  - `assets/chart_decreasing_3d.png`

The upstream MIT license text is included as `assets/LICENSE-FluentUIEmoji.txt`.

## Microsoft Fluent UI System Icons

- Source: https://github.com/microsoft/fluentui-system-icons
- License: MIT
- Files used:
  - `assets/icons/weather_sunny_20_regular.svg`
  - `assets/icons/weather_moon_20_regular.svg`

The upstream MIT license text is included as `assets/LICENSE-FluentUISystemIcons.txt`.

## Futu OpenAPI Python SDK

- Source: https://github.com/FutunnOpen/py-futu-api
- Package: `futu-api==10.9.6908`
- License: Apache License 2.0

The SDK is bundled with the Windows application to connect to a separately installed
Futu OpenD process on the user's own computer. Futu OpenD itself is not included in
this repository or release package. Market-data availability remains subject to the
user's Futu account permissions, subscription quota, and Futu terms.

The upstream Apache 2.0 license text is included as `assets/LICENSE-FutuAPI.txt`.

## CodexPet.xyz community skins

- Ella Wave by sehjk: https://codexpet.xyz/pets/community/ella-wave/
- GUGUGAGA by Hibik1: https://codexpet.xyz/pets/community/endminguga/
- ikunchick by LarryNigi: https://codexpet.xyz/pets/community/ikunchick/
- Maid-DeepSeek-Whale by DeaDumBM: https://codexpet.xyz/pets/community/maid-deepseek-whale/
- DeepSeek Whale by NIKO: https://codexpet.xyz/pets/community/deepseek/
- Files: `assets/skins/ella-wave.webp`, `assets/skins/endminguga.webp`,
  `assets/skins/ikunchick.webp`, `assets/skins/maid-deepseek-whale.webp`,
  `assets/skins/deepseek.webp`

These spritesheets are included for this user's local build with developer permission
reported by the user. See `assets/skins/README.md` for hashes and source details. Recheck
upstream terms before redistributing them.

## DeepSeek Pet expression assets

- Source: https://github.com/keleus/deepseek-pet
- Author: keleus
- Upstream commit: `b9b61a35c3b2443ecdeab0ed442d6b33c603d343`
- License: MIT
- Files: `assets/skins/deepseek-expressions/*.webp`, copied unchanged from
  upstream `src/client/assets/`.

The upstream MIT license and copyright notice are included as
`assets/LICENSE-DeepSeekPet.txt`. The native expression renderer is inspired by
the upstream whole-image reactions, time-based rotation and lightweight motion;
the DeepSeek Harness plugin and its web runtime are not bundled.

## DSH Pet animated assets

- Source: https://github.com/PC2005-cloud/dsh-pet
- Author: PC2005-cloud
- Upstream commit: `48007b64ac0bebb2e97ac88344aed62de8399dd4`
- License: MIT, copyright (c) 2026 PC2005-cloud.
- Files: all 106 original GIFs from upstream `dsh-pet/assets/preview/`, copied
  unchanged into `assets/skins/dsh-maid/`. The action labels and categories are
  derived from the upstream configuration and preview filename mapping.

The full upstream license is included as `assets/LICENSE-DSHPet.txt`.
`manifest.json` records the upstream revision and SHA-256 of every imported GIF.
The independent Qt player borrows the categorized action menu and idle/action
chain design; no upstream plugin runtime, AI API, Electron or credentials are used.
