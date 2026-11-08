# CS2 Nade Helper: proof of concept

A context-aware grenade lineup assistant for Counter-Strike 2. The game reports
the current map, side, round phase and carried grenades through Valve's official
Game State Integration (GSI); the app shows matching lineups in a browser page
that updates live. No game memory is read and nothing is injected into the game.

```
 CS2 ──HTTP POST (GSI JSON)──▶ server.py ──Server-Sent Events──▶ browser (PC, 2nd monitor)
                               │
                               └── lineups/<map>.json
```

Requires only Python 3.9+ (standard library, no installs).

## Run it with CS2

1. Copy `gamestate_integration_nadehelper.cfg` to
   `...\steamapps\common\Counter-Strike Global Offensive\game\csgo\cfg\`
2. Start CS2 (the config is only read at startup).
3. Run `python server.py` and open http://127.0.0.1:3000.
4. Start any match or a practice server.

## What the PoC demonstrates

| Feature | How |
|---|---|
| Automatic map and side detection | `map.name`, `player.team` |
| Side swap at halftime | side re-read on every update |
| Highlighting grenades you carry | `player.weapons` → grenades sorted first, others dimmed |
| Freeze time cue | `round.phase == "freezetime"` |
| Ignoring a spectated teammate's data | compares `player.steamid` with `provider.steamid` |
| Rejecting foreign data | `auth.token` must match the config |
| Unsupported maps / main menu | handled with a clear message |

## Lineup data

`lineups/<map>.json`, one file per map. Each lineup:

```json
{
  "id": "mir-t-window", "side": "T", "type": "smoke",
  "title": "Window smoke", "area": "Mid",
  "stand": "...", "aim": "...", "throw": "Jump-throw",
  "priority": 3, "image": "mirage/window.jpg", "verified": false
}
```

The included lineups for Mirage and Inferno are **placeholders** that show the
structure; screenshots and exact lineups will come later.

## Known limitations / next steps

- Lineup content is placeholder data.
- Recommendation ranks only by side, carried grenades and a manual priority.
  Possible extensions: player position (`player_position`), round economy,
  team strategy presets.
- No automatic config installation (locating the Steam library via `libraryfolders.vdf`).
- Behaviour of GSI in modes other than competitive/premier is untested.
