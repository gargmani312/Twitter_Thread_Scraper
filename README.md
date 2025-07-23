# Twitter_Thread_Scraper
Twitter Thread Scraper 

Scraper automation written in python playwright to extract a twitter/x thread's tweets, including its stats and media.
Developed on - Python 3.12.8 with Playwright 1.53.0
OS - MacOS/Ubuntu

How to run :
- Create a virtual environment, Run: python -m venv venv | Run: source venv/bin/activate (for MacOS/Ubuntu)
- Install requirements, Run: pip install -r requirements.txt
- Run: playwright install (to install playwright browser binary)
- To get X_AUTH_TOKEN token, Login into twitter, then: Right click on twitter page > Inspect > Storage > Cookies > get value for "auth_token"
- Rename '.env.example' to '.env', then add X_AUTH_TOKEN, TIMEZONE_ID (Python regional timezones i.e. 'Asia/Kolkata') and EXTRACT_MP4_ONLY values without quotes
- EXTRACT_MP4_ONLY: when 'True' will extract MP4 progressive links, set to 'False' will extract raw M3U8 streams
- Finally, run the scraper: python scraper.py -o <OUTPUTFILE_NAME> <TWITTER_THREAD_LINK>
e.g. python scraper.py -o response.json https://x.com/johnrushx/status/1941708690631049517
