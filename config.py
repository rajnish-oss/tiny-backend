import os
from dotenv import load_dotenv

load_dotenv()

# TinyFish API key — the tinyfish SDK reads TINYFISH_API_KEY from env automatically
TINYFISH_API_KEY = os.getenv("TINYFISH_API_KEY", "")

# Cache settings
CACHE_FILE = "jobs_cache.json"
CACHE_TTL_HOURS = 6  # Refresh cache every 6 hours

# Default job portals to scrape
DEFAULT_PORTALS = [
    {"name": "LinkedIn", "domain": "linkedin.com/jobs", "type": "portal"},
    {"name": "Indeed", "domain": "indeed.com", "type": "portal"},
    {"name": "Glassdoor", "domain": "glassdoor.com/Job", "type": "portal"},
    {"name": "Wellfound", "domain": "wellfound.com/jobs", "type": "portal"},
    {"name": "Naukri", "domain": "naukri.com", "type": "portal"},
    {"name": "Internshala", "domain": "internshala.com", "type": "portal"},
]

# Company careers pages (examples)
COMPANY_CAREERS = [
    {"name": "Google", "url": "https://careers.google.com/jobs/results/", "type": "company"},
    {"name": "Microsoft", "url": "https://careers.microsoft.com/us/en/search-results", "type": "company"},
    {"name": "Amazon", "url": "https://www.amazon.jobs/en/search", "type": "company"},
]
