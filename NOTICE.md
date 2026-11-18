# Licensing and upstream projects

The bridge, standalone plugin, documentation, and tests are provided under the
MIT license in `LICENSE`, except where a file explicitly declares otherwise.

`host-patch/component_query.patch` targets
[MaiBot](https://github.com/Mai-with-u/MaiBot), whose source is licensed under
GNU GPL version 3. The patch and `scripts/patch_host.py` (which contains the
matching upstream context) are GPL-3.0-only; the license text is included at
`licenses/GPL-3.0.txt`. Applying the patch does not relicense MaiBot. The full
MaiBot source, its SDK, and its runtime are not included in this repository.

The QQ file transport uses the public API of
[MaiBot-SnowLuma-Adapter](https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter).
No adapter/client implementation or account configuration is bundled.

This is a community integration, not an official Multica, MaiBot, or QQ product.
