# Hangar

A launcher for the games and apps you have outside Steam, for Linux.

- **Anything runs:** Windows `.exe`/`.msi`/`.bat` files through **Proton** (the newest GE-Proton by default) or **Wine**, and native programs, AppImages, scripts or plain commands (`flatpak run …`) as they are.
- **Pick the version per game:** every Proton on the PC is found (Steam's own, GE-Proton in `compatibilitytools.d`, Heroic's, Hangar's), and every Wine (system, Lutris, Heroic, Bottles runners). Settings has a button that downloads the newest GE-Proton, checksum-checked.
- **Games and Apps tabs,** each with **Library, Favourites, NSFW and Hidden** lists. NSFW entries only show in their own tab (unless you turn that off) and their covers are blurred elsewhere. Hidden entries only show under Hidden.
- **Grid of covers or a list.** Covers come from the Steam store or SteamGridDB (free API key, for games that aren't on Steam), or any image: pick one or drop it on the tile. Windows programs get their `.exe` icon automatically.
- **Themes:** Midnight, Gold, Tokyo Night, Catppuccin, Dracula, Rosé Pine, Nord, Gruvbox, Emerald, Crimson, OLED and Light, with any accent colour. Three grid sizes.
- **Per game:** its own prefix, arguments, start folder, environment variables, a command prefix (`gamescope -f --`), MangoHud and GameMode. Prefix tools: winecfg, regedit, winetricks (downloadable from Settings), running another `.exe` in the prefix, stopping it.
- Playtime and last played, sorting, search, the last launch log, app-menu shortcuts, and drag and drop of program files to add them.

## Get it

Download `Hangar-x86_64.AppImage` from the latest release, `chmod +x` it and run it. Without the AppImage: `pip install PyQt6 certifi` and run `./hangar.py`.

`hangar --check` prints the Proton/Wine versions and tools Hangar finds. `hangar --launch ID` runs one entry without the window (menu shortcuts use it).

## How Proton runs

With [umu-launcher](https://github.com/Open-Wine-Components/umu-launcher) installed (and the setting on), Proton runs through `umu-run` inside Steam's runtime. Otherwise Hangar runs Proton's own script directly (`proton waitforexitandrun`) with the `STEAM_COMPAT_*` variables set, which works for most games and needs nothing extra. A game's prefix is a folder with Proton's layout (`<prefix>/pfx` is the Wine prefix), so switching a game between Proton and Wine keeps its saves.

## Files

- `~/.config/hangar/library.json` and `settings.json`
- `~/.local/share/hangar/`: covers, icons, launch logs, downloaded tools
- `~/Games/Hangar/prefixes/` (changeable): prefixes of new Windows entries
- GE-Proton downloads go into Steam's `compatibilitytools.d`, so Steam can use them too

## Building

`./build_appimage.sh` (needs `appimagetool`; `--appdir` stops before it). GitHub Actions builds the AppImage on every `v*` tag and attaches it to the release.

MIT licensed.
