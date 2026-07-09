import os

def get_config():
    """
    Returns the configuration dictionary for the currently active country.
    Defaults to 'za' (South Africa) if not specified in the environment.
    """
    country_code = os.environ.get("MUCKSCRAPER_COUNTRY", "za").lower()
    return COUNTRY_CONFIGS.get(country_code, COUNTRY_CONFIGS["za"])


def get_country_code():
    return get_config()["country_code"]


def get_timezone():
    return get_config()["timezone"]



COUNTRY_CONFIGS = {
    "za": {
        "country_name": "South Africa",
        "country_code": "za",
        "language": "en",
        "timezone": "Africa/Johannesburg",

        # News API settings
        "newsapi_country": "za",
        "gnews_country": "za",


        # RSS feeds
        "rss_feeds": [
            "http://rss.iol.io/iol/news",
            "https://www.groundup.org.za/rss/",
            "https://mg.co.za/feed/",
            "http://feeds.news24.com/articles/news24/TopStories/rss",
            "https://www.dailymaverick.co.za/dmrss/",
            "https://ewn.co.za/RSS%20Feeds/Latest%20News",
            "https://www.sabcnews.com/sabcnews/feed/",
            "https://www.timeslive.co.za/rss/",
            "http://feeds.news24.com/articles/fin24/topstories/rss",
            "https://www.businesslive.co.za/rss/",
            "https://www.citizen.co.za/feed/",
            "https://feeds.reuters.com/reuters/AFRICATopNews",
            "https://feeds.bbci.co.uk/news/world/africa/rss.xml",
            "https://www.aljazeera.com/xml/rss/all.xml",
            "https://feeds.apnews.com/rss/topnews",
        ],
        "right_enrichment_feeds": [
            "https://www.businesslive.co.za/rss/",
            "https://www.citizen.co.za/feed/",
        ],
        "left_enrichment_feeds": [
            "http://rss.iol.io/iol/news",
            "https://www.groundup.org.za/rss/",
            "https://mg.co.za/feed/",
        ],

        # Outlet bias ratings (1 = Far-Left, 5 = Right)
        "outlet_bias": {
            # 1 = Far-Left / Populist Left
            "IOL": 1,
            
            # 2 = Center-Left / Social Justice
            "GroundUp": 2,
            "Mail & Guardian": 2,
            "amaBhungane": 2,
            
            # 3 = Center / Institutionalist
            "News24": 3,
            "Daily Maverick": 3,
            "SABC News": 3,
            "EWN": 3,
            "TimesLive": 3,
            "Fin24": 3,
            "The Witness": 3,
            
            # 4 = Center-Right / Pro-Business
            "eNCA": 4,
            "Business Day": 4,
            "The Citizen": 4,
            "Rapport": 4,
            "Beeld": 4,
            "Die Burger": 4,
            
            # 5 = Right / Conservative
            "Maroela Media": 5,
            
            # Wire services
            "Reuters": 3,
            "Associated Press": 3,
            "BBC News": 3,
            "Al Jazeera": 3,
        },

        # Outlet name normalisation map
        "outlet_name_map": {
            "news24": "News24",
            "daily maverick": "Daily Maverick",
            "ewn": "EWN",
            "iol": "IOL",
            "timeslive": "TimesLive",
            "times live": "TimesLive",
            "business day": "Business Day",
            "businesslive": "Business Day",
            "mail & guardian": "Mail & Guardian",
            "mg": "Mail & Guardian",
            "the citizen": "The Citizen",
            "sabc news": "SABC News",
            "fin24": "Fin24",
            "groundup": "GroundUp",
            "enca": "eNCA",
            "maroela media": "Maroela Media",
        },

        # Aggregator domains to skip (keep generic US/International ones for now, plus any SA specific if needed)
        "aggregators": ["Yahoo News", "Google News", "MSN", "AOL", "Startups"],

        # Blocked title keywords (country-specific)
        "blocked_title_keywords_extra": ["Lotto results", "lottery", "horoscope"],

        # Political analysis keywords
        "political_keywords": {
            "anc", "da", "eff", "mk", "parliament", "national assembly", "ncop",
            "premier", "municipality", "ramaphosa", "eskom", "state capture",
            "cadre deployment", "npa", "constitutional court", "public protector",
            "government", "election", "policy", "minister"
        },
        "business_keywords": {
            "sarb", "reserve bank", "jse", "rand", "load shedding", "eskom",
            "transnet", "bbbee", "nersa", "economy", "inflation", "revenue",
            "tax", "strike"
        },

        # Bias labels (for display)
        "bias_labels": {
            1: "Far-Left / Populist Left",
            2: "Center-Left / Social Justice",
            3: "Center / Institutionalist",
            4: "Center-Right / Pro-Business",
            5: "Right / Conservative",
        },
        
        # Bias label descriptions (for LLM prompts)
        "bias_descriptions": {
            1: "Focuses on Radical Economic Transformation (RET), state intervention, land expropriation, and anti-monopoly capital narratives",
            2: "Highlights systemic inequalities, the Gini coefficient, labor rights, and the impact of poverty on youth unemployment and healthcare",
            3: "Champions the Constitution, accountability journalism, and anti-corruption. Blends a belief in free markets with support for social safety nets",
            4: "Prioritizes economic growth, free-market solutions, and corporate interests. Highly critical of state inefficiencies, high taxes, and labor union strikes",
            5: "Emphasizes traditionalism, cultural preservation, ethno-nationalism, or strict libertarianism. Often focuses on agricultural security or specific community interests",
        },
        
        # Bias display modes per topic
        "bias_modes": {
            "SA Politics": "political",
            "SA News": "political",
            "International News": "none",
            "Business": "political",
            "Technology": "none",
            "AI": "none",
            "Gaming": "none",
            "Science": "none",
            "Medicine": "none",
            "Sports": "none",
            "Other": "none",
        },
        "default_bias_mode": "none",
    },
}
