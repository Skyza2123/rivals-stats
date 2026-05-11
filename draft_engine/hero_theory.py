"""
hero_theory.py — Marvel Rivals hero knowledge base.

Answers "WHAT HEROES DO" — not how the agent reasons.

DESIGN INTENT:
  Keep entries factual and brief. One archetype, one function sentence, tag lists.
  The LLM receives this as targeted context injection, not as part of the persona block.
  Do NOT embed reasoning instructions here. Those live in llm.py.
  Add new heroes each season by extending HERO_PROFILES.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Role definitions
# ---------------------------------------------------------------------------
ROLES: dict[str, str] = {
    "Vanguard":   "Frontline — controls space, absorbs damage, creates angles.",
    "Duelist":    "Damage dealer — creates picks, wins dives, secures eliminations.",
    "Strategist": "Support — sustains allies, enables plays, displaces threats.",
}

# ---------------------------------------------------------------------------
# Archetype definitions  (what a playstyle means, not which heroes have it)
# ---------------------------------------------------------------------------
ARCHETYPES: dict[str, str] = {
    "Brawler":     "Wins through sustained close-range damage and durability. Punishes teams that don't respect presence.",
    "Diver":       "High mobility, skips frontline to target backline. Strong pick threat; punishes isolated targets.",
    "Poke":        "Deals damage safely at range, exhausts cooldowns, forces bad engages. Wins by chipping before committing.",
    "Rush":        "Aggressive engage that forces reaction. Wins via speed and initiative.",
    "Flex":        "No specific archetype — can fit into multiple playstyles.",
}

SUBROLES: dict[str, str] = {
    "Controller": "Controls space and enemy movement with zoning, walls, or crowd control.",
    "Carry": "Primary win condition. Comp is built around this hero — protect or deny first.",
    "Flanker": "Skirts the edges of fights to pick off priority targets. Excels at creating chaos and exploiting positioning.",
    "Enabler": "Amplifies allies rather than dealing damage. Value is the carry it enhances, not its own output.",
    "Sustain": "Primary healing throughput. Punished by burst; rewards dive denial.",
}

# ---------------------------------------------------------------------------
# Hero profiles
# ---------------------------------------------------------------------------
# Fields:
#   role         : Vanguard | Duelist | Strategist
#   archetype    : key from ARCHETYPES
#   sub_role     : optional secondary role that captures a hero's strategic function more specifically than the primary role — e.g. "Controller" for a tank that specializes in zoning, or "Enabler" for a support whose value is in the carry it empowers rather than its own output
#   function     : 1-sentence plain-English description in a comp
#   comp_tags    : comp styles this hero enables — see COMP_ARCHETYPES keys
#   synergies    : hero names that pair well # REMOVE
#   teamup       : known Team-Up bonus names for this hero, or "" when none
#   countered_by : hero names or archetype labels that reliably shut this down # Remove this field if it overlaps too much with "comp_tags" — it's meant to capture hard counters that aren't necessarily a comp style, e.g. "heroes with CC immunity"
#   ban_priority : "high" | "medium" | "low" — general strategic importance
#   notes        : game-specific mechanic or season note that doesn't fit elsewhere

HERO_PROFILES: dict[str, dict] = {

    # ── Vanguards ──────────────────────────────────────────────────────────

    "Angela": {
        "role": "Vanguard",
        "archetype": "Diver",
        "sub_role": "Controller",
        "function": "Gets early picks on priority backline targets. Bypasses frontlines to create 2v1s in the back.",
        "comp_tags": ["dive", "rush"],
        "teamup": "Divine Armory",
        "ban_priority": "low",
        "notes": "Hybrid dive/rush tank. Rewards aggressive play — reward loops on kills rather than passive space control.",
    },

    "Captain America": {
        "role": "Vanguard",
        "archetype": "Diver",
        "sub_role": "",
        "function": "Can dive backline easily without using key cooldowns.",
        "comp_tags": ["dive", "rush"],
        "teamup": "Lucky Loan, Stars Aligned",
        "ban_priority": "medium",
        "notes": "Forces early fight — good into passive poke teams but struggles against brawl with good holds.", # CHECK 
    },

    "Dr. Strange": {
        "role": "Vanguard",
        "archetype": "Poke",
        "sub_role": "Controller",
        "function": "Can contest space from longer range than any other tank.",
        "comp_tags": ["poke"],
        "teamup": "Arcane Order, Psionic Mayhem",
        "ban_priority": "high",
        "notes": "Very strong Psionic Mayhem synergy with Invisible Woman",
    },

    "Tankpool": {
        "role": "Vanguard",
        "archetype": "Rush",
        "sub_role": "",
        "function": "Can do whatever he wants, mark angles, dive, shoot tanks, has two upgrade paths to invest into. Jack of all trades, master of none.",
        "comp_tags": ["dive", "rush"],
        "teamup": "Mr. Pool's Interdimensional Toy Box",
        "ban_priority": "medium",
        "notes": "Represents Deadpool in the Vanguard slot only. Deadpool can only appear once in a legal lineup, so Tankpool, DpsPool, and SupportPool are mutually exclusive placeholders.",
    },

    "Emma Frost": {
        "role": "Vanguard",
        "archetype": "Brawler",
        "sub_role": "",
        "function": "Sustained close-range brawler with a strong dueling kit and the ability to create space with her diamond form.",
        "comp_tags": ["brawl", "rush"],
        "teamup": "Chilling Assault",
        "ban_priority": "low",
        "notes": "Flexible enough to slot into multiple comp styles.",
    },

    "Groot": {
        "role": "Vanguard",
        "archetype": "Brawler",
        "sub_role": "Controller",
        "function": "Zone-control tank — places walls to split fights, block sightlines, and root enemies with ult.",
        "comp_tags": ["brawl"],
        "teamup": "Planet X Pals, Vibrant Vitality",
        "ban_priority": "low",
        "notes": "Wall placement is the skill expression. Good walls create 2v1s inside the split.",
    },

    "Hulk": {
        "role": "Vanguard",
        "archetype": "Rush",
        "sub_role": "Enabler",
        "function": "Occupies space and demands attention, better at physical disruption than damage. Creates windows for allies to follow up on with his crowd control and knockback.",
        "comp_tags": ["rush", "brawl"],
        "teamup": "Fastball Special, Gamma Charge",
        "ban_priority": "low",
        "notes": "Best use is with fastball. Not played if Wolverine is banned",
    },

    "Magneto": {
        "role": "Vanguard",
        "archetype": "Poke",
        "sub_role": "",
        "function": "Medium-range poke tank with shield utility; creates resource pressure from distance. Bubbles allies",
        "comp_tags": ["poke", "brawl"],
        "teamup": "Explosive Entanglement",
        "ban_priority": "low",
        "notes": "Meta-dependent: strong when poke is uncontested, weak when dive gets through.",
    },

    "Peni Parker": {
        "role": "Vanguard",
        "archetype": "Poke",
        "sub_role": "Controller",
        "function": "Anchor zone-control tank — spider-nest creates a mine field that controls an area entirely.",
        "comp_tags": ["poke", "brawl"],
        "teamup": "Parker Power-Up",
        "ban_priority": "low",
        "notes": "Nest anchor is almost immovable when positioned correctly. Weak if displaced or nest is destroyed. Not very good right now.",
    },

    "Rogue": {
        "role": "Vanguard",
        "archetype": "Rush",
        "sub_role": "",
        "function": "Absorbs an enemy ability and turns it against them. Strong mobility and her ultimater drains other ultimate charge, disrupting enemy ult economy.",
        "comp_tags": ["rush"],
        "teamup": "Explosive Entanglement",
        "ban_priority": "low",
        "notes": "Very strong with Explosive Entanglement. Can be strong in rush comps that rely on ultimates, but is very situational overall.",
    },

    "Thing": {
        "role": "Vanguard",
        "archetype": "Brawler",
        "sub_role": "Controller",
        "function": "High-HP close-range brawl tank — absorbs punishment and chunks grouped enemies. Strong anti dive with his ability to control space and disrupt enemy formations.",
        "comp_tags": ["brawl", "rush"],
        "teamup": "Fastball Special, First Steps, Gamma Charge",
        "ban_priority": "low",
        "notes": "Straightforward brawler — high floor, low ceiling.",
    },

    "Thor": {
        "role": "Vanguard",
        "archetype": "Rush",
        "sub_role": "Controller",
        "function": "High-mobility engage tank — hammer throw harasses at range, Lightning ult disrupts enemy formation.",
        "comp_tags": ["rush", "brawl"],
        "teamup": "Divine Armory",
        "ban_priority": "low",
        "notes": "Strong initiation ceiling. Ult demands a follow-up — a team that can't follow up wastes the engage. Back pocket pick",
    },

    "Venom": {
        "role": "Vanguard",
        "archetype": "Diver",
        "sub_role": "",
        "function": "Aggressive frontline dive tank — jumps backline to force fights on bad ground.",
        "comp_tags": ["dive"],
        "teamup": "Symbiote Shenanigans",
        "ban_priority": "medium",
        "notes": "Value scales directly with how good the accompanying dive duelists are.",
    },

    # ── Duelists ───────────────────────────────────────────────────────────

    "Black Cat": {
        "role": "Duelist",
        "archetype": "Diver",
        "sub_role": "Carry",
        "function": "Hypermobile flanker/brawler with high risk high reward variety of great utility through runes she gathers from stealing money from eneimies.",
        "comp_tags": ["dive", "brawl"],
        "teamup": "Lucky Loan",
        "ban_priority": "medium",
        "notes": "Among the highest mobility ceilings in the game — grapple/dash kit makes her nearly impossible to lock down. High execution, high reward. Permantly use invisible rune",
    },

    "Black Panther": {
        "role": "Duelist",
        "archetype": "Diver",
        "sub_role": "Flanker",
        "function": "High-mobility melee assassin — dashes in, bursts a target, escapes before peel arrives.",
        "comp_tags": ["dive"],
        "teamup": "Gamma Charge",
        "ban_priority": "low",
        "notes": "Extreme execution ceiling. Strong when ahead, weak when denied his initial pick.",
    },

    "Black Widow": {
        "role": "Duelist",
        "archetype": "Poke",
        "sub_role": "Carry",
        "function": "Precision ranged duelist — consistent headshot threat from safe distance.",
        "comp_tags": ["poke"],
        "teamup": "Primal Flame",
        "ban_priority": "low",
        "notes": "Reward is entirely execution-based — same level of value as the player's aim. Very bad right now, needs buffs to be viable.",
    },

    "Blade": {
        "role": "Duelist",
        "archetype": "Brawler",
        "sub_role": "Enabler",
        "function": "Life-steal melee duelist — sustains through combat, strong into low-burst brawl fights.",
        "comp_tags": ["brawl", "rush"],
        "teamup": "Blade of Khonshu",
        "ban_priority": "low",
        "notes": "Decent hero but too niche in a team setting",
    },

    "Star-Lord": {
        "role": "Duelist",
        "archetype": "Flex",
        "sub_role": "Carry",
        "function": "Mobile aerial poke carry — maintains range, punishes overextension, team ult is a fight winner. Creates pressure with zoning abilities.",
        "comp_tags": ["poke", "dive"],
        "teamup": "Rocket Network",
        "ban_priority": "medium",
        "notes": "Team ult changes the fight. High priority for both ban and protect depending on who has him. Good with Gambit ultimate. Good in tripple support comps.",
    },

    "Daredevil": {
        "role": "Duelist",
        "archetype": "Diver",
        'sub_role': "Carry",
        "function": "Melee dive duelist that targets supports — eliminates healers to create sustained pressure. Can see through walls to set up dives. Mark angles and contest flankers with his radar sense.",
        "comp_tags": ["dive"],
        "teamup": "Bestial Hunt",
        "ban_priority": "medium",
        "notes": "Depends on team composition for ban priority.",
    },

    "DpsPool": { #HELP
        "role": "Duelist",
        "archetype": "Diver",
        "sub_role": "Carry",
        "function": "Deadpool's Duelist placeholder — a damage-role flex pick used when the roster or draft records his Duelist version.",
        "comp_tags": ["brawl", "poke", "flex"],
        "teamup": "Mr. Pool's Interdimensional Toy Box",
        "ban_priority": "low",
        "notes": "Represents Deadpool in the Duelist slot only. Deadpool can only appear once in a legal lineup, so DpsPool, Tankpool, and SupportPool are mutually exclusive placeholders.",
    },


    "Elsa Bloodstone": {
        "role": "Duelist",
        "archetype": "Poke",
        "sub_role": "Carry",
        "function": "Ranged poke duelist with interrupt — punishes grouped enemies and cooldown-dependent targets.",
        "comp_tags": ["poke", "brawl"],
        "teamup": "Mr. Pool's Interdimensional Toy Box",
        "ban_priority": "medium",
        "notes": "Interrupt mechanic is situationally game-breaking into ult-dependent teams.",
    },

    "Hawkeye": {
        "role": "Duelist",
        "archetype": "Poke",
        "sub_role": "Carry",
        "function": "Long-range precision archer — highest single-shot damage ceiling among poke carries.",
        "comp_tags": ["poke"],
        "teamup": "Sword of Duality",
        "ban_priority": "low",
        "notes": "One-shot potential on charged shot makes him oppressive into slow-moving targets.",
    },

    "Hela": { #HELP
        "role": "Duelist",
        "archetype": "Poke",
        "sub_role": "Carry",
        "function": "High-damage ranged anchor carry — soul mechanic revives her on death if souls are stacked.",
        "comp_tags": ["poke", "brawl"],
        "teamup": "Deep Wrath, Symbiote Shenanigans",
        "ban_priority": "low",
        "notes": "Soul stack makes her resilient to burst — she is her own second life if positioned to stack souls safely.",
    },

    "Human Torch": {
        "role": "Duelist",
        "archetype": "Poke",
        "sub_role": "",
        "function": "Aerial fire-based ranged duelist — zones with AoE fire and harasses from above sightlines.",
        "comp_tags": ["poke"],
        "teamup": "First Steps",
        "ban_priority": "low",
        "notes": "Aerial positioning is his primary defense — loses value in close-quarter chokepoints.",
    },

    "Iron Fist": {
        "role": "Duelist",
        "archetype": "Diver",
        "sub_role": "Flanker",
        "function": "Melee martial arts duelist — rapid close-range burst with fast cooldown resets.",
        "comp_tags": ["dive"],
        "teamup": "Chilling Assault",
        "ban_priority": "low",
        "notes": "Gap close is instant — punishes supports that stand still. Weak into organized peel. Back pocket pick that can punish certain comps if left unchecked.",
    },

    "Iron Man": {
        "role": "Duelist",
        "archetype": "Poke",
        "sub_role": "",
        "function": "Aerial tech duelist — sustained energy beam and repulsor burst from range.",
        "comp_tags": ["poke"],
        "teamup": "Stark Protocol",
        "ban_priority": "low",
        "notes": "Portal synergy with Dr. Strange creates angles unavailable to most aerial poke comps.",
    },

    "Magik": {
        "role": "Duelist",
        "archetype": "Diver",
        "sub_role": "Flanker",
        "function": "Teleport-based melee duelist — opens portals to appear behind targets instantly.",
        "comp_tags": ["dive", "brawl"],
        "teamup": "Arcane Order",
        "ban_priority": "low",
        "notes": "Portal angle creation is unique — can appear in spots no other melee duelist can reach.",
    },

    "Moon Knight": {
        "role": "Duelist",
        "archetype": "Poke",
        "sub_role": "",
        "function": "Bouncing projectile duelist — crescent darts ricochet to hit multiple targets and punish grouped enemies. Used in spawn break comps to contest grouped enemies at spawn.",
        "comp_tags": ["poke", "brawl"],
        "teamup": "Blade of Khonshu",
        "ban_priority": "low",
        "notes": "Value scales with how grouped enemies are — spread comps significantly reduce his damage output.",
    },

    "Mr. Fantastic": { #HELP
        "role": "Duelist",
        "archetype": "Brawler",
        "sub_role": "Controller",
        "function": "Elastic melee brawler — soaks hits with elasticity mechanics, disrupts close-range fights.",
        "comp_tags": ["brawl", "rush"],
        "teamup": "Rocket Network",
        "ban_priority": "low",
        "notes": "Close-range fight disruptor. Stronger in triple-tank formats where the brawl goes extended.",
    },

    "Namor": {
        "role": "Duelist",
        "archetype": "Poke",
        "sub_role": "",
        "function": "Ranged aquatic duelist with summons — electric eel turrets create persistent zone denial.",
        "comp_tags": ["poke"],
        "teamup": "Deep Wrath",
        "ban_priority": "low",
        "notes": "Turret zones force enemy positioning decisions. Weak if the turrets are destroyed quickly.",
    },

    "Phoenix": {
        "role": "Duelist",
        "archetype": "Flex",
        "sub_role": "Flex",
        "function": "High-damage ult-centric carry — ult resets on kill, enabling chain wipes.",
        "comp_tags": ["dive", "brawl"],
        "synergies": ["Mantis", "Luna Snow", "Cloak & Dagger"],
        "teamup": "Primal Flame",
        "countered_by": ["Spread damage", "CC-heavy comps"],
        "ban_priority": "medium",
        "notes": "Ult chain is the win condition — teams that protect her protect the chain.",
    },

    "Psylocke": {
        "role": "Duelist",
        "archetype": "Diver",
        "sub_role": "Flanker",
        "function": "Fast mobile assassin — flanks, picks isolated targets, resets on kill.",
        "comp_tags": ["dive", "brawl"],
        "teamup": "Sword of Duality",
        "ban_priority": "medium",
        "notes": "Ban priority against teams with exposed backlines. Reset mechanic rewards snowballing.",
    },

    "Punisher": {
        "role": "Duelist",
        "archetype": "Brawler",
        "sub_role": "Carry",
        "function": "High-damage firearms duelist — consistent sustained output at mid-close range.",
        "comp_tags": ["brawl", "poke"],
        "teamup": "Bestial Hunt",
        "ban_priority": "low",
        "notes": "Predictable but reliable. Ult provides area-denial turret mode that controls a choke.",
    },

    "Scarlet Witch": {
        "role": "Duelist",
        "sub_role": "",
        "archetype": "Brawler",
        "function": "AoE chaos magic duelist — reality-warping projectiles and crowd control punish grouped enemies.",
        "comp_tags": ["brawl", "flex"],
        "teamup": "Arcane Order",
        "ban_priority": "low",
        "notes": "Ult is a full-room wipe threat on grouped enemies. Strong at choke angles.",
    },

    "Spider-Man": {
        "role": "Duelist",
        "archetype": "Diver",
        "sub_role": "Flanker",
        "function": "Extreme-mobility web-slinger — highest movement ceiling in the game, creates constant pick threat.",
        "comp_tags": ["dive"],
        "teamup": "Parker Power-Up",
        "ban_priority": "low",
        "notes": "Execution ceiling is among the highest. Rewards players who can maintain unpredictable angles.",
    },

    "Squirrel Girl": {
        "role": "Duelist",
        "archetype": "Poke",
        "sub_role": "Carry",
        "function": "Acorn projectile duelist — persistent area denial and poke damage from safe distance.",
        "comp_tags": ["poke"],
        "teamup": "Stark Protocol",
        "ban_priority": "low",
        "notes": "High-burst ult can one-shot grouped targets. Primarily a zone-denial poke tool.",
    },

    "Storm": {
        "role": "Duelist",
        "archetype": "Poke",
        "function": "Aerial weather-control duelist — creates lightning zones and persistent AoE from elevated positions.",
        "comp_tags": ["poke", "flex"],
        "synergies": ["Magneto", "Dr. Strange", "Invisible Woman"],
        "teamup": "Cosmic Cyclone",
        "countered_by": ["Dive reaching aerial targets", "Hard CC"],
        "ban_priority": "low",
        "notes": "Ult creates a persistent kill zone. Strong on maps with high ground angles.",
    },

    "Winter Soldier": {
        "role": "Duelist",
        "archetype": "Brawler",
        "function": "Mid-range brawl duelist — grapple creates burst windows at variable range.",
        "comp_tags": ["brawl", "poke"],
        "synergies": ["Captain America", "Mantis", "Invisible Woman"],
        "teamup": "Stars Aligned",
        "countered_by": ["High-mobility duelists that negate grapple", "Dive"],
        "ban_priority": "low",
        "notes": "Consistent mid-range damage. Grapple into burst is the value pattern.",
    },

    "Wolverine": {
        "role": "Duelist",
        "archetype": "Diver",
        "function": "Berserker melee duelist with regeneration — trades aggressively knowing regen will restore HP.",
        "comp_tags": ["dive", "brawl"],
        "synergies": ["Hulk", "Cloak & Dagger", "Mantis"],
        "teamup": "Fastball Special, Primal Flame",
        "countered_by": ["Sustained burst that outpaces regen", "Hard CC"],
        "ban_priority": "low",
        "notes": "Regen mechanic lets him take trades other duelists can't. Hybrid dive/brawl like Angela.",
    },

    # ── Strategists ────────────────────────────────────────────────────────

    "Adam Warlock": {
        "role": "Strategist",
        "archetype": "Sustain",
        "function": "Team resurrection support — ult revives fallen allies, enabling fight resets mid-round.",
        "comp_tags": ["brawl", "flex"],
        "synergies": ["Phoenix", "Hulk", "Captain America"],
        "teamup": "Cosmic Cyclone",
        "countered_by": ["Burst that downs multiple targets before ult resolves"],
        "ban_priority": "medium",
        "notes": "Ult is the value — not the healing throughput. Teams with a critical carry that dies often benefit most.",
    },

    "Cloak & Dagger": {
        "role": "Strategist",
        "archetype": "Flex",
        "function": "Sustain/damage hybrid — Dagger heals allies, Cloak damages enemies; toggleable identity.",
        "comp_tags": ["dive", "brawl", "flex"],
        "synergies": ["Daredevil", "Venom", "Psylocke"],
        "teamup": "Sword of Duality",
        "countered_by": ["Sustained burst before toggle cooldown", "Hard engage before peel"],
        "ban_priority": "medium",
        "notes": "Strong in dive because she can heal after the dive commits and survive burst with Cloak mode.",
    },

    "Gambit": {
        "role": "Strategist",
        "archetype": "Enabler",
        "function": "Charge-based support — builds and expends charge to amplify ally damage or disrupt enemies.",
        "comp_tags": ["brawl", "poke", "flex"],
        "synergies": ["Invisible Woman", "Mantis"],
        "teamup": "Explosive Entanglement",
        "countered_by": ["Dive that eliminates him before charge builds"],
        "ban_priority": "medium",
        "notes": "Appears as a top-played strategist comfort pick in Season 7 data.",
    },

    "Invisible Woman": {
        "role": "Strategist",
        "archetype": "Enabler",
        "function": "Bubble/force field support — peels for carries, cancels dives, extends fights via displacement.",
        "comp_tags": ["brawl", "poke", "dive"],
        "synergies": ["Star-Lord", "Phoenix", "Psylocke"],
        "teamup": "Psionic Mayhem",
        "countered_by": ["Sustained pressure that depletes bubbles", "CC stacking"],
        "ban_priority": "high",
        "notes": "Enables almost every comp style. Banning her removes peel and exposes the backline.",
    },

    "Jeff TLS": {
        "role": "Strategist",
        "archetype": "Displacer",
        "function": "Swallows enemies or allies to reposition — ult removes multiple players from a fight entirely.",
        "comp_tags": ["dive", "brawl", "flex"],
        "synergies": ["Venom", "Daredevil"],
        "teamup": "Mr. Pool's Interdimensional Toy Box, Planet X Pals, Symbiote Shenanigans",
        "countered_by": ["Range-heavy comps", "Teams that handle repositioned targets quickly"],
        "ban_priority": "high",
        "notes": "Ult is among the highest impact in the game — displaces full teams off objectives. Frequent ban target.",
    },

    "Loki": {
        "role": "Strategist",
        "archetype": "Flex",
        "function": "Copies ally heroes — provides flexibility and confuses opponent target priority.",
        "comp_tags": ["dive", "flex"],
        "synergies": ["Psylocke", "Daredevil", "Any high-value anchor"],
        "teamup": "Vibrant Vitality",
        "countered_by": ["Focused kill priority", "Low-value comps where copying is weak"],
        "ban_priority": "low",
        "notes": "Most valuable when there is an extremely high-value hero to copy.",
    },

    "Luna Snow": {
        "role": "Strategist",
        "archetype": "Sustain",
        "function": "High healing throughput with AoE ult — pairs with dive for rapid recovery after engages.",
        "comp_tags": ["brawl", "dive"],
        "synergies": ["Phoenix", "Psylocke", "Venom"],
        "teamup": "Blessing of the Kumiho, Chilling Assault",
        "countered_by": ["Burst that outpaces healing", "CC that prevents ult"],
        "ban_priority": "medium",
        "notes": "Ice disc slow creates zone control. Ult timing is the skill check.",
    },

    "Mantis": {
        "role": "Strategist",
        "archetype": "Enabler",
        "function": "Damage-amp and healing hybrid — Sleep dart creates pick opportunities on key targets.",
        "comp_tags": ["brawl", "poke", "dive"],
        "synergies": ["Phoenix", "Captain America", "Star-Lord"],
        "teamup": "Vibrant Vitality",
        "countered_by": ["Sustained burst before she can sustain", "Dive"],
        "ban_priority": "medium",
        "notes": "Sleep dart on a diving threat can flip a fight. High skill-cap impact.",
    },

    "Rocket Raccoon": {
        "role": "Strategist",
        "archetype": "Sustain",
        "function": "Consistent healing with revive ult — keeps team alive through sustained fights.",
        "comp_tags": ["brawl", "rush"],
        "synergies": ["Hulk", "Groot", "Captain America"],
        "teamup": "Planet X Pals, Rocket Network",
        "countered_by": ["Dive that eliminates him first", "High burst one-shots"],
        "ban_priority": "low",
        "notes": "Revive ult is game-state altering in close matches.",
    },

    "SupportPool": {
        "role": "Strategist",
        "archetype": "Flex",
        "function": "Deadpool's Strategist placeholder — a support-role flex pick used when the roster or draft records his Strategist version.",
        "comp_tags": ["brawl", "flex"],
        "synergies": ["Phoenix", "Venom", "Star-Lord"],
        "teamup": "Mr. Pool's Interdimensional Toy Box",
        "countered_by": ["Hard engage", "Burst before peel lands"],
        "ban_priority": "low",
        "notes": "Represents Deadpool in the Strategist slot only. Deadpool can only appear once in a legal lineup, so SupportPool, Tankpool, and DpsPool are mutually exclusive placeholders.",
    },

    "Ultron": {
        "role": "Strategist",
        "archetype": "Poke",
        "function": "Mobile aerial strategist with drone area denial — adds ranged pressure while supporting from safe angles.",
        "comp_tags": ["poke", "flex"],
        "synergies": ["Star-Lord", "Invisible Woman"],
        "teamup": "Stark Protocol",
        "countered_by": ["Dive", "High-burst flankers"],
        "ban_priority": "low",
        "notes": "Weaker when enemy forces active close-range fights.",
    },

    "White Fox": {
        "role": "Strategist",
        "archetype": "Flex",
        "function": "Mobile support with slows and escapes — kite potential and sustained healing on the move.",
        "comp_tags": ["poke", "flex"],
        "synergies": ["Star-Lord", "Elsa Bloodstone", "Invisible Woman"],
        "teamup": "Blessing of the Kumiho, Lucky Loan",
        "countered_by": ["Hard engage before escape cooldown", "Heavy CC"],
        "ban_priority": "low",
        "notes": "Better in spread/poke comps than brawl. Mobility makes her harder to dive.",
    },
}

# ---------------------------------------------------------------------------
# Comp archetypes
# ---------------------------------------------------------------------------
COMP_ARCHETYPES: dict[str, dict] = {
    "dive": {
        "name": "Dive",
        "description": "Mobile team that bypasses frontlines to kill supports. Win condition: pick before enemy reacts.",
        "core_roles": ["Diver Vanguard", "Diver/Assassin Duelist", "Sustain-on-reset Strategist"],
        "win_condition": "Isolate and eliminate priority backline target before opponent peels.",
        "beats": ["poke", "brawl without peel"],# MIGHT KIND OF WANT TO REWORK THIS TO BE MORE SPECIFIC ABOUT WHAT IT BEATS/LOSES TO
        "loses_to": ["brawl with strong peel", "CC-heavy bunker"],
        "key_heroes": ["Venom", "Psylocke", "Daredevil", "Cloak & Dagger", "Luna Snow"],
    },
    "poke": {
        "name": "Poke",
        "description": "Chip from range to exhaust cooldowns before committing. Win condition: force bad engage.",
        "core_roles": ["Controller/Poke Vanguard", "Ranged Duelist", "Mobile Strategist"],
        "win_condition": "Bring enemy to low HP via range pressure, then clean up with committed burst.",
        "beats": ["brawl that can't close distance", "rush that over-commits"],
        "loses_to": ["dive", "heavy shields"],
        "key_heroes": ["Dr. Strange", "Magneto", "Star-Lord", "Elsa Bloodstone", "Invisible Woman"],
    },
    "brawl": {
        "name": "Brawl",
        "description": "Close-range sustained fight — outlast opponents through healing and trading.",
        "core_roles": ["Brawler Vanguard", "Anchor Duelist", "Sustain Strategist"],
        "win_condition": "Out-sustain in prolonged engagements, punish overaggressive dives.",
        "beats": ["dive with peel", "rush that overextends"],
        "loses_to": ["poke at long range", "burst that exceeds healing"],
        "key_heroes": ["Hulk", "Captain America", "Phoenix", "Mantis", "Rocket Raccoon"],
    },
    "rush": {
        "name": "Rush",
        "description": "Aggressive fast engage — force fights before enemy sets up.",
        "core_roles": ["Rush Vanguard", "Brawl Duelist", "Sustain Strategist"],
        "win_condition": "Take objective via speed and aggression before defense peaks.",
        "beats": ["poke before setup"],
        "loses_to": ["brawl on good hold positions", "poke with long sightlines"],
        "key_heroes": ["Captain America", "Venom", "Angela"],
    },
    "flex": {
        "name": "Flex",
        "description": "Mixed archetype — harder to read and counter at draft time.",
        "core_roles": ["Flex Vanguard", "Flex Duelist", "Flex Strategist"],
        "win_condition": "Adapt in-game to whichever matchup emerges.",
        "beats": ["rigid single-style comps"],
        "loses_to": ["very high comfort on a strong archetype"],
        "key_heroes": ["Emma Frost", "Cloak & Dagger", "Rogue", "White Fox", "Loki"],
    },
    "triple_tank": {
        "name": "Triple Tank",
        "description": "Three Vanguards create overwhelming frontline presence; sacrifices raw damage output for space control, durability, and ability to hold any angle.",
        "core_roles": ["Controller/Anchor Vanguard", "Poke/Disruption Vanguard", "Rush/Brawl Vanguard"],
        "win_condition": "Control so much space that enemies are forced into unfavorable angles; outlast any response through raw HP and ability to hold.",
        "beats": ["dive that can't find clean priority targets behind triple frontline", "poke teams that run out of resources before breaking the wall"],
        "loses_to": ["two high-output self-sufficient duelists with high healing throughput behind them", "sustained burst that outpaces two-support healing"],
        "key_heroes": ["Dr. Strange", "Magneto", "Hulk", "Venom", "Captain America", "Emma Frost"],
        "support_requirements": "Requires two high-throughput supports. Rocket Raccoon + Luna Snow is the ideal pair — one revives, one sustains the brawl.",
        "example_core": ["Dr. Strange", "Magneto", "Hulk"],
        "why_those_three": "Strange provides angle creation and peel; Magneto provides ranged poke and shield coverage; Hulk provides raw durability and Bruce mode disruption.",
    },
    "triple_support": {
        "name": "Triple Support",
        "description": "Three Strategists with two self-sufficient Duelists; provides near-unlimited sustain and utility — the team cannot be attritioned.",
        "core_roles": ["Peel Strategist (bubble/displacement)", "Sustain Strategist (throughput healing)", "Flex/Enabler Strategist (amp or disruption)"],
        "win_condition": "Outlast any attrition war by recycling health continuously; protect two self-sufficient duelists who carry the damage.",
        "beats": ["brawl comps that rely on out-sustaining", "poke teams without enough burst to one-shot through triple healing"],
        "loses_to": ["burst-heavy dive that one-shots before heals land", "comps that correctly prioritize targeting the extra support"],
        "key_heroes": ["Invisible Woman", "Mantis", "Luna Snow", "Cloak & Dagger", "Jeff TLS", "Loki"],
        "duelist_requirements": "Requires two highly self-sufficient duelists. Phoenix + Psylocke is the canonical pairing — Phoenix self-heals through ult chain; Psylocke resets on kill.",
        "example_core": ["Invisible Woman", "Mantis", "Luna Snow"],
        "why_those_three": "Invisible Woman provides peel and bubbles to protect duelists; Mantis provides damage amp and sleep dart control; Luna Snow provides raw healing throughput and ult.",
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_hero_profile(hero_name: str) -> dict | None:
    """Return the profile dict for a hero (case-insensitive), or None if unknown."""
    key = (hero_name or "").strip()
    if key in HERO_PROFILES:
        return HERO_PROFILES[key]
    lower = key.lower()
    for name, profile in HERO_PROFILES.items():
        if name.lower() == lower:
            return profile
    return None


def get_comp_tags(heroes: list[str]) -> list[str]:
    """
    Given a hero list, return the top-2 comp style tags supported by the group.
    """
    from collections import Counter
    counts: Counter = Counter()
    for h in heroes:
        p = get_hero_profile(h)
        if p:
            for tag in p.get("comp_tags", []):
                counts[tag] += 1
    return [tag for tag, _ in counts.most_common(2)]


def describe_hero(hero_name: str) -> str:
    """One-line description of a hero for prompt injection."""
    p = get_hero_profile(hero_name)
    if not p:
        return f"{hero_name}: no profile available."
    return (
        f"{hero_name} ({p['role']} / {p['archetype']}): {p['function']} "
        f"Ban priority: {p['ban_priority']}."
    )


def describe_comp(heroes: list[str]) -> str:
    """
    Given a hero lineup, infer the comp archetype and return a brief description.
    """
    tags = get_comp_tags(heroes)
    if not tags:
        return "Unknown comp style — no matching hero profiles."
    comp = COMP_ARCHETYPES.get(tags[0])
    if not comp:
        return f"Comp style: {tags[0]}."
    return (
        f"Comp style: {comp['name']}. {comp['description']} "
        f"Beats: {', '.join(comp.get('beats', []))}. "
        f"Loses to: {', '.join(comp.get('loses_to', []))}."
    )


def get_heroes_for_prompt(hero_names: list[str]) -> str:
    """
    Return a concise prompt-ready block describing a set of heroes.
    Inject as a named section in build_draft_system_prompt when hero theory
    is relevant to the current question.
    """
    lines = []
    for name in hero_names:
        p = get_hero_profile(name)
        if p:
            synergies = ", ".join(p.get("synergies", [])[:3])
            teamup = p.get("teamup", "")
            counters = ", ".join(p.get("countered_by", [])[:2])
            lines.append(
                f"- {name} ({p['role']}/{p['archetype']}): {p['function']}"
                + (f" Synergies: {synergies}." if synergies else "")
                + (f" Team-Up: {teamup}." if teamup else "")
                + (f" Countered by: {counters}." if counters else "")
            )
    return "\n".join(lines) if lines else ""


# ---------------------------------------------------------------------------
# Hero attribute scores  (1–10 scale per dimension)
# ---------------------------------------------------------------------------
# Fields:
#   primary_style      : dominant comp style for this hero
#   secondary_style    : secondary style or None
#   mobility_score     : 1-10 — how independently mobile / hard to catch
#   sustain_score      : 1-10 — self-sustain or HP durability
#   poke_score         : 1-10 — threat at safe range
#   engage_score       : 1-10 — ability to initiate or create a fight
#   peel_score         : 1-10 — ability to protect allies from dives/flanks
#   execution_difficulty: 1-10 — mechanical/decision skill required to extract value

HERO_SCORES: dict[str, dict] = {

    # ── Vanguards ──────────────────────────────────────────────────────────
    "Dr. Strange": {
        "primary_style": "brawl", "secondary_style": "rush",
        "mobility_score": 5, "sustain_score": 4, "poke_score": 3,
        "engage_score": 7, "peel_score": 8, "execution_difficulty": 7,
    },
    "Tankpool": {
        "primary_style": "brawl", "secondary_style": "flex",
        "mobility_score": 5, "sustain_score": 6, "poke_score": 3,
        "engage_score": 6, "peel_score": 5, "execution_difficulty": 4,
    },
    "Hulk": {
        "primary_style": "brawl", "secondary_style": "rush",
        "mobility_score": 5, "sustain_score": 7, "poke_score": 2,
        "engage_score": 6, "peel_score": 5, "execution_difficulty": 5,
    },
    "Magneto": {
        "primary_style": "poke", "secondary_style": "brawl",
        "mobility_score": 4, "sustain_score": 5, "poke_score": 8,
        "engage_score": 4, "peel_score": 6, "execution_difficulty": 5,
    },
    "Venom": {
        "primary_style": "dive", "secondary_style": "rush",
        "mobility_score": 8, "sustain_score": 6, "poke_score": 1,
        "engage_score": 9, "peel_score": 3, "execution_difficulty": 5,
    },
    "Captain America": {
        "primary_style": "brawl", "secondary_style": "rush",
        "mobility_score": 7, "sustain_score": 5, "poke_score": 1,
        "engage_score": 9, "peel_score": 4, "execution_difficulty": 4,
    },
    "Emma Frost": {
        "primary_style": "brawl", "secondary_style": "poke",
        "mobility_score": 4, "sustain_score": 6, "poke_score": 6,
        "engage_score": 6, "peel_score": 5, "execution_difficulty": 7,
    },

    # ── Duelists ───────────────────────────────────────────────────────────
    "Star-Lord": {
        "primary_style": "poke", "secondary_style": "flex",
        "mobility_score": 7, "sustain_score": 3, "poke_score": 8,
        "engage_score": 4, "peel_score": 3, "execution_difficulty": 6,
    },
    "Daredevil": {
        "primary_style": "dive", "secondary_style": None,
        "mobility_score": 9, "sustain_score": 3, "poke_score": 1,
        "engage_score": 8, "peel_score": 2, "execution_difficulty": 7,
    },
    "DpsPool": {
        "primary_style": "brawl", "secondary_style": "poke",
        "mobility_score": 6, "sustain_score": 4, "poke_score": 5,
        "engage_score": 6, "peel_score": 3, "execution_difficulty": 4,
    },
    "Psylocke": {
        "primary_style": "dive", "secondary_style": None,
        "mobility_score": 9, "sustain_score": 3, "poke_score": 2,
        "engage_score": 8, "peel_score": 2, "execution_difficulty": 7,
    },
    "Phoenix": {
        "primary_style": "brawl", "secondary_style": "dive",
        "mobility_score": 6, "sustain_score": 5, "poke_score": 4,
        "engage_score": 6, "peel_score": 3, "execution_difficulty": 7,
    },
    "Elsa Bloodstone": {
        "primary_style": "poke", "secondary_style": None,
        "mobility_score": 4, "sustain_score": 2, "poke_score": 9,
        "engage_score": 3, "peel_score": 5, "execution_difficulty": 6,
        # peel_score=5 because interrupt mechanic can stop a dive from reaching a support
    },
    "Ultron": {
        "primary_style": "poke", "secondary_style": "flex",
        "mobility_score": 7, "sustain_score": 3, "poke_score": 7,
        "engage_score": 4, "peel_score": 2, "execution_difficulty": 5,
    },
    "Rogue": {
        "primary_style": "flex", "secondary_style": "dive",
        "mobility_score": 7, "sustain_score": 4, "poke_score": 3,
        "engage_score": 7, "peel_score": 3, "execution_difficulty": 8,
    },
    "Angela": {
        "primary_style": "dive", "secondary_style": "brawl",
        "mobility_score": 7, "sustain_score": 7, "poke_score": 2,
        "engage_score": 7, "peel_score": 3, "execution_difficulty": 5,
        # sustain_score=7 because on-kill healing is significant in both dive and brawl
    },
    "Black Cat": {
        "primary_style": "dive", "secondary_style": None,
        "mobility_score": 9, "sustain_score": 2, "poke_score": 2,
        "engage_score": 9, "peel_score": 2, "execution_difficulty": 8,
    },

    # ── Strategists ────────────────────────────────────────────────────────
    "Invisible Woman": {
        "primary_style": "brawl", "secondary_style": "poke",
        "mobility_score": 5, "sustain_score": 6, "poke_score": 4,
        "engage_score": 2, "peel_score": 10, "execution_difficulty": 7,
    },
    "Mantis": {
        "primary_style": "brawl", "secondary_style": "dive",
        "mobility_score": 5, "sustain_score": 7, "poke_score": 3,
        "engage_score": 2, "peel_score": 6, "execution_difficulty": 6,
    },
    "Jeff TLS": {
        "primary_style": "flex", "secondary_style": "brawl",
        "mobility_score": 6, "sustain_score": 6, "poke_score": 2,
        "engage_score": 3, "peel_score": 7, "execution_difficulty": 7,
    },
    "Rocket Raccoon": {
        "primary_style": "brawl", "secondary_style": "rush",
        "mobility_score": 4, "sustain_score": 9, "poke_score": 2,
        "engage_score": 2, "peel_score": 5, "execution_difficulty": 4,
    },
    "SupportPool": {
        "primary_style": "brawl", "secondary_style": "flex",
        "mobility_score": 5, "sustain_score": 6, "poke_score": 3,
        "engage_score": 3, "peel_score": 5, "execution_difficulty": 4,
    },
    "Luna Snow": {
        "primary_style": "brawl", "secondary_style": "dive",
        "mobility_score": 5, "sustain_score": 9, "poke_score": 3,
        "engage_score": 2, "peel_score": 5, "execution_difficulty": 5,
    },
    "Loki": {
        "primary_style": "flex", "secondary_style": "dive",
        "mobility_score": 6, "sustain_score": 5, "poke_score": 3,
        "engage_score": 2, "peel_score": 4, "execution_difficulty": 8,
    },
    "Cloak & Dagger": {
        "primary_style": "dive", "secondary_style": "brawl",
        "mobility_score": 6, "sustain_score": 7, "poke_score": 3,
        "engage_score": 3, "peel_score": 6, "execution_difficulty": 6,
    },
    "White Fox": {
        "primary_style": "poke", "secondary_style": "flex",
        "mobility_score": 8, "sustain_score": 5, "poke_score": 6,
        "engage_score": 2, "peel_score": 7, "execution_difficulty": 6,
        # peel_score=7 because mobility/slows let her kite divers off a carry
    },
    "Gambit": {
        "primary_style": "brawl", "secondary_style": "poke",
        "mobility_score": 4, "sustain_score": 5, "poke_score": 5,
        "engage_score": 3, "peel_score": 5, "execution_difficulty": 6,
    },

    # ── Additional Vanguards ───────────────────────────────────────────────
    "Groot": {
        "primary_style": "brawl", "secondary_style": "poke",
        "mobility_score": 2, "sustain_score": 6, "poke_score": 3,
        "engage_score": 4, "peel_score": 7, "execution_difficulty": 6,
        # peel_score=7 because walls can physically block divers from reaching allies
    },
    "Peni Parker": {
        "primary_style": "brawl", "secondary_style": "poke",
        "mobility_score": 3, "sustain_score": 6, "poke_score": 4,
        "engage_score": 3, "peel_score": 6, "execution_difficulty": 6,
    },
    "Thing": {
        "primary_style": "brawl", "secondary_style": None,
        "mobility_score": 3, "sustain_score": 8, "poke_score": 2,
        "engage_score": 5, "peel_score": 4, "execution_difficulty": 3,
    },
    "Thor": {
        "primary_style": "rush", "secondary_style": "brawl",
        "mobility_score": 7, "sustain_score": 5, "poke_score": 4,
        "engage_score": 9, "peel_score": 4, "execution_difficulty": 5,
    },

    # ── Additional Duelists ────────────────────────────────────────────────
    "Adam Warlock": {
        "primary_style": "brawl", "secondary_style": "flex",
        "mobility_score": 5, "sustain_score": 7, "poke_score": 3,
        "engage_score": 2, "peel_score": 5, "execution_difficulty": 6,
    },
    "Black Panther": {
        "primary_style": "dive", "secondary_style": None,
        "mobility_score": 9, "sustain_score": 3, "poke_score": 1,
        "engage_score": 9, "peel_score": 2, "execution_difficulty": 8,
    },
    "Black Widow": {
        "primary_style": "poke", "secondary_style": None,
        "mobility_score": 5, "sustain_score": 2, "poke_score": 8,
        "engage_score": 2, "peel_score": 2, "execution_difficulty": 7,
    },
    "Blade": {
        "primary_style": "brawl", "secondary_style": "dive",
        "mobility_score": 6, "sustain_score": 7, "poke_score": 1,
        "engage_score": 6, "peel_score": 2, "execution_difficulty": 5,
        # sustain_score=7 from lifesteal mechanic
    },
    "Hawkeye": {
        "primary_style": "poke", "secondary_style": None,
        "mobility_score": 4, "sustain_score": 2, "poke_score": 9,
        "engage_score": 2, "peel_score": 3, "execution_difficulty": 7,
    },
    "Hela": {
        "primary_style": "poke", "secondary_style": "brawl",
        "mobility_score": 5, "sustain_score": 6, "poke_score": 9,
        "engage_score": 3, "peel_score": 2, "execution_difficulty": 6,
        # sustain_score=6 from soul stack second-life mechanic
    },
    "Human Torch": {
        "primary_style": "poke", "secondary_style": "flex",
        "mobility_score": 8, "sustain_score": 3, "poke_score": 7,
        "engage_score": 4, "peel_score": 2, "execution_difficulty": 5,
    },
    "Iron Fist": {
        "primary_style": "dive", "secondary_style": None,
        "mobility_score": 8, "sustain_score": 3, "poke_score": 1,
        "engage_score": 8, "peel_score": 2, "execution_difficulty": 7,
    },
    "Iron Man": {
        "primary_style": "poke", "secondary_style": "flex",
        "mobility_score": 7, "sustain_score": 3, "poke_score": 7,
        "engage_score": 3, "peel_score": 2, "execution_difficulty": 5,
    },
    "Magik": {
        "primary_style": "dive", "secondary_style": None,
        "mobility_score": 9, "sustain_score": 3, "poke_score": 1,
        "engage_score": 9, "peel_score": 2, "execution_difficulty": 7,
    },
    "Moon Knight": {
        "primary_style": "poke", "secondary_style": "flex",
        "mobility_score": 5, "sustain_score": 3, "poke_score": 7,
        "engage_score": 3, "peel_score": 3, "execution_difficulty": 6,
    },
    "Mr. Fantastic": {
        "primary_style": "brawl", "secondary_style": "flex",
        "mobility_score": 5, "sustain_score": 6, "poke_score": 2,
        "engage_score": 5, "peel_score": 4, "execution_difficulty": 6,
        # sustain_score=6 because elastic form absorbs a portion of incoming damage
    },
    "Namor": {
        "primary_style": "poke", "secondary_style": None,
        "mobility_score": 4, "sustain_score": 3, "poke_score": 8,
        "engage_score": 3, "peel_score": 4, "execution_difficulty": 5,
        # peel_score=4 because turrets passively block access to an area
    },
    "Punisher": {
        "primary_style": "brawl", "secondary_style": "poke",
        "mobility_score": 3, "sustain_score": 3, "poke_score": 6,
        "engage_score": 4, "peel_score": 3, "execution_difficulty": 3,
    },
    "Scarlet Witch": {
        "primary_style": "poke", "secondary_style": "flex",
        "mobility_score": 5, "sustain_score": 3, "poke_score": 7,
        "engage_score": 4, "peel_score": 3, "execution_difficulty": 5,
    },
    "Spider-Man": {
        "primary_style": "dive", "secondary_style": None,
        "mobility_score": 10, "sustain_score": 2, "poke_score": 2,
        "engage_score": 9, "peel_score": 2, "execution_difficulty": 9,
    },
    "Squirrel Girl": {
        "primary_style": "poke", "secondary_style": None,
        "mobility_score": 4, "sustain_score": 2, "poke_score": 7,
        "engage_score": 3, "peel_score": 2, "execution_difficulty": 4,
    },
    "Storm": {
        "primary_style": "poke", "secondary_style": "flex",
        "mobility_score": 7, "sustain_score": 2, "poke_score": 7,
        "engage_score": 4, "peel_score": 2, "execution_difficulty": 5,
    },
    "Winter Soldier": {
        "primary_style": "brawl", "secondary_style": "poke",
        "mobility_score": 5, "sustain_score": 3, "poke_score": 6,
        "engage_score": 6, "peel_score": 3, "execution_difficulty": 5,
    },
    "Wolverine": {
        "primary_style": "dive", "secondary_style": "brawl",
        "mobility_score": 7, "sustain_score": 7, "poke_score": 1,
        "engage_score": 7, "peel_score": 3, "execution_difficulty": 5,
        # sustain_score=7 from regen — lets him trade where other melee duelists cannot
    },
}


# ---------------------------------------------------------------------------
# Playstyle comp assignments
# ---------------------------------------------------------------------------
# Describes which comp styles each hero fits into and WHY.
# Hybrid = works in both brawl AND dive (with explanation of why over others).
# triple_tank / triple_support = heroes valid for those specialty comp formats.

PLAYSTYLE_COMPS: dict[str, dict] = {
    "brawl": {
        "description": "Close-range sustained fight. Win by out-sustaining and trading efficiently at short range.",
        "heroes": [
            # Vanguards
            "Dr. Strange", "Hulk", "Magneto", "Captain America", "Emma Frost",
            "Thing", "Thor", "Groot", "Peni Parker", "Tankpool",
            # Duelists
            "Phoenix", "Hela", "Punisher", "Mr. Fantastic", "Winter Soldier", "DpsPool",
            # Strategists
            "Mantis", "Rocket Raccoon", "Luna Snow", "Invisible Woman", "Gambit", "Adam Warlock", "SupportPool",
        ],
    },
    "dive": {
        "description": "High-mobility burst team that bypasses the frontline and kills supports before peel arrives.",
        "heroes": [
            # Vanguards
            "Venom", "Angela",
            # Duelists
            "Psylocke", "Daredevil", "Black Cat", "Black Panther",
            "Spider-Man", "Magik", "Iron Fist",
            # Strategists
            "Luna Snow", "Cloak & Dagger",
        ],
    },
    "poke": {
        "description": "Chip from safe range, exhaust enemy cooldowns and health, then close out with burst.",
        "heroes": [
            # Vanguards
            "Magneto", "Dr. Strange", "Emma Frost",
            # Duelists
            "Star-Lord", "Elsa Bloodstone", "Hela", "Hawkeye", "DpsPool",
            "Black Widow", "Iron Man", "Human Torch", "Storm", "Namor",
            "Moon Knight", "Scarlet Witch", "Squirrel Girl",
            # Strategists
            "White Fox", "Invisible Woman", "Ultron",
        ],
    },
    "hybrid": {
        "description": "Heroes that function at high value in both brawl AND dive — making them harder to ban-out and more versatile in draft.",
        "heroes": {
            "Angela": (
                "Heals through on-kill, which applies equally in dive engages and sustained brawl trades. "
                "Unlike pure dive duelists she doesn't need to reset out — she can stay in the brawl. "
                "Unlike pure brawl duelists she has the mobility to dive with Venom."
            ),
            "Cloak & Dagger": (
                "Dagger mode heals after a dive commits; Cloak mode provides burst damage in brawl exchanges. "
                "Toggle lets her adapt to whatever fight shape emerges. "
                "Unlike single-mode supports, she doesn't need a different support slot for brawl vs. dive."
            ),
            "Phoenix": (
                "Ult chain can trigger off dive picks (chain wipe) OR off a brawl war of attrition. "
                "Her resurrection mechanic makes her self-sufficient in both styles. "
                "Unlike dive-only carries she doesn't need a dive tank — she can brawl on the frontline too."
            ),
            "Invisible Woman": (
                "Bubble peel works in dive (protect the diver on the way in) and in brawl (absorb burst on the carry). "
                "Unlike pure brawl supports she can enable a dive comp by giving the diver survivability post-engage. "
                "The only hero whose peel ceiling is equal in both comp styles."
            ),
            "Luna Snow": (
                "High-throughput healing covers both the sustained attrition of brawl and the burst recovery dive needs post-engage. "
                "Unlike Rocket (brawl-focused) she has the raw HPS to recover a dive in seconds. "
                "Unlike Mantis (brawl-amp focused) she works as the primary healer in fast dive recovery windows."
            ),
            "Blade": (
                "Lifesteal sustains him in extended brawl fights the same way it sustains dive engages — both styles reward staying in melee. "
                "Unlike pure brawl tanks he has enough mobility to dive with Venom. "
                "Unlike pure dive duelists he doesn't need to reset out — lifesteal means he can continue fighting in a sustained brawl."
            ),
            "Wolverine": (
                "Regen mechanic means he can take trades other melee duelists can't absorb, which applies equally in brawl wars and dive commit windows. "
                "Unlike pure dive duelists he can brawl indefinitely thanks to regen. "
                "Unlike pure brawl duelists he has the mobility to open a dive with Venom. Almost identical profile to Angela but Duelist role."
            ),
        },
    },
    "triple_tank": {
        "description": "Three Vanguards with two Strategists. Creates a wall of HP and abilities that forces enemies into disadvantaged angles.",
        "valid_vanguards": ["Dr. Strange", "Magneto", "Hulk", "Venom", "Captain America", "Emma Frost"],
        "example_core": ["Dr. Strange", "Magneto", "Hulk"],
        "example_supports": ["Rocket Raccoon", "Luna Snow"],
        "why_example_works": (
            "Strange provides angle creation + strong peel (portals can pull a diver mid-air). "
            "Magneto provides ranged threat so the comp isn't purely melee-dependent. "
            "Hulk provides raw HP sponge and Bruce mode that creates pick opportunities during the brawl. "
            "Rocket + Luna Snow give sustain throughput high enough to keep three tanks alive without a dedicated duelist bursting."
        ),
        "requires": "Two high-throughput supports. The comp trades damage for durability — your two supports must make up the difference.",
    },
    "triple_support": {
        "description": "Three Strategists with two self-sufficient Duelists. Trades one support slot of healing for utility, peel, or disruption.",
        "valid_supports": ["Invisible Woman", "Mantis", "Luna Snow", "Cloak & Dagger", "Jeff TLS", "Loki"],
        "example_core": ["Invisible Woman", "Mantis", "Luna Snow"],
        "example_duelists": ["Phoenix", "Psylocke"],
        "why_example_works": (
            "Invisible Woman peels so Psylocke and Phoenix never get burst down post-engage. "
            "Mantis sleep dart removes a priority target (usually the enemy healer) to enable Phoenix's ult chain. "
            "Luna Snow provides enough raw healing that the two duelists can take trades other comps couldn't survive. "
            "Phoenix self-sustains through ult chain; Psylocke resets on kill — neither needs a dedicated babysitter."
        ),
        "requires": "Two duelists who generate value independently without needing to stay near supports. Avoid duelists with low self-sufficiency scores.",
    },
}


# ---------------------------------------------------------------------------
# Score and playstyle helper functions
# ---------------------------------------------------------------------------

def get_hero_score(hero_name: str) -> dict | None:
    """Return the score dict for a hero, or None if not found."""
    key = (hero_name or "").strip()
    if key in HERO_SCORES:
        return HERO_SCORES[key]
    lower = key.lower()
    for name, scores in HERO_SCORES.items():
        if name.lower() == lower:
            return scores
    return None


def get_heroes_by_playstyle(style: str) -> list[str] | dict:
    """
    Return heroes valid for a comp style.
    style: 'brawl' | 'dive' | 'poke' | 'hybrid' | 'triple_tank' | 'triple_support'
    Returns list of hero names, or for 'hybrid' returns the dict with reasons.
    """
    comp = PLAYSTYLE_COMPS.get(style.lower().replace(" ", "_"))
    if not comp:
        return []
    if style.lower() == "hybrid":
        return comp["heroes"]
    if style.lower() in ("triple_tank", "triple_support"):
        field = "valid_vanguards" if "vanguard" in style.lower() else "valid_supports"
        return comp.get(field, [])
    return comp.get("heroes", [])


def describe_playstyle_comp(style: str) -> str:
    """Return a prompt-ready description of a comp style including example and why it works."""
    comp = PLAYSTYLE_COMPS.get(style.lower().replace(" ", "_"))
    if not comp:
        arch = COMP_ARCHETYPES.get(style.lower())
        if arch:
            return f"{arch['name']}: {arch['description']} Beats: {', '.join(arch.get('beats', []))}. Loses to: {', '.join(arch.get('loses_to', []))}."
        return f"No data for comp style: {style}."
    lines = [comp["description"]]
    if "example_core" in comp:
        lines.append(f"Example core: {', '.join(comp['example_core'])}.")
    if "why_example_works" in comp:
        lines.append(comp["why_example_works"])
    if "requires" in comp:
        lines.append(f"Requirement: {comp['requires']}")
    return " ".join(lines)
