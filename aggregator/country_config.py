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


def get_topics():
    return get_config().get("topics", [])


def get_scheduled_fetches():
    return get_config().get("scheduled_fetches", [])


COUNTRY_CONFIGS = {
    "za": {
        "country_name": "South Africa",
        "country_code": "za",
        "language": "en",
        "timezone": "Africa/Johannesburg",

        # News API settings
        "newsapi_country": "za",
        "gnews_country": "za",

        # Topic taxonomy shown in the admin sidebar / classifier fallback
        "topics": [
            {"label": "SA Politics", "icon": "SP"},
            {"label": "SA News", "icon": "SN"},
            {"label": "International News", "icon": "IN"},
            {"label": "Technology", "icon": "TE"},
            {"label": "Science", "icon": "SC"},
            {"label": "Medicine", "icon": "MD"},
            {"label": "AI", "icon": "AI"},
            {"label": "Gaming", "icon": "GA"},
            {"label": "Sports", "icon": "SP"},
            {"label": "Business", "icon": "BF"},
            {"label": "Other", "icon": "OT"},
        ],

        # Scheduled fetch queries (seeded onto matching Topic rows)
        "scheduled_fetches": [
            {
                "label": "SA Politics",
                "mode": "query",
                "country": None,
                "category": None,
                "query": "South Africa politics parliament ANC DA EFF government Ramaphosa",
                "gnews_query": "South Africa politics parliament government",
                "gnews_category": None,
            },
            {
                "label": "SA News",
                "mode": "top",
                "country": "za",
                "category": "general",
                "query": None,
                "gnews_query": None,
                "gnews_category": "nation",
            },
            {
                "label": "Business",
                "mode": "top",
                "country": "za",
                "category": "business",
                "query": None,
                "gnews_query": None,
                "gnews_category": "business",
            },
            {
                "label": "Science",
                "mode": "query",
                "country": None,
                "category": None,
                "query": "science research space",
                "gnews_query": "science",
                "gnews_category": "science",
            },
            {
                "label": "Technology",
                "mode": "query",
                "country": None,
                "category": None,
                "query": "technology gadgets software tech",
                "gnews_query": "technology",
                "gnews_category": "technology",
            },
            {
                "label": "Medicine",
                "mode": "query",
                "country": None,
                "category": None,
                "query": "medicine health medical disease hospital",
                "gnews_query": "health medicine",
                "gnews_category": "health",
            },
            {
                "label": "AI",
                "mode": "query",
                "country": None,
                "category": None,
                "query": "Artificial Intelligence AI machine learning ChatGPT generative",
                "gnews_query": "Artificial Intelligence AI machine learning",
                "gnews_category": "technology",
            },
            {
                "label": "Gaming",
                "mode": "query",
                "country": None,
                "category": None,
                "query": "Video games gaming esports PlayStation Xbox Nintendo PC",
                "gnews_query": "Video games gaming esports",
                "gnews_category": "entertainment",
            },
            {
                "label": "Sports",
                "mode": "top",
                "country": "za",
                "category": "sports",
                "query": None,
                "gnews_query": None,
                "gnews_category": "sports",
            },
            {
                "label": "International News",
                "mode": "query",
                "country": None,
                "category": None,
                "query": "international world global news conflicts diplomacy Africa",
                "gnews_query": "world global news Africa",
                "gnews_category": "world",
            },
        ],

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
    },
}
