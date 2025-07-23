# Twitter_Thread_Scraper
Twitter Thread Scraper 

Scraper automation written in python playwright to extract a twitter/x thread's tweets, including its stats and media.
Developed on - Python 3.12.8 with Playwright 1.53.0
OS - MacOS/Ubuntu

How to run :
1. Create a virtual environment, Run: python -m venv venv | Run: source venv/bin/activate (for MacOS/Ubuntu)
2. Install requirements, Run: pip install -r requirements.txt
3. Run: playwright install (to install playwright browser binary)
4. Add X_AUTH_TOKEN token in .env file, which can be extracted from twitter/x after login from any browser. 
5. To get token, Go to: Right click on twitter page > Inspect > Storage > Cookies > get value for "auth_token"
6. Finally, run the scraper: python scraper.py -o <OUTPUTFILE_NAME> <TWITTER_THREAD_LINK>
e.g. python scraper.py -o response.json https://x.com/johnrushx/status/1941708690631049517
