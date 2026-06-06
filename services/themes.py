import random
from typing import List, Dict, Tuple

THEME_MAP = {
    "Rock": {
        "emoji": "🎸",
        "genres": ["rock", "alt rock", "alternative rock", "hard rock", "classic rock", "grunge", "punk"],
        "lb_prompt": "tag:rock",
    },
    "Pop": {
        "emoji": "🎤",
        "genres": ["pop", "synthpop", "indie pop", "dance-pop", "j-pop", "k-pop", "chart"],
        "lb_prompt": "tag:pop",
    },
    "Folk": {
        "emoji": "🌿",
        "genres": ["folk", "indie folk", "acoustic", "singer-songwriter", "americana", "traditional"],
        "lb_prompt": "tag:folk",
    },
    "Country": {
        "emoji": "🤠",
        "genres": ["country", "bluegrass", "outlaw country", "classic country"],
        "lb_prompt": "tag:country",
    },
    "Electronic": {
        "emoji": "🎹",
        "genres": ["electronic", "synthwave", "house", "techno", "ambient", "edm", "idm"],
        "lb_prompt": "tag:electronic",
    },
    "Jazz": {
        "emoji": "🎷",
        "genres": ["jazz", "bebop", "cool jazz", "fusion", "smooth jazz", "swing"],
        "lb_prompt": "tag:jazz",
    },
    "R&B": {
        "emoji": "🕺",
        "genres": ["r&b", "soul", "funk", "neo-soul", "motown"],
        "lb_prompt": "tag:r&b",
    },
    "Metal": {
        "emoji": "⚡",
        "genres": ["metal", "heavy metal", "thrash metal", "death metal", "doom metal"],
        "lb_prompt": "tag:metal",
    },
    "Indie": {
        "emoji": "✨",
        "genres": ["indie", "indie rock", "indie pop", "shoegaze", "post-punk"],
        "lb_prompt": "tag:indie",
    },
    "Hip Hop": {
        "emoji": "🎤",
        "genres": ["hip hop", "rap", "trap", "lo-fi hip hop"],
        "lb_prompt": "tag:hip-hop",
    },
    "Classical": {
        "emoji": "🎻",
        "genres": ["classical", "orchestral", "baroque", "romantic", "minimalist"],
        "lb_prompt": "tag:classical",
    },
    "Blues": {
        "emoji": "🎸",
        "genres": ["blues", "delta blues", "chicago blues"],
        "lb_prompt": "tag:blues",
    },
}

# Date-based decade themes — LB Radio supports year range syntax
DECADES = {
    "2020s": {"emoji": "✨", "lb_prompt": "tag:2020s"},
    "2010s": {"emoji": "🕺", "lb_prompt": "tag:2010s"},
    "2000s": {"emoji": "🎧", "lb_prompt": "tag:2000s"},
    "90s":   {"emoji": "📼", "lb_prompt": "tag:90s"},
    "80s":   {"emoji": "🕹️", "lb_prompt": "tag:80s"},
    "70s":   {"emoji": "🪩", "lb_prompt": "tag:70s"},
}

def assign_themes(genre_distribution: Dict[str, int]) -> List[Tuple[str, str]]:
    """
    Given a genre distribution {genre_name: play_count}, pick 5 complementary themes
    weighted toward what they actually listen to.
    Returns list of (ThemeName, Emoji)
    """
    # Sum scores for our defined themes
    theme_scores = {}
    for theme, info in THEME_MAP.items():
        score = 0
        for theme_genre in info["genres"]:
            for user_genre, count in genre_distribution.items():
                if theme_genre in user_genre.lower():
                    score += count
        theme_scores[theme] = score
        
    # Sort themes based on user listening weight
    sorted_themes = sorted(theme_scores.items(), key=lambda x: x[1], reverse=True)
    
    # Select top user themes (e.g. up to 3) and pad with random/decades to make 5 total themes
    selected = []
    
    # 1. Add top genres that user has play history for
    for theme, score in sorted_themes:
        if score > 0 and len(selected) < 3:
            emoji = THEME_MAP[theme]["emoji"]
            selected.append((theme, emoji))
            
    # 2. Add a decade theme randomly
    decade_name, decade_info = random.choice(list(DECADES.items()))
    selected.append((decade_name, decade_info["emoji"]))
    
    # 3. Fill the remaining spots with random themes not already selected
    all_themes = list(THEME_MAP.keys())
    random.shuffle(all_themes)
    for theme in all_themes:
        if len(selected) >= 5:
            break
        if not any(item[0] == theme for item in selected):
            emoji = THEME_MAP[theme]["emoji"]
            selected.append((theme, emoji))
            
    return selected[:5]


def theme_to_genre_filter(theme: str) -> List[str]:
    """Maps theme name to list of potential Jellyfin genre tags."""
    if theme in THEME_MAP:
        return THEME_MAP[theme]["genres"]
    return [theme.lower()]


def theme_to_lb_radio_prompt(theme: str) -> str:
    """Convert a theme name to a valid LB Radio API prompt string."""
    if theme in THEME_MAP:
        return THEME_MAP[theme]["lb_prompt"]
    if theme in DECADES:
        return DECADES[theme]["lb_prompt"]
    # Fallback: use as a raw tag (lowercased, spaces to hyphens)
    return f"tag:{theme.lower().replace(' ', '-')}"
